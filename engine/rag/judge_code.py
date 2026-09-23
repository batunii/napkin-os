#!/usr/bin/env python3
"""
judge_code.py — deterministic admission predicates, run BEFORE any relevance backend.

A relevance backend answers "is this passage useful evidence for this brief?". This
module answers an earlier and cheaper question: "is this chunk allowed, and is it usable
at all?". The two are kept apart on purpose. An admission rule is a policy with a right
answer that code can compute exactly; asking a reranker or an LLM to enforce it would
make a policy probabilistic, spend a slow backend's capacity on passages that were never
eligible, and hide the refusal inside a relevance score nobody can audit.

    admit(metadata, text, ...)  ->  None      admitted
                                ->  "excluded" | "too_old" | "oversized"

The reason codes are closed (REFUSAL_REASONS) so the trace can be queried by reason, in
the same way BackendUnavailable.kind is closed in judge_base.

    excluded    the chunk's doc_id is in the caller's exclude list. Checked first: it is
                the caller's explicit decision and overrides everything else.
    oversized   the text, with whitespace runs collapsed to one space, is longer than
                MAX_PASSAGE_CHARS: far larger than any ordinary retrievable unit, so no
                backend should spend a call on it or let it swallow a prompt. This is a
                size policy, not a relevance question. Checked before recency because it
                holds for every request, whereas recency depends on this one.
    too_old     the chunk's award year is older than the caller's recency cap.

Why the length is measured with whitespace collapsed. Some playbook tables are padded
with tens of thousands of spaces: raw, five sections from two files are over 17,000
chars, but their content is 723 to 16,668 chars. Every backend sees the collapsed form
or its equivalent (judge_llm._clip collapses before clipping; a tokeniser makes no
tokens of whitespace), so padding costs no backend anything, and counting it refused
two ordinary-sized aaker worked examples (35,152 raw -> 757; 17,262 raw -> 723) that
could otherwise never be admitted. Text that fits the cap raw is never collapsed:
collapsing only shrinks it, so the common case pays for one len().

Why MAX_PASSAGE_CHARS is 12,000 (collapsed lengths on _index_v3, 7,315 chunks,
2026-09-23): p50 579, p90 2,210, p99 4,862, p99.9 7,861. The largest chunk outside one
file is 9,058 chars (a Cannes parent); 12,000 keeps a third of headroom above it. Above
the cap are only two sections of 91-pestle-steep-analysis.md, both long real content
rather than padding: "WORKED EXAMPLE B (STEEP)" (16,668; 1.8x the largest case parent)
and "WORKED EXAMPLE A (PESTLE)" (12,121, just over). Its "OUTPUT TEMPLATE" (238,675 raw,
11,480 collapsed) is admitted. So the cap admits 7,313 of 7,315 chunks (99.97%). The
refusal of the PESTLE example at 12,121 is a near thing; the cap is set by headroom over
the ordinary corpus, not tuned to that chunk, and test_cap_refuses_only_the_known_long_
chunks fails if a rebuild moves either side of it. The cap is deliberately NOT
parse_brief.EVIDENCE_MAX_CHARS (6,000): that one CLIPS text for display, and refusing at
6,000 would drop 38 legitimate case parents.

Recency rule — and why it is narrower than "compare `year` with the cap":
  * Only a clean year counts: an int, or a string that is exactly four digits. The
    corpus also holds "2020s", "c. 1980s", "1898 / 2012", "c. 350 BC", "varies". An
    absent or unparseable year ADMITS. The alternative, refusing what cannot be dated,
    would silently drop the whole template bucket (no year at all) for no evidence.
  * Only sources whose `year` is the AWARD year are capped (RECENCY_SOURCES: ipa, effie,
    cannes, dandad — the same set chunking._CASE_SOURCES uses to derive `as_of`, and a
    test keeps the two from drifting). A playbook's `year` is when the FRAMEWORK was
    invented (FCB Grid 1980, DAGMAR 1961); 647 playbook chunks carry such a clean year,
    and a five-year cap applied to them would refuse most of the craft bucket for being
    old ideas. A playbook's `as_of` is not a substitute: it is the file's mtime, which
    moves whenever someone touches the file and so says nothing reliable either.
  * The boundary is inclusive: with recency_years=5 and as_of in 2026, 2021 is admitted
    and 2020 is refused. A year after as_of (a future award year) is admitted.

Not implemented: licence class. Excluding material by licence (e.g. "internal use only",
"no model training") is an admission question and belongs here, but the metadata
contract (../schema/rag_metadata.v1.json, v1.3.0, locked) has no licence field, so there
is nothing to test against. Adding it is future work that needs a contract minor bump
and a retag backfill first; guessing a licence from `source` here would be a policy
nobody signed off.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Iterable

# Closed so the trace can be grouped by reason; admit() never returns anything else.
REFUSAL_REASONS = ("excluded", "too_old", "oversized")

# See the module docstring for the measurement behind this number.
MAX_PASSAGE_CHARS = 12_000

# Sources whose `year` is the award year, i.e. when the lesson was judged true. Mirrors
# chunking._CASE_SOURCES (guarded by test_recency_sources_match_chunking).
RECENCY_SOURCES = frozenset({"ipa", "effie", "cannes", "dandad"})

_YEAR_RE = re.compile(r"\s*(\d{4})\s*")
_SPACE_RE = re.compile(r"\s+")


def content_length(text: str | None) -> int:
    """Length of `text` with every whitespace run collapsed to one space and the ends
    stripped: what a backend actually has to read. None counts as empty. admit() calls it
    only when the raw text is over the cap (collapsing never lengthens a string); it is
    public so tests and traces can report the size the cap was compared with."""
    return len(_SPACE_RE.sub(" ", text or "").strip())


def parse_year(value) -> int | None:
    """A clean four-digit year from metadata, or None when there is none to trust.

    Accepts an int (PyYAML reads `year: 2024` as one, even though the contract stores a
    string) or a string that is exactly four digits, surrounding whitespace allowed.
    Everything else — decades, ranges, "c. 1320", floats, bools — is None, because
    picking a year out of "1898 / 2012" would be a guess dressed up as data."""
    if isinstance(value, bool):          # bool is an int subclass; True is not year 1
        return None
    if isinstance(value, int):
        return value if 1000 <= value <= 9999 else None
    if isinstance(value, str):
        m = _YEAR_RE.fullmatch(value)
        return int(m.group(1)) if m else None
    return None


def admit(hit_metadata: dict, text: str, *, exclude_doc_ids: Iterable[str] = frozenset(),
          recency_years: int | None = None, as_of: date | None = None,
          max_chars: int = MAX_PASSAGE_CHARS) -> str | None:
    """None if the chunk may go on to relevance judging, else one of REFUSAL_REASONS.

    hit_metadata     the chunk's contract metadata (doc_id, source, year, ...); None is
                     treated as {}
    text             the chunk text that would be shown to a backend; its size is
                     measured by content_length(), so whitespace padding does not count
    exclude_doc_ids  doc ids the caller has ruled out; compared as strings
    recency_years    maximum award-year age; None means no recency cap
    as_of            the date the age is measured from; default today. Passed explicitly
                     by tests and by replays, so a recorded run refuses the same chunks
                     next year as it did this year
    max_chars        oversize cap on content_length(text); default MAX_PASSAGE_CHARS

    Checks run in the order excluded, oversized, too_old, and the first that fires is
    the reason reported (see the module docstring for why that order)."""
    md = hit_metadata or {}
    if recency_years is not None and (isinstance(recency_years, bool) or recency_years < 0):
        raise ValueError(f"recency_years must be a non-negative int or None, got {recency_years!r}")

    excluded = {str(d) for d in exclude_doc_ids} if exclude_doc_ids else set()
    doc_id = md.get("doc_id")
    if excluded and doc_id is not None and str(doc_id) in excluded:
        return "excluded"

    raw = text or ""
    if len(raw) > max_chars and content_length(raw) > max_chars:   # raw fits -> collapsed fits
        return "oversized"

    if recency_years is not None and md.get("source") in RECENCY_SOURCES:
        year = parse_year(md.get("year"))
        if year is not None:
            ref = (as_of or date.today()).year
            if ref - year > recency_years:
                return "too_old"
    return None
