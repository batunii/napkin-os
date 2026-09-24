"""Confidence: derived, never self-reported, never averaged.

Fact confidence (Contract 3 §4, middleware-api §3):
  start at the best source's tier     primary -> medium, secondary/tertiary -> low,
                                      reviewer-verified (a human) -> high
  +1 per independent corroborating source (distinct registrable domain),
     at most +2, capped at high
  a fact resting on tertiary sources only is capped at medium

Finding confidence (Contract 3 §6.1): the lowest cited pin's confidence,
one level lower for a single citation or any stale citation, floor low.
"""

from __future__ import annotations

from ..doc import CONF

TIER_BASE = {"primary": 1, "secondary": 0, "tertiary": 0, "reviewer-verified": 2}


def fact_confidence(sources: list[dict]) -> str:
    """`sources`: [{tier, domain}] — every source the layer row rests on."""
    if not sources:
        return "low"
    base = max(TIER_BASE.get(s.get("tier"), 0) for s in sources)
    independent = len({s.get("domain") or s.get("id") for s in sources})
    level = min(2, base + min(2, independent - 1))
    if all(s.get("tier") == "tertiary" for s in sources):
        level = min(level, 1)
    return CONF[level]


def finding_confidence(cited: list[dict]) -> str:
    lvl = min(CONF.index(f.get("confidence", "low")) for f in cited)
    if len(cited) == 1 or any(f.get("stale") for f in cited):
        lvl -= 1
    return CONF[max(0, lvl)]


def coverage_of(found_source_counts: list[int]) -> str:
    """One lens x market run: filled = every fact corroborated, thin = some
    single-source, empty = nothing found."""
    if not found_source_counts:
        return "empty"
    return "filled" if all(n >= 2 for n in found_source_counts) else "thin"


def merge_coverage(values: list[str]) -> str:
    """Across markets: filled only if filled everywhere, empty only if empty
    everywhere, otherwise thin."""
    if all(v == "filled" for v in values):
        return "filled"
    if all(v == "empty" for v in values):
        return "empty"
    return "thin"
