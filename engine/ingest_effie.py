#!/usr/bin/env python3
"""
ingest_effie.py — Convert the Effie Effectiveness OS dataset into RAG-ready markdown,
following the same pattern as ingest_ipa.py / ingest_cannes.py.

The master is an .xlsx (193 cases, 57 fields, tiers Grand/Gold/Silver/Bronze/Cautionary).
This script is dependency-free and reads CSV or JSON, so export the sheet to CSV first
(File > Save As / Download as CSV) — no pandas/openpyxl needed.

Writes one .md per case to reference/rag/effie/effie_<NNNN>.md with unified frontmatter
(source: effie), so rag/build_rag.sh picks it up and it's queryable via --where source=effie.
"Cautionary" cases (what does NOT work) are tagged category: effie_cautionary for contrast.

Usage:
    python ingest_effie.py [path/to/effie.csv|effie.json]

Default input: reference/_raw/effie.csv

Column matching is tolerant: headers are normalised (lowercased, non-alphanumerics dropped)
and matched against candidate name-sets, so minor header differences still map correctly.
"""

from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_INPUT = HERE / "reference" / "_raw" / "effie.csv"
OUT_DIR = HERE / "reference" / "rag" / "effie"


def _slug(text: str) -> str:
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", str(text).lower())).strip("-")


def _norm(h: str) -> str:
    """Normalise a column header for fuzzy matching: lowercase, alphanumerics only."""
    return re.sub(r"[^a-z0-9]+", "", str(h).lower())


def _clean(v) -> str:
    if v is None:
        return ""
    return re.sub(r"\s+", " ", str(v)).strip()


# field -> ordered candidate header names (normalised at match time)
FIELDS = {
    "title":      ["title", "campaign", "campaignname", "casetitle", "entry", "name"],
    "client":     ["client", "brand", "advertiser", "company"],
    "agency":     ["agency", "entrant", "leadagency"],
    "year":       ["year", "awardyear", "publicationyear"],
    "region":     ["region", "market", "country", "geography"],
    "sector":     ["sector", "industry", "category", "productcategory"],
    "award_tier": ["awardtier", "tier", "award", "metal", "result"],
}

# narrative sections rendered as the case body, in this order
SECTIONS = [
    ("Challenge & Objective", ["challenge", "objective", "objectives", "businesschallenge", "task", "background"]),
    ("Audience",              ["audience", "targetaudience", "target"]),
    ("Insight",               ["insight", "keyinsight", "humaninsight"]),
    ("Strategy",              ["strategy", "strategicapproach", "approach", "ideastrategy"]),
    ("The Idea",              ["idea", "bigidea", "creativeidea", "thebigidea"]),
    ("Execution",             ["execution", "activation", "thework", "implementation", "channels"]),
    ("Results / Effectiveness", ["results", "effectiveness", "outcomes", "impact", "businessresults", "proof"]),
    ("Why It Worked",         ["whyitworked", "whatmadeitwork", "learnings", "keylearnings", "takeaways"]),
]


def _index_row(row: dict) -> dict:
    """Map an arbitrary-header row to normalised-header -> value."""
    return {_norm(k): v for k, v in row.items()}


def _pick(nrow: dict, candidates, default="not recorded") -> str:
    for c in candidates:
        if c in nrow:
            v = _clean(nrow[c])
            if v:
                return v
    return default


def _is_cautionary(tier: str) -> bool:
    return "caution" in tier.lower()


def build_markdown(index: int, nrow: dict) -> str:
    g = lambda f: _pick(nrow, FIELDS[f], default="not recorded")
    title = g("title"); client = g("client"); agency = g("agency")
    year = g("year"); region = g("region"); sector = g("sector"); award_tier = g("award_tier")

    cautionary = _is_cautionary(award_tier)
    category = "effie_cautionary" if cautionary else "effie_case"
    tags = [_slug(award_tier), _slug(sector), _slug(region), "effie_case"]
    if cautionary:
        tags.append("cautionary")
    tags = [t for t in dict.fromkeys(tags) if t]

    body = "\n\n".join(
        f"## {heading}\n{_pick(nrow, keys, default='not recorded')}"
        for heading, keys in SECTIONS
    )
    caution_note = ("\n_NOTE: Cautionary case — included as a contrast example of what did NOT work._\n"
                    if cautionary else "")

    return f"""---
source: effie
framework_id: effie_{index:04d}
framework_name: "{title} ({year})"
category: {category}
award_tier: {award_tier}
year: {year}
client: {client}
agency: {agency}
sector: {sector}
region: {region}
tags: [{", ".join(tags)}]
---

# {title} ({year})
**Award:** {award_tier} · Effie Effectiveness Awards · **Year:** {year}
**Client:** {client} · **Agency:** {agency} · **Region:** {region} · **Sector:** {sector}
{caution_note}
{body}

## Retrieval Queries
- How did {sector} brands prove effectiveness and move the business?
- {award_tier} Effie case study {sector} {region} {year}
- Effectiveness evidence: what drove results for {client}?
- {'What does NOT work in ' + sector + ' effectiveness?' if cautionary else 'Proven effective strategy in ' + sector}
"""


def _load_rows(src: Path) -> list[dict]:
    if src.suffix.lower() == ".json":
        data = json.loads(src.read_text(encoding="utf-8"))
        rows = data if isinstance(data, list) else (data.get("cases") or data.get("rows") or [])
        return [r for r in rows if isinstance(r, dict)]
    # CSV
    with open(src, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def main():
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_INPUT
    if not src.exists():
        sys.exit(
            f"Effie dataset not found at: {src}\n"
            f"Export Effie_Effectiveness_OS.xlsx to CSV (or JSON) and drop it there, then re-run."
        )
    rows = _load_rows(src)
    if not rows:
        sys.exit(f"No rows found in {src}.")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    wrote = skipped = 0
    for i, row in enumerate(rows, start=1):
        nrow = _index_row(row)
        # skip rows with no title and no narrative content (e.g. rubric/dictionary tabs)
        if _pick(nrow, FIELDS["title"], default="") == "" and all(
            _pick(nrow, keys, default="") == "" for _, keys in SECTIONS
        ):
            skipped += 1
            continue
        (OUT_DIR / f"effie_{i:04d}.md").write_text(build_markdown(i, nrow), encoding="utf-8")
        wrote += 1

    print(f"Wrote {wrote} cases to reference/rag/effie/")
    if skipped:
        print(f"Skipped {skipped} empty/non-case rows.")
    print("Now rebuild the index: cd rag && ./build_rag.sh")


if __name__ == "__main__":
    main()
