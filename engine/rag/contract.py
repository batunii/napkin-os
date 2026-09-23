#!/usr/bin/env python3
"""
contract.py — the one place the RAG metadata contract is read from.

The contract itself lives in ../schema/rag_metadata.v1.json so that the Rust side
(CLAN SDK, dossier export) and this Python side read the same bytes. This module
never defines a field name or an enum value of its own: it loads the JSON and
exposes it in shapes the rest of the pipeline wants.

    from contract import SCHEMA, indexed_fields, validate, apply_defaults

    indexed_fields()          -> ["scope", "category", ...]   (what needs a payload index)
    apply_defaults(metadata)  -> metadata with status/verdict/scope/schema_version filled
    validate(metadata)        -> [] if clean, else ["category: 'FMCG' not in enum", ...]

Design notes
  * stdlib only. The engine avoids hard dependencies; the contract is small enough
    that a hand-rolled validator is clearer than pulling in jsonschema/pydantic.
  * validate() RETURNS problems instead of raising. The backfill job (C2) wants to
    tag 50 chunks, count failures and fix the prompt — an exception on the first
    bad chunk would hide the other 49.
  * Enum matching is exact and case-sensitive on purpose. Normalising here would
    quietly accept `FMCG`, and the whole point of a closed list is that it does
    not quietly accept anything.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCHEMA_DIR = HERE.parent / "schema"
SCHEMA_PATH = SCHEMA_DIR / "rag_metadata.v1.json"

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---- model ---------------------------------------------------------------
@dataclass(frozen=True)
class Field:
    """One metadata key as the contract describes it. Frozen: the contract is read-only."""
    name: str
    type: str                       # enum | string | date | scope
    indexed: bool
    required: bool
    origin: str                     # system | review | corpus
    description: str = ""
    values: tuple[str, ...] = ()    # enum only
    pattern: str | None = None      # scope only
    default: str | None = None

    def check(self, value) -> str | None:
        """Return a problem string for `value`, or None when it is acceptable."""
        if value is None:
            return f"{self.name}: required" if self.required else None
        if not isinstance(value, str):
            return f"{self.name}: expected string, got {type(value).__name__}"
        if self.type == "enum" and value not in self.values:
            return f"{self.name}: {value!r} not in enum {list(self.values)}"
        if self.type == "date":
            if not _ISO_DATE.match(value):
                return f"{self.name}: {value!r} is not YYYY-MM-DD"
            try:
                date.fromisoformat(value)
            except ValueError:
                return f"{self.name}: {value!r} is not a real date"
        if self.type == "scope" and self.pattern and not re.match(self.pattern, value):
            return f"{self.name}: {value!r} does not match {self.pattern}"
        return None


@dataclass(frozen=True)
class Schema:
    """The whole contract as loaded from rag_metadata.v1.json: its version, whether it is
    locked, every Field by name, and the keys it excludes outright (re-identification
    risk). Frozen; build one with load(), not by hand."""
    version: str
    locked: bool
    fields: dict[str, Field]
    excluded: tuple[str, ...] = field(default_factory=tuple)

    @property
    def names(self) -> list[str]:
        """Every contract field name, in file order."""
        return list(self.fields)

    def indexed(self) -> list[str]:
        """Names of the fields that need a payload index, i.e. the ones filters may use."""
        return [f.name for f in self.fields.values() if f.indexed]

    def required(self) -> list[str]:
        """Names of the fields a chunk must carry for validate() to pass."""
        return [f.name for f in self.fields.values() if f.required]

    def enum_values(self, name: str) -> tuple[str, ...]:
        """The closed value list for field `name`. Raises KeyError for an unknown field;
        returns () for a field that is not an enum."""
        return self.fields[name].values

    def defaults(self) -> dict[str, str]:
        """Field -> default for every field that declares one, plus `schema_version` set
        to this contract's version, which is how each written chunk records the contract
        it was checked against."""
        d = {f.name: f.default for f in self.fields.values() if f.default is not None}
        d["schema_version"] = self.version
        return d


# ---- loading -------------------------------------------------------------
@lru_cache(maxsize=None)
def load(path: Path | str = SCHEMA_PATH) -> Schema:
    """Parse the contract once per process. Cached: it is read on every chunk build."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    fields = {}
    for name, spec in raw["fields"].items():
        fields[name] = Field(
            name=name,
            type=spec["type"],
            indexed=bool(spec.get("indexed", False)),
            required=bool(spec.get("required", False)),
            origin=spec.get("origin", "corpus"),
            description=spec.get("description", ""),
            values=tuple(spec.get("values", ())),
            pattern=spec.get("pattern"),
            default=spec.get("default"),
        )
    return Schema(
        version=raw["version"],
        locked=bool(raw.get("locked", False)),
        fields=fields,
        excluded=tuple(raw.get("excluded", {}).get("fields", ())),
    )


SCHEMA = load()


# ---- convenience API (what the rest of the pipeline actually calls) --------
def indexed_fields() -> list[str]:
    """Names of the contract fields that need a payload index.
    store_qdrant._index_fields() indexes the same set, and test_contract checks the two
    agree."""
    return SCHEMA.indexed()


def apply_defaults(metadata: dict) -> dict:
    """Return a copy of `metadata` with the contract's defaults filled where a key is
    missing or None. Existing values are never overwritten.

    Why defaults matter: a filter on a missing payload field silently drops the point.
    `status != superseded` would exclude every chunk that has no `status` at all, which
    is the entire existing corpus. Defaulting `status` to `active` at write time is what
    keeps old material visible."""
    out = dict(metadata)
    for k, v in SCHEMA.defaults().items():
        if out.get(k) is None:
            out[k] = v
    return out


def validate(metadata: dict, *, strict_keys: bool = False) -> list[str]:
    """Every way `metadata` disagrees with the contract, as human-readable strings.
    Empty list = valid.

    strict_keys=True also reports keys the contract does not know. Off by default
    because the awards corpus carries frontmatter (client, agency, lions_category...)
    that rides along as payload and is fine to keep — it just cannot be filtered on."""
    problems: list[str] = []
    for f in SCHEMA.fields.values():
        p = f.check(metadata.get(f.name))
        if p:
            problems.append(p)
    for k in SCHEMA.excluded:
        if k in metadata:
            problems.append(f"{k}: excluded from the contract (re-identification risk)")
    if strict_keys:
        for k in metadata:
            if k not in SCHEMA.fields:
                problems.append(f"{k}: not in contract v{SCHEMA.version}")
    # cross-field rule: a reason code only makes sense on a rejection
    if metadata.get("reason_code") and metadata.get("verdict") != "rejected":
        problems.append("reason_code: set but verdict is not 'rejected'")
    return problems


def require_valid(metadata: dict) -> dict:
    """apply_defaults + validate, raising on the first failure. For write paths that
    must refuse bad data (the dossier ingestion gate), not for audits."""
    md = apply_defaults(metadata)
    problems = validate(md)
    if problems:
        raise ValueError("metadata violates contract v%s:\n  %s" % (SCHEMA.version, "\n  ".join(problems)))
    return md


if __name__ == "__main__":       # `python3 contract.py` prints the contract summary
    print(f"rag metadata contract v{SCHEMA.version}  ({'LOCKED' if SCHEMA.locked else 'draft'})  {SCHEMA_PATH}")
    print(f"{'field':<16} {'type':<7} {'idx':<4} {'req':<4} {'origin':<7} values/default")
    for f in SCHEMA.fields.values():
        extra = ", ".join(f.values) if f.values else (f.pattern or "")
        if f.default:
            extra = f"[default={f.default}] {extra}"
        print(f"{f.name:<16} {f.type:<7} {'yes' if f.indexed else '-':<4} {'yes' if f.required else '-':<4} {f.origin:<7} {extra}")
    print(f"\nexcluded: {', '.join(SCHEMA.excluded)}")
