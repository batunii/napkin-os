#!/usr/bin/env python3
"""
build_test_corpus.py — assemble a small, unified TEST corpus that samples every RAG
source, so the pipeline can be exercised end-to-end without the full downloads.

Output: reference/rag_test/{ipa,cannes,dandad,effie,playbooks}/  (a distinct corpus,
kept separate from the production reference/rag/ — build it into rag/_index_test).

- ipa / cannes / dandad / playbooks: a deterministic stratified sample of the REAL
  files already in reference/rag/ (every Nth, so tiers/years spread out).
- effie: SYNTHETIC, clearly-labeled test fixtures (real Effie .xlsx is not reachable).
  These carry `synthetic: true` + `category: effie_synthetic` + a body banner, and
  live ONLY in the test corpus so fabricated cases can never reach a real brief run.
"""
from __future__ import annotations
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SRC = ROOT / "reference" / "rag"
DST = ROOT / "reference" / "rag_test"

# how many to sample from each real source (stratified by even stride)
SAMPLE = {"ipa/cases": 15, "cannes": 15, "dandad": 15, "playbooks": 10, "briefing-template": 3}


def sample_dir(rel: str, n: int) -> int:
    src = SRC / rel
    files = sorted(src.glob("*.md"))
    if not files:
        return 0
    stride = max(1, len(files) // n)
    picks = files[::stride][:n]
    out = DST / rel.split("/")[0]          # flatten ipa/cases -> ipa
    out.mkdir(parents=True, exist_ok=True)
    for f in picks:
        shutil.copy2(f, out / f.name)
    return len(picks)


# --- synthetic Effie test fixtures (clearly fictional) -----------------------
EFFIE = [
    ("Grand", "UK", "FMCG", "The Quiet Comeback",
     "A heritage biscuit brand in terminal decline.", "Nostalgia is a shortcut to trust when a category feels commoditised.",
     "Reframe the brand as the nation's shared teatime ritual, not a product.",
     "Penetration +6.4pts, value share +3.1pts, £4.10 ROMI over 24 months."),
    ("Gold", "Ireland", "Financial", "Small Print, Big Trust",
     "A challenger bank fighting incumbent inertia.", "People don't switch banks over rates; they switch over feeling respected.",
     "Radical transparency as the whole brand act — no asterisks.", "Account openings +38%, CAC -22%, brand-trust +11pts."),
    ("Gold", "USA", "Retail", "Aisle of Everyone",
     "A grocery chain seen as 'not for me' by younger shoppers.", "Belonging beats price for the first weekly shop.",
     "Localise the shelf to each neighbourhood's real basket.", "Under-35 footfall +19%, basket size +7%, sales +$210m."),
    ("Silver", "Germany", "Auto", "Range Without Anxiety",
     "EV consideration stalled on range fear.", "The barrier is emotional (fear of stranding), not the spec sheet.",
     "Show real ordinary journeys completed, never the number.", "Test drives +27%, consideration +9pts."),
    ("Silver", "India", "Telecom", "The Signal Everywhere",
     "Rural users assumed the network wasn't for them.", "Coverage is believed through proof, not maps.",
     "Prove reach via user-shot films from the remotest places.", "Rural activations +44%, churn -13%."),
    ("Bronze", "Brazil", "Beverage", "Heat of the Street",
     "A soft drink losing summer occasions.", "Refreshment is a street-culture moment, not a fridge moment.",
     "Own the informal street vendor as the hero channel.", "Summer volume +12%, distribution +8%."),
    ("Bronze", "Australia", "Public Sector", "Slow Down, Legend",
     "Young-driver speeding messaging ignored.", "Lecturing backfires; peer pride works.",
     "Let mates, not authorities, deliver the message.", "Self-reported speeding -16%, recall +21%."),
    ("Grand", "France", "Luxury", "The Unhurried House",
     "A maison diluted by fast-fashion collabs.", "Scarcity of time signals luxury more than scarcity of stock.",
     "Sell the wait as the product.", "Full-price sell-through +14%, brand-desire +18pts, €62m incremental."),
    ("Gold", "Japan", "Tech", "Made to Be Repaired",
     "A gadget brand seen as disposable.", "Durability is a trust proof in an anti-waste culture.",
     "Warranty and repair as the campaign, not the footnote.", "Repeat purchase +23%, NPS +15."),
    ("Silver", "Canada", "Health", "Ask Once",
     "Men avoiding a screening.", "Shame, not apathy, is the blocker.",
     "Normalise the ask with blunt, funny permission.", "Bookings +31%, stigma-index -12pts."),
    ("Cautionary", "UK", "Retail", "The Rebrand That Wasn't",
     "A logo refresh mistaken for a strategy.", "Changing the mark without changing the offer moves nothing.",
     "A visual-identity swap with no message shift.", "No sales lift; awareness flat; £3m written off. LESSON: identity is not strategy."),
    ("Cautionary", "USA", "FMCG", "Viral, Then Vapour",
     "A stunt that trended but didn't sell.", "Fame without a brand link is entertainment, not marketing.",
     "A meme-chasing stunt untethered from the product.", "Huge reach, zero share movement. LESSON: link fame to the buy."),
]


def write_effie() -> int:
    out = DST / "effie"
    out.mkdir(parents=True, exist_ok=True)
    for i, (tier, region, sector, title, challenge, insight, strategy, results) in enumerate(EFFIE, 1):
        caution = tier == "Cautionary"
        cat = "effie_cautionary" if caution else "effie_synthetic"
        tags = [tier.lower(), sector.lower().replace(" ", "-"), region.lower(), "effie_case", "synthetic"]
        (out / f"effie_{i:04d}.md").write_text(f"""---
source: effie
framework_id: effie_test_{i:04d}
framework_name: "{title} ({2024})"
category: {cat}
synthetic: true
award_tier: {tier}
year: 2024
sector: {sector}
region: {region}
tags: [{", ".join(tags)}]
---

# {title} (2024)
**Award:** {tier} · Effie (SYNTHETIC TEST CASE) · **Sector:** {sector} · **Region:** {region}
_SYNTHETIC TEST FIXTURE — fictional case for pipeline testing only. Not a real Effie winner._

## Challenge & Objective
{challenge}

## Insight
{insight}

## Strategy
{strategy}

## Results / Effectiveness
{results}

## Retrieval Queries
- How did {sector} brands prove effectiveness in {region}?
- {tier} effectiveness case {sector} — what drove business results?
- {'What does NOT work in ' + sector + ' effectiveness?' if caution else 'Proven effective strategy in ' + sector}
""", encoding="utf-8")
    return len(EFFIE)


def main():
    if DST.exists():
        shutil.rmtree(DST)
    totals = {}
    for rel, n in SAMPLE.items():
        totals[rel] = sample_dir(rel, n)
    totals["effie (synthetic)"] = write_effie()
    print("Test corpus →", DST)
    for k, v in totals.items():
        print(f"  {k:22s} {v}")
    print("Build:  cd rag && RAG_EMBED_MODEL unchanged; "
          "python3 rag.py build --corpus ../reference/rag_test --index ./_index_test")


if __name__ == "__main__":
    main()
