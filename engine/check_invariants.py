#!/usr/bin/env python3
"""Deterministic brief invariants — the regression gate ("ruler") for the pipeline.

Usage:  python3 check_invariants.py outputs/<brief-name> [...]
Reads each brief_object.json and asserts the invariants the credibility fixes enforce.
Exit code 0 if every HARD check passes for every brief, 1 otherwise. ADVISORY checks
print but never fail the run.
"""
import json
import re
import sys
from pathlib import Path

import parse_brief as pb

HERO_LIMITS = {"smp": 20, "insight": 50}
FORBIDDEN_DO = ("engage with", "explore the", "interact with", "connect with the brand")


def _val(gf, fid):
    f = gf.get(fid) or {}
    return f.get("value") if isinstance(f, dict) else f


def check(brief_dir: Path):
    obj = json.loads((brief_dir / "brief_object.json").read_text())
    gf = (obj.get("loop2_golden") or {}).get("fields", {}) or {}
    oqs = (obj.get("loop2_brief") or {}).get("open_questions", []) or []
    hard, advisory = [], []

    # 1. No fill-and-flag: a field an open question says is unresolved must NOT be shown filled.
    for q in oqs:
        if isinstance(q, dict) and q.get("blocks_field"):
            fid = q["blocks_field"]
            if _val(gf, fid):
                hard.append(f"fill-and-flag: '{fid}' is filled but also flagged as an open question")

    # 2. SMP within its word limit (one ownable sentence).
    smp = _val(gf, "smp")
    if smp and len(str(smp).split()) > HERO_LIMITS["smp"] * 1.2:
        hard.append(f"SMP over word limit ({len(str(smp).split())} > {HERO_LIMITS['smp']})")

    # 3. SMP is a campaign choice, not a masterbrand echo (advisory — proxy vs captured brand lines).
    brand = pb._brand_boilerplate(" ".join(str(_val(gf, f) or "") for f in
                                           ("background", "competitor_context", "tone_world_assets")))
    if smp and brand and pb._text_overlap(str(smp), brand) >= 0.5:
        advisory.append("SMP may echo the masterbrand vision/claim")

    # 4. desired_response 'do' is an observable behaviour.
    dr = _val(gf, "desired_response")
    if isinstance(dr, dict):
        do = str(dr.get("do", "")).lower()
        if any(v in do for v in FORBIDDEN_DO):
            hard.append(f"desired_response 'do' is not observable: {dr.get('do')!r}")

    # 5. No duplicate open questions.
    norm = [re.sub(r"[^a-z0-9]+", " ", (q if isinstance(q, str) else q.get("question") or "").lower()).strip()
            for q in oqs]
    norm = [n for n in norm if n]
    if len(norm) != len(set(norm)):
        hard.append("duplicate open questions present")

    # 6. Insight isn't the old Mad-Lib template (advisory regression guard).
    ins = str(_val(gf, "insight") or "").lower()
    if ins and "crave" in ins and "because" in ins:
        advisory.append("insight looks like the old 'they crave X because' template")

    # 7. Objectives carry a measure, or the gap is flagged (advisory).
    obj_v = _val(gf, "objectives")
    obj_txt = json.dumps(obj_v) if obj_v else ""
    if obj_v and not re.search(r"\d", obj_txt) and not any("objective" in str(q).lower() for q in oqs):
        advisory.append("objectives have no measure/number and no open question flags it")

    return hard, advisory


def main():
    dirs = [Path(d) for d in sys.argv[1:]] or sys.exit("usage: check_invariants.py outputs/<name> ...")
    failed = False
    for d in dirs:
        if not (d / "brief_object.json").exists():
            print(f"⚠️  {d}: no brief_object.json"); continue
        hard, advisory = check(d)
        status = "✅ PASS" if not hard else "❌ FAIL"
        print(f"\n{status}  {d.name}")
        for h in hard:
            print(f"   ❌ {h}")
        for a in advisory:
            print(f"   ⚠️  {a}")
        if not hard and not advisory:
            print("   (all clean)")
        failed = failed or bool(hard)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
