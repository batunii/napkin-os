#!/usr/bin/env python3
"""
simulate.py — show what the brief model would actually receive, before wiring anything in.

Retrieval that scores well on a golden set can still hand a model useless context: the
right document retrieved for the wrong reason, a constraint block full of prose that
says nothing, a precedent that matches on category and on nothing else. The golden set
measures whether we find the intended document. This measures whether what comes back
is worth putting in front of a model.

    python3 simulate.py ./_index_v3            # all briefs, summary + one full prompt
    python3 simulate.py ./_index_v3 --full     # every brief's full prompt text
    python3 simulate.py ./_index_v3 --brief 2  # just one

The briefs below are written to stress different parts of the design, not to flatter it:
a category with plenty of precedent, one with almost none, one with no usable category
at all, and one whose campaign type has no equivalent in the corpus vocabulary.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import brief_context as bc  # noqa: E402

BRIEFS = [
    {
        "_name": "Automotive launch — the happy path",
        "_tests": "a category with real depth (225 automotive chunks) and a campaign type that maps",
        "brand": "BMW", "category": "Automotive", "campaign_type": "launch",
        "product": "new hybrid series",
        "problem": "buyers see hybrids as a compromise rather than an upgrade",
        "audience": "urban professionals 30-45 considering their first electrified car",
        "objective": "shift consideration without discounting",
    },
    {
        "_name": "Charity, no campaign type",
        "_tests": "a well-represented category (189 chunks) with no campaign_type supplied at all",
        "brand": "Shelter", "category": "Charity",
        "problem": "donors assume homelessness is someone else's problem and rough sleeping is all of it",
        "audience": "comfortable homeowners aged 45+ who donate occasionally",
        "objective": "raise regular giving without shock tactics",
    },
    {
        "_name": "B2B technology — a thin category",
        "_tests": "technology has only 51 IPA chunks; does it widen, and is what comes back still useful",
        "brand": "Xero", "category": "Technology", "campaign_type": "launch",
        "product": "payroll automation for small firms",
        "problem": "small business owners trust their accountant more than any software brand",
        "audience": "owner-managers of firms under 20 staff, and their accountants",
        "objective": "make the accountant the route to the owner",
    },
    {
        "_name": "Unknown category, unmapped campaign type",
        "_tests": "graceful degradation: no category filter possible, campaign type has no corpus equivalent",
        "brand": "Peloton", "category": "Connected Fitness", "campaign_type": "always_on",
        "problem": "lapsed members remember the guilt, not the community",
        "audience": "members who have not ridden in 90 days",
        "objective": "restart the habit without nagging",
    },
]


def summarise(ctx: bc.BriefContext) -> None:
    t = ctx.trace()
    print(f"  query      : {ctx.query[:104]}{'…' if len(ctx.query) > 104 else ''}")
    print(f"  keywords   : {ctx.keywords}")
    print(f"  filters    : exemplars = {t['filters']['exemplars']}")
    print(f"  widened    : {ctx.widened or 'none'}")
    print(f"  tokens     : {ctx.tokens} of {sum(bc.DEFAULT_BUDGET.values())}")
    for name in ("exemplars", "craft", "rules", "instructions"):
        b = ctx.blocks[name]
        print(f"    {name:<13} {len(b.hits)} hits, {b.tokens:>4} tok, {b.dropped:>2} dropped")
    print("  precedent  :")
    if not ctx.blocks["exemplars"].hits:
        print("    (none — this is the failure mode to watch for)")
    for h in ctx.blocks["exemplars"].hits:
        md = h.metadata
        print(f"    [{h.cite}] {h.title[:52]:<52} cat={md.get('category')} eff={md.get('effectiveness_type')}")
    print("  constraints:")
    for h in ctx.blocks["rules"].hits:
        first = " ".join(h.text.split())[:88]
        print(f"    [{h.cite}] {first}…")


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    index = args[0] if args else None
    full = "--full" in sys.argv
    only = None
    if "--brief" in sys.argv:
        only = int(sys.argv[sys.argv.index("--brief") + 1])

    for i, brief in enumerate(BRIEFS):
        if only is not None and i != only:
            continue
        pairs = {k: v for k, v in brief.items() if not k.startswith("_")}
        print("=" * 78)
        print(f"BRIEF {i} — {brief['_name']}")
        print(f"  tests: {brief['_tests']}")
        print("=" * 78)
        ctx = bc.build(pairs, index_dir=index)
        summarise(ctx)
        if full or (only is None and i == 0):
            print("\n  ---- what the model would receive ----\n")
            for line in ctx.prompt_text().splitlines():
                print("  " + line)
        print()


if __name__ == "__main__":
    main()
