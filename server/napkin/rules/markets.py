"""Market codes: ISO 3166-1 alpha-2, the UK is GB. Used to resolve a typed
answer ("Ireland and GB") and to normalise what the model reads."""

from __future__ import annotations

import re

from ..doc import ISO_3166

COUNTRIES = [
    (r"\bNorthern Ireland\b", "GB"), (r"\b(?:the\s+)?(?:UK|U\.K\.)\b", "GB"),
    (r"\bUnited Kingdom\b", "GB"), (r"\b(?:Great\s+)?Britain\b", "GB"), (r"\bGB\b", "GB"),
    (r"\bEngland\b", "GB"), (r"\bScotland\b", "GB"), (r"\bWales\b", "GB"),
    (r"(?<!Northern )\bIreland\b", "IE"), (r"\bROI\b", "IE"),
    (r"\bFrance\b", "FR"), (r"\bGermany\b", "DE"), (r"\bSpain\b", "ES"), (r"\bItaly\b", "IT"),
    (r"\b(?:the\s+)?Netherlands\b", "NL"), (r"\bBelgium\b", "BE"), (r"\bPortugal\b", "PT"),
    (r"\bPoland\b", "PL"), (r"\bSweden\b", "SE"), (r"\bDenmark\b", "DK"), (r"\bNorway\b", "NO"),
    (r"\bFinland\b", "FI"), (r"\bAustria\b", "AT"), (r"\bSwitzerland\b", "CH"),
    (r"\b(?:the\s+)?(?:US|USA|U\.S\.|United States)\b", "US"), (r"\bCanada\b", "CA"),
    (r"\bAustralia\b", "AU"), (r"\bNew Zealand\b", "NZ"),
]


def normalise(code: str) -> str | None:
    c = (code or "").strip().upper()
    if c == "UK":
        c = "GB"
    return c if c in ISO_3166 else None


def from_text(text: str) -> list[str]:
    hits = sorted((m.start(), code) for rx, code in COUNTRIES for m in re.finditer(rx, text or ""))
    codes = [c for _, c in hits]
    codes += [c for c in re.findall(r"\b[A-Z]{2}\b", text or "") if c in ISO_3166]
    return list(dict.fromkeys(codes))
