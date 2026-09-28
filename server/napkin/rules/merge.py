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


def identity(c: dict) -> tuple:
    return (c["entity"], c["key"], c.get("market"))


def _vkey(v) -> str:
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return json.dumps(v, sort_keys=True)


def _quotes(cs: list[dict]) -> dict:
    """Each source's quote across candidates that agree: the first one a source gave."""
    out: dict = {}
    for c in cs:
        for sid, q in (c.get("quotes") or {}).items():
            out.setdefault(sid, q)
    return out


def merge(candidates: list[dict], pinned: dict[tuple, dict], open_contest_keys: set[str]) -> dict:
    """candidates: [{entity, key, market?, value, unit, sources: [src ids], run: '<lens>/<market>', ...}]
    pinned: identity -> the pin the document holds.
    Returns {"pins": [merged candidate], "contests": [{key, values: [candidate | pin]}],
             "already": [identity]}."""
    groups: dict[tuple, list[dict]] = {}
    for c in candidates:
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
            merged = dict(cs[0])
            merged["sources"] = list(dict.fromkeys(s for c in cs for s in c["sources"]))
            merged["quotes"] = _quotes(cs)
            merged["runs"] = list(dict.fromkeys(c["run"] for c in cs))
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
                m = dict(vs[0])
                m["sources"] = list(dict.fromkeys(s for c in vs for s in c["sources"]))
                m["quotes"] = _quotes(vs)
                m["runs"] = list(dict.fromkeys(c["run"] for c in vs))
                vals.append(m)
            contests.append({"key": ckey, "identity": ident, "pinned": prior, "values": vals})
    return {"pins": pins, "contests": contests, "already": already}
