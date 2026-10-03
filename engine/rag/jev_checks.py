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
  fact_conflicts      research facts (C1d, 2026-09-29): does the client brief conflict with a
                      verified fact? A conflict is recorded for CLAN to settle, never decided.
  claims_supported    evaluation only (grounding.py, 2026-09-28): each claim of a finished
                      brief as supported / contradicted / not_in_brief against the client
                      brief. On the 2026-09-28 trial it separated the briefs that invent proof
                      points (bord-gais and friskies on ragAdded: 4 of 4) from those that do not (0).
                      Never used inside the pipeline.

The whole brief is read (2026-10-01, Sai: never leave anything from the input): a brief longer
than STATE_CHARS goes to jev in overlapping pieces (brief_chunks) and each question takes its
best piece; research fact lines sit in their own slot of every state, in as many parts as
they need. Before, every check saw only the first 60,000 characters, so a 228k-character
tender lost three quarters of itself and the facts appended after it. choose_category,
check_scorecard and sort_segments still read the first STATE_CHARS: they judge the brief as
a whole, and how to combine their answers over pieces is not decided.

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
CHUNK_OVERLAP = 2_000            # a longer brief is read in pieces that overlap by this much
RESEARCH_CHARS = 15_000          # research fact lines per state; more go in further states
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
FIGURE_Q_RESEARCH = ("Does every number in ITEM appear in the client brief or in the verified research "
                     "facts in the state, used for the same thing?")
FIGURE_T_RESEARCH = ("Each figure (percentage, count, price, duration, year, age) in the item is stated in "
                     "the brief or in a research fact (a share may be written as a percentage) and refers "
                     "to the same thing there.")
FIGURE_F_RESEARCH = ("At least one figure in the item is in neither the brief nor the research facts, is "
                     "computed or rounded from other figures, or refers to something different there.")

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


def brief_chunks(text: str, size: int = STATE_CHARS) -> list:
    """The whole of `text` in pieces of at most `size` characters that overlap by
    CHUNK_OVERLAP, each cut at a paragraph or line break near its end when there is one
    (Sai 2026-10-01: never leave anything from the input). One piece, exactly the text,
    when it fits, so a brief of up to `size` characters is asked as before."""
    text = str(text or "")
    if len(text) <= size:
        return [text]
    size = max(size, 2 * CHUNK_OVERLAP)
    out, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            cut = max(text.rfind("\n\n", start + size // 2, end), text.rfind("\n", start + size // 2, end))
            end = cut if cut > start else end
        out.append(text[start:end])
        if end >= len(text):
            break
        start = max(end - CHUNK_OVERLAP, start + 1)
    return out


def research_parts(lines: list, size: int = RESEARCH_CHARS) -> list:
    """Whole research fact lines packed in order into parts of at most about `size`
    characters (a single longer line is its own part). [] when there are none."""
    parts, cur, n = [], [], 0
    for ln in (str(x) for x in (lines or [])):
        if cur and n + len(ln) + 1 > size:
            parts.append("\n".join(cur))
            cur, n = [], 0
        cur.append(ln)
        n += len(ln) + 1
    if cur:
        parts.append("\n".join(cur))
    return parts


def _states(brief_text: str, research: "list | None" = None) -> list:
    """Every state needed to show jev the whole brief and, when given, every research fact
    line: each brief piece paired with each research part. A brief of up to STATE_CHARS is
    one piece with the research beside it (at most STATE_CHARS + RESEARCH_CHARS, the size
    claims_supported always sent); a longer brief is cut so piece plus research stays within
    STATE_CHARS. Without research and within STATE_CHARS this is [{"client_brief": brief}],
    the state every check sent before."""
    parts = research_parts(research or [])
    text = str(brief_text or "")
    widest = max((len(x) for x in parts), default=0)
    pieces = [text] if len(text) <= STATE_CHARS else brief_chunks(text, max(STATE_CHARS - widest, STATE_CHARS // 2))
    if not parts:
        return [{"client_brief": c} for c in pieces]
    return [{"client_brief": c, "verified_research_facts": r} for c in pieces for r in parts]


def _ask_states(states: list, questions: dict, what: str) -> "dict | None":
    """{name: [answer per state]} for the same questions asked against every state; None
    when any state cannot be answered (the check then did not run, as before)."""
    out: dict = {}
    for st in states:
        got = _ask(st, questions, what)
        if got is None:
            return None
        for n, a in got.items():
            out.setdefault(n, []).append(a)
    return out


def _most(answers: "list | None"):
    """The highest probability among one question's noul answers across states (the fact
    or figure is found in some piece of the brief), or None."""
    ps = [a for a in (answers or []) if isinstance(a, (int, float))]
    return max(ps) if ps else None


def has_figure(text: str) -> bool:
    """True when `text` carries a digit or a number word, i.e. something to check."""
    return bool(_NUM_RE.search(str(text or "")))


def figures_supported(brief_text: str, items: list, research: "list | None" = None) -> "list | None":
    """p(every figure in the item is in the brief) per item, None for an item with no
    figure (not asked). The whole brief is read (in overlapping pieces when long) and an
    item takes its best piece. With `research` (the run's verified fact lines) the facts
    sit in their own slot of every state and the question accepts a figure stated there
    (2026-10-01: appended after a 228k-character tender they were cut off, and every
    cited RTB failed). None when jev cannot answer."""
    q, t, f = ((FIGURE_Q_RESEARCH, FIGURE_T_RESEARCH, FIGURE_F_RESEARCH) if research
               else (FIGURE_Q, FIGURE_T, FIGURE_F))
    asked = {f"f{i:03d}": _noul(q, t, f, item=str(it)[:600])
             for i, it in enumerate(items) if has_figure(it)}
    if not asked:
        return [None] * len(items)
    got = _ask_states(_states(brief_text, research), asked, "rtb figures")
    if got is None:
        return None
    return [_most(got.get(f"f{i:03d}")) for i in range(len(items))]


CLAIM_Q = "Is CLAIM supported by the client brief in the state?"
CLAIM_CRITERIA = {
    "supported": "The brief states this, or it follows directly from facts the brief states.",
    "contradicted": "The brief states something that conflicts with this claim.",
    "not_in_brief": "The brief does not contain this: the claim adds a fact, figure, source or proof "
                    "that the brief does not state."}


CLAIM_Q_RESEARCH = ("Is CLAIM supported by the client brief, or by the verified research facts, "
                    "in the state?")
CLAIM_CRITERIA_RESEARCH = {
    "supported": "The client brief states this, or it follows directly from facts the brief states.",
    "supported_by_research": "The brief does not state it, but one of the verified research facts does, "
                             "or it follows directly from one.",
    "contradicted": "The brief or a research fact states something that conflicts with this claim.",
    "not_in_brief": "Neither the brief nor the research facts contain this: the claim adds a fact, "
                    "figure, source or proof that neither states."}


def claims_supported(brief_text: str, claims: list, research: "list | None" = None) -> "list | None":
    """(verdict, p) per claim, verdict one of CLAIM_CRITERIA and p its probability, in the
    order given. With `research` (verified fact lines, C1c 2026-09-29) jev also sees the
    facts and may answer supported_by_research; without it the question is exactly as before,
    so reports stay comparable. None when jev cannot answer."""
    q, crit = (CLAIM_Q_RESEARCH, CLAIM_CRITERIA_RESEARCH) if research else (CLAIM_Q, CLAIM_CRITERIA)
    asked = {f"c{i:03d}": {"type": "choice", "instructions": {"question": q, "claim": str(c)[:600]},
                           "criteria": crit} for i, c in enumerate(claims)}
    if not asked:
        return []
    got = _ask_states(_states(brief_text, research), asked, "claim grounding")
    if got is None:
        return None
    out = []
    for i in range(len(claims)):
        # across pieces: support anywhere wins, then a contradiction, then not_in_brief
        rank = {"supported": 0, "supported_by_research": 1, "contradicted": 2}
        choice, probs = min(got[f"c{i:03d}"], key=lambda a: (rank.get(a[0], 3), -float(a[1].get(a[0], 0.0))))
        out.append((choice, round(float(probs.get(choice, 0.0)), 2)))
    return out


CONFLICT_Q = "Does the client brief in the state say something that conflicts with FACT?"
CONFLICT_T = "The brief states a value, figure or claim about the same thing that differs from FACT."
CONFLICT_F = "The brief says nothing about this, or says the same thing as FACT."


def fact_conflicts(brief_text: str, fact_lines: list) -> "list | None":
    """p(the client brief conflicts with the fact) per verified research fact line, in order
    (C1d, 2026-09-29). None when jev cannot answer."""
    asked = {f"k{i:03d}": _noul(CONFLICT_Q, CONFLICT_T, CONFLICT_F, fact=str(f)[:600])
             for i, f in enumerate(fact_lines)}
    if not asked:
        return []
    got = _ask_states(_states(brief_text), asked, "fact conflicts")
    if got is None:
        return None
    return [_most(got.get(f"k{i:03d}")) for i in range(len(fact_lines))]


ANSWERED_Q = "Does the client brief in the state already answer OPEN_QUESTION?"
ANSWERED_T = "The brief states the answer: a reader of the brief would not need to ask this."
ANSWERED_F = "The brief does not give the answer, or gives only part of it."


def questions_answered(brief_text: str, questions: list) -> "list | None":
    """p(the client brief already answers the question) per open question, in order (audit
    JL-13, evaluation only). None when jev cannot answer."""
    asked = {f"q{i:03d}": _noul(ANSWERED_Q, ANSWERED_T, ANSWERED_F, open_question=str(q)[:600])
             for i, q in enumerate(questions)}
    if not asked:
        return []
    got = _ask_states(_states(brief_text), asked, "open questions answered")
    if got is None:
        return None
    return [_most(got.get(f"q{i:03d}")) for i in range(len(questions))]


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


# The brief's chain of argument (2026-10-02, Sai: "can we use jev for that final 4 questions"):
# one yes/no question per link, all four in one request over the chain itself. Keyed by the
# later field; the earlier field texts go in as instructions named in the question.
COHERENCE_QUESTIONS = {
    "insight": ("Does INSIGHT explain why the PROBLEM exists: the human reason behind the BACKGROUND and OBJECTIVES?",
                "The insight names a truth or tension about people that accounts for the problem the objectives set out to solve.",
                "The insight is unrelated to the problem, only restates a fact, or explains something other than the problem."),
    "smp": ("Does PROPOSITION grow out of INSIGHT and answer its tension?",
            "The proposition is what you would say to someone who holds the insight, and it resolves the insight's tension.",
            "The proposition ignores the insight, contradicts it, or answers a different problem."),
    "reasons_to_believe": ("Do REASONS_TO_BELIEVE prove PROPOSITION?",
                           "Each reason is evidence that the proposition is true.",
                           "The reasons prove something else (the size of the need, another message) or nothing at all."),
    "desired_response": ("Is DESIRED_RESPONSE what someone would think, feel and do if PROPOSITION landed, consistent with INSIGHT?",
                         "The think, feel and do follow from the proposition and fit the insight.",
                         "The response asks for something the proposition does not lead to, or contradicts the insight."),
}
_COHERENCE_NAMES = {"background": "BACKGROUND", "objectives": "OBJECTIVES", "insight": "INSIGHT", "smp": "PROPOSITION",
                    "reasons_to_believe": "REASONS_TO_BELIEVE", "desired_response": "DESIRED_RESPONSE"}


def coherence_links(chain: dict, later_fields: list) -> "dict | None":
    """p(the link into each later field holds), {field id: p}, for the fields in `later_fields`
    that have a question. `chain` is {field id: text} for background, objectives, insight, smp,
    reasons_to_believe and desired_response (missing ones are left out). One request; the
    chain is the state. None when jev cannot answer (the caller then asks Claude)."""
    given = {_COHERENCE_NAMES[k]: str(v)[:4000] for k, v in chain.items() if k in _COHERENCE_NAMES and v}
    asked = {}
    for fid in later_fields:
        if fid in COHERENCE_QUESTIONS and fid in chain and chain[fid]:
            q, t, f = COHERENCE_QUESTIONS[fid]
            asked[fid] = _noul(q, t, f, **{k: v for k, v in given.items()})
    if not asked:
        return {}
    state = {"brief_chain": "\n".join(f"{k}: {v}" for k, v in given.items())}
    got = _ask(state, asked, "brief coherence")
    if got is None:
        return None
    return {fid: got.get(fid) for fid in asked}


# The gap-filler's store check (ADR 0019): which held facts help with a gap.
RELEVANT_Q = "Does FACT help answer NEED for this brand's brief?"
RELEVANT_T = "The fact gives evidence or information that directly addresses the need."
RELEVANT_F = "The fact is about something else, or too general to help with the need."


def facts_relevant(need: str, fact_lines: list) -> "list | None":
    """p(the fact helps with the need) per fact line, in order; None when jev cannot answer."""
    asked = {f"r{i:03d}": _noul(RELEVANT_Q, RELEVANT_T, RELEVANT_F, need=str(need)[:500], fact=str(f)[:600])
             for i, f in enumerate(fact_lines)}
    if not asked:
        return []
    got = _ask({"need": str(need)[:500]}, asked, "gap facts")
    if got is None:
        return None
    return [got.get(f"r{i:03d}") for i in range(len(fact_lines))]
