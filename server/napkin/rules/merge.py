"""The per-market merge and contests (Contract 3 §8).

Identity is entity + key + market (market None = market-independent).

  same identity, same value         one pin; sources unioned (corroboration)
  same entity + key, other market   two facts, two pins — not a conflict
  same identity, different values   a contest, open, nothing picked
  (including a market-independent key two market runs disagree on)
  a value the document already pins differently  -> a contest against the pin

Pure: takes candidates, returns what to pin and what to contest. Writing to
the layers and minting ids is the caller's.
"""

from __future__ import annotations

import json
import re


def identity(c: dict) -> tuple:
    return (c["entity"], c["key"], c.get("market"))


def _vkey(v) -> str:
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return json.dumps(v, sort_keys=True)


def _combined(cs: list[dict]) -> dict:
    """Candidates that agree, as one: the first, with every source, quote, run and source
    record. One that rests on the client's material keeps the whole fact at brand scope."""
    m = dict(cs[0])
    m["sources"] = list(dict.fromkeys(s for c in cs for s in c["sources"]))
    m["quotes"] = _quotes(cs)
    m["runs"] = list(dict.fromkeys(c["run"] for c in cs))
    recs = {r.get("id"): r for c in cs for r in c.get("records") or []}
    if recs:
        m["records"] = list(recs.values())
    if any(c.get("confidential") for c in cs):
        m["layer"], m["confidential"] = "brand", True
    return m


def _quotes(cs: list[dict]) -> dict:
    """Each source's quote across candidates that agree: the first one a source gave."""
    out: dict = {}
    for c in cs:
        for sid, q in (c.get("quotes") or {}).items():
            out.setdefault(sid, q)
    return out


def _slug(v) -> str:
    """A readable key part for one list item, unique by a short hash of the whole value."""
    import hashlib
    base = re.sub(r"[^a-z0-9]+", "_", str(v).lower()).strip("_")[:32].strip("_") or "item"
    return f"{base}_{hashlib.sha256(str(v).encode()).hexdigest()[:6]}"


def split_lists(candidates: list[dict]) -> list[dict]:
    """Different free-text values under one identity are items of a list (four launch dates, three
    claims), not a disagreement: each becomes its own fact, keyed by its value. Numbers and
    yes/no answers that differ still contest (two market sizes cannot both be right). The
    owner, 2026-10-01: four Nintendo dates were shown as "the sources disagree"."""
    groups: dict[tuple, list[dict]] = {}
    for c in candidates:
        groups.setdefault(identity(c), []).append(c)
    out = []
    for cs in groups.values():
        vals = {_vkey(c["value"]) for c in cs}
        # statements only: a date, a year or a code for one thing still has one right answer
        if len(vals) > 1 and all(isinstance(c["value"], str) and c.get("unit") in ("text", None) for c in cs):
            for c in cs:
                out.append({**c, "key": f"{c['key']}.{_slug(c['value'])}"})
        else:
            out += cs
    return out


def merge(candidates: list[dict], pinned: dict[tuple, dict], open_contest_keys: set[str]) -> dict:
    """candidates: [{entity, key, market?, value, unit, sources: [src ids], run: '<lens>/<market>', ...}]
    pinned: identity -> the pin the document holds.
    Returns {"pins": [merged candidate], "contests": [{key, values: [candidate | pin]}],
             "already": [identity]}."""
    groups: dict[tuple, list[dict]] = {}
    for c in split_lists(candidates):
        groups.setdefault(identity(c), []).append(c)
    pins, contests, already = [], [], []
    for ident, cs in groups.items():
        entity, key, market = ident
        ckey = f"{entity}:{key}" + (f"@{market}" if market else "")
        by_value: dict[str, list[dict]] = {}
        for c in cs:
            by_value.setdefault(_vkey(c["value"]), []).append(c)
        prior = pinned.get(ident)
        if len(by_value) == 1:
            merged = _combined(cs)
            if prior is not None:
                if _vkey(prior.get("value")) == _vkey(merged["value"]):
                    already.append(ident)
                    continue
                if ckey in open_contest_keys:
                    continue
                contests.append({"key": ckey, "identity": ident, "pinned": prior, "values": [merged]})
                continue
            pins.append(merged)
        else:
            if ckey in open_contest_keys:
                continue
            vals = []
            for vs in by_value.values():
                vals.append(_combined(vs))
            contests.append({"key": ckey, "identity": ident, "pinned": prior, "values": vals})
    return {"pins": pins, "contests": contests, "already": already}
