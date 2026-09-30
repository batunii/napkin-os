"""Figures: the cite rule's no-unsourced-figure check, and whether a quote
supports a numeric value.

The report rule (Contract 3 §17): a claim states no figure that is not in a
cited pin (or verbatim in a cited finding). A "figure" is any digit run
`\\d+(?:[.,]\\d+)?`. What a cited pin allows is its value as written
(`repr`, `:g`), and for a proportion its percentage (`v*100:g` and the
rounded percentage). Digits inside entity names ("Lúnasa 0.0", "BMW i4")
are names, not figures.

Numbers are compared as numbers, not as text: "2 August 2026" matches a stored
2026-08-02, "1,500,000" and "€1.5m" match 1500000 (a rounded figure matches when the
stored number rounds to what is shown), and a year matches when a cited fact holds it.
"""

from __future__ import annotations

import re

NUM = re.compile(r"\d+(?:[.,]\d+)?")

# A figure in prose: a number (thousands commas allowed) and an optional scale or percent sign right after it.
_FIG = re.compile(r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:[.,]\d+)?)"
                  r"(?:(%)|\s?(per ?cent|percent|thousand|million|billion|bn|mn)\b|(k|m|b)\b)?", re.I)
_SCALE = {"thousand": 1e3, "k": 1e3, "million": 1e6, "mn": 1e6, "m": 1e6, "billion": 1e9, "bn": 1e9, "b": 1e9}
_YEAR = re.compile(r"(\d{4})")


def _parse(raw: str) -> tuple[float, int]:
    """(value, decimals shown) of a written number: 1,500,000 is 1500000, 3,5 is 3.5, 08 is 8."""
    if re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", raw):
        raw = raw.replace(",", "")
    elif "," in raw:
        raw = raw.replace(",", ".")
    return float(raw), (len(raw.split(".")[1]) if "." in raw else 0)


def figures(text: str) -> list[tuple[str, float, float, int]]:
    """Every figure in `text`: (as written, value, scale, decimals shown). "€1.5m" is ("1.5", 1.5, 1e6, 1)."""
    out = []
    text = text or ""
    for m in _FIG.finditer(text):
        value, dec = _parse(m.group(1))
        word = (m.group(3) or m.group(4) or "").lower().replace(" ", "")
        glued = m.start() > 0 and text[m.start() - 1].isalpha()  # the 2 in "B2B" is part of a name, not a quantity
        scale = 1.0 if glued else _SCALE.get(word, 1.0)
        out.append((m.group(1), value, scale, dec))
    return out


def _numbers_in(text: str) -> list[float]:
    """Every number written in `text`, both as written and with its scale word applied."""
    out: list[float] = []
    for _, value, scale, _ in figures(text):
        out.append(value)
        if scale != 1:
            out.append(value * scale)
    return out


def allowed_numbers_for(cites, pins: dict, findings: dict, names=()) -> list[float]:
    """The numbers a claim may state: those in the cited pins (value, the percentage of a proportion, the
    numbers inside a text or date value, the year of the as-of date) and cited findings, and the digits in
    entity names ("BMW i4" names a car, not a figure)."""
    out: list[float] = []
    for c in cites:
        if c in pins:
            pin = pins[c]
            v = pin.get("value")
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out.append(float(v))
                if pin.get("unit") == "proportion":
                    out += [v * 100, float(round(v * 100))]
            else:
                out += _numbers_in(str(v))
            year = _YEAR.match(str(pin.get("as_of") or ""))
            if year:
                out.append(float(year.group(1)))
        elif c in findings:
            out += _numbers_in(findings[c].get("statement", ""))
    for n in names:
        out += _numbers_in(n or "")
    return out


def _supported(value: float, scale: float, dec: int, allowed: list[float]) -> bool:
    """A figure is supported when an allowed number equals it, or, for a figure written with a scale word
    or decimals, when that number rounds to what the sentence shows (1.48m supports "1.5m"; 1.2m does not)."""
    target = value * scale
    for a in allowed:
        if abs(a - target) <= 1e-9 * max(1.0, abs(target)):
            return True
        if (scale != 1 or dec > 0) and a != 0 and abs(round(a / scale, dec) - round(value, dec)) < 1e-9:
            return True
    return False


def unsupported_figures(text: str, allowed: list[float]) -> list[str]:
    """The figures in `text`, as written, that no allowed number supports."""
    return [raw for raw, value, scale, dec in figures(text) if not _supported(value, scale, dec, allowed)]


def unsourced_figures(text: str, cites, pins: dict, findings: dict, names=()) -> list[str]:
    return unsupported_figures(text, allowed_numbers_for(cites, pins, findings, names))


_MULT = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mn": 1e6, "million": 1e6, "bn": 1e9, "b": 1e9,
         "billion": 1e9}
_QNUM = re.compile(r"(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(%|per ?cent|percent|k\b|mn\b|m\b|bn\b|b\b|thousand|million|billion)?",
                   re.I)


def numbers_in(quote: str) -> list[float]:
    out = []
    for m in _QNUM.finditer(quote or ""):
        raw = m.group(1).replace(",", "")
        try:
            x = float(raw)
        except ValueError:
            continue
        suf = (m.group(2) or "").lower().replace(" ", "")
        if suf in ("%", "percent", "percent"):
            out += [x, x / 100]
        elif suf in _MULT:
            out += [x * _MULT[suf], x]
        else:
            out.append(x)
    return out


def quote_supports(value, unit: str, quote: str) -> bool:
    """A numeric fact must have its number in the quote it came from (to 1%,
    after percentages and k/m/bn multipliers); a date its year. Text and
    boolean values are judged by the quote being verbatim, not here."""
    if isinstance(value, bool) or value is None:
        return True
    if isinstance(value, (int, float)):
        cands = numbers_in(quote)
        targets = [float(value)]
        if unit == "proportion":
            targets.append(float(value) * 100)
        for t in targets:
            for c in cands:
                if t == 0 and c == 0:
                    return True
                if t != 0 and abs(c - t) <= abs(t) * 0.01:
                    return True
        return False
    if unit == "date" and isinstance(value, str) and re.match(r"\d{4}", value):
        return value[:4] in (quote or "")
    return True
