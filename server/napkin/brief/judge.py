"""The Judge (middleware-api.md §10.8): engine/golden_critic.py, ported.

1. Auto checks in code (`rubric.auto_checks`).
2. Model checks: one structured call per field, carrying all its pending
   `llm` checks with the rubric's good and bad examples (golden_critic
   `critic_prompts_batched`), schema {<check>: {verdict, reason, fix}}.
3. Coherence, once every field is judged: the dependencies and the definition
   of done, including `one_strategy`.

The Judge never sees a drafter's prompt, candidates or grounds — only the
brief as it would stand, the rubric and loop-7 decision-rule passages (UC-6).
Its verdicts are the job's decisions (`job.py`); this module decides.
"""

from __future__ import annotations

import logging

from .fields import RUBRIC_FIELDS, filled
from .rubric import DEPENDENCIES, DOD, FAIL, PASS, REVIEW, auto_checks, rubric_value

log = logging.getLogger("napkin.brief.judge")

JUDGE_SYSTEM = ("You are a rigorous but fair brief-quality critic judging ONE field of a creative brief against "
                "its rubric. Judge the VALUE on EACH test independently, using the context fields where given "
                "(verify derivation and ownability against the real context — do not fail derivation merely because "
                "the context is not repeated in the line). The decision rules are planning craft for "
                "pressure-testing; apply them. For each test: verdict pass or fail, one line of reason, and — only "
                "when it fails — one concrete fix (otherwise null).")
COHERENCE_SYSTEM = ("You are a strategy director checking that a creative brief holds together as ONE strategy. "
                    "Judge each rule against the brief as a whole: pass or fail, one line of reason, and a concrete fix "
                    "when it fails (otherwise null). Two messages mean two briefs; a brief sets the problem and does "
                    "not prescribe the creative idea.")
COHERENCE_LLM = ("backbone_balance", "smp_derivation", "rtb_supports_smp", "response_ladders")
DOD_LLM = ("one_strategy", "no_solution_prescribed")
# Context a field's tests need beyond the rubric's depends_on. why_now asks for
# the timing, and the capture files a launch date under budget & scope and the
# relaunch under the commercial objective: without them the Judge failed a
# background whose brief said "launch March" and told the planner to take it
# back to the client. ownable/derivation need the competitors.
JUDGE_CONTEXT = {"background": ("budget_scope", "objectives"), "insight": ("competitor_context",),
                 "smp": ("competitor_context",)}
# which rubric fields each definition-of-done model check names (for verdicts)
DOD_FIELDS = {"one_strategy": ["insight", "smp", "reasons_to_believe", "desired_response"]}


REASON_MAX, FIX_MAX = 900, 600


def clip(text, n: int) -> str:
    """`text` stripped, whole when it fits; otherwise cut at the last sentence
    (or, failing that, word) that fits, with an ellipsis. A reason is never
    cut mid-word, and what follows it (a fix) is never glued on mid-sentence."""
    t = " ".join(str(text or "").split())
    if len(t) <= n:
        return t
    head = t[:n - 1]
    ends = [head.rfind(p) + 1 for p in (". ", "; ", "! ", "? ")]
    cut = max(ends)
    if cut >= n // 2:  # whole sentences, and a mark that more was said
        return head[:cut].rstrip() + " …"
    cut = head.rfind(" ")
    return (head[:cut] if cut > 0 else head).rstrip(" ,;:—-") + "…"


def sentence(text) -> str:
    """`text` as a sentence: ends with a full stop unless it already ends."""
    t = str(text or "").strip()
    return t if not t or t[-1] in ".!?…" else t + "."


def _obj(props: dict) -> dict:
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


_VERDICT = _obj({"verdict": {"type": "string", "enum": ["pass", "fail"]}, "reason": {"type": "string"},
                 "fix": {"anyOf": [{"type": "string"}, {"type": "null"}]}})


def judge_field(model, rid: str, values: dict, rules: list[dict]) -> dict:
    """The rubric field `rid` over the brief as it would stand (`values`: app
    key -> value). -> {rubric, checks, model_ran, error}."""
    f = RUBRIC_FIELDS[rid]
    value = rubric_value(rid, values)
    checks = auto_checks(rid, value, competitor_context=values.get("competitor_context"))
    pending = [c for c in checks if c["method"] == "llm" and c["status"] == REVIEW and filled(value)]
    out = {"rubric": rid, "checks": checks, "model_ran": False, "error": None}
    if not pending:
        return out
    tests = {r["id"]: r["test"] for r in f.get("rubric", [])}
    deps = list(f.get("depends_on") or []) + [d for d in JUDGE_CONTEXT.get(rid, ()) if d not in (f.get("depends_on") or [])]
    context = {d: rubric_value(d, values) for d in deps if d in RUBRIC_FIELDS}
    payload = {"field": f["label"], "what_the_field_is": f.get("prompt"), "good_example": f.get("good_example"),
               "bad_example": f.get("bad_example"), "why_the_bad_one_fails": f.get("bad_reason"),
               "value": value, "context": {k: v for k, v in context.items() if filled(v)},
               "tests": [{"id": c["check"], "test": tests.get(c["check"], "")} for c in pending],
               "decision_rules": rules}
    schema = _obj({c["check"]: _VERDICT for c in pending})
    try:
        res = model.structured(f"judge_{rid}", JUDGE_SYSTEM, payload, schema, max_tokens=3000)
    except Exception as e:  # the checks stay at review; the verdict says a person must look
        out["error"] = f"the judge's model call failed ({getattr(e, 'kind', type(e).__name__)})"
        for c in pending:
            c["note"] = out["error"]
        return out
    out["model_ran"] = True
    for c in pending:
        r = res.get(c["check"]) or {}
        c["status"] = PASS if r.get("verdict") == "pass" else FAIL
        c["note"] = clip(r.get("reason"), REASON_MAX) or c["status"]
        if c["status"] == FAIL and (r.get("fix") or "").strip():
            c["fix"] = clip(r["fix"], FIX_MAX)
    return out


def failures(result: dict) -> list[dict]:
    return [{"check": c["check"], "reason": c["note"], "fix": c.get("fix")} for c in result["checks"]
            if c["status"] == FAIL]


def coherence(model, values: dict, rules: list[dict], only: set | None = None) -> tuple[list[dict], bool, str | None]:
    """The dependencies and the definition of done's model checks. `only`:
    rubric fields the checks must involve (regenerate_field). -> (results,
    model_ran, error). Each result {id, status, note, fix?, fields: [rubric ids]}."""
    out, ask = [], []
    for did, d in DEPENDENCIES.items():
        fields_ = list(d["fields"])
        if only is not None and not (set(fields_) & only):
            continue
        all_filled = all(filled(rubric_value(fid, values)) for fid in fields_)
        r = {"id": did, "fields": fields_, "rule": d["rule"]}
        if did == "ownable_needs_competitors":
            ok = filled(values.get("competitor_context"))
            r.update(status=PASS if ok else FAIL, note="competitor context present" if ok else "fill competitor_context",
                     method="auto")
        elif not all_filled:
            st = FAIL if did in ("rtb_supports_smp", "response_ladders") else REVIEW
            r.update(status=st, note="a field it links is missing", method="auto")
        else:
            r.update(status=REVIEW, note="", method="llm")
            ask.append(r)
        out.append(r)
    for d in DOD:
        if d["id"] in DOD_LLM:
            fields_ = DOD_FIELDS.get(d["id"], [])
            if only is not None and d["id"] == "no_solution_prescribed":
                continue
            if only is not None and not (set(fields_) & only):
                continue
            r = {"id": d["id"], "fields": fields_, "rule": d["rule"], "status": REVIEW, "note": "", "method": "llm"}
            ask.append(r)
            out.append(r)
    if not ask:
        return out, False, None
    brief = {k: v for k, v in values.items() if filled(v)}
    try:
        res = model.structured("judge_coherence", COHERENCE_SYSTEM,
                               {"brief": brief, "rules": [{"id": r["id"], "rule": r["rule"]} for r in ask],
                                "decision_rules": rules},
                               _obj({r["id"]: _VERDICT for r in ask}), max_tokens=3000)
    except Exception as e:
        err = f"the coherence check's model call failed ({getattr(e, 'kind', type(e).__name__)})"
        for r in ask:
            r["note"] = err
        return out, False, err
    for r in ask:
        v = res.get(r["id"]) or {}
        r["status"] = PASS if v.get("verdict") == "pass" else FAIL
        r["note"] = clip(v.get("reason"), REASON_MAX) or r["status"]
        if r["status"] == FAIL and (v.get("fix") or "").strip():
            r["fix"] = clip(v["fix"], FIX_MAX)
    return out, True, None


def definition_of_done(field_results: list[dict], coh: list[dict], values: dict, evaluation_present: bool,
                       high_questions: int) -> list[dict]:
    """golden_critic `_run_dod`, with the model's verdicts where it gave them."""
    by = {fr["rubric"]: fr for fr in field_results}
    coh_by = {c["id"]: c for c in coh}

    def chk(rid, cid):
        for c in (by.get(rid) or {}).get("checks", []):
            if c["check"] == cid:
                return c["status"]
        return None

    required = all(filled(rubric_value(f["id"], values)) for f in RUBRIC_FIELDS.values() if f.get("required"))
    within = all(c["status"] != FAIL for fr in field_results for c in fr["checks"]
                 if c["check"] in ("within_limit", "max_items"))
    smp_single = REVIEW
    if chk("smp", "single_sentence") == PASS and chk("smp", "within_limit") == PASS:
        smp_single = PASS
    elif chk("smp", "single_sentence") == FAIL:
        smp_single = FAIL
    rtb = coh_by.get("rtb_supports_smp", {}).get("status") or (
        REVIEW if filled(values.get("reasons_to_believe")) else FAIL)
    m = {"all_required_filled": PASS if required else FAIL, "objectives_linked": chk("objectives", "three_levels") or REVIEW,
         "smp_single": smp_single, "rtb_supports_smp": rtb, "evaluation_present": PASS if evaluation_present else FAIL,
         "open_questions_clear": FAIL if high_questions else PASS, "within_limits": PASS if within else FAIL}
    for cid in DOD_LLM:
        if cid in coh_by:
            m[cid] = coh_by[cid]["status"]
    return [{"id": d["id"], "status": m.get(d["id"], REVIEW)} for d in DOD]
