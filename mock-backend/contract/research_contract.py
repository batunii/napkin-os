#!/usr/bin/env python3
"""Contract suite for the research port — napkin.research/1 (contract 5, §2, §8.2).
Moved from mock-research/contract_test.py and tightened to the contract.

    python3 research_contract.py --base-url http://127.0.0.1:8797
    python3 research_contract.py --base-url URL --no-live           # input checks only; spends nothing
    python3 research_contract.py --base-url URL --save out.json

    --token T          bearer token, when the service is configured with one
    --request FILE     the live request body (default: BMW EV, market_structure, IE, max_sources 6)
    --fresh            send the first live request with ?fresh=1 (a cache bypass the mock honours)
    --cache-check      require a repeat to be identical (optional for a real service)
    --no-cap-check     skip the extra live call that checks max_sources

Names no implementation. The live part makes real research calls (minutes,
and model spend on the mock). Standard library only.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from suite import Client, Result, error_type, is_error_shape  # noqa: E402

DEFAULT_REQUEST = {"query": "BMW electric and hybrid cars market in Ireland", "lens": "market_structure",
                   "market": "IE", "entity": "brand/bmw", "category": "automotive.ev_charging", "max_sources": 6}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
URL_RE = re.compile(r"^https?://[^/#\s]+[^#\s]*$")
FORBIDDEN = {"facts", "fact", "confidence", "tier", "tiers"}


def is_date(s) -> bool:
    if not isinstance(s, str) or not DATE_RE.match(s):
        return False
    try:
        _dt.date.fromisoformat(s)
        return True
    except ValueError:
        return False


def check_response(r: Result, resp, max_sources: int, label: str) -> None:
    r.check(isinstance(resp, dict), f"{label}: body is an object")
    if not isinstance(resp, dict):
        return
    r.check("error" not in resp, f"{label}: a 200 carries no error key")
    r.check(not (FORBIDDEN & set(resp)), f"{label}: no facts/confidence/tier")
    sources = resp.get("sources")
    r.check(isinstance(sources, list), f"{label}: sources is a list")
    trace = resp.get("trace")
    r.check(isinstance(trace, dict) and isinstance(trace.get("backend"), str) and trace["backend"],
            f"{label}: trace.backend is a non-empty string")
    r.check(isinstance(trace, dict) and isinstance(trace.get("queries", []), list)
            and all(isinstance(q, str) for q in trace.get("queries", [])), f"{label}: trace.queries, when present, "
                                                                            f"is a list of strings")
    if not isinstance(sources, list):
        return
    r.check(len(sources) <= max_sources, f"{label}: {len(sources)} sources <= max_sources {max_sources}")
    today = _dt.datetime.now(_dt.timezone.utc).date() + _dt.timedelta(days=1)  # one day of clock skew
    ids, urls, bad = set(), set(), []
    for i, s in enumerate(sources):
        w = f"sources[{i}]"
        if not isinstance(s, dict):
            bad.append(f"{w} not an object")
            continue
        if not (isinstance(s.get("id"), str) and s["id"]):
            bad.append(f"{w}.id")
        if not (isinstance(s.get("url"), str) and URL_RE.match(s["url"])):
            bad.append(f"{w}.url {s.get('url')!r}")
        for k in ("publisher", "title"):
            if not (isinstance(s.get(k), str) and s[k].strip()):
                bad.append(f"{w}.{k}")
        if not (is_date(s.get("retrieved_at")) and _dt.date.fromisoformat(s["retrieved_at"]) <= today):
            bad.append(f"{w}.retrieved_at {s.get('retrieved_at')!r}")
        pub = s.get("published_at")
        if pub is not None and not (is_date(pub) and _dt.date.fromisoformat(pub) <= today):
            bad.append(f"{w}.published_at {pub!r}")
        ex = s.get("excerpts")
        if not (isinstance(ex, list) and 1 <= len(ex) <= 5 and all(
                isinstance(e, dict) and isinstance(e.get("quote"), str) and e["quote"].strip()
                and len(e["quote"]) <= 1500 for e in ex)):
            bad.append(f"{w}.excerpts")
        extra = set(s) - {"id", "url", "publisher", "title", "retrieved_at", "published_at", "excerpts"}
        if extra:
            bad.append(f"{w} carries {sorted(extra)}")
        ids.add(s.get("id"))
        urls.add(s.get("url"))
    r.check(not bad, f"{label}: every source well-formed" + (f" ({'; '.join(bad[:5])})" if bad else ""))
    r.check(len(ids) == len(sources), f"{label}: source ids unique")
    r.check(len(urls) == len(sources), f"{label}: one source per URL")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--token")
    ap.add_argument("--request", type=Path)
    ap.add_argument("--save", type=Path)
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--no-live", action="store_true")
    ap.add_argument("--cache-check", action="store_true")
    ap.add_argument("--no-cap-check", action="store_true")
    ap.add_argument("--timeout", type=float, default=900)
    a = ap.parse_args()
    c = Client(a.base_url, a.token, a.timeout)
    r = Result()

    r.section("health")
    st, body, _, _ = c.call("GET", "/healthz", timeout=10)
    r.check(st == 200 and isinstance(body, dict) and body.get("ok") is True, f"GET /healthz -> {st}")

    r.section("every §2.1 rule violated is 400 invalid_input")
    good = dict(DEFAULT_REQUEST)
    drop = lambda k: {x: v for x, v in good.items() if x != k}  # noqa: E731
    cases = {
        "not JSON": dict(raw=b"{nope"),
        "JSON array": dict(body=[good]),
        "missing query": dict(body=drop("query")),
        "empty query": dict(body={**good, "query": "   "}),
        "query not a string": dict(body={**good, "query": 7}),
        "query over 500 characters": dict(body={**good, "query": "ev " * 200}),
        "missing lens": dict(body=drop("lens")),
        "unknown lens": dict(body={**good, "lens": "vibes"}),
        "missing market": dict(body=drop("market")),
        "market not ISO alpha-2": dict(body={**good, "market": "Ireland"}),
        "market UK (the UK is GB)": dict(body={**good, "market": "UK"}),
        "entity not an entity ref": dict(body={**good, "entity": "BMW"}),
        "entity not a string": dict(body={**good, "entity": ["brand/bmw"]}),
        "category not a leaf code": dict(body={**good, "category": "automotive"}),
        "max_sources zero": dict(body={**good, "max_sources": 0}),
        "max_sources 21": dict(body={**good, "max_sources": 21}),
        "max_sources not an int": dict(body={**good, "max_sources": "5"}),
        "unknown field": dict(body={**good, "brand": "bmw"}),
    }
    for name, kw in cases.items():
        st, body, _, _ = c.call("POST", "/v1/research", timeout=30, **kw)
        r.check(st == 400 and error_type(body) == "invalid_input" and is_error_shape(body),
                f"{name} -> {st} {error_type(body)}")
    st, body, _, _ = c.call("POST", "/v1/nope", body=good, timeout=10)
    r.check(st == 404, f"unknown path -> {st}")

    if a.no_live:
        return r.report(f"research contract against {a.base_url} [no live]")

    req = json.loads(a.request.read_text()) if a.request else dict(DEFAULT_REQUEST)
    max_sources = req.get("max_sources", 8)
    r.section(f"live: {req['lens']} / {req['market']} — {req['query']!r}")
    st, first, _, t1 = c.call("POST", "/v1/research" + ("?fresh=1" if a.fresh else ""), body=req)
    print(f"  ..    {st} in {t1:.1f}s")
    if not r.check(st == 200, f"live request -> {st}" + (f" {first}" if st != 200 else "")):
        return r.report(f"research contract against {a.base_url}")
    check_response(r, first, max_sources, "live")
    if a.save:
        a.save.write_text(json.dumps({"request": req, "latency_s": round(t1, 2), "response": first}, indent=2,
                                     ensure_ascii=False))
    for s in first.get("sources", []):
        print(f"  src   {s.get('url')}  ({len(s.get('excerpts') or [])} quotes)")

    if a.cache_check:
        r.section("a repeat is identical (--cache-check)")
        st, again, _, t2 = c.call("POST", "/v1/research", body=req)
        r.check(st == 200 and isinstance(again, dict) and again.get("sources") == first.get("sources"),
                f"repeat returns identical sources ({t2:.2f}s vs {t1:.1f}s)")

    if not a.no_cap_check:
        r.section("max_sources caps the list")
        capped = {**req, "max_sources": 2}
        st, small, _, t3 = c.call("POST", "/v1/research", body=capped)
        print(f"  ..    {st} in {t3:.1f}s")
        if r.check(st == 200, f"max_sources=2 -> {st}"):
            check_response(r, small, 2, "capped")
    return r.report(f"research contract against {a.base_url}")


if __name__ == "__main__":
    sys.exit(main())
