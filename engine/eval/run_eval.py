#!/usr/bin/env python3
"""Minimal eval harness: collect the four existing quality signals from brief
output dirs into one comparable record, so pipeline/pack changes are measured
instead of assumed.

No new judge model, no LLM calls — this only *reads* what a run already wrote:

  1. golden_critic.validate(...)  -> health 0-100 + per-check pass/fail/review
  2. check_invariants.check(dir)  -> hard / advisory defect lists
  3. betterbriefs scorecard       -> per-dimension pass/vague/missing verdicts
  4. meta.llm_stats               -> cost / latency regression signals

Usage:
    python3 eval/run_eval.py outputs/baseline-v0 [outputs/other ...]
    python3 eval/run_eval.py --save eval/baselines/v0.json outputs/baseline-v0
    python3 eval/run_eval.py --compare eval/baselines/v0.json outputs/candidate

The retrieval set (loops3_7 sources/citations) is captured verbatim so the
"identical retrieval sets" no-op check is a diff of two JSON files.
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENGINE = HERE.parent
sys.path.insert(0, str(ENGINE))

import golden_critic  # noqa: E402
import check_invariants  # noqa: E402

SCHEMA_PATH = ENGINE / "golden-brief" / "golden_brief.schema.json"


def collect(brief_dir: Path) -> dict:
    bo_path = brief_dir / "brief_object.json"
    bo = json.loads(bo_path.read_text())

    # 1. golden critic (validation only — the LLM checks stay whatever the run left them as)
    schema = json.loads(SCHEMA_PATH.read_text())
    brief = golden_critic.from_brief_object(bo)
    validation = golden_critic.validate(schema, brief)
    checks = [c for f in validation["fields"] for c in f["checks"]]
    by_status = {s: sum(1 for c in checks if c["status"] == s) for s in ("pass", "fail", "review")}
    dod_fails = [d["id"] for d in validation["definition_of_done"] if d["status"] == "fail"]

    # 2. invariants
    hard, advisory = check_invariants.check(brief_dir)

    # 3. BetterBriefs scorecard
    dims = (bo.get("betterbriefs_scorecard") or {}).get("dimensions") or []
    verdicts = {d.get("dimension", f"dim{i}"): d.get("verdict") for i, d in enumerate(dims)}

    # 4. cost / latency
    stats = (bo.get("meta") or {}).get("llm_stats") or {}

    # retrieval set — the no-op refactor check diffs this block. Snippets are
    # REDACTED to content hashes: identity comparison stays exact, but no
    # corpus text (licensed material) ever lands in a tracked eval record.
    loops = json.loads(json.dumps(bo.get("loops3_7") or {}))
    for loop in (loops.get("loops") or {}).values():
        for e in loop.get("evidence") or []:
            if e.get("snippet"):
                e["snippet"] = "sha256:" + hashlib.sha256(
                    e["snippet"].encode()).hexdigest()[:16]

    return {
        "dir": str(brief_dir),
        "health": validation["health"],
        "checks": by_status,
        "dod_fails": dod_fails,
        "open_questions_high": sum(
            1 for q in validation["open_questions"] if q.get("severity") == "high"
        ),
        "invariants": {"hard": hard, "advisory": advisory},
        "betterbriefs": verdicts,
        "llm_stats": {
            k: stats.get(k)
            for k in ("logical_calls", "calls", "http_attempts", "prompt_tokens",
                      "completion_tokens", "rate_limited", "wall_seconds")
        },
        "retrieval": loops,
    }


def summarize(rec: dict) -> str:
    inv = rec["invariants"]
    bb = rec["betterbriefs"]
    st = rec["llm_stats"]
    return (
        f"{Path(rec['dir']).name:24s} health={rec['health']:3d}  "
        f"checks p/f/r={rec['checks']['pass']}/{rec['checks']['fail']}/{rec['checks']['review']}  "
        f"dod_fails={len(rec['dod_fails'])}  inv hard/adv={len(inv['hard'])}/{len(inv['advisory'])}  "
        f"bb pass={sum(1 for v in bb.values() if v == 'pass')}/{len(bb)}  "
        f"calls={st.get('logical_calls') or st.get('calls')}  "
        f"tokens={st.get('prompt_tokens')}+{st.get('completion_tokens')}  "
        f"wall={round(st.get('wall_seconds') or 0)}s"
    )


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Collect quality signals from brief output dirs into comparable records")
    ap.add_argument("dirs", nargs="+", help="brief output dirs (containing brief_object.json)")
    ap.add_argument("--save", metavar="FILE", help="write full records as JSON")
    ap.add_argument("--compare", metavar="BASELINE_JSON",
                    help="diff each dir's record against a saved baseline")
    args = ap.parse_args(argv)

    records = [collect(Path(d)) for d in args.dirs]
    for rec in records:
        print(summarize(rec))

    if args.save:
        out = Path(args.save)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(records, indent=2))
        print(f"saved -> {out}")

    if args.compare:
        base = json.loads(Path(args.compare).read_text())
        base_by_name = {Path(b["dir"]).name: b for b in base}
        for rec in records:
            b = base_by_name.get(Path(rec["dir"]).name) or base[0]
            print(f"\n--- vs baseline {Path(b['dir']).name} ---")
            for key in ("health",):
                if rec[key] != b[key]:
                    print(f"  {key}: {b[key]} -> {rec[key]}")
            same_retrieval = rec["retrieval"] == b["retrieval"]
            print(f"  retrieval set identical: {same_retrieval}")
            for k, v in rec["llm_stats"].items():
                bv = b["llm_stats"].get(k)
                if v != bv:
                    print(f"  llm_stats.{k}: {bv} -> {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
