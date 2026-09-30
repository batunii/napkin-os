"""Figures: the cite rule's no-unsourced-figure check, and whether a quote
supports a numeric value.

The report rule (Contract 3 §17): a claim states no figure that is not in a
cited pin (or verbatim in a cited finding). A "figure" is any digit run
`\\d+(?:[.,]\\d+)?`. What a cited pin allows is its value as written
(`repr`, `:g`), and for a proportion its percentage (`v*100:g` and the
rounded percentage). Digits inside entity names ("Lúnasa 0.0", "BMW i4")
are names, not figures.
"""

from __future__ import annotations

import re

NUM = re.compile(r"\d+(?:[.,]\d+)?")
FALL = re.compile(r"\b(down|fell|fall(s|en|ing)?|declin\w*|decreas\w*|drop(s|ped|ping)?|lower|reduc\w*|"
                  r"contract\w*|shr[ai]nk\w*|slump\w*|minus|loss)\b|[-\u2212]\s?\d", re.I)


def allowed_for_pin(pin: dict) -> set[str]:
    v = pin.get("value")
    out: set[str] = set()
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        out |= {repr(v), f"{v:g}"}
        if pin.get("unit") == "proportion":
            out |= {f"{v * 100:g}", f"{round(v * 100)}"}
    else:
        out |= set(NUM.findall(str(v)))
    return out


def allowed_for(cites, pins: dict, findings: dict, names=()) -> set[str]:
    allowed: set[str] = set()
    for c in cites:
        if c in pins:
            allowed |= allowed_for_pin(pins[c])
        elif c in findings:
            allowed |= set(NUM.findall(findings[c].get("statement", "")))
    for n in names:
        allowed |= set(NUM.findall(n or ""))
    return allowed


def unsourced_figures(text: str, cites, pins: dict, findings: dict, names=()) -> list[str]:
    allowed = allowed_for(cites, pins, findings, names)
    return [n for n in NUM.findall(text or "") if n not in allowed]


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
        # A fall is usually written as a size and a word ("down 0.2%", "fell 5%"):
        # a negative value matches its magnitude only when the quote says it fell.
        if value < 0 and FALL.search(quote or ""):
            targets += [abs(t) for t in targets]
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
