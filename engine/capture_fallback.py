"""
capture_fallback.py — the Loop 1 capture when the model capture fails, or with no model at
all (audit C13; Sai, 2026-09-29: jev first, a deterministic reader when jev fails).

    fields, used, reader = capture_fallback.capture(segments, text, allow_jev=True)
    # reader: "jev" or "rules"; fields as the model capture shapes them; used = segment indexes

Two readers, tried in this order:

  jev    one jev choice question per sentence: which part of a brief is it (18 capture
         fields or "other"), with the sentence's section heading as a hint. A different
         vendor from Claude, so usually still up when Claude is not; ~1-2 s and a fraction of
         a cent per brief, no Claude calls. Measured 2026-09-29 against the model capture on
         7 saved briefs: 46% of the model's fields (50 of 109), 2 wrong; on an unseen brief
         5 of 16.
  rules  deterministic, regex and word lists only: the same text always gives the same
         capture. A heading (a short line without a full stop, a question, or a known heading
         phrase, also when glued to the first sentence) sets the field for the text under it;
         `Label: value` lines go to their label's field; per-field sentence cues (currency,
         dates, must/logo, KPIs, ages, roles and emails, insight/tension ...) add a sentence to
         further fields, so one sentence may feed several. Measured: 37% (40 of 109) on the 7
         briefs the rules were written against, 2 of 16 on the unseen one, against 9% and 0
         for the keyword rules it replaces.

Neither can rewrite or summarise as the model does; both place whole sentences. The brief
says which reader ran (meta.capture_fallback.reader, an open question, review.md).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Capture fields that hold a list of entries; the rest hold one entry whose text grows.
LIST_FIELDS = {"deliverables", "mandatories", "timeline", "success_metrics", "decision_makers",
               "constraints", "objective", "proof_points", "evaluation_criteria"}

# Heading phrases per field, English then Romanian. Longest phrases are tried first, so
# "background to the problem" is the problem, not the background.
HEADINGS = {
    "business_problem": ["background to the problem", "what is the problem", "business problem", "the challenge",
                         "challenge", "the problem", "problem", "the task", "task", "provocarea", "provocare", "problema"],
    "background_context": ["what is the reason for the campaign", "reason for the campaign", "background", "context",
                           "situation", "overview", "about the brand", "about us", "contextul"],
    "objective": ["what do we want to achieve", "what we want to achieve", "strategic objectives", "business objectives",
                  "communication objectives", "objectives", "objective", "goals", "goal", "aims", "ambition",
                  "ce vrem sa obtinem", "ce vrem să obținem", "obiective", "obiectivul", "obiectiv"],
    "target_audience": ["who are we talking to", "target audience", "target group", "who is it for", "audience", "target",
                        "the consumer", "public tinta", "public țintă", "grup tinta", "grup țintă"],
    "key_message": ["what is the one thing", "single-minded proposition", "single minded proposition", "key message",
                    "proposition", "message", "mesaj cheie", "mesajul cheie"],
    "proof_points": ["reasons to believe", "reason to believe", "rtbs", "rtb", "proof points", "support",
                     "product in category", "product"],
    "strategic_angle": ["consumer insights", "insights", "insight", "strategy", "strategic direction", "positioning"],
    "deliverables": ["deliverables", "what we need from you", "what we need", "scope of work", "scope", "outputs",
                     "output", "livrabile"],
    "mandatories": ["mandatories", "mandatory", "must-haves", "must haves", "guidelines", "legal", "mandatorii",
                    "obligatorii"],
    "budget": ["budget", "buget", "bugetul"],
    "timeline": ["key dates", "timing", "timeline", "deadline", "ddl", "dates", "termen", "termene", "calendar"],
    "success_metrics": ["how will we measure success", "how will we measure", "kpis", "kpi", "measurement",
                        "success metrics", "success"],
    "competitors_market": ["competitors", "competition", "competitive landscape", "market", "concurenta", "concurența"],
    "tone_and_brand": ["tone of voice", "tone", "brand personality", "look and feel", "brand values", "tonul"],
    "decision_makers": ["decision makers", "stakeholders", "approvals", "campaign owner", "project manager"],
    "constraints": ["constraints", "limitations"],
    "evaluation_criteria": ["how will the work be judged", "evaluation criteria", "evaluation"],
}
_PHRASES = sorted(((p, f) for f, ps in HEADINGS.items() for p in ps), key=lambda x: -len(x[0]))

# Sentence cues: a sentence matching one also goes to that field, wherever it sits.
CUES = {
    "budget": [r"[€$£]\s?\d", r"\b\d[\d.,]*\s?(k|m|mil|million|thousand)?\s?(eur|euro|lei|ron|usd|gbp)\b", r"\bbudget\b"],
    "timeline": [r"\b\d{1,2}[./]\d{1,2}([./]\d{2,4})?\b",
                 r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\b",
                 r"\b(q[1-4]|deadline|ddl|launch date|by (monday|tuesday|wednesday|thursday|friday|next week|end of))\b",
                 r"\bweek of\b"],
    "mandatories": [r"\bmust (include|have|feature|be|use|appear|show)\b", r"\bmandator", r"\blogo\b", r"\bdisclaimer",
                    r"\b18\+", r"\blegal\b", r"\bguidelines?\b", r"\bnon-negotiable"],
    "success_metrics": [r"\bkpis?\b", r"\bmeasure[sd]?\b", r"\bsuccess (will|is|=)",
                        r"\b(uplift|conversion|market share|awareness|consideration)\b.*\d",
                        r"\d+\s?%.*\b(increase|growth|awareness|share|sales)\b"],
    "objective": [r"\b(we want to|we need to|the goal is|the aim is|objective|to increase|to grow|to drive|to build|achieve)\b",
                  r"\bincrease\b.*\d"],
    "target_audience": [r"\b(aged?|ages)\s?\d", r"\b\d{2}\s?[–-]\s?\d{2}\b",
                        r"\b(target audience|our audience|consumers? who|people who|millennials|gen ?z|mothers|parents|bettors|shoppers|students)\b"],
    "deliverables": [r"\b(deliverables?|we need|please (provide|send|prepare)|presentation|key visual|kv|tactics|concepts?|toolkit|assets)\b"],
    "decision_makers": [r"\b(cmo|ceo|brand manager|marketing (director|manager)|project manager|campaign owner|account director|strategy:|cs:)\b",
                        r"[\w.]+@[\w.]+\.\w+"],
    "competitors_market": [r"\b(competitors?|competition|market leader|vs\.?|versus|rival)\b"],
    "tone_and_brand": [r"\b(tone|tone of voice|feel(s)? (like|premium|fun)|brand personality|look and feel)\b"],
    "key_message": [r"\b(key message|single[- ]minded|the message|we want (people|them|the public) to (think|feel|know))\b"],
    "strategic_angle": [r"\b(insight|tension|observation|positioning|territory)\b", r"→"],
    "proof_points": [r"\b(made with|contains|no artificial|natural ingredients|certified|award[- ]winning|number one|#1)\b"],
    "evaluation_criteria": [r"\b(will be (judged|evaluated|assessed)|evaluation criteria|we will judge)\b"],
}
_CUE = {f: [re.compile(p, re.I) for p in ps] for f, ps in CUES.items()}
_CURRENCY = _CUE["budget"][:2]
_DATE = _CUE["timeline"][:2]
_LABEL_LINE = re.compile(r"^([A-Za-z /]{3,30}?)\s*[:=]\s*(.+)$")

# What each field is, for jev's choice question.
FIELD_DESC = {
    "background_context": "background or context about the brand, product, market situation or why the campaign exists",
    "business_problem": "the business problem or challenge the campaign must solve",
    "objective": "what the campaign must achieve: commercial, behavioural or attitudinal goals",
    "target_audience": "who the campaign talks to: the audience, target group or consumer description",
    "key_message": "the single main message or proposition to communicate",
    "proof_points": "facts or product features that support the message (reasons to believe)",
    "evaluation_criteria": "how the agency's work or pitch will be judged",
    "strategic_angle": "an insight, strategic observation, tension or positioning direction",
    "anti_target": "who the campaign is NOT for",
    "deliverables": "what the agency must produce or deliver",
    "mandatories": "non-negotiable requirements: logos, legal lines, brand guidelines, must-include items",
    "budget": "money: budget, spend or fees",
    "timeline": "dates, deadlines, timing or schedule",
    "success_metrics": "KPIs or how success will be measured",
    "competitors_market": "competitors or the competitive market",
    "tone_and_brand": "tone of voice, brand personality or look and feel",
    "decision_makers": "people: names, roles, owners, approvers, contacts",
    "constraints": "limitations or restrictions on the work",
    "other": "none of these: greetings, sign-offs, headings or filler",
}


def heading_of(seg: str) -> tuple:
    """(field, rest) when `seg` is, or starts with, a heading; (None, seg) otherwise. `rest`
    is the text after a heading glued to its first sentence ('Context Dr. Oetker ...'); an
    unknown question heading returns ("other", "")."""
    s = seg.strip()
    low = re.sub(r"^[\d.)\-•*#\s]+", "", s.lower()).rstrip(" :?")
    words = s.split()
    for p, f in _PHRASES:
        if low == p or (len(words) <= 8 and not s.endswith(".") and low.startswith(p)):
            return f, ""
        if low.startswith(p + " ") and len(s) > len(p) + 3:
            rest = s[len(p):].lstrip(" :–-")
            if rest[:1].isupper() or s[:len(p)].isupper():
                return f, rest
    if s.endswith("?") and len(words) <= 12:
        return "other", ""
    return None, s


def _entry(text: str, conf: float) -> dict:
    """One captured entry in the model capture's shape."""
    return {"value": text, "status": "fact", "source_quote": text, "confidence": conf}


def _add(fields: dict, field: str, text: str, conf: float) -> None:
    """Add `text` to `field`: appended to a list field, joined onto a single field; the same
    text is never added twice."""
    if field in LIST_FIELDS:
        lst = fields.setdefault(field, [])
        if not any(e["value"] == text for e in lst):
            lst.append(_entry(text, conf))
    elif field in fields:
        if text not in fields[field]["value"]:
            fields[field]["value"] += " " + text
            fields[field]["source_quote"] += " " + text
    else:
        fields[field] = _entry(text, conf)


def rules_capture(segments: list) -> tuple:
    """The deterministic reader: (fields, used segment indexes). Sections first, then cues."""
    fields, used, cur = {}, set(), None
    for idx, seg in enumerate(segments):
        m = _LABEL_LINE.match(seg)
        if m:
            f, _ = heading_of(m.group(1))
            if f and f != "other":
                _add(fields, f, m.group(2).strip(), 0.5); used.add(idx)
                continue
        f, rest = heading_of(seg)
        if f:
            cur = f
            if rest and f != "other":
                _add(fields, f, rest, 0.5)
            used.add(idx)
            continue
        if cur and cur != "other":
            placed = cur
        elif any(p.search(seg) for p in _CURRENCY):
            placed = "budget"
        elif any(p.search(seg) for p in _DATE):
            placed = "timeline"
        else:
            placed = "background_context" if cur is None else None   # text before any heading
        if placed:
            _add(fields, placed, seg, 0.5); used.add(idx)
    for idx, seg in enumerate(segments):
        f, rest = heading_of(seg)
        text = rest if f else seg
        if not text:
            continue
        for field, pats in _CUE.items():
            if any(p.search(text) for p in pats):
                _add(fields, field, text, 0.4); used.add(idx)
    return fields, used


def jev_capture(segments: list, text: str) -> "tuple | None":
    """The jev reader: (fields, used), or None when jev cannot answer."""
    sys.path.insert(0, str(HERE / "rag"))
    try:
        import jev_checks
    except Exception:            # noqa: BLE001 — retrieval package unavailable: no jev
        return None
    hints, cur = [], None
    for s in segments:
        f, _rest = heading_of(s)
        cur = f or cur
        hints.append(cur or "none")
    got = jev_checks.sort_segments(text, segments, FIELD_DESC, hints)
    if got is None:
        return None
    fields, used = {}, set()
    for i, (seg, (choice, p)) in enumerate(zip(segments, got)):
        used.add(i)
        if choice != "other" and choice in FIELD_DESC:
            _add(fields, choice, seg, round(float(p), 2))
    return fields, used


def capture(segments: list, text: str, *, allow_jev: bool = True) -> tuple:
    """(fields, used, reader): jev when allowed and it answers, else the deterministic rules."""
    if allow_jev:
        got = jev_capture(segments, text)
        if got is not None:
            return got[0], got[1], "jev"
    fields, used = rules_capture(segments)
    return fields, used, "rules"
