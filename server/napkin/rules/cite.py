"""The cite rule, enforced in code after a model writes (Contract 3 §17).

Every claim — headline, summary line, claim block, finding statement,
audience statement — cites at least one pin or (non-rejected) finding the
document holds, and states no figure absent from what it cites. A claim that
fails is fixed where it can be (unknown cites dropped) and otherwise dropped.
"""

from __future__ import annotations

import re

from .figures import unsourced_figures

# Words that assert a share against half. "most" is left out on purpose: it is mostly a superlative
# ("the most popular"), and a rule that fires on it would drop good claims.
_MAJORITY = re.compile(r"\bmajority\b|\b(?:over|more than|above) half\b|\bmore than 50\b", re.I)
_MINORITY = re.compile(r"\bminority\b|\b(?:under|less than|below|fewer than) half\b", re.I)


def wrong_share_word(text: str, cites: list, pins: dict) -> str | None:
    """Why the claim's share word ("majority", "less than half") is not backed by what it cites, else None.
    Checked against the cited proportions only; a claim that cites a finding is not checked, because a
    finding's statement holds no structured share to compare."""
    want_high, want_low = bool(_MAJORITY.search(text)), bool(_MINORITY.search(text))
    if not (want_high or want_low) or any(c not in pins for c in cites):
        return None
    shares = [pins[c]["value"] for c in cites
              if pins[c].get("unit") == "proportion" and isinstance(pins[c].get("value"), (int, float))
              and not isinstance(pins[c].get("value"), bool)]
    if want_high and not any(v > 0.5 for v in shares):
        return "says majority or over half, but no cited share is above half"
    if want_low and not any(v < 0.5 for v in shares):
        return "says minority or under half, but no cited share is below half"
    return None


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
    why = wrong_share_word(text, real, pins)
    if why:
        return None, why
    return {"text": text.strip(), "cites": real}, None
