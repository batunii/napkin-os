#!/usr/bin/env python3
"""
checkpoint_run.py — the paired before/after measurement Sai asked for on 2026-09-26: the
same three briefs through the OLD code (a git worktree at the baseline commit) and the
NEW code (this tree), one after another, same transport, same judge, and a summary.

    python3 checkpoint_run.py --before <worktree dir> --briefs mamaliga-engleza,employer-awareness-campaign-brief,friskies-engleza
    python3 checkpoint_run.py --before ... --arms before,after,after_novalidator

Each arm runs engine/rag/e2e_eval.py --trace <stem> --transport cli in its own tree with
RAG_STORE=local RAG_INDEX=_index_v4; the trace (calls, cost, seconds, health judged once
by Sonnet 5, per-check fails) is moved to outputs/e2e/checkpoint_<label>/<arm>/. The
before arm runs with RAG_VALIDATOR=none (the old code discarded validation anyway); the
after arm runs with the tree's default (jev on); `after_novalidator` isolates jev.

Limits, stated on every report: one judge sample per brief per arm, three briefs, so only
swings above roughly 13 health points are distinguishable from run-to-run noise (audit
BW9). The per-check pass/fail lists are the more useful comparison.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENGINE = HERE.parent


def run_one(tree: Path, stem: str, env_extra: dict, out_dir: Path) -> dict:
    """One brief through one tree's e2e_eval --trace; returns the trace's headline numbers."""
    env = {**os.environ, "RAG_STORE": "local", "RAG_INDEX": "_index_v4",
           "BRIEF_CLAUDE_TRANSPORT": "cli", **env_extra}
    rag = tree / "engine" / "rag"
    log = out_dir / f"{stem}.log"
    t0 = time.time()
    with open(log, "w") as f:
        proc = subprocess.run([sys.executable, "e2e_eval.py", "--trace", stem, "--transport", "cli"],
                              cwd=rag, env=env, stdout=f, stderr=subprocess.STDOUT)
    src = tree / "engine" / "outputs" / "e2e" / f"trace_mix_{stem}.json"
    src_dir = tree / "engine" / "outputs" / "e2e" / f"trace_mix_{stem}"
    row = {"stem": stem, "exit": proc.returncode, "wall": round(time.time() - t0, 1)}
    if src.exists():
        shutil.move(str(src), out_dir / f"trace_mix_{stem}.json")
        if src_dir.exists():
            shutil.rmtree(out_dir / f"trace_mix_{stem}", ignore_errors=True)
            shutil.move(str(src_dir), out_dir / f"trace_mix_{stem}")
        t = json.loads((out_dir / f"trace_mix_{stem}.json").read_text())
        llm = [e for e in t["events"] if e["kind"] == "llm"]
        s = t.get("score") or {}
        row.update({"brief_secs": t.get("brief_secs"), "calls": len(llm),
                    "usd": round(sum(e.get("usd", 0) for e in llm), 3),
                    "health": s.get("health"), "quality": s.get("quality"),
                    "judged": s.get("judged_checks"), "judge_model": s.get("judge_model"),
                    "failed_checks": s.get("failed_checks"), "signoff_fails": s.get("signoff_fails"),
                    "fallback_links": t.get("fallback_links"), "retrieval_fallback": t.get("retrieval_fallback"),
                    "validation_degraded": t.get("validation_degraded")})
    else:
        row["error"] = "no trace written (see log)"
    return row


def render(label: str, arms: dict, rows: dict) -> str:
    """Markdown: one table per brief across arms, then the per-check differences."""
    L = [f"# Checkpoint run {label}", "",
         "Same three briefs, Claude Code CLI, local store, Sonnet 5 judging once per brief. Three briefs and one "
         "judge sample each: treat health differences under ~13 points as noise; read the per-check lists.", ""]
    for stem in rows[next(iter(arms))]:
        L += [f"## {stem}", "| arm | health | quality | judged | brief s | calls | $ list | failed checks | sign-off fails |",
              "|---|---|---|---|---|---|---|---|---|"]
        for arm in arms:
            r = rows[arm].get(stem, {})
            if "error" in r:
                L.append(f"| {arm} | ERROR | | | | | | {r['error']} | |")
                continue
            L.append(f"| {arm} | {r.get('health')} | {r.get('quality')} | {r.get('judged')} | {r.get('brief_secs')} | "
                     f"{r.get('calls')} | {r.get('usd')} | {', '.join(r.get('failed_checks') or [])} | "
                     f"{', '.join(r.get('signoff_fails') or [])} |")
        L.append("")
    L.append("## Totals")
    L.append("| arm | health sum | quality sum | brief s sum | $ sum |")
    L.append("|---|---|---|---|---|")
    for arm in arms:
        ok = [r for r in rows[arm].values() if "error" not in r and r.get("health") is not None]
        L.append(f"| {arm} | {sum(r['health'] for r in ok)} | {sum(r['quality'] or 0 for r in ok)} | "
                 f"{round(sum(r['brief_secs'] or 0 for r in ok), 1)} | {round(sum(r['usd'] for r in ok), 3)} |")
    return "\n".join(L) + "\n"


def main() -> None:
    """Run every brief through every arm, sequentially, and write the report."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--before", required=True, help="worktree of the baseline commit")
    ap.add_argument("--after", default=str(ENGINE.parent), help="the tree under test (default: this one)")
    ap.add_argument("--briefs", default="mamaliga-engleza,employer-awareness-campaign-brief,friskies-engleza")
    ap.add_argument("--arms", default="before,after")
    ap.add_argument("--label", default=None)
    ap.add_argument("--reuse", default=None,
                    help="an earlier checkpoint dir whose arms (those not named in --arms) are copied "
                         "instead of re-run, e.g. the 'before' arm, which does not change between checkpoints")
    a = ap.parse_args()
    label = a.label or dt.datetime.now().strftime("%Y-%m-%d_%H%M")
    out = ENGINE / "outputs" / "e2e" / f"checkpoint_{label}"
    all_arms = {"before": (Path(a.before), {"RAG_VALIDATOR": "none"}),
                "after": (Path(a.after), {}),
                "after_novalidator": (Path(a.after), {"RAG_VALIDATOR": "none"})}
    arms = {k: all_arms[k] for k in a.arms.split(",") if k in all_arms}
    rows: dict = {arm: {} for arm in arms}
    briefs = [b.strip() for b in a.briefs.split(",") if b.strip()]
    if a.reuse:
        prev = json.loads((Path(a.reuse) / "rows.json").read_text())
        for arm, prev_rows in prev.items():
            if arm in arms or not prev_rows:
                continue
            rows[arm] = prev_rows
            arms = {arm: all_arms[arm], **arms}          # reused arms first in the report
            out.mkdir(parents=True, exist_ok=True)
            if (Path(a.reuse) / arm).exists() and not (out / arm).exists():
                shutil.copytree(Path(a.reuse) / arm, out / arm)
            print(f"[{arm}] reused from {a.reuse}", file=sys.stderr, flush=True)
    for arm, (tree, env_extra) in arms.items():
        if rows.get(arm):
            continue                                       # reused
        (out / arm).mkdir(parents=True, exist_ok=True)
        for stem in briefs:
            print(f"[{arm}] {stem} …", file=sys.stderr, flush=True)
            rows[arm][stem] = run_one(tree, stem, env_extra, out / arm)
            print(f"[{arm}] {stem}: {rows[arm][stem]}", file=sys.stderr, flush=True)
            (out / "rows.json").write_text(json.dumps(rows, indent=1))
    (out / "report.md").write_text(render(label, arms, rows))
    print((out / "report.md").read_text())
    print(f"-> {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
