"""The golden-brief rubric's deterministic half: the auto checks of
engine/golden_critic.py (a 1:1 port), the app <-> rubric value mapping, the
health score and the verdict reason codes (middleware-api.md §10.8).

Everything here is code. The model checks (method `llm`) are the Judge's
model calls (`judge.py`).
"""

from __future__ import annotations

import re

from .fields import RUBRIC, RUBRIC_FIELDS, filled

PASS, FAIL, REVIEW = "pass", "fail", "review"


def _words(s) -> list[str]:
    return str(s or "").strip().split()


def _wc(v) -> int:
    if isinstance(v, dict):
        return sum(_wc(x) for x in v.values())
    if isinstance(v, list):
        return sum(_wc(x) for x in v)
    return len(_words(v))


def _sentences(s) -> list[str]:
    return [x for x in re.split(r"[.!?]+(?:\s|$)", str(s or "").strip()) if x.strip()]


def _list_items(v) -> list[str]:
    if isinstance(v, list):
        return [x for x in v if x]
    return [x.strip() for x in re.split(r"[·;\n]|,(?![^()]*\))", str(v or "")) if x.strip()]


def _within_limit(f, v):
    n = _wc(v)
    if f.get("max_words") and n > f["max_words"]:
        return FAIL, f"{n}/{f['max_words']} words"
    if f.get("min_words") and n < f["min_words"]:
        return FAIL, f"too short ({n} words)"
    return PASS, f"{n}/{f['max_words']} words" if f.get("max_words") else f"{n} words"


def _max_items(f, v):
    n = len(_list_items(v))
    cap = f.get("max_items", 99)
    return (PASS, f"{n} items") if n <= cap else (FAIL, f"{n} > {cap}")


def _single_sentence(f, v):
    n = len(_sentences(v))
    return (PASS, "1 sentence") if n <= 1 else (FAIL, f"{n} sentences")


def _single_minded(f, v):
    s = re.sub(r",(?=\d{3}\b)", "", str(v or ""))
    listy = re.search(r"(,| and | & |·|;|/)", s)
    return (REVIEW, "may carry more than one idea") if listy else (PASS, "one idea")


def _reveals_why(f, v):
    ok = re.search(r"\bbecause\b|\bso the job\b|\bwhich means\b", str(v or ""), re.I)
    return (PASS, "states a 'why'") if ok else (FAIL, "no motivation ('because…')")


def _shape_filled(f, v):
    o = v if isinstance(v, dict) else {}
    shape = f.get("shape", [])
    have = [k for k in shape if filled(o.get(k))]
    return (PASS, f"{len(have)}/{len(shape)}") if len(have) == len(shape) else (FAIL, f"{len(have)}/{len(shape)}")


def _has_constraint(f, v):
    ok = filled(v) and re.search(r"\d|budget|media|€|\$|£|prioritise|scope|cities|national", str(v or ""), re.I)
    return (PASS, "constraint set") if ok else (FAIL, "no constraint")


def _has_deliverables(f, v):
    ok = re.search(r"\d|×|x\d|s\b|OOH|social|TV|print|radio|deliver|live|cutdown", str(v or ""), re.I)
    return (PASS, "deliverables listed") if ok else (REVIEW, "check deliverables")


def _names_rivals(f, v):
    return (PASS, "category read present") if filled(v) and _wc(v) > 4 else (FAIL, "too thin")


AUTO = {
    "within_limit": _within_limit, "max_items": _max_items, "single_sentence": _single_sentence,
    "single_minded": _single_minded, "reveals_why": _reveals_why, "three_levels": _shape_filled,
    "all_three": _shape_filled, "has_constraint": _has_constraint, "has_deliverables": _has_deliverables,
    "names_rivals": _names_rivals,
}


def rubric_value(rid: str, values: dict):
    """The rubric field's value from the app's keys (`values`: app key -> value)."""
    if rid == "objectives":
        return {k: values.get(f"objectives.{k}") or "" for k in ("commercial", "behavioural", "attitudinal")}
    if rid == "desired_response":
        return {k: values.get(f"desired_response.{k}") or "" for k in ("think", "feel", "do")}
    app = {"background": "background", "audience": "audience", "budget_scope": "budget_and_scope",
           "competitor_context": "competitor_context", "insight": "insight", "smp": "single_minded_proposition",
           "reasons_to_believe": "reasons_to_believe", "tone_world_assets": "tone_and_world",
           "mandatories": "mandatories"}[rid]
    v = values.get(app)
    if isinstance(v, list) and RUBRIC_FIELDS[rid].get("type") != "list":
        return " · ".join(v)
    return v


def auto_checks(rid: str, value, competitor_context=None) -> list[dict]:
    """Every rubric check of the field: auto ones decided here, `llm` ones left
    at `review` for the Judge's model call (golden_critic `_run_field_checks`)."""
    f = RUBRIC_FIELDS[rid]
    out = []
    for c in f.get("rubric", []):
        base = {"check": c["id"], "method": c["method"]}
        if not filled(value):
            out.append({**base, "status": REVIEW, "note": "empty"})
        elif c["method"] == "auto" and c["id"] in AUTO:
            status, note = AUTO[c["id"]](f, value)
            out.append({**base, "status": status, "note": note})
        elif c["id"] == "ownable" and not filled(competitor_context):
            out.append({**base, "status": FAIL, "note": "needs competitor context to judge",
                        "fix": "Fill the competitor context so the proposition can be judged ownable."})
        else:
            out.append({**base, "status": REVIEW, "note": "the judge to decide" if c["method"] == "llm"
                        else "a person to confirm"})
    return out


def auto_gate(rid: str, value) -> tuple[bool, list[str]]:
    """A drafter's gate on a candidate: the auto checks only."""
    fails = [f"{c['check']}: {c['note']}" for c in auto_checks(rid, value, competitor_context="-")
             if c["method"] == "auto" and c["status"] == FAIL]
    return not fails, fails


# §10.8.5 — pending the creative director's redline (Contract 5 O11)
REASON_CODES = {
    "within_limit": "not_single_minded", "single_sentence": "not_single_minded", "single_minded": "not_single_minded",
    "one_strategy": "not_single_minded", "max_items": "not_single_minded",
    "ownable": "cliche", "not_a_tagline": "cliche",
    "smp_derivation": "off_strategy", "rtb_supports_smp": "off_strategy", "supports_smp": "off_strategy",
    "response_ladders": "off_strategy", "derives_from": "off_strategy", "linked": "off_strategy",
    "backbone_balance": "off_strategy", "has_constraint": "unfeasible",
}


def reason_code(check: str) -> str:
    return REASON_CODES.get(check, "other")


def health(field_results: list[dict], dod: list[dict], high_questions: int) -> int:
    """golden_critic `_health`: weighted check score (hero fields x2), less
    penalties for failed definition-of-done items and high-severity questions."""
    total = score = 0.0
    for fr in field_results:
        w = 2 if RUBRIC_FIELDS[fr["rubric"]].get("hero") else 1
        for c in fr["checks"]:
            total += w
            score += w if c["status"] == PASS else (w * 0.5 if c["status"] == REVIEW else 0)
    base = score / total if total else 0
    penalty = sum(1 for d in dod if d["status"] == FAIL) * 0.04 + high_questions * 0.03
    return max(0, min(100, round((base - penalty) * 100)))


DEPENDENCIES = {d["id"]: d for d in RUBRIC["dependencies"]}
DOD = RUBRIC["gate"]["definition_of_done"]
