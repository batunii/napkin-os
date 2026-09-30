#!/usr/bin/env python3
"""
score_as_sent.py — the client's brief scored as the client sent it, before the system
does anything (Sai, 2026-09-26: "what is the scoring of the brief originally, without
passing it through the system").

    python3 score_as_sent.py --briefs mamaliga-engleza,employer-awareness-campaign-brief,friskies-engleza

The golden critic grades a brief field by field, so the client's text is first placed
into the template by the extraction step alone: parse_brief.run(golden=True,
loops37=False) copies what the client wrote into fields and generates nothing (no
retrieval, no writers, no sharpening, no gates); a field the client did not write stays
missing. The same critic (Sonnet 5, one judged call) then scores it exactly as the
checkpoints do, and the BetterBriefs scorecard grades the raw text itself.
Writes outputs/e2e/as_sent_<date>/<stem>.json and rows.json (git-ignored: client briefs).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent))
BRIEFS = HERE.parent.parent / "client_briefs"


def score_one(stem: str) -> dict:
    """Extraction-only brief object for one client brief, scored by the golden critic."""
    import parse_brief as pb
    import golden_critic as gc
    import labelset
    from e2e_eval import brief_files            # one file per stem, the client's own first
    text = labelset._doc_text(brief_files(BRIEFS)[stem]).strip()
    t = time.time()
    brief = pb.run(None, loops37=False, golden=True, raw_text=text, source_name=stem)
    secs = round(time.time() - t, 1)
    mode = (brief.get("meta") or {}).get("extraction_mode") or ""
    if not mode.startswith("anthropic:"):
        # Every Claude call failing (a usage limit mid-run, 2026-09-26) sends the pipeline
        # to its heuristic extraction; scores of that are not the client's brief.
        raise RuntimeError(f"{stem}: extraction answered by {mode or 'nothing'}, not Claude - not scored")
    schema = json.loads(gc.SCHEMA_PATH.read_text())
    gb = gc.from_brief_object(brief)
    v = gc.validate(schema, gb)
    v, judged = gc.run_critic_sampled(schema, gb, v)
    if not judged:
        raise RuntimeError(f"{stem}: the critic judged 0 checks - not scored")
    q = gc.quality_split(schema, gb, v)
    gf = (brief.get("loop2_golden") or {}).get("fields") or {}
    dims = (brief.get("betterbriefs_scorecard") or {}).get("dimensions") or []
    return {"stem": stem, "secs": secs, "health": v["health"] if judged else None, "judged": judged,
            "judge_model": v.get("judge_model"), "quality": q["quality"], "client_gaps": q["client_gaps"],
            "failed_checks": [f"{fr['id']}.{c['id']}" for fr in v["fields"] for c in fr["checks"] if c["status"] == "fail"],
            "missing_fields": sorted(k for k, x in gf.items() if isinstance(x, dict) and x.get("source") == "missing"),
            "filled_fields": sum(1 for x in gf.values() if isinstance(x, dict) and x.get("source") != "missing"),
            "scorecard": {d.get("dimension"): d.get("verdict") for d in dims},
            "scorecard_tally": {k: sum(1 for d in dims if d.get("verdict") == k) for k in ("pass", "vague", "missing")},
            "smp": (gf.get("smp") or {}).get("value"), "insight": (gf.get("insight") or {}).get("value")}


def main() -> None:
    """Score each brief as sent and write the rows."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--critic", default="claude-fable-5-1x3", metavar="MODEL[xN]",
                    help="critic model and samples (default Fable 5.1 x3, as every evaluation from 2026-09-28)")
    ap.add_argument("--briefs", default="mamaliga-engleza,employer-awareness-campaign-brief,friskies-engleza")
    a = ap.parse_args()
    m, _, n = a.critic.partition("x")
    os.environ["CRITIC_MODEL"], os.environ["CRITIC_SAMPLES"] = m, n or "1"   # read by run_critic_sampled
    out = HERE.parent / "outputs" / "e2e" / f"as_sent_{dt.date.today().isoformat()}"
    out.mkdir(parents=True, exist_ok=True)
    rows = {}
    for stem in [b.strip() for b in a.briefs.split(",") if b.strip()]:
        rows[stem] = score_one(stem)
        (out / f"{stem}.json").write_text(json.dumps(rows[stem], indent=1, ensure_ascii=False))
        r = rows[stem]
        print(f"{stem}: health {r['health']} quality {r['quality']} filled {r['filled_fields']} "
              f"missing {r['missing_fields']} scorecard {r['scorecard_tally']}", file=sys.stderr, flush=True)
    (out / "rows.json").write_text(json.dumps(rows, indent=1, ensure_ascii=False))
    print(f"-> {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
