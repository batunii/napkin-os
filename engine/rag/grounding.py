#!/usr/bin/env python3
"""
grounding.py — how many claims in a finished brief are not in the client's document
(phase A item 5, Sai 2026-09-28). Evaluation only: the pipeline never calls it.

    import grounding
    g = grounding.check(brief_text, client_brief_md)
    g["invented"], g["of"], g["to_confirm"]        # e.g. 1, 6, 2

Why: health rewards a filled field and barely penalises an invented one, so a brief that
fills its reasons to believe with facts the client never gave can outscore one that keeps
to the document and leaves the gap as an open question (bord-gais 2026-09-28: health 66 vs
45, the higher one resting on a smart-meter rollout the document never mentions). This
count sits beside health in every checkpoint report so that cannot pass unseen.

What is counted: each reason to believe from the saved client_brief.md, the brief's facts. jev (jev_checks.claims_supported) answers
supported / contradicted / not_in_brief per claim; a claim counts as invented when the
answer is not "supported" at p >= INVENTED_P. A line starting "TO CONFIRM" is the brief
asking for evidence, not claiming it, so it is counted apart as `to_confirm`. The SMP,
insight and desired response are meant to go beyond the document and are not counted: on
the 2026-09-28 trial jev called three purely creative SMPs "not in brief" (media-gaa and
betfair on the current code, plus-auto on ragAdded), while the one SMP that did invent
(bord-gais on ragAdded) rested on reasons to believe that were caught anyway.
Placeholders ("To be agreed") are skipped. Costs one jev request per brief (~1-2 s, no
Claude calls); returns None when jev cannot answer.

open_questions() is the second count (audit JL-13, 2026-09-29): the brief's open questions
that the client's document already answers (jev p >= INVENTED_P), a regression guard against
a brief asking the client what the brief already says. On 82 recorded questions none was
answered at p >= 0.9 (median 0.075), so it should read 0; one more jev request per brief.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

INVENTED_P = 0.9                 # the same confidence the pipeline's figure check fails at
SUPPORTED = ("supported", "supported_by_research")
COUNTED = {"Reasons to believe": "rtb"}
# The note brief_render puts above a kept draft ('_Draft — to review: it failed ..._') is
# the brief talking about the field, not a claim; the draft's items below it are counted.
REVIEW_NOTE = re.compile(r"^_Draft\b.*\bto review\b", re.I)


def claims_from_md(md: str) -> list:
    """(field, claim) for each reason to believe in a client_brief.md, in order;
    placeholder lines ('To be agreed') and the review note of a kept draft left out."""
    out, cur = [], None
    for line in (md or "").splitlines():
        if line.startswith("## "):
            cur = COUNTED.get(line[3:].strip())
            continue
        t = line.strip()
        if not cur or not t or "To be agreed" in t or REVIEW_NOTE.match(t):
            continue
        out.append((cur, re.sub(r"^[-*]\s+", "", t)))
    return out


def is_request(claim: str) -> bool:
    """True for the brief's own request for evidence ('TO CONFIRM: ...')."""
    return claim.strip().upper().startswith("TO CONFIRM")


def summarise(claims: list, verdicts: list) -> dict:
    """{"invented", "of", "to_confirm", "rows"} from (field, claim) pairs and their
    (verdict, p) answers. `of` counts claims, not requests."""
    rows = [{"field": f, "claim": c, "verdict": v, "p": p, "request": is_request(c)}
            for (f, c), (v, p) in zip(claims, verdicts)]
    flagged = [r for r in rows if r["verdict"] not in SUPPORTED and r["p"] >= INVENTED_P]
    out = {"invented": sum(1 for r in flagged if not r["request"]),
           "of": sum(1 for r in rows if not r["request"]),
           "to_confirm": sum(1 for r in rows if r["request"]),
           "rows": rows}
    research = sum(1 for r in rows if r["verdict"] == "supported_by_research" and not r["request"])
    if research:
        out["from_research"] = research
    return out


def check(brief_text: str, client_brief_md: str, research: "list | None" = None) -> "dict | None":
    """The grounding count of one finished brief against its client brief and, when the
    brief was written with verified research facts, those facts (C1c, 2026-09-29: a claim a
    fact supports is counted as from_research, not as invented). None when jev cannot
    answer. A brief with no counted claims returns zeros."""
    import jev_checks
    claims = claims_from_md(client_brief_md)
    asked = [c for _f, c in claims if not is_request(c)]         # requests are not sent to jev
    got = jev_checks.claims_supported(brief_text, asked, research=research or None)
    if got is None:
        return None
    answers = iter(got)
    return summarise(claims, [("request", 1.0) if is_request(c) else next(answers) for _f, c in claims])


OPEN_Q_HEADING = "## Open questions"
PRIORITY_TAG = re.compile(r"^\*\*\[[^\]]*\]\*\*\s*")


def questions_from_md(md: str) -> list:
    """The open questions listed in a client_brief.md, in order, priority tags removed."""
    out, on = [], False
    for line in (md or "").splitlines():
        if line.startswith("## "):
            on = line.startswith(OPEN_Q_HEADING)
            continue
        t = line.strip()
        if on and t.startswith(("- ", "* ")):
            q = PRIORITY_TAG.sub("", t[2:].strip())
            if q:
                out.append(q)
    return out


def open_questions(brief_text: str, client_brief_md: str) -> "dict | None":
    """{"answered", "of", "rows"}: how many of the brief's open questions the client's
    document already answers (p >= INVENTED_P). None when jev cannot answer; zeros when the
    brief lists no questions."""
    import jev_checks
    qs = questions_from_md(client_brief_md)
    got = jev_checks.questions_answered(brief_text, qs)
    if got is None:
        return None
    rows = [{"question": q, "p": (round(p, 2) if isinstance(p, float) else None)} for q, p in zip(qs, got)]
    return {"answered": sum(1 for r in rows if (r["p"] or 0) >= INVENTED_P), "of": len(rows), "rows": rows}
