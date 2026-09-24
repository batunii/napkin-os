#!/usr/bin/env python3
"""
Contract suite for the research port — the swap guarantee.

Runs against ANY service speaking the research endpoint (components brief §3):
the Claude Code stand-in in this directory today, a real search/discovery
service later. It names no implementation and asserts only what the contract
says.

    python3 mock-research/contract_test.py --base-url http://127.0.0.1:8792
    python3 mock-research/contract_test.py --base-url URL --no-live      # input checks only
    python3 mock-research/contract_test.py --base-url URL --save out.json

    --request FILE     the live request body (default: BMW EV/hybrid, market_structure, IE)
    --fresh            send the first live request with ?fresh=1 (bypass a cache)
    --no-cache-check   do not require the repeat to be identical and fast
    --no-cap-check     skip the extra live call that checks max_sources

The live part makes real research calls (minutes, and model spend on the
stand-in). Python 3.11+, standard library only.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

LENSES = ["market_structure", "brands_positioning", "consumer_culture", "category_codes",
          "rhythm_moments", "media_spend", "regulation_clearance", "effectiveness_evidence"]
DEFAULT_REQUEST = {
    "query": "BMW electric and hybrid cars market in Ireland",
    "lens": "market_structure",
    "market": "IE",
    "entity": "brand/bmw",
    "category": "automotive.ev_hybrid",
}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
FORBIDDEN = {"facts", "fact", "confidence", "tier", "tiers"}

failures: list[str] = []
passes = 0


def check(cond: bool, what: str) -> bool:
    global passes
    if cond:
        passes += 1
        print(f"  ok    {what}")
    else:
        failures.append(what)
        print(f"  FAIL  {what}")
    return cond


def call(base: str, method: str, path: str, body=None, raw: bytes | None = None,
         timeout: float = 900) -> tuple[int, dict | None, float]:
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(base.rstrip("/") + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, text = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, text = e.code, e.read()
    elapsed = time.monotonic() - t0
    try:
        payload = json.loads(text) if text else None
    except json.JSONDecodeError:
        payload = None
    return status, payload, elapsed


def is_date(s) -> bool:
    if not isinstance(s, str) or not DATE_RE.match(s):
        return False
    try:
        _dt.date.fromisoformat(s)
        return True
    except ValueError:
        return False


def check_response(resp, max_sources: int, label: str) -> None:
    """Everything a 200 must satisfy."""
    check(isinstance(resp, dict), f"{label}: body is an object")
    if not isinstance(resp, dict):
        return
    check("error" not in resp, f"{label}: a 200 carries no error key")
    check(not (FORBIDDEN & set(resp)), f"{label}: no facts/confidence/tier at top level")
    sources = resp.get("sources")
    check(isinstance(sources, list), f"{label}: sources is a list")
    trace = resp.get("trace")
    check(isinstance(trace, dict) and isinstance(trace.get("backend"), str) and trace["backend"],
          f"{label}: trace.backend is a non-empty string")
    check(isinstance(trace, dict) and isinstance(trace.get("queries"), list)
          and all(isinstance(q, str) for q in trace["queries"]),
          f"{label}: trace.queries is a list of strings")
    if not isinstance(sources, list):
        return
    check(len(sources) <= max_sources, f"{label}: {len(sources)} sources <= max_sources {max_sources}")
    tomorrow = (_dt.date.today() + _dt.timedelta(days=1)).isoformat()
    ids, urls, bad = set(), set(), []
    for i, s in enumerate(sources):
        where = f"sources[{i}]"
        if not isinstance(s, dict):
            bad.append(f"{where} not an object")
            continue
        if not (isinstance(s.get("id"), str) and s["id"].startswith("src_")):
            bad.append(f"{where}.id")
        if not (isinstance(s.get("url"), str) and re.match(r"^https?://[^\s/]+", s["url"])):
            bad.append(f"{where}.url {s.get('url')!r}")
        for k in ("publisher", "title"):
            if not (isinstance(s.get(k), str) and s[k].strip()):
                bad.append(f"{where}.{k}")
        if not (is_date(s.get("retrieved_at")) and s["retrieved_at"] <= tomorrow):
            bad.append(f"{where}.retrieved_at {s.get('retrieved_at')!r}")
        if "published_at" in s and not is_date(s["published_at"]):
            bad.append(f"{where}.published_at {s['published_at']!r}")
        ex = s.get("excerpts")
        if not (isinstance(ex, list) and ex and all(
                isinstance(e, dict) and isinstance(e.get("quote"), str) and e["quote"].strip()
                for e in ex)):
            bad.append(f"{where}.excerpts")
        if FORBIDDEN & set(s):
            bad.append(f"{where} carries {sorted(FORBIDDEN & set(s))}")
        ids.add(s.get("id"))
        urls.add(s.get("url"))
    check(not bad, f"{label}: every source well-formed" + (f" ({'; '.join(bad[:5])})" if bad else ""))
    check(len(ids) == len(sources), f"{label}: source ids unique")
    check(len(urls) == len(sources), f"{label}: source urls unique")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--request", type=Path)
    ap.add_argument("--save", type=Path, help="write the live response here")
    ap.add_argument("--fresh", action="store_true")
    ap.add_argument("--no-live", action="store_true")
    ap.add_argument("--no-cache-check", action="store_true")
    ap.add_argument("--no-cap-check", action="store_true")
    ap.add_argument("--timeout", type=float, default=900)
    args = ap.parse_args()
    base = args.base_url

    print("health")
    st, body, _ = call(base, "GET", "/healthz", timeout=10)
    check(st == 200 and isinstance(body, dict), "GET /healthz is 200 JSON")

    print("invalid input is 400")
    good = dict(DEFAULT_REQUEST)
    cases = {
        "not JSON": dict(raw=b"{nope"),
        "JSON array": dict(body=[good]),
        "missing query": dict(body={k: v for k, v in good.items() if k != "query"}),
        "empty query": dict(body={**good, "query": "   "}),
        "query not a string": dict(body={**good, "query": 7}),
        "missing lens": dict(body={k: v for k, v in good.items() if k != "lens"}),
        "unknown lens": dict(body={**good, "lens": "vibes"}),
        "missing market": dict(body={k: v for k, v in good.items() if k != "market"}),
        "market not ISO alpha-2": dict(body={**good, "market": "Ireland"}),
        "entity not a string": dict(body={**good, "entity": ["brand/bmw"]}),
        "category not a string": dict(body={**good, "category": 3}),
        "max_sources zero": dict(body={**good, "max_sources": 0}),
        "max_sources not an int": dict(body={**good, "max_sources": "5"}),
    }
    for name, kw in cases.items():
        st, body, _ = call(base, "POST", "/v1/research", timeout=30, **kw)
        check(st == 400, f"{name} -> {st}")
    st, _, _ = call(base, "POST", "/v1/nope", body=good, timeout=10)
    check(st == 404, f"unknown path -> {st}")

    if args.no_live:
        return report()

    req = json.loads(args.request.read_text()) if args.request else dict(DEFAULT_REQUEST)
    max_sources = req.get("max_sources", 8)
    print(f"live: {req['lens']} / {req['market']} — {req['query']!r}")
    st, first, t1 = call(base, "POST", "/v1/research" + ("?fresh=1" if args.fresh else ""),
                         body=req, timeout=args.timeout)
    print(f"  ..    {st} in {t1:.1f}s")
    if not check(st == 200, f"live request -> {st}" + (f" {first}" if st != 200 else "")):
        return report()
    check_response(first, max_sources, "live")
    if args.save:
        args.save.write_text(json.dumps({"request": req, "latency_s": round(t1, 2),
                                         "response": first}, indent=2, ensure_ascii=False))
        print(f"  ..    saved to {args.save}")
    for s in first.get("sources", []):
        print(f"  src   {s.get('url')}  ({len(s.get('excerpts') or [])} quotes)")

    if not args.no_cache_check:
        print("repeat is a cache hit")
        st, again, t2 = call(base, "POST", "/v1/research", body=req, timeout=args.timeout)
        check(st == 200, f"repeat -> {st}")
        check(isinstance(again, dict) and again.get("sources") == first.get("sources"),
              "repeat returns identical sources")
        check(t2 < 3.0, f"repeat is fast ({t2:.2f}s vs {t1:.1f}s)")

    if not args.no_cap_check:
        print("max_sources caps the list")
        capped = {**req, "max_sources": 2}
        st, small, t3 = call(base, "POST", "/v1/research", body=capped, timeout=args.timeout)
        print(f"  ..    {st} in {t3:.1f}s")
        if check(st == 200, f"max_sources=2 -> {st}"):
            check_response(small, 2, "capped")
    return report()


def report() -> int:
    print(f"\n{passes} passed, {len(failures)} failed")
    for f in failures:
        print(f"  - {f}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
