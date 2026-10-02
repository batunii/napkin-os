"""The check loop (the owner, 2026-10-01): whoever wrote the brief, jev finds the parts that
need rewriting, Jude says why, Dara rewrites those parts.

  1. jev, one yes/no per written part: does it need rewriting before a creative team sees it?
     One more for the whole brief: does objective -> insight -> proposition -> proof hold?
     And the seven BetterBriefs dimensions as choices, for the review and a before/after score.
     Fast and cheap; its 0.5 line is provisional (never checked on Napkin data), so every
     probability is logged and every time Jude disagrees with a flag is logged too.
  2. Jude (a model call) on the flagged and the empty parts only: does it really need work, why,
     and one instruction for Dara. Something only the client can supply is not rewritten.
  3. Dara rewrites those parts in one call, the whole brief in view.
  4. jev again on what changed, for the log: did the rewrite help?

jev not configured or not answering: the loop says so in the log and the brief stands as written.
"""
from __future__ import annotations

from ..jev import JevError, choice, noul
from .fields import KEYS, LABELS, filled
from .planned import DIMS, DRAFT_SYSTEM, WHAT, _short, draft_schema

FLAG_AT = 0.5

PART_Q = "Does the part named in PART need rewriting before this brief goes to a creative team?"
PART_T = ("Yes: it is vague or generic, restates the request instead of answering it, contradicts another part, "
          "or claims something the brief and the client's words do not support.")
PART_F = ("No: it is specific, does its job for this brief, fits the other parts and claims nothing unsupported.")
CHAIN_Q = ("Do the objectives, the audience, the insight, the single-minded proposition and the reasons to believe "
           "make one argument, each following from the one before?")
DIM_Q = {
    "objectives_quality": "a handful of objectives, benchmarked and time-stamped, the commercial, behavioural and "
                          "attitudinal ones linked",
    "audience_vividness": "a vivid picture of the audience (who, habits, needs), not a bare age range, and who it is "
                          "not for",
    "single_minded_message": "one key message, with relevant proof",
    "evaluation_criteria": "how the work will be judged",
    "budget_interlock": "budget, objectives and audience feasible together",
    "strategic_clarity": "a clear strategic choice, including what not to do",
    "language": "simple, succinct language with no jargon",
}

JUDE_SYSTEM = """You are Jude, the agency's brief judge. A fast checker (jev) read the creative brief and flagged the
parts it thinks need rewriting, each with its probability; some parts are empty. For each part listed:
- rework: true when a rewrite from what the brief, the client's words and the facts hold would make it
  better; false when the flag is wrong, or when only the client can supply it (a budget figure, success
  measures, mandatories they hold); then it is a question for the client, not a rewrite.
- why: one sentence, specific to this part (never a stock phrase).
- instruction: one clear instruction for the writer (empty when rework is false).
If the checker doubted that objective -> insight -> proposition -> proof holds, say in `chain` what breaks and
which parts to change; otherwise chain.issue is null.
For each BetterBriefs dimension the checker reads as missing, say in `dimensions` which parts would supply it
and give one instruction (e.g. evaluation criteria: add to the desired response how the work will be judged);
when only the client can supply it, put it in `questions` instead and leave its parts empty. Plain British
English."""


def _obj(props):
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


def jude_schema(keys):
    s = {"type": "string"}
    return _obj({"parts": _obj({k: _obj({"rework": {"type": "boolean"}, "why": s, "instruction": s}) for k in keys}),
                 "dimensions": {"type": "array", "items": _obj({
                     "dimension": {"type": "string", "enum": DIMS}, "instruction": s,
                     "parts": {"type": "array", "items": {"type": "string", "enum": list(KEYS)}}})},
                 "chain": _obj({"issue": {"anyOf": [s, {"type": "null"}]},
                                "parts": {"type": "array", "items": {"type": "string", "enum": list(KEYS)}}}),
                 "questions": {"type": "array", "items": s}})


def text_of(v) -> str:
    return "; ".join(map(str, v)) if isinstance(v, list) else str(v or "")


def render(values: dict) -> str:
    return "\n".join(f"{LABELS.get(k, k)}: {text_of(values.get(k)) if filled(values.get(k)) else '(empty)'}"
                     for k in KEYS)


def jev_read(job, values: dict, said: str, parts: list, what: str) -> dict | None:
    """jev's answers: {"p": {part: p(needs rewriting)}, "chain": p(holds), "dims": {dim: choice}}."""
    if not job.caps.jev.configured:
        job.caps.runlog.note("jev_off", detail="no jev key is configured: the brief is not checked")
        return None
    qs = {f"part_{i:02d}": noul(PART_Q, PART_T, PART_F, part=LABELS.get(k, k), text=text_of(values[k])[:1500])
          for i, k in enumerate(parts)}
    qs["chain"] = noul(CHAIN_Q, "Yes: they make one argument.", "No: a link is missing or they pull apart.")
    for d in DIMS:
        qs[f"dim_{d}"] = choice(f"How well does the creative brief in the state meet this standard: {DIM_Q[d]}?",
                                {"pass": "It clearly meets it.", "vague": "It touches it, loosely or in part.",
                                 "missing": "It does not address it."})
    try:
        got = job.caps.jev.ask({"creative_brief": render(values)[:50_000], "client_said": said[:8000]}, qs, what)
    except JevError as e:
        job.caps.runlog.note("jev_failed", check=what, detail=str(e)[:200])
        return None
    p = {k: got[f"part_{i:02d}"] for i, k in enumerate(parts)}
    dims = {d: got[f"dim_{d}"][0] for d in DIMS}
    score = sum({"pass": 2, "vague": 1}.get(v, 0) for v in dims.values())
    job.caps.runlog.note(what, flags={k: round(x, 3) for k, x in p.items()}, chain=round(got["chain"], 3), dims=dims,
                         jev_score=score)
    return {"p": p, "chain": got["chain"], "dims": dims, "score": score}


def run(job, values: dict, said: str, open_keys: list, evidence: dict, library: list, plan=None) -> dict:
    """The loop over the brief as it stands. Returns {"before", "after", "jude", "rewritten", "todo"}:
    rewritten = {key: {value, basis, cites, builds_on_client, why}} from Dara, `why` being Jude's."""
    rl = job.caps.runlog
    written = [k for k in open_keys if filled(values.get(k)) and k != "open_questions"]
    empty = [k for k in open_keys if not filled(values.get(k)) and k != "open_questions"]
    before = jev_read(job, values, said, written, "jev_before")
    out = {"before": before, "after": None, "jude": {}, "rewritten": {}, "todo": {}, "questions": []}
    if before is None and not empty:
        return out
    flagged = [k for k in written if before and before["p"][k] >= FLAG_AT]
    chain_doubt = bool(before and before["chain"] < FLAG_AT)
    missing = [d for d in DIMS if before and before["dims"].get(d) == "missing"]
    ask = flagged + empty
    if not ask and not chain_doubt and not missing:
        rl.note("check_clean", detail="jev flagged nothing and every part is written")
        return out
    payload = {"brief": {k: values.get(k) for k in KEYS if filled(values.get(k))}, "client_said": said[:8000],
               "flagged": {k: round(before["p"][k], 3) for k in flagged} if before else {},
               "empty": empty, "chain_holds": round(before["chain"], 3) if before else None,
               "missing_dimensions": {d: DIM_Q[d] for d in missing},
               "facts": evidence.get("facts") or [], "findings": evidence.get("findings") or []}
    jude = job.caps.model.structured("explain_flags", JUDE_SYSTEM, payload, jude_schema(ask),
                                     max_tokens=6000)
    out["jude"] = jude
    todo = {k: v["instruction"] for k, v in (jude.get("parts") or {}).items()
            if k in ask and v.get("rework") and str(v.get("instruction") or "").strip()}
    chain = jude.get("chain") or {}
    if chain.get("issue"):
        for k in chain.get("parts") or []:
            if k in open_keys and k != "open_questions":
                todo.setdefault(k, f"Make it follow the brief's one argument: {chain['issue']}")
    for dfix in jude.get("dimensions") or []:
        ins = str(dfix.get("instruction") or "").strip()
        for k in dfix.get("parts") or []:
            if ins and k in open_keys and k != "open_questions":
                todo[k] = (todo[k] + " Also: " + ins) if k in todo else ins
    out["questions"] = list(jude.get("questions") or [])
    disagreed = [k for k in flagged if not (jude.get("parts") or {}).get(k, {}).get("rework")]
    rl.note("jude_on_flags", flagged=flagged, empty=empty, rework=sorted(todo), disagreed_with_jev=disagreed,
            chain_issue=bool(chain.get("issue")), missing_dimensions=missing,
            dimension_fixes=[{"dimension": x.get("dimension"), "parts": x.get("parts")} for x in jude.get("dimensions") or []])
    out["todo"] = todo
    if not todo:
        return out
    pay = {"client_said": said[:8000], "plan": plan, "brief": {k: values.get(k) for k in KEYS if filled(values.get(k))},
           "rewrite": todo, "facts": evidence.get("facts") or [], "findings": evidence.get("findings") or [],
           "library": library, "parts": {k: WHAT.get(k, LABELS.get(k, k)) for k in todo}}
    got = job.caps.model.structured("rewrite_brief_parts", DRAFT_SYSTEM + "\n\nRewrite ONLY the parts in `rewrite`, "
                                    "following each instruction, keeping the rest of the brief in mind.", pay,
                                    draft_schema(list(todo)), max_tokens=8000)
    ids = {f["id"] for f in evidence.get("facts") or []} | {x["id"] for x in evidence.get("findings") or []} | \
        {x["id"] for x in library}
    from .fields import clean
    for k, r in (got or {}).items():
        if k not in todo or not isinstance(r, dict):
            continue
        v = clean(k, r.get("value"))
        if v is None:
            continue
        cites = [c for c in dict.fromkeys(r.get("cites") or []) if c in ids]
        basis = r.get("basis") if r.get("basis") in ("client", "research", "library", "assumption") else "assumption"
        if basis in ("research", "library") and not cites:
            basis = "assumption"
        why = ((jude.get("parts") or {}).get(k) or {}).get("why") or chain.get("issue") or ""
        out["rewritten"][k] = {"value": v, "basis": basis, "cites": cites, "builds_on_client": bool(r.get("builds_on_client")),
                               "why": _short(f"Rewritten on Jude's note: {why}", 300), "revised": True,
                               "downgraded": False}
    after_vals = {**values, **{k: v["value"] for k, v in out["rewritten"].items()}}
    out["after"] = jev_read(job, after_vals, said, [k for k in open_keys if filled(after_vals.get(k))
                                                     and k != "open_questions"], "jev_after")
    if before and out["after"]:
        rl.note("check_result", score_before=before["score"], score_after=out["after"]["score"],
                rewritten=sorted(out["rewritten"]),
                flags_after={k: round(out["after"]["p"].get(k, 0), 3) for k in out["rewritten"]})
    return out
