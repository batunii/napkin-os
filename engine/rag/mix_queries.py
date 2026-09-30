"""
mix_queries.py — the five per-field retrieval queries of the mix path, in one place.

Loops 3-7 of the brief generator (parse_brief.loops_3_7) and the middleware entry point
(rag_io.handle, retrieval.path = "mix") both retrieve one evidence set per brief field
with these queries. Keeping them here means the evidence Shrey's middleware receives is
retrieved exactly as the brief generator retrieves it.

Each spec is (field key, display title, query builder); a builder takes the brief gist
{"problem", "objective", "audience", "key_message"} (strings, "" when unknown).
"""
from __future__ import annotations

import re

LOOP37_SPECS = [
    ("loop3_research", "Loop 3 · Research & category intelligence",
     lambda g: f"how to research the category, competitors and audience for {g['audience']}; {g['problem']}"),
    ("loop4_insight", "Loop 4 · Human insight & cultural tension",
     lambda g: f"find the human insight and cultural tension for {g['audience']} given {g['problem']}"),
    ("loop5_proposition", "Loop 5 · Single-minded proposition",
     lambda g: f"single-minded proposition and key message to achieve {g['objective']}; {g['key_message']}"),
    ("loop6_substantiation", "Loop 6 · Substantiation & effectiveness evidence",
     lambda g: f"effectiveness evidence and proof a strategy delivers {g['objective']}; how brands grow"),
    ("loop7_qa", "Loop 7 · Strategic QA & decision rules",
     lambda g: f"common mistakes and decision rules to pressure-test {g['key_message']} for {g['objective']}"),
]


def queries_for(gist: dict) -> dict[str, str]:
    """{field key: query text} for a gist; missing gist keys read as ''."""
    g = {k: str(gist.get(k) or "") for k in ("problem", "objective", "audience", "key_message")}
    return {key: re.sub(r"\s+", " ", qfn(g)).strip() for key, _t, qfn in LOOP37_SPECS}
