#!/usr/bin/env python3
"""
ingest_dandad.py — Convert a scraped D&AD Awards archive export into RAG-ready markdown,
following the same pattern as ingest_ipa.py / ingest_cannes.py.

D&AD archive entries (dandad.org, public) carry a case summary + Pencil tier + credits.
Writes one .md per case to reference/rag/dandad/dandad_<NNNN>.md with unified frontmatter
(source: dandad), queryable via --where source=dandad.

Usage:
    python ingest_dandad.py [path/to/dandad.json]
Default input: reference/_raw/dandad.json  (a JSON list of scraped records).

Expected record fields (all best-effort): title, tier, year, client, agency, category,
country, description, url.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_INPUT = HERE / "reference" / "_raw" / "dandad.json"
OUT_DIR = HERE / "reference" / "rag" / "dandad"


def _slug(text: str) -> str:
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", str(text).lower())).strip("-")


def _clean(v) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()


def _g(case: dict, *keys, default="not recorded") -> str:
    for k in keys:
        v = _clean(case.get(k))
        if v:
            return v
    return default


def build_markdown(index: int, case: dict) -> str:
    title = _g(case, "title", "name")
    year = _g(case, "year")
    client = _g(case, "client", "brand")
    agency = _g(case, "agency", "entrant")
    category = _g(case, "category", "sector")          # D&AD discipline, e.g. "Book Design"
    country = _g(case, "country", "market", "region")
    tier = _g(case, "tier", "award_tier", "award")
    desc = _g(case, "description", "summary", "overview")

    tags = [_slug(tier), _slug(category), _slug(country), "dandad_case"]
    tags = [t for t in dict.fromkeys(tags) if t]

    return f"""---
source: dandad
framework_id: dandad_{index:04d}
framework_name: "{title} ({year})"
category: dandad_case
award_tier: {tier}
year: {year}
client: {client}
agency: {agency}
sector: {category}
country: {country}
tags: [{", ".join(tags)}]
---

# {title} ({year})
**Award:** {tier} · D&AD · **Discipline:** {category} · **Year:** {year}
**Client:** {client} · **Agency:** {agency} · **Country:** {country}

## Overview
{desc}

## Retrieval Queries
- Award-winning {category} work and craft for {client}
- D&AD Pencil case study {category} {country} {year}
- Examples of {category} that won a D&AD {tier}
- How did {client} create standout work in {category}?
"""


def main():
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_INPUT
    if not src.exists():
        sys.exit(f"D&AD export not found at: {src}\nDrop the scraped JSON there, then re-run.")
    data = json.loads(src.read_text(encoding="utf-8"))
    cases = data if isinstance(data, list) else (data.get("records") or data.get("cases") or [])
    if not cases:
        sys.exit(f"No records found in {src}.")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    wrote = skipped = 0
    for i, case in enumerate(cases, start=1):
        if not isinstance(case, dict) or _g(case, "description", "summary", default="") == "":
            skipped += 1
            continue
        (OUT_DIR / f"dandad_{i:04d}.md").write_text(build_markdown(i, case), encoding="utf-8")
        wrote += 1

    print(f"Wrote {wrote} cases to reference/rag/dandad/")
    if skipped:
        print(f"Skipped {skipped} records with no case text.")
    print("Now rebuild the index: cd rag && ./build_rag.sh")


if __name__ == "__main__":
    main()
