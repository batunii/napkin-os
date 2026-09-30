#!/usr/bin/env python3
"""
filters.py — the metadata filter language, shared by every store.

Until now a filter was `{"source": "ipa"}` and meant "substring match, ANDed". The brief
needs three things that cannot express:

    status   != superseded          exclude what a human overruled
    stage    != production          D&AD serves the production clan, not the brief
    scope    in (global, category:automotive, brand:bmw)
                                    house lessons must not be buried by a narrow filter
    as_of    >= 2020-01-01          recency, once dossiers carry real review dates

So a filter value may now be either a plain value (equality) or a single-key operator
dict. Both forms are accepted everywhere, and the plain form still means what it always
did, so existing callers keep working.

    {"source": "ipa"}                                  source == ipa
    {"status": {"ne": "superseded"}}                   status != superseded
    {"scope": {"in": ["global", "brand:bmw"]}}         scope is any of these
    {"as_of": {"gte": "2020-01-01"}}                   as_of >= that date

Why the operators live here rather than in each store: the local store matches in
memory and the remote store translates to its own query JSON, but they must agree on
what a filter MEANS. One definition, two renderers. A store that renders only part of
the language is required to say so rather than silently return the wrong rows.

Missing fields. `eq` and `in` on an absent field do not match, which is the usual
trap — a filter narrows, and a chunk that never declared the field is not evidence that
it qualifies. `ne` and `nin` DO match an absent field, deliberately: "not superseded"
should include a chunk written before supersession existed. This asymmetry is the whole
reason the contract stamps defaults at write time, and it is why the corpus stayed
visible when `status` arrived.
"""
from __future__ import annotations

from typing import Any

OPS = ("eq", "ne", "in", "nin", "gt", "gte", "lt", "lte", "exists")


class FilterError(ValueError):
    """A filter that cannot be honoured. Raised rather than silently ignored: a filter
    that quietly does nothing returns confident, wrong results."""


def normalise(where: dict | None) -> dict[str, tuple[str, Any]]:
    """{field: (op, value)}. A plain value becomes ('eq', value)."""
    if not where:
        return {}
    out: dict[str, tuple[str, Any]] = {}
    for field, spec in where.items():
        if isinstance(spec, dict):
            if len(spec) != 1:
                raise FilterError(f"{field}: one operator per field, got {sorted(spec)}")
            op, value = next(iter(spec.items()))
            if op not in OPS:
                raise FilterError(f"{field}: unknown operator {op!r}; expected one of {list(OPS)}")
            if op in ("in", "nin") and not isinstance(value, (list, tuple, set)):
                raise FilterError(f"{field}: {op!r} needs a list, got {type(value).__name__}")
        else:
            op, value = "eq", spec
        out[field] = (op, value)
    return out


def _cmp(a, b) -> int | None:
    """Ordering for gt/gte/lt/lte. Dates are ISO strings, so string order is date order;
    numbers compare as numbers. Mixed or unorderable types return None (no match)."""
    try:
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return (a > b) - (a < b)
        a, b = str(a), str(b)
        return (a > b) - (a < b)
    except Exception:
        return None


def matches(metadata: dict, where: dict | None) -> bool:
    """Does this chunk's metadata satisfy the filter? Used by in-memory stores."""
    md = metadata or {}
    for field, (op, value) in normalise(where).items():
        present = md.get(field) is not None
        got = md.get(field)
        if op == "exists":
            if bool(value) != present:
                return False
            continue
        if op == "eq":
            if not present or str(got) != str(value):
                return False
        elif op == "ne":
            if present and str(got) == str(value):
                return False            # absent field passes: see the module docstring
        elif op == "in":
            if not present or str(got) not in {str(v) for v in value}:
                return False
        elif op == "nin":
            if present and str(got) in {str(v) for v in value}:
                return False
        else:
            if not present:
                return False            # a range over an absent value is not a match
            c = _cmp(got, value)
            if c is None:
                return False
            if op == "gt" and not c > 0: return False
            if op == "gte" and not c >= 0: return False
            if op == "lt" and not c < 0: return False
            if op == "lte" and not c <= 0: return False
    return True


def to_qdrant(where: dict | None, prefix: str = "metadata.") -> dict | None:
    """Translate to Qdrant's filter JSON. `ne`/`nin` become must_not, ranges become
    `range` (or `datetime_range` for ISO dates), which is why the payload index type
    for a date field has to be `datetime` — see store_qdrant._index_fields()."""
    norm = normalise(where)
    if not norm:
        return None
    must: list[dict] = []
    must_not: list[dict] = []
    for field, (op, value) in norm.items():
        key = f"{prefix}{field}"
        if op == "eq":
            must.append({"key": key, "match": {"value": value}})
        elif op == "ne":
            must_not.append({"key": key, "match": {"value": value}})
        elif op == "in":
            must.append({"key": key, "match": {"any": list(value)}})
        elif op == "nin":
            must_not.append({"key": key, "match": {"any": list(value)}})
        elif op == "exists":
            (must if value else must_not).append({"is_empty": {"key": key}} if not value
                                                 else {"key": key, "match": {"except": []}})
        else:
            rng = {op: value}
            is_date = isinstance(value, str) and len(value) >= 10 and value[4] == "-" and value[7] == "-"
            must.append({"key": key, ("datetime_range" if is_date else "range"): rng})
    out: dict = {}
    if must:
        out["must"] = must
    if must_not:
        out["must_not"] = must_not
    return out or None


def describe(where: dict | None) -> str:
    """One-line human form, for run logs and the retrieval trace."""
    parts = []
    for field, (op, value) in normalise(where).items():
        if op == "eq":
            parts.append(f"{field}={value}")
        elif op == "in":
            parts.append(f"{field} in ({', '.join(map(str, value))})")
        elif op == "nin":
            parts.append(f"{field} not in ({', '.join(map(str, value))})")
        else:
            parts.append(f"{field} {op} {value}")
    return " AND ".join(parts) if parts else "(no filter)"
