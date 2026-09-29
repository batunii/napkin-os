"""Exact-quote verification.

A span's `quote` must be verbatim in the material it cites, and a fact's
evidence quote verbatim in the source excerpt it names. The model is asked
for verbatim quotes; this module checks, and never trusts. A quote that
differs from the text only in whitespace or typographic quote marks is
located and replaced by the text's own characters, so what is stored is
always a true substring. Anything else is rejected.
"""

from __future__ import annotations

import re

_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-",
                         "—": "-", " ": " "})


def _norm_with_map(text: str, fold: bool = True):
    """Collapse whitespace runs and (with `fold`) fold quote marks; return
    (normalised, index map from normalised position to original position)."""
    out, idx = [], []
    prev_space = False
    for i, ch in enumerate(text.translate(_QUOTES) if fold else text):
        if ch.isspace():
            if prev_space:
                continue
            out.append(" ")
            idx.append(i)
            prev_space = True
        else:
            out.append(ch)
            idx.append(i)
            prev_space = False
    return "".join(out), idx


def find_quote(text: str, quote: str) -> tuple[int, int] | None:
    """(start, end) of `quote` in `text`, or None. Exact first; then the
    whitespace/quote-mark-folded form mapped back onto the original."""
    if not isinstance(quote, str):
        return None
    q = quote.strip()
    if len(q) < 2 or not isinstance(text, str):
        return None
    i = text.find(q)
    if i >= 0:
        return i, i + len(q)
    nt, idx = _norm_with_map(text)
    nq, _ = _norm_with_map(q)
    nq = nq.strip()
    j = nt.find(nq)
    if j < 0:
        j = nt.lower().find(nq.lower()) if len(nq) >= 12 else -1  # case only for long quotes
    if j < 0 or not nq:
        return None
    start = idx[j]
    end = idx[j + len(nq) - 1] + 1
    return start, end


def verbatim(text: str, quote: str) -> str | None:
    """The text's own rendering of `quote`, or None when it is not there."""
    span = find_quote(text, quote)
    return text[span[0]:span[1]] if span else None


def verbatim_ws(text: str, quote: str) -> str | None:
    """The text's own rendering of `quote` when the two match exactly after
    one normalisation only: every whitespace run, on both sides, is one space.
    No case folding, no quote-mark or dash folding (a client's words, Contract
    4 §7.5: "In the proof" means this and nothing looser). None otherwise."""
    if not isinstance(quote, str) or not isinstance(text, str):
        return None
    nt, idx = _norm_with_map(text, fold=False)
    nq = _norm_with_map(quote, fold=False)[0].strip()
    j = nt.find(nq) if nq else -1
    if j < 0:
        return None
    return text[idx[j]:idx[j + len(nq) - 1] + 1]


def contains_word(text: str, word: str) -> bool:
    return bool(word) and re.search(rf"(?<!\w){re.escape(word)}(?!\w)", text or "", re.I) is not None
