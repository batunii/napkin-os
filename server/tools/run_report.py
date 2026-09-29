#!/usr/bin/env python3
"""Turn a run's two ledgers into one table: per stage, calls, seconds, tokens, cost, searches.

    python3 server/tools/run_report.py <run dir>

The run dir holds `middleware.jsonl` (server/napkin/metrics.py: stage, unit,
model and research events) and `mock/metrics.jsonl` (mock-backend/common.py:
what each `claude -p` cost, its tokens and its WebSearch/WebFetch uses).
Writes `report.json` and `report.md` beside them and prints the table.
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

STAGE_OF = {"extract": "extract", "identify": "identify", "classify_category": "identify", "select": "select",
            "extract_facts": "research", "synthesise": "synthesise", "report": "report", "layout": "report"}


def load(p):
    p = Path(p)
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.is_file() else []


def build(run: Path) -> dict:
    mw, mock = load(run / "middleware.jsonl"), load(run / "mock" / "metrics.jsonl")
    stages = defaultdict(lambda: dict(wall=0.0, model_calls=0, model_secs=0.0, cost=0.0, tin=0, tout=0,
                                      research_calls=0, research_cached=0, research_secs=0.0, searches=0, fetches=0,
                                      turns=0, retries=0))
    for e in mw:
        if e["kind"] == "stage" and e.get("finished"):
            stages[e["stage"]]["wall"] += e["secs"]
    for e in mock:
        if e.get("cached"):
            stages["research"]["research_cached"] += 1
            continue
        st = "research" if e["family"] == "research" else STAGE_OF.get(e.get("purpose"), e.get("purpose") or "?")
        s = stages[st]
        s["cost"] += e.get("cost_usd") or 0
        s["tin"] += (e.get("in_fresh") or 0) + (e.get("cache_write") or 0) + (e.get("cache_read") or 0)
        s["tout"] += e.get("out") or 0
        s["turns"] += e.get("turns") or 0
        if e["family"] == "research":
            s["research_calls"] += 1
            s["research_secs"] += e["secs"]
            s["searches"] += e.get("web_searches") or 0
            s["fetches"] += e.get("web_fetches") or 0
        else:
            s["model_calls"] += 1
            s["model_secs"] += e["secs"]
    for e in mw:
        if e["kind"] == "model" and e.get("attempt", 1) > 1:
            stages[STAGE_OF.get(e["purpose"], e["purpose"])]["retries"] += 1
    units = [e for e in mw if e["kind"] == "unit"]
    by_purpose = defaultdict(lambda: dict(calls=0, secs=0.0, cost=0.0, tin=0, tout=0))
    for e in mock:
        if e["family"] == "model":
            b = by_purpose[e.get("purpose") or "?"]
            b["calls"] += 1
            b["secs"] += e["secs"]
            b["cost"] += e.get("cost_usd") or 0
            b["tin"] += (e.get("in_fresh") or 0) + (e.get("cache_write") or 0) + (e.get("cache_read") or 0)
            b["tout"] += e.get("out") or 0
    tot = {k: sum(s[k] for s in stages.values()) for k in
           ("wall", "model_calls", "cost", "tin", "tout", "research_calls", "research_cached", "searches", "fetches")}
    tot["units"] = len(units)
    tot["units_reused"] = sum(1 for u in units if u["reused"])
    tot["units_zero_sources"] = sum(1 for u in units if not u["reused"] and not u["sources"])
    tot["units_with_facts"] = sum(1 for u in units if u["facts"] > 0)
    return {"stages": stages, "units": units, "by_purpose": by_purpose, "totals": tot}


def quality(run: Path) -> dict | None:
    """What the job produced, graded from facts.json and outcome.json (None for runs saved before they existed)."""
    fp, op = run / "facts.json", run / "outcome.json"
    if not fp.is_file():
        return None
    facts = json.loads(fp.read_text())
    out = json.loads(op.read_text()) if op.is_file() else {}
    sel, rep = out.get("selection") or {}, out.get("report") or {}
    n = len(facts) or 1
    conf = defaultdict(int)
    for f in facts:
        conf[f.get("confidence", "?")] += 1
    per_lens = defaultdict(int)
    for f in facts:
        per_lens[(f.get("key") or "").split(".")[0]] += 1
    single = sum(1 for f in facts if len(f.get("sources") or []) < 2)
    claims = sum(1 for s in rep.get("sections") or [] for b in s.get("blocks") or [] if b.get("kind") == "claim")
    cbm = sel.get("coverage_by_market") or {}
    cov = defaultdict(int)
    for m in cbm.values():
        for v in m.values():
            cov[v] += 1
    return {"facts": len(facts), "confidence": dict(conf), "share_single_source": round(single / n, 2),
            "share_with_quote": round(sum(1 for f in facts if f.get("quotes")) / n, 2),
            "share_dated": round(sum(1 for f in facts if f.get("as_of")) / n, 2), "per_lens": dict(per_lens),
            "gaps": len(sel.get("gaps") or []), "contests": len(sel.get("contested") or []),
            "coverage_cells": dict(cov), "findings": len(out.get("findings") or []), "report_claims": claims}


def table(r: dict) -> str:
    rows = ["| stage | wall s | model calls | research calls (cached) | searches/fetches | tokens in / out | cost $ |",
            "|---|---|---|---|---|---|---|"]
    for name in ("extract", "identify", "select", "research", "synthesise", "report"):
        s = r["stages"].get(name)
        if s:
            rows.append(f"| {name} | {s['wall']:.0f} | {s['model_calls']} | {s['research_calls']} ({s['research_cached']}) | "
                        f"{s['searches']}/{s['fetches']} | {s['tin']:,} / {s['tout']:,} | {s['cost']:.2f} |")
    t = r["totals"]
    rows.append(f"| **total** | {t['wall']:.0f} | {t['model_calls']} | {t['research_calls']} ({t['research_cached']}) | "
                f"{t['searches']}/{t['fetches']} | {t['tin']:,} / {t['tout']:,} | {t['cost']:.2f} |")
    rows += ["", f"units: {t['units']}, reused from DB: {t['units_reused']}, zero sources: {t['units_zero_sources']}, "
                 f"with facts: {t['units_with_facts']}", "", "| model purpose | calls | secs | tokens in / out | cost $ |",
             "|---|---|---|---|---|"]
    for k, b in sorted(r["by_purpose"].items()):
        rows.append(f"| {k} | {b['calls']} | {b['secs']:.0f} | {b['tin']:,} / {b['tout']:,} | {b['cost']:.2f} |")
    q = r.get("quality")
    if q:
        rows += ["", "| quality | value |", "|---|---|",
                 f"| facts pinned | {q['facts']} |", f"| confidence high / medium / low | "
                 f"{q['confidence'].get('high', 0)} / {q['confidence'].get('medium', 0)} / {q['confidence'].get('low', 0)} |",
                 f"| single-source share | {q['share_single_source']:.0%} |", f"| with quote / dated | "
                 f"{q['share_with_quote']:.0%} / {q['share_dated']:.0%} |", f"| gaps / contests | {q['gaps']} / {q['contests']} |",
                 f"| lens x market cells filled / thin / empty | {q['coverage_cells'].get('filled', 0)} / "
                 f"{q['coverage_cells'].get('thin', 0)} / {q['coverage_cells'].get('empty', 0)} |",
                 f"| findings / report claims | {q['findings']} / {q['report_claims']} |"]
    return "\n".join(rows)


if __name__ == "__main__":
    run = Path(sys.argv[1])
    r = build(run)
    r["quality"] = quality(run)
    (run / "report.json").write_text(json.dumps(r, indent=1, default=dict))
    md = table(r)
    (run / "report.md").write_text(md + "\n")
    print(md)
