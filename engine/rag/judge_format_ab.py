"""
judge_format_ab.py — the paired judge test for a change to how the judges answer (phase C
change 2, Sai 2026-09-29: "only commit this once you check and test the before and after").

    python3 judge_format_ab.py <checkpoint dir>/<arm>/judge_calls.jsonl [--repeats 2]

A brief-level A/B cannot tell a judge-format change from generation noise: every run writes
different drafts. This replays the SAME drafts a real run judged (recorded with
BRIEF_JUDGE_DUMP) through each format, `repeats` times, and compares the verdicts:

    same format twice      the judge's own noise (full vs full, compact vs compact)
    full vs compact        the format's effect; it matters only above the noise

per test verdict, per draft pass/fail, and the winner (calls with several drafts), plus each
format's output tokens, time and cost per call. The fact checks by jev are off here
(BRIEF_JEV_CHECKS=0): they run in code after the judge and do not depend on its format.
Writes <jsonl dir>/judge_format_ab.json and prints a report.
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
os.environ.setdefault("BRIEF_CLAUDE_TRANSPORT", "cli")
os.environ["BRIEF_JEV_CHECKS"] = "0"
os.environ.pop("BRIEF_JUDGE_DUMP", None)            # a replay must not append to its own input
import brief_llm  # noqa: E402
import parse_brief as pb  # noqa: E402
from e2e_eval import _usd  # noqa: E402

FORMATS = ("full", "compact")


_TL = threading.local()
_real_call = pb._json_call


def _recording_call(*a, **k):
    """The judge call, with its reply kept for this thread (the replay reads the verdicts
    from the reply itself, the two territory tests included)."""
    out = _real_call(*a, **k)
    _TL.reply = out
    return out


pb._json_call = _recording_call


def judge_once(rec: dict, fmt: str) -> dict:
    """One judge call on a recorded record, in the format BRIEF_JUDGE_FORMAT is set to for
    this phase: per-draft test verdicts, pass/fail, winner, and the call's tokens, seconds
    and cost."""
    field = rec["field"]
    cands = [{"value": v} for v in rec["values"]]
    tests = [r["id"] for r in field.get("rubric") or [] if r.get("method") == "llm"] \
        + (["own_territory", "brand_only"] if rec.get("territory") else [])
    _TL.reply = None
    t0 = time.time()
    with brief_llm._stats_scope() as led:
        judged = pb._judge_and_gate(field, cands, rec.get("brand_lines") or "", rec.get("ctx") or "",
                                    rec.get("territory"), allowed_text=rec.get("allowed_text"),
                                    facts=rec.get("facts"), brief_text=rec.get("brief_text") or "")
    secs = round(time.time() - t0, 1)
    reply = _TL.reply if isinstance(_TL.reply, dict) else {}
    results = {str(k): v for k, v in (reply.get("results") or {}).items()} \
        if isinstance(reply.get("results"), dict) else {}
    if fmt == "compact":
        results = pb._from_compact(results, tests)
    label = next(iter(led.get("answered_by") or {}), "")
    usd = _usd(label, led["prompt_tokens"], led["completion_tokens"],
               led["cache_read_tokens"], led["cache_creation_tokens"])
    verdicts = {}
    for c, ok, fails in judged:
        i = next(k for k, x in enumerate(cands) if x["value"] == c["value"])
        verdicts[i] = {"ok": ok, "unjudged": any(str(f).startswith(pb.UNJUDGED) for f in fails),
                       "tests": {t: pb._verdict(results.get(str(i)) or {}, t) for t in tests}}
    winner = next((c for c, _ok, _f in judged), None)
    return {"fmt": fmt, "verdicts": verdicts, "reply": reply,
            "winner": next(k for k, x in enumerate(cands) if winner and x["value"] == winner["value"]) if winner else None,
            "out": led["completion_tokens"], "in_total": led["prompt_tokens"] + led["cache_read_tokens"]
            + led["cache_creation_tokens"], "secs": secs, "usd": usd, "model": label}


def agree(a: dict, b: dict) -> dict:
    """How far two judgements of the same drafts agree: tests, drafts, winner."""
    t_same = t_all = d_same = 0
    for i, va in a["verdicts"].items():
        vb = b["verdicts"].get(i)
        if not vb:
            continue
        d_same += va["ok"] == vb["ok"]
        for t, x in va["tests"].items():
            t_all += 1
            t_same += x is not None and x == vb["tests"].get(t)
    return {"tests": (t_same, t_all), "drafts": (d_same, len(a["verdicts"])),
            "winner": (int(a["winner"] == b["winner"]), 1) if len(a["verdicts"]) > 1 else (0, 0)}


def _add(tot: dict, x: dict):
    """Sum agreement tuples."""
    for k, (s, n) in x.items():
        ts, tn = tot.get(k, (0, 0))
        tot[k] = (ts + s, tn + n)


def main():
    """Replay every recorded judge call in both formats and report."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("jsonl", nargs="+")
    ap.add_argument("--repeats", type=int, default=2)
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    recs = [json.loads(line) for f in a.jsonl for line in Path(f).read_text().splitlines() if line.strip()]
    print(f"{len(recs)} judge calls x {len(FORMATS)} formats x {a.repeats}", file=sys.stderr)
    runs = []
    for fmt in FORMATS:                      # one format per phase: the switch is process-wide
        os.environ["BRIEF_JUDGE_FORMAT"] = fmt
        jobs = [(i, fmt, r) for i in range(len(recs)) for r in range(a.repeats)]
        with ThreadPoolExecutor(a.workers) as ex:
            runs += list(ex.map(lambda j: (j, judge_once(recs[j[0]], j[1])), jobs))
        print(f"  {fmt}: {len(jobs)} replays done", file=sys.stderr, flush=True)
    by = {}
    for (i, fmt, r), res in runs:
        by.setdefault(i, {}).setdefault(fmt, []).append(res)
    agg = {"full~full": {}, "compact~compact": {}, "full~compact": {}}
    cost = {fmt: {"calls": 0, "out": 0, "in_total": 0, "secs": 0.0, "usd": 0.0, "unjudged": 0} for fmt in FORMATS}
    per_field = {}
    for i, d in by.items():
        fid = recs[i]["field"]["id"]
        for fmt in FORMATS:
            for res in d[fmt]:
                c = cost[fmt]
                c["calls"] += 1
                c["out"] += res["out"]
                c["in_total"] += res["in_total"]
                c["secs"] += res["secs"]
                c["usd"] += res["usd"]
                c["unjudged"] += sum(v["unjudged"] for v in res["verdicts"].values())
                pf = per_field.setdefault(fid, {f: {"calls": 0, "out": 0, "secs": 0.0} for f in FORMATS})[fmt]
                pf["calls"] += 1
                pf["out"] += res["out"]
                pf["secs"] += res["secs"]
        for fmt in FORMATS:
            for x, y in itertools.combinations(d[fmt], 2):
                _add(agg[f"{fmt}~{fmt}"], agree(x, y))
        for x in d["full"]:
            for y in d["compact"]:
                _add(agg["full~compact"], agree(x, y))
    report = {"calls": len(recs), "repeats": a.repeats, "agreement": agg, "cost": cost, "per_field": per_field,
              "runs": [{"call": i, "field": recs[i]["field"]["id"], **res} for (i, _f, _r), res in runs]}
    out = Path(a.jsonl[0]).parent / "judge_format_ab.json"
    out.write_text(json.dumps(report, indent=1, default=str))

    pct = lambda s, n: f"{100 * s / n:.0f}% ({s}/{n})" if n else "-"   # noqa: E731
    print(f"\nPaired judge test: {len(recs)} recorded judge calls, each format x{a.repeats}\n")
    print("| agreement | tests | drafts pass/fail | winner |\n|---|---|---|---|")
    for k, v in agg.items():
        print(f"| {k} | {pct(*v.get('tests', (0, 0)))} | {pct(*v.get('drafts', (0, 0)))} | {pct(*v.get('winner', (0, 0)))} |")
    print("\n| format | calls | out tokens / call | in tokens / call | secs / call | $ / call | unjudged drafts |\n|---|---|---|---|---|---|---|")
    for fmt, c in cost.items():
        n = c["calls"] or 1
        print(f"| {fmt} | {c['calls']} | {c['out'] / n:.0f} | {c['in_total'] / n:.0f} | {c['secs'] / n:.1f} "
              f"| {c['usd'] / n:.4f} | {c['unjudged']} |")
    print("\n| field | full out/call | compact out/call | full s/call | compact s/call |\n|---|---|---|---|---|")
    for fid, d in per_field.items():
        f, c = d["full"], d["compact"]
        print(f"| {fid} | {f['out'] / max(f['calls'], 1):.0f} | {c['out'] / max(c['calls'], 1):.0f} "
              f"| {f['secs'] / max(f['calls'], 1):.1f} | {c['secs'] / max(c['calls'], 1):.1f} |")
    print(f"\n-> {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
