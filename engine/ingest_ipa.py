#!/usr/bin/env python3
"""
ingest_ipa.py — Convert IPA intelligence_layer.json into RAG-ready markdown files.

Usage:
    python ingest_ipa.py

Writes one .md file per case to reference/rag/ipa/cases/<framework_id>.md
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
IPA_JSON = HERE / "archive" / "ipa-award-winners-dataset" / "intelligence_layer.json"
OUT_DIR = HERE / "reference" / "rag" / "ipa" / "cases"


def _slug(text: str) -> str:
    """Lowercase, replace non-alphanumeric with hyphens, collapse runs."""
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", str(text).lower())).strip("-")


def _get(d, *keys, default="not recorded"):
    """Safe nested get."""
    v = d
    for k in keys:
        if not isinstance(v, dict):
            return default
        v = v.get(k)
    if v is None or (isinstance(v, str) and not v.strip()):
        return default
    return v


def _flatten_intel(value) -> str:
    """Convert an intelligence sub-field (dict or str) into plain prose."""
    if isinstance(value, str):
        return value.strip() or "not recorded"
    if isinstance(value, dict):
        parts = []
        for v in value.values():
            if isinstance(v, str) and v.strip():
                parts.append(v.strip())
            elif isinstance(v, list):
                parts.extend(str(x).strip() for x in v if str(x).strip())
        return " ".join(parts) if parts else "not recorded"
    return "not recorded"


def _is_empty_intelligence(intel: dict) -> bool:
    if not intel or not isinstance(intel, dict):
        return True
    return all(not _flatten_intel(v) or _flatten_intel(v) == "not recorded"
               for v in intel.values())


def build_markdown(index: int, case: dict) -> str:
    meta = case.get("meta") or {}
    cannes = case.get("cannes_crossref") or {}
    intel = case.get("intelligence") or {}

    framework_id = f"ipa_{index:04d}"
    title = _get(meta, "title")
    year = _get(meta, "year")
    client = _get(meta, "client")
    agency = _get(meta, "agency")
    market = _get(meta, "market")
    award_tier = _get(meta, "award_tier")
    sector = _get(cannes, "sector", default="general")
    effectiveness_type = _get(cannes, "effectiveness_type", default="general")
    strategic_territory = _get(cannes, "strategic_territory", default="general")

    award_tier_lower = _slug(str(award_tier))
    sector_slug = _slug(str(sector))
    eff_slug = _slug(str(effectiveness_type))

    tags = [award_tier_lower, sector_slug, eff_slug, "ipa_case"]
    tags_yaml = "[" + ", ".join(tags) + "]"

    insight_text = _flatten_intel(intel.get("insight"))
    strategic_approach_text = _flatten_intel(intel.get("strategic_approach"))
    the_idea_text = _flatten_intel(intel.get("the_idea"))
    execution_text = _flatten_intel(intel.get("execution"))
    results_text = _flatten_intel(intel.get("results"))
    what_made_it_work_text = _flatten_intel(intel.get("what_made_it_work"))

    md = f"""---
source: ipa
framework_id: {framework_id}
framework_name: "{title} ({year})"
category: ipa_effectiveness_case
award_tier: {award_tier}
year: {year}
client: {client}
agency: {agency}
sector: {sector}
effectiveness_type: {effectiveness_type}
strategic_territory: {strategic_territory}
tags: {tags_yaml}
---

# {title} ({year})
**Award:** {award_tier} · IPA Effectiveness Awards · **Year:** {year}
**Client:** {client} · **Agency:** {agency} · **Market:** {market}

## Insight
{insight_text}

## Strategic Approach
{strategic_approach_text}

## The Idea
{the_idea_text}

## Execution
{execution_text}

## Results
{results_text}

## What Made It Work
{what_made_it_work_text}

## Retrieval Queries
- What insight worked for {sector} brands facing {effectiveness_type} challenges?
- Examples of {strategic_territory} strategy achieving {effectiveness_type}
- How did brands use {strategic_territory} to win in {sector}?
- IPA {award_tier} case study {sector} {year}
"""
    return md


def main():
    if not IPA_JSON.exists():
        sys.exit(f"intelligence_layer.json not found at: {IPA_JSON}")

    with open(IPA_JSON, encoding="utf-8") as f:
        cases = json.load(f)

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    wrote = 0
    skipped = 0
    for i, case in enumerate(cases, start=1):
        intel = case.get("intelligence") or {}
        if _is_empty_intelligence(intel):
            skipped += 1
            continue
        framework_id = f"ipa_{i:04d}"
        md = build_markdown(i, case)
        out_path = OUT_DIR / f"{framework_id}.md"
        out_path.write_text(md, encoding="utf-8")
        wrote += 1

    print(f"Wrote {wrote} cases to reference/rag/ipa/cases/")
    if skipped:
        print(f"Skipped {skipped} cases with empty intelligence.")


if __name__ == "__main__":
    main()
