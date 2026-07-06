#!/usr/bin/env python3
"""
golden_critic.py — Python port of golden-brief.js validate() + the bridge from
our BriefObject (Loops 1-2, parse_brief.py) to the Golden Brief schema.

Three jobs:
  1. from_brief_object()  — map brief_object.json -> golden brief shape
                            (fact/assumption/gap -> client_stated/inferred/missing)
  2. validate()           — run the schema's auto checks, dependency checks,
                            definition-of-done and health score (port of golden-brief.js)
  3. critic_prompts()     — emit the LLM-judge prompts for every "llm" rubric check,
                            carrying the schema's contrastive good/bad examples,
                            ready for the Nemotron NIM path in parse_brief.py

Stdlib only. Schema source of truth: golden-brief/golden_brief.schema.json.

Usage:
  python3 golden_critic.py outputs/client_briefs_v4/<name>/brief_object.json
  python3 golden_critic.py --all outputs/client_briefs_v4        # scoreboard
  python3 golden_critic.py <path> --prompts                      # show critic prompts
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

SCHEMA_PATH = Path(__file__).parent / "golden-brief" / "golden_brief.schema.json"

PASS, FAIL, REVIEW = "pass", "fail", "review"


def _nim_call(prompt_text: str, max_tokens: int = 400) -> "dict | None":
    """Single NIM call for a critic judgment. Returns parsed JSON dict or None."""
    key = os.environ.get("NVIDIA_API_KEY")
    model = os.environ.get("BRIEF_MODEL", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning")
    base = os.environ.get("BRIEF_BASE_URL", "https://integrate.api.nvidia.com/v1")
    if not key:
        return None
    body = json.dumps({
        "model": model, "temperature": 0.1, "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt_text}],
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode()
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions", data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            content = json.loads(r.read())["choices"][0]["message"].get("content", "")
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
        content = re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.MULTILINE).strip()
        start = content.find("{")
        if start != -1:
            content = content[start:]
        return json.loads(content)
    except Exception:
        return None


# ----------------------------------------------------------------- utilities
def _words(s) -> list[str]:
    return str(s or "").strip().split()


def _wc(s) -> int:
    return len(_words(s))


def _sentences(s) -> list[str]:
    return [x for x in re.split(r"[.!?]+(?:\s|$)", str(s or "").strip()) if x.strip()]


def _list_items(v) -> list[str]:
    if isinstance(v, list):
        return [x for x in v if x]
    return [x.strip() for x in re.split(r"[·;\n]|,(?![^()]*\))", str(v or "")) if x.strip()]


def _is_filled(v) -> bool:
    if v is None:
        return False
    if isinstance(v, list):
        return bool([x for x in v if x])
    if isinstance(v, dict):
        return any(str(x or "").strip() for x in v.values())
    return bool(str(v).strip())


def _val(brief, fid):
    return (brief.get("fields", {}).get(fid) or {}).get("value")


def _entry(brief, fid):
    return brief.get("fields", {}).get(fid) or {}


# ------------------------------------------------- auto evaluators (1:1 port)
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
    return (REVIEW, "may carry >1 idea") if listy else (PASS, "one idea")


def _reveals_why(f, v):
    ok = re.search(r"\bbecause\b|\bso the job\b|\bwhich means\b", str(v or ""), re.I)
    return (PASS, "states a 'why'") if ok else (FAIL, "no motivation ('because…')")


def _shape_filled(f, v):
    o = v if isinstance(v, dict) else {}
    shape = f.get("shape", [])
    have = [k for k in shape if _is_filled(o.get(k))]
    n, total = len(have), len(shape)
    return (PASS, f"{n}/{total}") if n == total else (FAIL, f"{n}/{total}")


def _has_constraint(f, v):
    ok = _is_filled(v) and re.search(
        r"\d|budget|media|€|\$|£|prioritise|scope|cities|national", str(v or ""), re.I)
    return (PASS, "constraint set") if ok else (FAIL, "no constraint")


def _has_deliverables(f, v):
    ok = re.search(r"\d|×|x\d|s\b|OOH|social|TV|print|radio|deliver|live|cutdown",
                   str(v or ""), re.I)
    return (PASS, "deliverables listed") if ok else (REVIEW, "check deliverables")


def _names_rivals(f, v):
    return (PASS, "category read present") if _is_filled(v) and _wc(v) > 4 else (FAIL, "too thin")


AUTO = {
    "within_limit": _within_limit,
    "max_items": _max_items,
    "single_sentence": _single_sentence,
    "single_minded": _single_minded,
    "reveals_why": _reveals_why,
    "three_levels": _shape_filled,
    "all_three": _shape_filled,
    "has_constraint": _has_constraint,
    "has_deliverables": _has_deliverables,
    "names_rivals": _names_rivals,
}


# ----------------------------------------------------------------- validation
def _field_required(field, brief) -> bool:
    if field.get("required"):
        return True
    unless = field.get("required_unless_brief_type")
    if unless:
        return (brief.get("meta", {}).get("brief_type")) not in unless
    return False


def _run_field_checks(field, brief):
    v = _val(brief, field["id"])
    filled = _is_filled(v)
    checks = []
    for c in field.get("rubric", []):
        base = {"id": c["id"], "test": c["test"], "method": c["method"]}
        if not filled:
            checks.append({**base, "status": REVIEW, "note": "empty"})
        elif c["method"] == "auto" and c["id"] in AUTO:
            status, note = AUTO[c["id"]](field, v)
            checks.append({**base, "status": status, "note": note})
        elif c["id"] == "ownable":
            if not _is_filled(_val(brief, "competitor_context")):
                checks.append({**base, "status": FAIL, "note": "needs competitor_context"})
            else:
                checks.append({**base, "status": REVIEW, "note": "agent to judge"})
        else:
            note = "agent to judge" if c["method"] == "llm" else "human to confirm"
            checks.append({**base, "status": REVIEW, "note": note})
    return {"id": field["id"], "label": field["label"], "filled": filled,
            "hero": bool(field.get("hero")), "checks": checks}


def _derive_open_questions(schema, brief):
    floor = schema.get("confidence_floor", 0.6)
    out = []
    for f in schema["fields"]:
        e = _entry(brief, f["id"])
        if _field_required(f, brief) and not _is_filled(e.get("value")):
            out.append({"question": f"Missing: {f['label']}", "blocks_field": f["id"],
                        "severity": "high"})
        elif e.get("source") == "missing":
            out.append({"question": f"Confirm with client: {f['label']}",
                        "blocks_field": f["id"], "severity": "high"})
        elif isinstance(e.get("confidence"), (int, float)) and e["confidence"] < floor \
                and _is_filled(e.get("value")):
            out.append({"question": f"Low confidence on {f['label']} — confirm.",
                        "blocks_field": f["id"], "severity": "medium"})
    out.extend(brief.get("open_questions") or [])
    return out


def _run_dependencies(schema, brief):
    results = []
    for d in schema["dependencies"]:
        all_filled = all(_is_filled(_val(brief, fid)) for fid in d["fields"])
        status, note = REVIEW, ("" if d["method"] == "auto" else "agent to judge")
        if d["id"] == "rtb_supports_smp":
            status = REVIEW if all_filled else FAIL
            note = "RTBs present — agent to confirm support" if all_filled else "missing SMP or RTBs"
        elif d["id"] == "response_ladders":
            status = REVIEW if all_filled else FAIL
            note = "agent to confirm ladder" if all_filled else "missing response/objectives"
        elif d["id"] == "ownable_needs_competitors":
            ok = _is_filled(_val(brief, "competitor_context"))
            status, note = (PASS, "competitor context present") if ok else (FAIL, "fill competitor_context")
        results.append({"id": d["id"], "rule": d["rule"], "method": d["method"],
                        "status": status, "note": note})
    return results


def _run_dod(schema, brief, field_results, open_questions):
    by_id = {fr["id"]: fr for fr in field_results}

    def chk(fid, cid):
        fr = by_id.get(fid)
        if not fr:
            return None
        for c in fr["checks"]:
            if c["id"] == cid:
                return c["status"]
        return None

    required_filled = all(
        _is_filled(_val(brief, f["id"]))
        for f in schema["fields"] if _field_required(f, brief))
    within_limits = all(
        c["status"] != FAIL
        for fr in field_results for c in fr["checks"]
        if c["id"] in ("within_limit", "max_items"))
    high_open = any(q.get("severity") == "high" for q in open_questions)
    eval_present = bool(str((brief.get("gate") or {}).get("evaluation_criteria") or "").strip())

    smp_single = REVIEW
    if chk("smp", "single_sentence") == PASS and chk("smp", "within_limit") == PASS:
        smp_single = PASS
    elif chk("smp", "single_sentence") == FAIL:
        smp_single = FAIL

    mapping = {
        "all_required_filled": PASS if required_filled else FAIL,
        "objectives_linked": chk("objectives", "three_levels") or REVIEW,
        "smp_single": smp_single,
        "rtb_supports_smp": REVIEW if _is_filled(_val(brief, "reasons_to_believe")) else FAIL,
        "evaluation_present": PASS if eval_present else FAIL,
        "open_questions_clear": FAIL if high_open else PASS,
        "within_limits": PASS if within_limits else FAIL,
    }
    return [{"id": d["id"], "rule": d["rule"], "method": d["method"],
             "status": mapping.get(d["id"], REVIEW)}
            for d in schema["gate"]["definition_of_done"]]


def _health(field_results, dod, open_questions) -> int:
    total = score = 0.0
    for fr in field_results:
        w = 2 if fr["hero"] else 1
        for c in fr["checks"]:
            total += w
            score += w if c["status"] == PASS else (w * 0.5 if c["status"] == REVIEW else 0)
    base = score / total if total else 0
    penalty = (sum(1 for d in dod if d["status"] == FAIL) * 0.04
               + sum(1 for q in open_questions if q.get("severity") == "high") * 0.03)
    return max(0, min(100, round((base - penalty) * 100)))


def validate(schema, brief):
    field_results = [_run_field_checks(f, brief) for f in schema["fields"]]
    open_questions = _derive_open_questions(schema, brief)
    dependencies = _run_dependencies(schema, brief)
    dod = _run_dod(schema, brief, field_results, open_questions)
    return {"fields": field_results, "dependencies": dependencies,
            "definition_of_done": dod, "open_questions": open_questions,
            "health": _health(field_results, dod, open_questions)}


# ------------------------------------------- BriefObject -> Golden Brief map
# status (ours) -> provenance (golden)
_PROV = {"fact": "client_stated", "assumption": "inferred", "gap": "missing"}


def _cap(node):
    """Collapse a Captured (or list of Captured) into (value, source, confidence)."""
    if node is None:
        return None, "missing", None
    if isinstance(node, list):
        vals = [n for n in node if isinstance(n, dict) and _is_filled(n.get("value"))]
        if not vals:
            return None, "missing", None
        value = [str(n["value"]) for n in vals]
        worst = "client_stated"
        for n in vals:
            p = _PROV.get(str(n.get("status")), "inferred")
            if p == "missing" or (p == "inferred" and worst == "client_stated"):
                worst = p
        confs = [n["confidence"] for n in vals if isinstance(n.get("confidence"), (int, float))]
        return value, worst, (min(confs) if confs else None)
    if isinstance(node, dict):
        v = node.get("value")
        if not _is_filled(v):
            return None, "missing", None
        return v, _PROV.get(str(node.get("status")), "inferred"), node.get("confidence")
    return node, "inferred", None


def _join(*parts):
    vals = [str(p) for p in parts if _is_filled(p)]
    return " — ".join(vals) if vals else None


def from_brief_object(bo: dict) -> dict:
    """Map a parse_brief.py BriefObject onto the Golden Brief shape.
    Zone 3 fields (insight, desired_response) are pulled from loop2_golden
    when present (filled by Loop 4/5), otherwise marked missing.
    """
    l1 = bo.get("loop1_capture", {}).get("fields", {})
    l2 = bo.get("loop2_brief", {})
    # loop2_golden carries schema-grounded extractions + Loop 4/5 fills
    lg = (bo.get("loop2_golden") or {}).get("fields", {})
    fields: dict = {}

    def put(fid, node, fallback=None):
        v, src, conf = _cap(node)
        if v is None and fallback is not None:
            v, src, conf = _cap(fallback)
        entry = {"value": v, "source": src}
        if conf is not None:
            entry["confidence"] = conf
        fields[fid] = entry

    # Zone 1 — why we're here
    put("background", l2.get("problem"), l1.get("background_context") or l1.get("business_problem"))
    put("competitor_context", l1.get("competitors_market"))

    # objectives: ours is one captured value; golden wants the 3-level shape.
    obj_v, obj_src, obj_conf = _cap(l2.get("objective"))
    obj_type = (l2.get("objective") or {}).get("objective_type") if isinstance(l2.get("objective"), dict) else None
    shaped = {"commercial": "", "behavioural": "", "attitudinal": ""}
    if obj_v is not None:
        shaped[obj_type or "commercial"] = obj_v if isinstance(obj_v, str) else "; ".join(obj_v)
    entry = {"value": shaped, "source": obj_src}
    if obj_conf is not None:
        entry["confidence"] = obj_conf
    fields["objectives"] = entry

    # Zone 2 — who & what we have
    put("audience", l2.get("audience"), l1.get("target_audience"))
    bud_v, bud_src, bud_conf = _cap(l1.get("budget"))
    scope_v, _, _ = _cap(l2.get("scope"))
    fields["budget_scope"] = {"value": _join(bud_v, None if bud_v else scope_v),
                              "source": bud_src if bud_v else "missing"}
    if bud_conf is not None:
        fields["budget_scope"]["confidence"] = bud_conf

    # Zone 3 — the spark. SMP <- key_message; RTB <- proof_points; the rest
    # has no producing loop yet (Loops 3-5) and stays missing on purpose.
    put("smp", l2.get("key_message"), l1.get("key_message"))
    put("reasons_to_believe", l1.get("proof_points"))
    _ins = lg.get("insight", {})
    if _ins and _is_filled(_ins.get("value")):
        fields["insight"] = _ins
    else:
        fields["insight"] = {"value": None, "source": "missing"}
    _dr = lg.get("desired_response", {})
    if _dr and _is_filled(_dr.get("value")):
        fields["desired_response"] = _dr
    else:
        fields["desired_response"] = {"value": {}, "source": "missing"}
    put("tone_world_assets", l1.get("tone_and_brand"))

    # Zone 4 — guardrails
    mand_v, mand_src, mand_conf = _cap(l1.get("mandatories"))
    deliv_v, _, _ = _cap(l1.get("deliverables"))
    time_v, _, _ = _cap(l1.get("timeline"))
    joined = _join(mand_v if isinstance(mand_v, str) else (" · ".join(mand_v) if mand_v else None),
                   deliv_v if isinstance(deliv_v, str) else (" · ".join(deliv_v) if deliv_v else None),
                   time_v if isinstance(time_v, str) else (" · ".join(time_v) if time_v else None))
    fields["mandatories"] = {"value": joined, "source": mand_src if mand_v else "inferred"}
    if mand_conf is not None:
        fields["mandatories"]["confidence"] = mand_conf

    eval_v, _, _ = _cap(l2.get("evaluation_criteria"))
    meta = bo.get("meta", {})
    brief = {
        "meta": {"brand": meta.get("client"), "project": meta.get("project"),
                 "status": "draft", "brief_type": "launch"},
        "gate": {"evaluation_criteria": eval_v or ""},
        "fields": fields,
        # carry our Loop-2 open questions through
        "open_questions": [
            {"question": q.get("question", ""), "blocks_field": "",
             "severity": {"blocker": "high", "important": "medium"}.get(q.get("priority"), "low")}
            for q in l2.get("open_questions", [])
        ],
    }
    return brief


# --------------------------------------------------------- critic prompts
CRITIC_TEMPLATE = """You are a strategy director reviewing one field of a creative brief.

FIELD: {label}
CHECK: {check_id} — {test}

THE FIELD'S BRIEF (what good looks like): {prompt}

GOOD EXAMPLE: {good}
BAD EXAMPLE: {bad}
WHY THE BAD ONE FAILS: {bad_reason}

THE CANDIDATE VALUE TO JUDGE:
{value}
{context}
Answer in strict JSON: {{"check": "{check_id}", "verdict": "pass" | "fail", "reason": "<one sentence>", "fix": "<one concrete rewrite suggestion, or null if pass>"}}"""


def critic_prompts(schema, brief, validation):
    """One prompt per unresolved llm-check on a filled field. Feed to Nemotron/Claude;
    write verdicts back over the matching check, then re-run health."""
    by_id = {f["id"]: f for f in schema["fields"]}
    prompts = []
    for fr in validation["fields"]:
        if not fr["filled"]:
            continue
        field = by_id[fr["id"]]
        v = _val(brief, fr["id"])
        for c in fr["checks"]:
            if c["status"] != REVIEW or c["method"] != "llm":
                continue
            context = ""
            deps = field.get("depends_on") or []
            if deps:
                lines = [f"  {d}: {json.dumps(_val(brief, d), ensure_ascii=False)}" for d in deps]
                context = "\nCONTEXT (fields this one must stay consistent with):\n" + "\n".join(lines) + "\n"
            prompts.append({
                "field": fr["id"], "check": c["id"],
                "prompt": CRITIC_TEMPLATE.format(
                    label=field["label"], check_id=c["id"], test=c["test"],
                    prompt=field["prompt"], good=field["good_example"],
                    bad=field["bad_example"], bad_reason=field["bad_reason"],
                    value=json.dumps(v, ensure_ascii=False, indent=2), context=context),
            })
    return prompts


def critic_prompts_batched(schema, brief, validation):
    """One prompt per FIELD carrying ALL its unresolved llm-checks. The field header,
    value and dependency context are sent once instead of once per check — 20–30
    calls collapse to one-per-field (~5–10) at a fraction of the tokens."""
    by_id = {f["id"]: f for f in schema["fields"]}
    out = []
    for fr in validation["fields"]:
        if not fr["filled"]:
            continue
        field = by_id[fr["id"]]
        v = _val(brief, fr["id"])
        checks = [c for c in fr["checks"] if c["status"] == REVIEW and c["method"] == "llm"]
        if not checks:
            continue
        context = ""
        deps = field.get("depends_on") or []
        if deps:
            lines = [f"  {d}: {json.dumps(_val(brief, d), ensure_ascii=False)}" for d in deps]
            context = "\nCONTEXT (fields this one must stay consistent with):\n" + "\n".join(lines) + "\n"
        tests = "\n".join(f'- {c["id"]}: {c["test"]}' for c in checks)
        out.append({
            "field": fr["id"], "checks": [c["id"] for c in checks],
            "prompt": (
                f"You are a rigorous brief-quality critic judging the '{field['label']}' field.\n"
                f"WHAT THE FIELD IS: {field['prompt']}\n"
                f"GOOD EXAMPLE (a different brand — shape only): {field['good_example']}\n"
                f"BAD EXAMPLE: {field['bad_example']}  ({field['bad_reason']})\n"
                f"{context}"
                f"FIELD VALUE:\n{json.dumps(v, ensure_ascii=False, indent=2)}\n\n"
                f"Judge the VALUE on EACH test independently:\n{tests}\n\n"
                'Return ONLY raw JSON: {"<test_id>": {"verdict": "pass"|"fail", '
                '"reason": "one line", "fix": "one line, only when fail"}}'
            ),
        })
    return out


def run_critic(schema, brief, validation):
    """Fire all pending llm checks against NIM — batched ONE call per field (all of a
    field's checks judged together). Update verdicts in-place, recompute health.
    Returns (updated_validation, n_ran)."""
    batches = critic_prompts_batched(schema, brief, validation)
    if not batches:
        return validation, 0

    # build lookup so we can write results back into the live validation dict
    check_index = {}
    for fr in validation["fields"]:
        for c in fr["checks"]:
            check_index[(fr["id"], c["id"])] = c

    ran = 0
    for b in batches:
        result = _nim_call(b["prompt"], max_tokens=min(150 * len(b["checks"]) + 150, 1200))
        if not isinstance(result, dict):
            continue
        for cid in b["checks"]:
            res = result.get(cid)
            c = check_index.get((b["field"], cid))
            if not c or not isinstance(res, dict):
                continue
            verdict = res.get("verdict", "")
            if verdict in (PASS, FAIL):
                c["status"] = verdict
                c["note"] = str(res.get("reason", ""))[:200]
                if res.get("fix"):
                    c["fix"] = str(res["fix"])[:300]
                ran += 1

    # recompute DoD and health with updated verdicts
    validation["definition_of_done"] = _run_dod(
        schema, brief, validation["fields"], validation["open_questions"])
    validation["health"] = _health(
        validation["fields"], validation["definition_of_done"], validation["open_questions"])
    return validation, ran


# ------------------------------------------------------------------ report
def _fmt_status(s):
    return {"pass": "PASS", "fail": "FAIL", "review": "····"}[s]


def report(name, validation):
    lines = [f"\n=== {name} — Golden Brief health: {validation['health']}/100 ==="]
    for fr in validation["fields"]:
        flag = "HERO " if fr["hero"] else "     "
        filled = "filled" if fr["filled"] else "EMPTY "
        checks = "  ".join(f"{c['id']}:{_fmt_status(c['status'])}" for c in fr["checks"])
        lines.append(f"  {flag}{fr['id']:<22}{filled}  {checks}")
    fails = [d for d in validation["definition_of_done"] if d["status"] == FAIL]
    lines.append(f"  gate: {len(fails)} definition-of-done FAILs"
                 + (": " + ", ".join(d["id"] for d in fails) if fails else ""))
    highs = [q for q in validation["open_questions"] if q.get("severity") == "high"]
    lines.append(f"  open questions: {len(validation['open_questions'])} ({len(highs)} high)")
    n_llm = sum(1 for fr in validation["fields"] for c in fr["checks"]
                if c["status"] == REVIEW and c["method"] == "llm" and fr["filled"])
    lines.append(f"  awaiting critic agent: {n_llm} llm checks")
    return "\n".join(lines)


def main(argv):
    schema = json.loads(SCHEMA_PATH.read_text())
    show_prompts = "--prompts" in argv
    run_critic_flag = "--critic" in argv
    argv = [a for a in argv if a not in ("--prompts", "--critic")]

    if argv and argv[0] == "--all":
        root = Path(argv[1] if len(argv) > 1 else "outputs/client_briefs_v4")
        rows = []
        for bo_path in sorted(root.glob("*/brief_object.json")):
            bo = json.loads(bo_path.read_text())
            v = validate(schema, from_brief_object(bo))
            fails = sum(1 for d in v["definition_of_done"] if d["status"] == FAIL)
            highs = sum(1 for q in v["open_questions"] if q.get("severity") == "high")
            rows.append((bo_path.parent.name, v["health"], fails, highs))
        print(f"{'brief':<46}{'health':>7}{'DoD fails':>11}{'high OQs':>10}")
        for name, h, f, q in sorted(rows, key=lambda r: -r[1]):
            print(f"{name:<46}{h:>7}{f:>11}{q:>10}")
        return

    if not argv:
        print(__doc__)
        return
    bo_path = Path(argv[0])
    bo = json.loads(bo_path.read_text())
    brief = from_brief_object(bo)
    v = validate(schema, brief)
    print(report(bo_path.parent.name, v))
    if run_critic_flag:
        print(f"  running {sum(1 for fr in v['fields'] for c in fr['checks'] if c['status'] == 'review' and c['method'] == 'llm' and fr['filled'])} llm checks against NIM...")
        v, ran = run_critic(schema, brief, v)
        print(f"  critic ran {ran} checks")
        print(report(bo_path.parent.name, v))
    if show_prompts:
        ps = critic_prompts(schema, brief, v)
        print(f"\n--- {len(ps)} critic prompts ready (showing first) ---\n")
        if ps:
            print(ps[0]["prompt"])


if __name__ == "__main__":
    main(sys.argv[1:])
