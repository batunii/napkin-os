#!/usr/bin/env python3
"""golden_check.py — retrieval regression set for the brain. Run after every rebuild/migrate.

    python3 golden_check.py            # against the store named by RAG_STORE
    RAG_STORE=local python3 golden_check.py

Each case: a query the brief pipeline could plausibly issue, a filter, and a predicate
on the top hits. Fails loudly (exit 1) so it can gate a migrate in CI/scripts."""
from __future__ import annotations

import sys
import retrieve

GOLDEN = [
    # Agency MRI shape: B2B software, sceptical SME buyers -> Xero must be a top-2 parent case
    dict(q="B2B software launch to sceptical small business owners and their accountants",
         where={"source": "ipa"}, level="parent", k=2,
         ok=lambda hits: any("xero" in (h["framework"] or "").lower() for h in hits),
         name="ipa parent · Xero for B2B SME software"),
    # k=2 at parent level must return two DIFFERENT cases
    dict(q="award-winning precedent insight agency operations directors belief versus evidence",
         where={"source": "ipa"}, level="parent", k=2,
         ok=lambda hits: len({h["doc_id"] for h in hits}) == 2,
         name="ipa parent · k=2 gives two distinct cases"),
    # a Results child must be findable by sector/brand thanks to the header
    dict(q="McCain frozen chips price elasticity results sustained success",
         where={"source": "ipa"}, level="child", k=3,
         ok=lambda hits: any("mccain" in str(h["metadata"].get("client", "")).lower() and h["section"] == "Results" for h in hits),
         name="ipa child · header lets a Results chunk be found by brand"),
    dict(q="single-minded proposition: how to focus the message on one idea",
         where={"source": "playbook"}, k=3,
         ok=lambda hits: any("single" in (h["framework"] or h["source"]).lower() for h in hits),
         name="playbook · single-minded proposition"),
    dict(q="film craft for a consumer tech brand christmas campaign",
         where={"source": "cannes"}, level="parent", k=2,
         ok=lambda hits: len(hits) == 2 and all(h["level"] == "parent" for h in hits),
         name="cannes parent · whole cases only"),
    dict(q="anything", where={"source": "dandad"}, k=1,
         ok=lambda hits: hits == [],
         name="dandad · excluded from vectors"),
]


def main() -> int:
    print(f"store: {retrieve.index_label()}  available={retrieve.index_available()}")
    failed = 0
    for g in GOLDEN:
        hits = retrieve.retrieve(g["q"], k=g["k"], where=g.get("where"), level=g.get("level"))
        ok = bool(g["ok"](hits))
        failed += (not ok)
        print(f"  {'PASS' if ok else 'FAIL'}  {g['name']}")
        for h in hits[:3]:
            print(f"        [{h['score']:.3f}] {h['citation'][:70]}  ({h['level']})")
    print("all passed" if not failed else f"{failed} FAILED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
