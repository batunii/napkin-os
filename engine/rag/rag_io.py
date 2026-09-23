#!/usr/bin/env python3
"""
rag_io.py — the RAG module's front door: validate a middleware request, run it, return
a contract-shaped response.

The contract lives in ../schema/rag_io.v1.json (JSON Schema, draft 2020-12) so the
middleware can validate the same bytes in whatever language it is written in. This
module defines no field of its own: it loads that file, checks requests against it, and
maps a valid request onto brief_context.build().

    from rag_io import handle, validate, RequestInvalid

    resp = handle(request)              # dict shaped like $defs/response
    problems = validate(request)        # [] when clean, else ["authority.brand: ...", ...]

Inputs   a request dict ($defs/request). `authority` is the confidentiality boundary and
         is injected by the middleware; nothing else in the request can set scope,
         tenant or brand.
Outputs  a response dict ($defs/response): blocks of citable hits in prompt reading
         order, the rendered prompt text, token count, notes and the full trace.
Failure  an invalid request raises RequestInvalid carrying EVERY problem found, not just
         the first — a caller fixing a payload wants the whole list. Retrieval failures
         propagate from brief_context unchanged; this layer adds no retries.

Design notes
  * stdlib only, like contract.py. The validator implements the subset of JSON Schema
    the contract uses (type, enum, required, properties, additionalProperties, items,
    min/max length and items, pattern, minimum/maximum, local $ref) plus two extension
    keywords: `x-enum-from` resolves a closed enum from rag_metadata.v1.json at check
    time, so the two contracts cannot drift; `x-status` marks a field `live` or
    `planned`.
  * `planned` fields are validated but not acted on. The adapter deliberately ignores
    them rather than half-using them — test_rag_io asserts that every `live` field
    changes what build() receives, so the contract cannot claim more than the code does.
"""
from __future__ import annotations

import json
import re
import sys
from functools import lru_cache
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

IO_SCHEMA_PATH = HERE.parent / "schema" / "rag_io.v1.json"

# Prompt reading order, matching BriefContext.prompt_text(): the standard first, then
# what must not be done, then how to think, then precedent.
BLOCK_ORDER = ("instructions", "rules", "craft", "exemplars")


class RequestInvalid(ValueError):
    """The request does not satisfy the contract. `.problems` lists every violation."""

    def __init__(self, problems: list[str]):
        """Keep every problem; the message joins them for logs."""
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


# ---- contract ------------------------------------------------------------------
@lru_cache(maxsize=1)
def io_schema() -> dict:
    """The parsed contract file, loaded once per process."""
    return json.loads(IO_SCHEMA_PATH.read_text())


def version() -> str:
    """The contract version this code speaks, e.g. '1.0.0'."""
    return io_schema()["version"]


def _enum_from(field_name: str) -> tuple[str, ...]:
    """Closed enum of `field_name` in rag_metadata.v1.json — the target of `x-enum-from`."""
    from contract import SCHEMA
    return tuple(SCHEMA.enum_values(field_name))


# ---- validator -----------------------------------------------------------------
_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def _resolve(node: dict) -> dict:
    """Follow a local `#/$defs/...` $ref to the schema node it names. Other refs raise."""
    ref = node.get("$ref")
    if not ref:
        return node
    if not ref.startswith("#/$defs/"):
        raise ValueError(f"unsupported $ref {ref!r} — only local #/$defs/ refs")
    return _resolve(io_schema()["$defs"][ref[len("#/$defs/"):]])


def _check(value, node: dict, path: str, out: list[str]) -> None:
    """Check `value` against schema `node`, appending problems to `out` under `path`.

    Recurses into arrays and objects. A type mismatch stops checks on that value (the
    remaining keywords would only produce noise); every other problem is collected."""
    node = _resolve(node)
    where = path or "(root)"

    types = node.get("type")
    if types is not None:
        allowed = types if isinstance(types, list) else [types]
        if not any(_TYPES[t](value) for t in allowed):
            out.append(f"{where}: expected {' or '.join(allowed)}, got {type(value).__name__}")
            return
    if value is None:
        return

    if "enum" in node and value not in node["enum"]:
        out.append(f"{where}: {value!r} not in {node['enum']}")
    if "x-enum-from" in node:
        vals = _enum_from(node["x-enum-from"])
        if value not in vals:
            out.append(f"{where}: {value!r} not in rag_metadata {node['x-enum-from']} enum {list(vals)}")

    if isinstance(value, str):
        if "minLength" in node and len(value) < node["minLength"]:
            out.append(f"{where}: shorter than {node['minLength']}")
        if "maxLength" in node and len(value) > node["maxLength"]:
            out.append(f"{where}: longer than {node['maxLength']} chars ({len(value)})")
        if "pattern" in node and not re.search(node["pattern"], value):
            out.append(f"{where}: {value!r} does not match {node['pattern']}")

    if _TYPES["number"](value):
        if "minimum" in node and value < node["minimum"]:
            out.append(f"{where}: {value} below minimum {node['minimum']}")
        if "maximum" in node and value > node["maximum"]:
            out.append(f"{where}: {value} above maximum {node['maximum']}")

    if isinstance(value, list):
        if "minItems" in node and len(value) < node["minItems"]:
            out.append(f"{where}: fewer than {node['minItems']} items")
        if "maxItems" in node and len(value) > node["maxItems"]:
            out.append(f"{where}: more than {node['maxItems']} items ({len(value)})")
        if "items" in node:
            for i, item in enumerate(value):
                _check(item, node["items"], f"{path}[{i}]", out)

    if isinstance(value, dict):
        props = node.get("properties", {})
        for key in node.get("required", []):
            if key not in value:
                out.append(f"{path + '.' if path else ''}{key}: required")
        extra = node.get("additionalProperties", True)
        for key, v in value.items():
            sub = f"{path}.{key}" if path else key
            if key in props:
                _check(v, props[key], sub, out)
            elif extra is False:
                out.append(f"{sub}: not in the contract")
            elif isinstance(extra, dict):
                _check(v, extra, sub, out)


def validate(instance, definition: str = "request") -> list[str]:
    """Problems with `instance` against $defs/<definition>. [] means valid."""
    out: list[str] = []
    _check(instance, {"$ref": f"#/$defs/{definition}"}, "", out)
    if definition == "request" and isinstance(instance, dict):
        cv = instance.get("contract_version")
        if isinstance(cv, str) and cv.split(".")[0] != version().split(".")[0]:
            out.append(f"contract_version: {cv!r} is not compatible with {version()!r} "
                       f"(major versions differ)")
    return out


# ---- request -> build() ----------------------------------------------------------
def _join(*parts) -> str:
    """Comma-join strings and lists of strings, dropping blanks and case-insensitive repeats."""
    seen, out = set(), []
    for p in parts:
        for v in (p if isinstance(p, list) else [p]):
            v = str(v or "").strip()
            if v and v.lower() not in seen:
                seen.add(v.lower()); out.append(v)
    return ", ".join(out)


def to_build_args(request: dict) -> tuple[dict, list[str]]:
    """Map a VALID request onto brief_context.build() keyword arguments.

    Returns (kwargs, notes). Only `live` fields are read. The authorised brand and
    tenant come from `authority` alone; brand.name and the campaign pairs only ever
    become search text, so nothing a document says can reach the scope boundary."""
    notes: list[str] = []
    auth = request.get("authority") or {}
    brand = request.get("brand") or {}
    campaign = dict(request.get("campaign") or {})

    # effectiveness_type is `planned`: build() has no pair key for it yet, and passing
    # it through would quietly turn a filter value into query text.
    campaign.pop("effectiveness_type", None)
    pairs: dict = dict(campaign)

    # `brand` carries the subject name ONLY: scopes_for() reads it as the document's
    # claim about who the client is, and "BMW, BMW i" would read as a different client.
    # Aliases are exact keyword terms too, so they ride on `product` instead.
    subject = brand.get("name") or (auth.get("brand") or "").replace("_", " ")
    if subject:
        pairs["brand"] = subject
    product = _join(campaign.get("product"), brand.get("aliases") or [])
    if product:
        pairs["product"] = product

    cats = brand.get("categories") or []
    if cats:
        pairs["category"] = cats[0]
        if len(cats) > 1:
            notes.append(f"rag_io: secondary category {cats[1]!r} recorded, not yet used "
                         f"to widen (planned)")

    market = _join(campaign.get("market"), brand.get("markets") or [])
    if market:
        pairs["market"] = market

    # Comparators and competitors are exact lexical terms — they help find cases ABOUT
    # them, which are public corpus. A `parent` is not a search term: Dove's brief does
    # not want Unilever's corporate cases.
    others = [r["name"] for r in auth.get("references") or []
              if r.get("role") in ("comparator", "competitor")]
    if others:
        pairs["competitors"] = _join(others)

    kwargs = {"pairs": pairs, "brand": auth.get("brand"), "tenant": auth.get("tenant")}
    # Brief context for the validator: research findings then attachments, each labelled.
    # Attachments are untrusted, which is exactly why they only ever reach this string —
    # a relevance judgement — and never pairs, scope or tenant.
    ctx = [f"[research {f['id']}] {f['text']}" for f in request.get("research") or []]
    ctx += [f"[attachment {a['id']}] {a['text']}" for a in request.get("attachments") or []]
    if ctx:
        kwargs["context"] = "\n".join(ctx)
    admission = {}
    excl = (request.get("memory") or {}).get("exclude_doc_ids")
    if excl:
        admission["exclude_doc_ids"] = list(excl)
    rec = (request.get("limits") or {}).get("recency_years")
    if rec:
        admission["recency_years"] = int(rec)
    if admission:
        kwargs["admission"] = admission
    budget = (request.get("limits") or {}).get("token_budget")
    if budget:
        kwargs["budget"] = dict(budget)
    return kwargs, notes


# ---- BriefContext -> response ------------------------------------------------------
def _weight(hit) -> str:
    """How much authority a hit carries: constraint (a reviewer rejected it), advice
    (a rules-bucket pitfall), or evidence (everything else)."""
    if hit.bucket != "rules":
        return "evidence"
    return "constraint" if hit.metadata.get("verdict") == "rejected" else "advice"


def _hit_out(h) -> dict:
    """One brief_context.Hit as $defs/hit. `relevance` is null until validation exists."""
    md = h.metadata or {}
    return {
        "cite": h.cite, "doc_id": h.doc_id, "source": h.source,
        "title": h.title, "section": h.section, "text": h.text,
        "tokens": h.tokens, "retrieval_score": float(h.score),
        "relevance": h.relevance,
        "scope": str(md.get("scope") or "global"),
        "tenant": str(md.get("tenant") or "house"),
        "category": md.get("category"),
        "year": md.get("year"),
        "weight": _weight(h),
    }


def response_from(ctx, run_id: str, notes: list[str] | None = None) -> dict:
    """Shape a BriefContext as $defs/response."""
    blocks = []
    for name in BLOCK_ORDER:
        b = ctx.blocks.get(name)
        if b is None:
            continue
        blocks.append({"bucket": b.bucket, "hits": [_hit_out(h) for h in b.hits],
                       "tokens": b.tokens, "budget": b.budget, "dropped": b.dropped,
                       "over_target": b.over_target, "truncated": b.truncated})
    return {
        "contract_version": version(),
        "run_id": run_id,
        "blocks": blocks,
        "prompt_text": ctx.prompt_text(),
        "tokens": ctx.tokens,
        "validation": (ctx.validation or {}).get("contract"),
        "notes": list(notes or []) + list(ctx.widened),
        "trace": ctx.trace(),
    }


# ---- entry point -------------------------------------------------------------------
def handle(request: dict, *, index_dir=None, build=None) -> dict:
    """Validate, retrieve, respond. `build` is injectable for tests."""
    problems = validate(request)
    if problems:
        raise RequestInvalid(problems)
    if build is None:
        from brief_context import build
    kwargs, notes = to_build_args(request)
    ctx = build(kwargs.pop("pairs"), index_dir=index_dir, **kwargs)
    return response_from(ctx, request["run_id"], notes)


if __name__ == "__main__":                       # validate a request file: rag_io.py req.json
    if len(sys.argv) != 2:
        print("usage: rag_io.py <request.json>", file=sys.stderr)
        sys.exit(2)
    found = validate(json.loads(Path(sys.argv[1]).read_text()))
    print("\n".join(found) if found else f"valid against rag_io v{version()}")
    sys.exit(1 if found else 0)
