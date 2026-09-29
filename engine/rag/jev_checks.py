#!/usr/bin/env python3
"""
jev_checks.py — jev as a checker inside the brief pipeline (Sai, 2026-09-26; ADR 0011).

jev answers yes/no ("noul") and multiple-choice questions about one text with a calibrated
probability, in 0.2-0.45 s warm, for about $0.04 per million input tokens. It cannot
write, count or do arithmetic, so it checks and chooses; it never replaces a writer.
Four uses, each measured in the 2026-09-24 jev lab (engine/outputs/audit_2026_09_24/jev-lab):

  figures_supported   RTB and desired-response items: "does every number in ITEM appear in
                      the brief, used for the same thing?" AUC 0.997 on 156 real claims;
                      at p(unsupported) >= 0.9: precision 1.0, recall 0.91 on generated
                      items. The pipeline fails a draft that crosses that line.
  choose_category     the brief's category among the 18 locked ones: 13 of 13 correct, 10
                      at p >= 0.99, lowest correct 0.85. Used as retrieval's category
                      filter when no upstream category is given, at p >= 0.85.
  check_scorecard     each BetterBriefs dimension as pass / vague / missing. Unmeasured, so
                      it only records agreement and flags disputes (p >= 0.9); the
                      scorecard's own verdict stands until the CD/planner labels say
                      which is right.
  synthesis_support   each cited sentence of a loop synthesis against its own cited
                      passages. Unmeasured against sources (against the brief the lab
                      measured AUC 0.73), so it only marks sentences in the review file.

  sort_segments       the Loop 1 capture fallback (capture_fallback.py, 2026-09-29): which
                      part of a brief each sentence is, when the model capture failed.
                      46% of the model's fields on 7 saved briefs, 2 wrong.
  claims_supported    evaluation only (grounding.py, 2026-09-28): each claim of a finished
                      brief as supported / contradicted / not_in_brief against the client
                      brief. On the 2026-09-28 trial it separated the briefs that invent proof
                      points (bord-gais and friskies on ragAdded: 4 of 4) from those that do not (0).
                      Never used inside the pipeline.

Every function returns None when jev cannot answer (no TYPESAFE_API_KEY, SDK missing,
timeout, bad response, BRIEF_JEV_CHECKS=0) and prints one line saying so; callers record
that the check did not run and carry on unchecked, never blocked.
"""
from __future__ import annotations

import os
import re
import sys
import threading

STATE_CHARS = 60_000             # ~15k tokens: well inside jev's 32k state-plus-question rule
BATCH = 40                       # questions per request (jev accepts 50; the lab used <= 40)
FIGURE_FAIL_P = 0.9              # p(unsupported) at which a figure fails a draft (lab: P 1.0, R 0.91)
CATEGORY_MIN_P = 0.85            # lowest correct category probability in the lab
DISPUTE_P = 0.9                  # scorecard / synthesis: flag only above this confidence

CATEGORY_DESC = {
    "fmcg": "household, personal care and pet products",
    "food_drink": "what people eat and drink, including QSR and food delivery",
    "alcohol": "alcoholic drinks",
    "financial_services": "banks, insurance, payments, investment",
    "retail": "shops and retailers, including a shop that sells through an app",
    "luxury": "luxury goods: scarcity, restraint, no price",
    "automotive": "cars, vans, vehicles and their makers",
    "technology": "the product IS the technology: devices, model providers, apps",
    "b2b": "selling to businesses",
    "telecoms": "mobile, broadband and telecom operators",
    "travel": "airlines, airports, hotels, tourism",
    "public_sector": "government departments and public bodies",
    "charity": "charities and non-profits",
    "healthcare": "health, pharma and medical",
    "media_entertainment": "media, publishing, TV, sport and entertainment (never gambling)",
    "gambling_betting": "betting and gambling operators",
    "fashion_beauty": "fashion, clothing and beauty",
    "other": "none of the above (for example energy utilities, oil and fuel)",
}

FIGURE_Q = "Does every number in ITEM appear in the client brief in the state, used for the same thing?"
FIGURE_T = ("Each figure (percentage, count, price, duration, year, age) in the item is stated in "
            "the brief and refers to the same thing there.")
FIGURE_F = ("At least one figure in the item is absent from the brief, is computed or rounded from "
            "other figures, or refers to something different in the brief.")

SCORECARD_DEFS = {
    "objectives_quality": "a handful of objectives at most, benchmarked and time-stamped, the commercial, "
                          "behavioural and attitudinal chain linked, a clear hierarchy",
    "audience_vividness": "a vivid picture of the audience (demographics, psychographics, needs) that says "
                          "who it is NOT for; demographic cliches or bare age ranges are vague",
    "single_minded_message": "ONE key message, supported by relevant proof points",
    "evaluation_criteria": "the brief states how the work will be judged",
    "budget_interlock": "budget, objectives and audience are mutually feasible",
    "strategic_clarity": "a clear strategic choice, including what NOT to do",
    "language": "simple, jargon-free, succinct language with no category-speak",
}

_NUM_RE = re.compile(r"\d|\b(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|twenty|"
                     r"thirty|forty|fifty|hundred|thousand|million|billion|half|double|twice|triple)\b", re.I)
_backend = None
_lock = threading.Lock()
_warned: set = set()


def _say(msg: str) -> None:
    """One stderr line per distinct message per process."""
    if msg not in _warned:
        _warned.add(msg)
        print(msg, file=sys.stderr)


def backend():
    """The process's own JevBackend for these checks (separate from the validation chain's
    client, so the two never share a connection), or None when jev cannot be built or
    BRIEF_JEV_CHECKS=0."""
    global _backend
    if os.environ.get("BRIEF_JEV_CHECKS", "1") == "0":
        return None
    with _lock:
        if _backend is None:
            try:
                import judge_jev
                _backend = judge_jev.JevBackend()
            except Exception as e:      # noqa: BLE001 — BackendNotConfigured, SDK missing
                _say(f"[i] jev checks off: {e.__class__.__name__}: {str(e)[:160]}")
                _backend = False
        return _backend or None


def _ask(state: dict, questions: dict, what: str):
    """Send `questions` in batches of BATCH; merge the answers. {name: answer} where an
    answer is a probability (noul) or (choice, {option: probability}) (choice). None on
    any failure, with one line naming `what`."""
    b = backend()
    if b is None or not questions:
        return None if b is None else {}
    names, out = list(questions), {}
    try:
        for i in range(0, len(names), BATCH):
            chunk = {n: questions[n] for n in names[i:i + BATCH]}
            r = b.ask(state, chunk)
            for n, q in chunk.items():
                if q["type"] == "noul":
                    p = getattr(r.nouls.get(n), "noul", None)
                    if not isinstance(p, (int, float)) or isinstance(p, bool) or not 0 <= p <= 1:
                        raise ValueError(f"{n}: no probability")
                    out[n] = float(p)
                else:
                    a = r.choices.get(n)
                    out[n] = (a.choice, {k: float(v) for k, v in dict(a.probabilities).items()})
        return out
    except Exception as e:              # noqa: BLE001 — a check that fails never blocks the brief
        _say(f"[!] jev check '{what}' did not run ({e.__class__.__name__}: {str(e)[:120]}); left unchecked.")
        return None


def _noul(question: str, true: str, false: str, **instructions) -> dict:
    """One yes/no question in the SDK's raw form."""
    return {"type": "noul", "instructions": {"question": question, **instructions},
            "criteria": {"true": true, "false": false}}


def has_figure(text: str) -> bool:
    """True when `text` carries a digit or a number word, i.e. something to check."""
    return bool(_NUM_RE.search(str(text or "")))


def figures_supported(brief_text: str, items: list) -> "list | None":
    """p(every figure in the item is in the brief) per item, None for an item with no
    figure (not asked). None when jev cannot answer."""
    asked = {f"f{i:03d}": _noul(FIGURE_Q, FIGURE_T, FIGURE_F, item=str(it)[:600])
             for i, it in enumerate(items) if has_figure(it)}
    if not asked:
        return [None] * len(items)
    got = _ask({"client_brief": str(brief_text or "")[:STATE_CHARS]}, asked, "rtb figures")
    if got is None:
        return None
    return [got.get(f"f{i:03d}") for i in range(len(items))]


CLAIM_Q = "Is CLAIM supported by the client brief in the state?"
CLAIM_CRITERIA = {
    "supported": "The brief states this, or it follows directly from facts the brief states.",
    "contradicted": "The brief states something that conflicts with this claim.",
    "not_in_brief": "The brief does not contain this: the claim adds a fact, figure, source or proof "
                    "that the brief does not state."}


def claims_supported(brief_text: str, claims: list) -> "list | None":
    """(verdict, p) per claim, verdict one of CLAIM_CRITERIA and p its probability, in the
    order given. None when jev cannot answer."""
    asked = {f"c{i:03d}": {"type": "choice", "instructions": {"question": CLAIM_Q, "claim": str(c)[:600]},
                           "criteria": CLAIM_CRITERIA} for i, c in enumerate(claims)}
    if not asked:
        return []
    got = _ask({"client_brief": str(brief_text or "")[:STATE_CHARS]}, asked, "claim grounding")
    if got is None:
        return None
    out = []
    for i in range(len(claims)):
        choice, probs = got[f"c{i:03d}"]
        out.append((choice, round(float(probs.get(choice, 0.0)), 2)))
    return out


def sort_segments(brief_text: str, segments: list, labels: dict, hints: list) -> "list | None":
    """(label, p) per segment: which of `labels` ({name: description}) each brief sentence
    belongs to, with its section heading as a hint (capture_fallback's jev reader,
    2026-09-29). None when jev cannot answer."""
    asked = {f"s{i:03d}": {"type": "choice",
                           "instructions": {"question": "Which part of an advertising brief is SENTENCE?",
                                            "sentence": str(s)[:600], "section_heading_hint": str(h or "none")},
                           "criteria": labels} for i, (s, h) in enumerate(zip(segments, hints))}
    if not asked:
        return []
    got = _ask({"client_brief": str(brief_text or "")[:STATE_CHARS]}, asked, "capture fallback")
    if got is None:
        return None
    out = []
    for i in range(len(segments)):
        choice, probs = got[f"s{i:03d}"]
        out.append((choice, round(float(probs.get(choice, 0.0)), 2)))
    return out


def choose_category(brief_text: str) -> "dict | None":
    """{"category", "p", "probabilities"} for the client's category, or None."""
    q = {"category": {"type": "choice",
                      "instructions": {"question": "Which category is the CLIENT in? Choose by what the "
                                                   "client sells or does, not by the campaign topic."},
                      "criteria": dict(CATEGORY_DESC)}}
    got = _ask({"client_brief": str(brief_text or "")[:STATE_CHARS]}, q, "category")
    if not got:
        return None
    choice, probs = got["category"]
    return {"category": choice, "p": round(probs.get(choice, 0.0), 3),
            "probabilities": {k: round(v, 3) for k, v in sorted(probs.items(), key=lambda kv: -kv[1])[:3]}}


def check_scorecard(brief_text: str, dimensions: list) -> "dict | None":
    """jev's own verdict per scorecard dimension: {dimension: {"choice", "p"}}, or None."""
    qs = {}
    for d in dimensions:
        name = str(d.get("dimension") or "")
        if name not in SCORECARD_DEFS:
            continue
        qs[name] = {"type": "choice",
                    "instructions": {"question": f"How well does the client brief in the state meet this "
                                                 f"standard: {SCORECARD_DEFS[name]}?"},
                    "criteria": {"pass": "The brief clearly meets the standard.",
                                 "vague": "The brief touches it, but loosely, generically or only in part.",
                                 "missing": "The brief does not address it."}}
    got = _ask({"client_brief": str(brief_text or "")[:STATE_CHARS]}, qs, "scorecard")
    if got is None:
        return None
    return {n: {"choice": c, "p": round(pr.get(c, 0.0), 3)} for n, (c, pr) in got.items()}


def synthesis_support(sentences: list, sources: dict) -> "list | None":
    """p(the sentence is supported by the sources it cites) per (sentence, [cite keys]),
    None for a sentence with no known cite. `sources` maps cite key -> passage text."""
    state = {"sources": {k: str(v)[:1500] for k, v in list(sources.items())[:40]}}
    qs = {}
    for i, (sent, cites) in enumerate(sentences):
        keys = [c for c in cites if c in state["sources"]]
        if keys:
            qs[f"s{i:03d}"] = _noul("Is SENTENCE supported by the sources it cites (CITES) in the state?",
                                    "What the sentence says is stated or directly implied by the cited sources.",
                                    "The sentence adds something the cited sources do not say, or contradicts them.",
                                    sentence=str(sent)[:600], cites=keys)
    if not qs:
        return [None] * len(sentences)
    got = _ask(state, qs, "synthesis support")
    if got is None:
        return None
    return [got.get(f"s{i:03d}") for i in range(len(sentences))]
