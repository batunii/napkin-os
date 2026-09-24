"""The cite rule, enforced in code after a model writes (Contract 3 §17).

Every claim — headline, summary line, claim block, finding statement,
audience statement — cites at least one pin or (non-rejected) finding the
document holds, and states no figure absent from what it cites. A claim that
fails is fixed where it can be (unknown cites dropped) and otherwise dropped.
"""

from __future__ import annotations

from .figures import unsourced_figures


def clean_claim(text: str, cites: list, pins: dict, findings: dict, names=()) -> tuple[dict | None, str | None]:
    """-> ({text, cites} with only real cites, None) or (None, why it was dropped)."""
    if not isinstance(text, str) or not text.strip():
        return None, "empty text"
    real = [c for c in dict.fromkeys(cites or []) if c in pins or
            (c in findings and findings[c].get("status") != "rejected")]
    if not real:
        return None, "cites nothing the document holds"
    bad = unsourced_figures(text, real, pins, findings, names)
    if bad:
        return None, f"states {', '.join(sorted(set(bad)))}, which no cited pin or finding holds"
    return {"text": text.strip(), "cites": real}, None
