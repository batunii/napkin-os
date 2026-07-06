#!/usr/bin/env python3
"""
ingest_cannes.py — Convert a Cannes Lions ("Love The Work" / IAPI) export into
RAG-ready markdown, following the same pattern as ingest_ipa.py.

Writes one .md file per case to reference/rag/cannes/cannes_<NNNN>.md with the
unified frontmatter (source: cannes), so the existing rag/build_rag.sh picks it up
and it becomes queryable via  --where source=cannes.

Usage:
    python ingest_cannes.py [path/to/cannes_export.json]

Default input: reference/_raw/cannes.json  (drop the export there, untracked).

Input shape — tolerant of two layouts (auto-detected per record):
  A) Cannes-native flat:   {title, brand|client, agency, year, award_tier|tier,
                            lions_category|category, market|region,
                            insight, idea, strategy, execution, results, why_it_worked}
  B) IPA-style nested:     {meta:{title,year,client,agency,market,award_tier},
                            cannes_crossref:{sector,...},
                            intelligence:{insight,strategic_approach,the_idea,
                                          execution,results,what_made_it_work}}
Missing fields degrade gracefully to "not recorded".
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_INPUT = HERE / "reference" / "_raw" / "cannes.json"
OUT_DIR = HERE / "reference" / "rag" / "cannes"


def _slug(text: str) -> str:
    """Lowercase, replace non-alphanumeric with hyphens, collapse runs."""
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", str(text).lower())).strip("-")


def _flatten_intel(value) -> str:
    """Convert a content field (str / list / dict) into plain prose."""
    if isinstance(value, str):
        return value.strip() or "not recorded"
    if isinstance(value, list):
        parts = [str(x).strip() for x in value if str(x).strip()]
        return " ".join(parts) if parts else "not recorded"
    if isinstance(value, dict):
        parts = []
        for v in value.values():
            if isinstance(v, str) and v.strip():
                parts.append(v.strip())
            elif isinstance(v, list):
                parts.extend(str(x).strip() for x in v if str(x).strip())
        return " ".join(parts) if parts else "not recorded"
    return "not recorded"


def _pick(case: dict, *keys, default="not recorded"):
    """Return the first present, non-empty value across the case root, its `meta`,
    its `intelligence`, and its `cannes_crossref` blocks, for any of `keys`."""
    scopes = [case]
    for block in ("meta", "intelligence", "cannes_crossref"):
        sub = case.get(block)
        if isinstance(sub, dict):
            scopes.append(sub)
    for key in keys:
        for scope in scopes:
            if key in scope:
                v = scope[key]
                if v is None or (isinstance(v, str) and not v.strip()):
                    continue
                return v
    return default


# the six narrative sections, each mapped to its candidate source keys
SECTIONS = [
    ("Insight",            ("insight", "human_insight", "key_insight")),
    ("Strategy",           ("strategy", "strategic_approach", "approach")),
    ("The Idea",           ("idea", "the_idea", "big_idea", "creative_idea")),
    ("Execution",          ("execution", "the_work", "activation")),
    ("Results",            ("results", "outcomes", "effectiveness", "impact")),
    ("Why It Worked",      ("why_it_worked", "what_made_it_work", "learnings")),
]


def _sections_of(case: dict) -> dict:
    """A scraped record carries a free-form `sections` dict (question heading -> answer),
    since Cannes case questions vary per category. Render those verbatim when present."""
    s = case.get("sections")
    if isinstance(s, dict):
        return {k: _flatten_intel(v) for k, v in s.items() if _flatten_intel(v) not in ("", "not recorded")}
    return {}


def _is_empty(case: dict) -> bool:
    """True when neither the free-form sections nor the fixed narrative fields carry content."""
    if _sections_of(case):
        return False
    return all(
        _flatten_intel(_pick(case, *keys, default="")) in ("", "not recorded")
        for _, keys in SECTIONS
    )


def build_markdown(index: int, case: dict) -> str:
    framework_id = f"cannes_{index:04d}"
    title = _pick(case, "title", "campaign", "name")
    year = _pick(case, "year", "released_year")
    client = _pick(case, "client", "brand", "advertiser")
    agency = _pick(case, "agency", "entrant", "company")
    market = _pick(case, "market", "region", "country")
    award_tier = _pick(case, "award_tier", "tier", "award", "metal")
    # Cannes has BOTH a business sector and a Lions category (Film, Outdoor, …)
    sector = _pick(case, "sector", "industry", default="general")
    lions_category = _pick(case, "lions_category", "category", "medium", default="general")
    subcategory = _pick(case, "subcategory", default="")

    tags = [_slug(award_tier), _slug(sector), _slug(lions_category), "cannes_case"]
    tags = [t for t in tags if t]
    tags_yaml = "[" + ", ".join(dict.fromkeys(tags)) + "]"

    # Prefer the scraped free-form sections; fall back to the fixed narrative mapping.
    scraped = _sections_of(case)
    if scraped:
        body_sections = "\n\n".join(f"## {h}\n{txt}" for h, txt in scraped.items())
    else:
        body_sections = "\n\n".join(
            f"## {heading}\n{_flatten_intel(_pick(case, *keys, default='not recorded'))}"
            for heading, keys in SECTIONS
        )

    md = f"""---
source: cannes
framework_id: {framework_id}
framework_name: "{title} ({year})"
category: cannes_case
award_tier: {award_tier}
year: {year}
client: {client}
agency: {agency}
sector: {sector}
lions_category: {lions_category}
subcategory: {subcategory}
market: {market}
tags: {tags_yaml}
---

# {title} ({year})
**Award:** {award_tier} · Cannes Lions · **Category:** {lions_category} · **Year:** {year}
**Client:** {client} · **Agency:** {agency} · **Market:** {market}

{body_sections}

## Retrieval Queries
- What creative idea won {award_tier} for {sector} brands at Cannes?
- Examples of award-winning {lions_category} work in {sector}
- How did {client} win at Cannes Lions {year}?
- Cannes {award_tier} case study {sector} {lions_category} {year}
"""
    return md


def main():
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_INPUT
    if not src.exists():
        sys.exit(
            f"Cannes export not found at: {src}\n"
            f"Drop the lovethework/IAPI export there (or pass a path), then re-run."
        )

    with open(src, encoding="utf-8") as f:
        data = json.load(f)
    # accept either a top-level list or {"cases": [...]} / {"entries": [...]}
    cases = data if isinstance(data, list) else (
        data.get("cases") or data.get("entries") or data.get("results") or []
    )
    if not cases:
        sys.exit(f"No cases found in {src} (expected a JSON list or a cases/entries key).")

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    wrote = skipped = 0
    for i, case in enumerate(cases, start=1):
        if not isinstance(case, dict) or _is_empty(case):
            skipped += 1
            continue
        (OUT_DIR / f"cannes_{i:04d}.md").write_text(build_markdown(i, case), encoding="utf-8")
        wrote += 1

    print(f"Wrote {wrote} cases to reference/rag/cannes/")
    if skipped:
        print(f"Skipped {skipped} cases with empty/invalid content.")
    print("Now rebuild the index: cd rag && ./build_rag.sh")


if __name__ == "__main__":
    main()
