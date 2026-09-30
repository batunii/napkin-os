"""The retrieval port — `napkin.retrieval/1` (contract 5, §3 and §3.6).

  GET  /v1/packs      what this caller may search (house packs + its agency's)
  POST /v1/retrieve   verbatim passages from the pack files

No embeddings and no vector store: one fresh `claude -p` picks and quotes
sections of the pack files on disk, and every quote is then checked to be a
real contiguous span of its section (after collapsing whitespace) and
replaced by the text exactly as it stands in the file. A quote that is not
in the file is dropped. Nothing generated reaches a response.

Packs: directories under BRIEF_CORPUS when set, else MOCK_PACKS_DIR (default
engine/packs_dist), discovered as engine/packs.py does (dirname = pack id,
`_`-prefixed = off, pack.yaml for tag/kind/k/loops; engine/rag/packs.lock
supplies tag/k/loops for a pack without a pack.yaml, so a digest pack answers
to the tag the real index uses). MOCK_AGENCY_PACKS_DIR/<org slug>/<pack>/
adds packs owned by org/<org slug> (S6).

Cache (<MOCK_DATA>/cache/retrieval/): keyed by sha256 of the normalised
request, the org header, the model, a prompt version and every searched
pack's version. It holds the response only — pack passages — never the
query, which is built from the client's brief.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

from common import (REPO, ClaudeCall, ClaudeFailure, Config, DiskCache, PeripheralError, Request, Response,
                    Slots, canon, log, parse_json_object, refuse_scope_in_body, run_claude, sha256_hex)

API = "napkin.retrieval/1"
BACKEND = "claude-code-packs"
PROMPT_VERSION = "2"
MAX_TEXT = 4000
REQUEST_FIELDS = {"query", "k", "packs", "where", "purpose"}
TAG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
ORG_RE = re.compile(r"^org/([a-z0-9][a-z0-9._-]*)$")
PURPOSE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
KINDS = ("case", "playbook", "template", "digest")
LICENCES = ("open", "licensed-internal", "client-confidential")
DEFAULT_CASE_LOOPS = ("loop4_insight", "loop6_substantiation")   # engine/packs.py
NOT_METADATA = {"retrieval_queries", "RETRIEVAL_QUERIES"}

PICKS_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["picks"],
    "properties": {"picks": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["section", "quote"],
        "properties": {
            "section": {"type": "integer", "description": "the index of the section the quote is from"},
            "quote": {"type": "string", "description": "a contiguous span copied character for character "
                                                       "from that section"}}}}},
}

SYSTEM = """You select passages from a library of reference documents for a strategy planner.

You receive a request and a numbered list of sections. Choose the sections that best answer the request and,
from each, copy the passage that answers it: a contiguous span of the section — a sentence, a bullet, a
paragraph or the whole section — copied CHARACTER FOR CHARACTER, including markdown such as ** and *.
Never paraphrase, summarise, shorten with ellipses, join separate spans or add words: a quote that is not an
exact span of its section is discarded.

Return at most the number of picks asked for, best first. Pick a section at most once. If nothing is
relevant, return an empty list."""


# ------------------------------------------------------------------ files


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """engine/rag/rag.py parse_frontmatter, stdlib path; scalars typed
    (int, float, true/false) so `where` matches as the engine's YAML would."""
    text = text.lstrip("﻿")
    m = re.match(r"^\s*---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.DOTALL)
    if not m:
        return {}, text
    raw, body = m.group(1), m.group(2)
    meta: dict = {}
    for line in raw.splitlines():
        if ":" not in line or line.strip().startswith("#"):
            continue
        k, v = line.split(":", 1)
        k, v = k.strip(), v.strip()
        if v.startswith("[") and v.endswith("]"):
            meta[k] = [x.strip().strip("'\"") for x in v[1:-1].split(",") if x.strip()]
        else:
            meta[k] = v.strip("'\"") if v[:1] in ("'", '"') else _scalar(v)
    return meta, body


def _scalar(v: str):
    if v in ("true", "True"):
        return True
    if v in ("false", "False"):
        return False
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    if re.fullmatch(r"-?\d+\.\d+", v):
        return float(v)
    return v


def split_h2(body: str) -> list[tuple[str, str]]:
    """engine/rag/rag.py split_h2, verbatim: split at H1 or H2, H3+ stay inside."""
    sections, heading, buf = [], "(intro)", []
    for line in body.splitlines():
        if re.match(r"^#{1,2}\s+(?!#)", line):
            if buf:
                sections.append((heading, "\n".join(buf).strip()))
            heading = line.lstrip("# ").strip()
            buf = []
        else:
            buf.append(line)
    if buf:
        sections.append((heading, "\n".join(buf).strip()))
    return [(h, t) for h, t in sections if t]


def _flat_yaml(text: str) -> dict:
    out: dict = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, _, val = line.partition(":")
        val = val.strip().strip("'\"")
        if val.startswith("[") and val.endswith("]"):
            out[key.strip()] = [v.strip().strip("'\"") for v in val[1:-1].split(",") if v.strip()]
        elif val.isdigit():
            out[key.strip()] = int(val)
        else:
            out[key.strip()] = val
    return out


def slug(text: str) -> str:
    """Contract 5 §3.2: lowercase, each run outside [a-z0-9] -> one '-', trimmed."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def passage_identity(pack: str, source: str, section: str, text: str) -> tuple[str, str, str]:
    """(id, uri, text_sha256) exactly as §3.2 defines them."""
    text_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    pid = "psg_" + hashlib.sha256("\n".join([pack, source, section, text]).encode("utf-8")).hexdigest()[:20]
    uri = f"passage://{pack}/{source}#{slug(section)}@{text_sha[:16]}"
    return pid, uri, text_sha


@dataclass
class Section:
    pack: str
    source: str        # path within the pack
    heading: str
    text: str
    metadata: dict


@dataclass
class Pack:
    tag: str
    id: str
    kind: str
    scope: str                    # house | agency
    owner: str | None             # org/<slug> for an agency pack
    licence: str
    k: int
    loops: list
    path: Path
    sections: list = field(default_factory=list)
    filterable: list = field(default_factory=list)
    version: str = ""

    def public(self) -> dict:
        return {"tag": self.tag, "id": self.id, "kind": self.kind, "scope": self.scope, "licence": self.licence,
                "k": self.k, "loops": list(self.loops), "passages": len(self.sections),
                "filterable": list(self.filterable), "version": self.version}


def _lock_entries() -> dict:
    p = REPO / "engine" / "rag" / "packs.lock"
    try:
        return {e["id"]: e for e in json.loads(p.read_text()).get("packs", []) if isinstance(e, dict) and "id" in e}
    except (OSError, json.JSONDecodeError):
        return {}


def load_pack(d: Path, scope: str, owner: str | None, lock: dict) -> Pack | None:
    cfg = _flat_yaml((d / "pack.yaml").read_text()) if (d / "pack.yaml").is_file() else {}
    entry = lock.get(d.name, {}) if scope == "house" else {}
    files = sorted(p for p in d.rglob("*.md") if p.is_file())
    if not files:
        return None
    is_digest = (d / ".digest_state.json").is_file() or [f.name for f in files] == ["digest.md"]
    kind = str(cfg.get("kind") or ("digest" if is_digest else entry.get("kind", "case")))
    if kind not in KINDS:
        log(f"pack {d}: kind {kind!r} is not one of {KINDS}; skipped")
        return None
    tag = str(cfg.get("tag") or entry.get("tag") or d.name)
    if not TAG_RE.match(tag):
        log(f"pack {d}: tag {tag!r} is not a valid tag; skipped")
        return None
    loops = cfg.get("loops", entry.get("loops", list(DEFAULT_CASE_LOOPS) if kind == "case" else []))
    licence = str(cfg.get("licence") or "licensed-internal")
    if licence not in LICENCES:
        licence = "licensed-internal"
    pack = Pack(tag=tag, id=d.name, kind=kind, scope=scope, owner=owner, licence=licence,
                k=int(cfg.get("k", entry.get("k", 2))), loops=list(loops or []), path=d)
    h = hashlib.sha256()
    filterable: set = set()
    for f in files:
        rel = f.relative_to(d).as_posix()
        data = f.read_bytes()
        h.update(rel.encode() + b"\0" + data + b"\0")
        meta, body = parse_frontmatter(data.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n"))
        scalars = {k: v for k, v in meta.items()
                   if k not in NOT_METADATA and isinstance(v, (str, int, float, bool))}
        filterable |= set(scalars)
        for heading, text in split_h2(body):
            if re.search(r"retrieval[_ ]queries", heading, re.I):
                continue
            pack.sections.append(Section(tag, rel, heading, text, {"source": tag, **scalars}))
    pack.filterable = sorted(filterable)
    pack.version = "sha256:" + h.hexdigest()
    return pack


def discover(cfg: Config) -> tuple[Path, list[Pack]]:
    root = Path(cfg.brief_corpus) if cfg.brief_corpus else cfg.packs_dir
    lock = _lock_entries()
    packs: list[Pack] = []
    if root.is_dir():
        for d in sorted(root.iterdir()):
            if d.is_dir() and not d.name.startswith(("_", ".")):
                p = load_pack(d, "house", None, lock)
                if p:
                    packs.append(p)
    else:
        log(f"packs directory {root} does not exist; no house packs")
    house_tags = {p.tag for p in packs}
    if cfg.agency_packs_dir and Path(cfg.agency_packs_dir).is_dir():
        for od in sorted(Path(cfg.agency_packs_dir).iterdir()):
            if not od.is_dir() or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", od.name):
                continue
            for d in sorted(od.iterdir()):
                if d.is_dir() and not d.name.startswith(("_", ".")):
                    p = load_pack(d, "agency", f"org/{od.name}", {})
                    if p is None:
                        continue
                    if p.tag in house_tags:
                        log(f"agency pack {d} reuses the house tag {p.tag!r}; skipped")
                        continue
                    packs.append(p)
    return root, packs


# ------------------------------------------------------------------ quotes


def _norm_map(s: str) -> tuple[str, list[int]]:
    """s with every whitespace run collapsed to one space (trimmed), and for
    each output character its index in s."""
    out, idx, pending = [], [], False
    for i, c in enumerate(s):
        if c.isspace():
            pending = bool(out)
            continue
        if pending:
            out.append(" ")
            idx.append(i)
            pending = False
        out.append(c)
        idx.append(i)
    return "".join(out), idx


def locate(quote: str, section: str) -> str | None:
    """The span of `section` the quote names, exactly as it stands in the
    section, or None when the quote is not a contiguous span (after collapsing
    whitespace on both sides)."""
    q = " ".join(quote.split())
    if not q:
        return None
    norm, idx = _norm_map(section)
    at = norm.find(q)
    if at < 0:
        return None
    return section[idx[at]: idx[at + len(q) - 1] + 1]


def cut(text: str) -> tuple[str, bool]:
    if len(text) <= MAX_TEXT:
        return text, False
    head = text[:MAX_TEXT]
    sp = max(head.rfind(" "), head.rfind("\n"))
    head = head[:sp] if sp > MAX_TEXT // 2 else head
    return head.rstrip(), True


_WORD = re.compile(r"[a-z0-9]+")


def _tokens(s: str) -> set:
    return {w for w in _WORD.findall(s.lower()) if len(w) > 2}


# ------------------------------------------------------------------ the port


def bad(message: str) -> PeripheralError:
    return PeripheralError(400, "invalid_input", message)


class Retrieval:
    def __init__(self, cfg: Config, slots: Slots):
        self.cfg, self.slots = cfg, slots
        self.cache = DiskCache(cfg.cache_root, "retrieval")
        self._lock = threading.Lock()
        self.root, self.packs = discover(cfg)
        log(f"retrieval: {len(self.packs)} packs from {self.root} "
            f"({', '.join(f'{p.tag}:{p.kind}:{len(p.sections)}' for p in self.packs)})")

    def health(self) -> dict:
        try:
            shown = self.root.relative_to(REPO).as_posix()
        except ValueError:
            shown = str(self.root)
        return {"api": API, "backend": BACKEND, "model": self.cfg.retrieval_model, "packs_dir": shown,
                "packs": [p.tag for p in self.packs if p.scope == "house"],
                "kinds": sorted({p.kind for p in self.packs if p.scope == "house"}),
                "agency_packs": sorted({f"{p.owner}:{p.tag}" for p in self.packs if p.scope == "agency"}),
                "embed_model": None}

    # -- scope ---------------------------------------------------------------
    @staticmethod
    def org_of(req: Request) -> str:
        org = req.header("x-napkin-org")
        if not org:
            raise PeripheralError(400, "missing_scope", "X-Napkin-Org is required")
        if not ORG_RE.match(org):
            raise bad("X-Napkin-Org must be org/<slug>")
        return org

    def visible(self, org: str) -> list[Pack]:
        return [p for p in self.packs if p.scope == "house" or p.owner == org]

    # -- /v1/packs -----------------------------------------------------------
    def list_packs(self, org: str) -> dict:
        return {"packs": [p.public() for p in self.visible(org)], "embed_model": None, "backend": BACKEND}

    # -- /v1/retrieve --------------------------------------------------------
    def parse(self, raw: bytes, org: str) -> tuple[dict, list[Pack]]:
        body = parse_json_object(raw)
        refuse_scope_in_body(body)
        unknown = sorted(set(body) - REQUEST_FIELDS)
        if unknown:
            raise bad(f"unknown field(s): {', '.join(unknown)} (the retrieval port takes query, k, packs, "
                      "where and purpose only)")
        query = body.get("query")
        if not isinstance(query, str) or not query.strip():
            raise bad("query must be a non-empty string")
        if len(query) > 2000:
            raise bad("query is longer than 2000 characters")
        k = body.get("k")
        if isinstance(k, bool) or not isinstance(k, int) or not 1 <= k <= 20:
            raise bad("k must be an integer from 1 to 20")
        purpose = body.get("purpose")
        if purpose is not None and (not isinstance(purpose, str) or not PURPOSE_RE.match(purpose)):
            raise bad("purpose must be a slug ^[a-z][a-z0-9_]{0,63}$")
        visible = {p.tag: p for p in self.visible(org)}
        tags = body.get("packs")
        if tags is None:
            chosen = list(visible.values())
        else:
            if not isinstance(tags, list) or not tags or not all(isinstance(t, str) for t in tags):
                raise bad("packs must be a non-empty list of pack tags")
            if len(set(tags)) != len(tags):
                raise bad("packs must not repeat a tag")
            for t in tags:
                if t not in visible:
                    # Never 403: the answer does not say whether the pack exists.
                    raise PeripheralError(404, "unknown_pack", "a named pack is not searchable by this caller")
            chosen = [visible[t] for t in tags]
        where = body.get("where")
        if where is not None:
            if not isinstance(where, dict):
                raise bad("where must be an object of exact-match filters")
            for key, val in where.items():
                if isinstance(val, (dict, list)) or val is None:
                    raise bad("where values must be strings, numbers or booleans")
                not_ok = [p.tag for p in chosen if key not in p.filterable]
                if not_ok:
                    raise bad(f"where key {key!r} is not filterable in pack(s) {', '.join(not_ok)}")
        norm = {"query": " ".join(query.split()), "k": k, "packs": sorted(p.tag for p in chosen),
                "where": where or {}}
        return norm, chosen

    def candidates(self, norm: dict, chosen: list[Pack]) -> list[tuple[Pack, Section]]:
        where = norm["where"]

        def match(s: Section) -> bool:
            for key, val in where.items():
                have = s.metadata.get(key)
                if isinstance(val, bool) or isinstance(have, bool):
                    if have is not val:
                        return False
                elif have != val:
                    return False
            return True

        cands = [(p, s) for p in chosen for s in p.sections if match(s)]
        total = sum(len(s.text) for _, s in cands)
        if total <= self.cfg.retrieval_max_chars:
            return cands
        # Lexical prefilter: token overlap with the query, keep the best that fit.
        qt = _tokens(norm["query"])
        ranked = sorted(cands, key=lambda c: -len(qt & _tokens(c[1].heading + " " + c[1].text)))
        kept, used = [], 0
        for c in ranked:
            if used + len(c[1].text) > self.cfg.retrieval_max_chars:
                continue
            kept.append(c)
            used += len(c[1].text)
        return kept

    def prompt(self, norm: dict, cands: list) -> str:
        lines = [f"Request: {norm['query']}", f"Picks wanted: at most {norm['k']}", "", "Sections:"]
        for i, (p, s) in enumerate(cands):
            lines += [f'<section index="{i}" pack="{p.tag}" file="{s.source}" heading="{s.heading}">', s.text,
                      "</section>", ""]
        return "\n".join(lines)

    def pick(self, norm: dict, cands: list) -> list:
        cfg = self.cfg
        call = ClaudeCall(alias=cfg.retrieval_model, prompt=self.prompt(norm, cands), system=SYSTEM,
                          json_schema=PICKS_SCHEMA, max_turns=cfg.max_turns)
        if not self.slots.acquire(cfg.queue_timeout_for("retrieval")):
            raise PeripheralError(503, "overloaded", f"all {self.slots.n} claude slots busy (MOCK_CONCURRENCY)")
        try:
            env = run_claude(cfg, call, cfg.timeout["retrieval"], cfg.data / "work")
        except ClaudeFailure as f:
            if f.kind == "timeout":
                raise PeripheralError(504, "timeout", f"retrieval did not finish within "
                                                      f"{cfg.timeout['retrieval']:g}s") from None
            raise PeripheralError(502, "upstream_failed", f.message) from None
        finally:
            self.slots.release()
        if env.get("_exit") or env.get("is_error") or str(env.get("subtype", "success")) != "success":
            raise PeripheralError(502, "upstream_failed",
                                  f"the retrieval CLI failed (exit {env.get('_exit')}, {env.get('subtype')})")
        log(f"retrieval: {len(cands)} candidate sections, cost {env.get('total_cost_usd')}")
        out = env.get("structured_output")
        if not isinstance(out, dict) or not isinstance(out.get("picks"), list):
            raise PeripheralError(502, "upstream_failed", "the retrieval CLI returned no structured picks")
        return out["picks"]

    def passages(self, norm: dict, cands: list, picks: list) -> tuple[list, dict]:
        k = norm["k"]
        out, seen = [], set()
        drops = {"bad_index": 0, "not_verbatim": 0, "duplicate": 0, "over_k": 0}
        for pk in picks:
            idx = pk.get("section") if isinstance(pk, dict) else None
            quote = pk.get("quote") if isinstance(pk, dict) else None
            if isinstance(idx, bool) or not isinstance(idx, int) or not 0 <= idx < len(cands) \
                    or not isinstance(quote, str):
                drops["bad_index"] += 1
                continue
            pack, sec = cands[idx]
            span = locate(quote, sec.text)
            if span is None:
                drops["not_verbatim"] += 1
                continue
            text, truncated = cut(span)
            # At most one passage per section, as an index of section chunks returns.
            ident = (pack.tag, sec.source, sec.heading, idx)
            if ident in seen:
                drops["duplicate"] += 1
                continue
            if len(out) >= k:
                drops["over_k"] += 1
                continue
            seen.add(ident)
            pid, uri, text_sha = passage_identity(pack.tag, sec.source, sec.heading, text)
            rank = len(out) + 1
            out.append({"id": pid, "uri": uri, "pack": pack.tag, "scope": pack.scope, "licence": pack.licence,
                        "source": sec.source, "section": sec.heading,
                        "citation": f"{sec.source} › {sec.heading}", "text": text, "text_sha256": text_sha,
                        "truncated": truncated, "rank": rank, "score": round(1 - (rank - 1) / k, 6),
                        "metadata": dict(sec.metadata)})
        return out, drops

    def retrieve(self, raw: bytes, org: str, fresh: bool) -> dict:
        norm, chosen = self.parse(raw, org)
        versions = {p.tag: p.version for p in chosen}
        trace = {"backend": BACKEND, "embed_model": None, "packs": versions}
        key = sha256_hex(canon({"req": {**norm, "query": norm["query"].lower()}, "org": org,
                                "model": self.cfg.retrieval_model, "v": PROMPT_VERSION, "packs": versions}))
        with self.cache.lock(key):
            if not fresh and not self.cfg.no_cache:
                hit = self.cache.get(key)
                if hit and isinstance(hit.get("response"), dict):
                    return hit["response"]
            cands = self.candidates(norm, chosen)
            if not cands:
                return {"passages": [], "trace": trace}    # nothing to search: honest, no model call
            picks = self.pick(norm, cands)
            passages, drops = self.passages(norm, cands, picks)
            if any(drops.values()):
                log(f"retrieval dropped {drops}")
            response = {"passages": passages, "trace": trace}
            self.cache.put(key, {"response": response})     # pack passages only, never the query
            return response

    def handle(self, req: Request) -> Response:
        try:
            path = req.path.rstrip("/")
            if path not in ("/v1/packs", "/v1/retrieve"):
                raise PeripheralError(404, "not_found", f"no route {req.path}")
            want = "GET" if path == "/v1/packs" else "POST"
            if req.method != want:
                raise PeripheralError(405, "method_not_allowed", f"use {want}")
            org = self.org_of(req)
            if path == "/v1/packs":
                return Response(200, self.list_packs(org))
            fresh = (req.q("fresh") or "0") not in ("", "0", "false")
            resp = self.retrieve(req.body, org, fresh)
            return Response(200, resp, note=f"passages={len(resp['passages'])}")
        except PeripheralError as e:
            return e.response()
