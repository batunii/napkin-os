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


# Region words a person uses for markets, read as the countries research would run for (the owner,
# 2026-10-01: "a normal chat agent would understand what Global means"). Offered for the person to
# confirm or trim, never researched unasked: research runs once per market.
REGIONS = [
    (r"\b(?:global(?:ly)?|worldwide|world-wide|international(?:ly)?|all markets)\b",
     ["US", "GB", "DE", "FR", "JP", "CN", "IN", "BR"], "global"),
    (r"\bEMEA\b", ["GB", "DE", "FR", "IT", "ES", "NL", "AE", "ZA"], "EMEA"),
    (r"\b(?:Europe(?:an)?|the EU|EU)\b", ["GB", "DE", "FR", "IT", "ES", "NL", "PL", "SE"], "Europe"),
    (r"\b(?:Nordics?|Scandinavia(?:n)?)\b", ["SE", "NO", "DK", "FI"], "the Nordics"),
    (r"\bDACH\b", ["DE", "AT", "CH"], "DACH"),
    (r"\bBenelux\b", ["BE", "NL", "LU"], "Benelux"),
    (r"\b(?:APAC|Asia[- ]Pacific)\b", ["CN", "JP", "IN", "AU", "KR", "SG", "ID"], "Asia-Pacific"),
    (r"\b(?:LATAM|Latin America)\b", ["BR", "MX", "AR", "CO", "CL"], "Latin America"),
    (r"\bNorth America\b", ["US", "CA"], "North America"),
    (r"\b(?:the )?Middle East\b", ["AE", "SA", "QA", "KW"], "the Middle East"),
    (r"\b(?:UK and Ireland|UK & Ireland|UKI|the British Isles)\b", ["GB", "IE"], "the UK and Ireland"),
]


def region_in(text: str) -> tuple[str, list[str]] | None:
    """(the region in words, its countries) for the first region word in `text`, or None."""
    hits = sorted((m.start(), name, codes) for rx, codes, name in REGIONS
                  for m in re.finditer(rx, text or "", re.I))
    return (hits[0][1], list(hits[0][2])) if hits else None


def normalise(code: str) -> str | None:
    c = (code or "").strip().upper()
    if c == "UK":
        c = "GB"
    return c if c in ISO_3166 else None


def from_text(text: str) -> list[str]:
    """The countries `text` names, a region word read as its countries ("Global" -> the eight)."""
    hits = sorted((m.start(), code) for rx, code in COUNTRIES for m in re.finditer(rx, text or ""))
    codes = [c for _, c in hits]
    reg = region_in(text)
    if reg:
        codes += reg[1]
    codes += [c for c in re.findall(r"\b[A-Z]{2}\b", text or "") if c in ISO_3166]
    return list(dict.fromkeys(codes))
