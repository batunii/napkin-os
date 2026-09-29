#!/usr/bin/env python3
"""
eval_history.py — every checkpoint measurement in one history file, for the Brief Eval
Tracker page (Sai, 2026-09-26: "a place to track the evaluation so we know if we are
making progress").

    python3 eval_history.py            # writes outputs/e2e/eval_history.json, prints a summary

Reads eval_checkpoints.json (the registry: which checkpoint_<dir> and arm holds each code
state, what changed, whether it is comparable) and each dir's rows.json (written by
checkpoint_run.py). Writes {checkpoints: [...], runs: [...]} in the exact document shape
the tracker's database holds: one `checkpoints/<id>` document per registry entry and one
`runs/<checkpoint>__<brief>` document per brief. Claude pushes it to the page with one
batched write after each checkpoint; the page updates live. The output stays in the
git-ignored outputs folder because run rows name client briefs.

After a new checkpoint: add its entry to eval_checkpoints.json, run this, push.
"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
E2E = HERE.parent / "outputs" / "e2e"
KEEP = ("health", "quality", "judged", "judge_model", "brief_secs", "calls", "usd",
        "failed_checks", "signoff_fails", "fallback_links", "retrieval_fallback",
        "validation_degraded", "error",
        # cost and speed per brief (2026-09-29): jev and embedding calls, the stage breakdown,
        # the grading's own time, calls and cost, the critic, the grounding count
        "jev_calls", "embed_calls", "stages", "grading", "critic", "grounding",
        "tokens", "jev_tokens", "claude_usd", "jev_usd", "total_usd")


def build() -> dict:
    """The history: registry entries as checkpoint documents, rows.json rows as run
    documents. A registry entry whose dir or arm is missing is kept with runs=0."""
    reg = json.loads((HERE / "eval_checkpoints.json").read_text())
    cps, runs = [], []
    for c in reg["checkpoints"]:
        f = E2E / c["dir"] / "rows.json"
        rows = (json.loads(f.read_text()).get(c["arm"]) or {}) if f.exists() else {}
        for brief, r in rows.items():
            if "claude_usd" not in r:      # older checkpoints: from the saved trace, no rerun
                import checkpoint_run
                r = checkpoint_run.add_trace_numbers(E2E / c["dir"] / c["arm"], brief, r)
            if isinstance(r.get("grounding"), dict):   # the per-claim rows stay in rows.json
                r = {**r, "grounding": {k: v for k, v in r["grounding"].items() if k != "rows"}}
            runs.append({"id": f"{c['id']}__{brief}", "checkpoint": c["id"], "brief": brief,
                         **{k: r.get(k) for k in KEEP if k in r}})
        cps.append({k: v for k, v in c.items() if k != "dir"} | {"runs": len(rows),
                                                                 "briefs": sorted(rows)})
    return {"checkpoints": cps, "runs": runs}


def main() -> None:
    """Write outputs/e2e/eval_history.json and print health/quality/time sums per checkpoint."""
    h = build()
    out = E2E / "eval_history.json"
    out.write_text(json.dumps(h, indent=1))
    for c in h["checkpoints"]:
        rs = [r for r in h["runs"] if r["checkpoint"] == c["id"]]
        s = lambda k: round(sum((r.get(k) or 0) for r in rs), 1)
        print(f"{c['id']:16} runs={len(rs)} health={s('health')} quality={s('quality')} "
              f"secs={s('brief_secs')} usd={s('usd')} comparable={c['comparable']}")
    print(f"-> {out}")


if __name__ == "__main__":
    main()
