#!/usr/bin/env python3
"""Turn a run's two ledgers into one table: per stage, calls, seconds, tokens, cost, searches.

    python3 server/tools/run_report.py <run dir>

The run dir holds `middleware.jsonl` (server/napkin/metrics.py: stage, unit,
model and research events) and `mock/metrics.jsonl` (mock-backend/common.py:
what each `claude -p` cost, its tokens and its WebSearch/WebFetch uses).
Writes `report.json` and `report.md` beside them and prints the table.
"""
import json
import re
import sys
from collections import defaultdict
import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
PRICES = json.loads((HERE / "prices.json").read_text())["models"]

STAGE_OF = {"extract": "extract", "identify": "identify", "classify_category": "identify", "select": "select",
            "extract_facts": "research", "synthesise": "synthesise", "report": "report", "layout": "report"}


def load(p):
    p = Path(p)
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()] if p.is_file() else []


def list_cost(model: str, tin: int, tout: int, breakdown: dict | None) -> float | None:
    """Cost at list price from the ledger's token counts, or None when the model has no price row.
    With a cache breakdown, fresh/cache-write/cache-read are priced apart; without one every input token is fresh."""
    p = PRICES.get(model)
    if not p:
        return None
    b = breakdown or {"fresh": tin, "cache_write": 0, "cache_read": 0}
    return (b.get("fresh", 0) * p["in"] + b.get("cache_write", 0) * p["cache_write_5m"]
            + b.get("cache_read", 0) * p["cache_read"] + tout * p["out"]) / 1e6


# search-jev writes one extra ledger line per unit beside its search agent: "jev" (the passage ranking, with its cost
# and the unit's reader counts) or "reader" (no page could be read; the unit fell back to the agent). They are not
# research calls of their own.
READER_ALIASES = ("jev", "reader")
READ_LOG = re.compile(r"search-jev (\w+)/(\w+): (\d+) candidates, (\d+) readable")
FAIL_LOG = re.compile(r"search-jev: could not read [^:]+: (HTTP \d+|\w+)")


def reader(run: Path, mock: list) -> dict | None:
    """What search-jev's page reading did, from the ledger's reader lines; for a run recorded before those lines
    existed, the counts the mock log holds (candidates, read, fallbacks, fetch errors), with the rest marked not
    recorded. None for a run that did not use search-jev."""
    lines = [e["reader"] for e in mock if e.get("alias") in READER_ALIASES and isinstance(e.get("reader"), dict)]
    jev_cost = sum(e.get("cost_usd") or 0 for e in mock if e.get("alias") == "jev")
    if lines:
        failed = defaultdict(int)
        for r in lines:
            for k, v in (r.get("failed") or {}).items():
                failed[k] += v
        return {"recorded": True, "units": len(lines), "candidates": sum(r["candidates"] for r in lines),
                "read": sum(r["read"] for r in lines), "failed": dict(failed),
                "fallback_units": sum(1 for r in lines if r.get("fallback")),
                "passages": sum(r.get("passages") or 0 for r in lines), "kept_chars": sum(r.get("kept_chars") or 0 for r in lines),
                "sources": sum(r.get("sources") or 0 for r in lines), "jev_tokens": sum(r.get("jev_tokens") or 0 for r in lines),
                "jev_cost": jev_cost}
    log = run / "mock.log"
    text = log.read_text(errors="replace") if log.is_file() else ""
    units = READ_LOG.findall(text)
    if not units:
        return None
    failed = defaultdict(int)
    for why in FAIL_LOG.findall(text):
        failed["http_error" if why.startswith("HTTP") or why == "HTTPError" else "network" if why in ("URLError", "TimeoutError")
               else "error"] += 1
    cands, read = sum(int(u[2]) for u in units), sum(int(u[3]) for u in units)
    logged = sum(failed.values())
    if cands - read - logged > 0:
        failed["bot_wall_or_thin_text"] = cands - read - logged   # not told apart before the reader lines existed
    return {"recorded": False, "units": len(units), "candidates": cands, "read": read, "failed": dict(failed),
            "fallback_units": sum(1 for u in units if u[3] == "0"), "passages": None, "kept_chars": None,
            "sources": None, "jev_tokens": None, "jev_cost": jev_cost}


def build(run: Path) -> dict:
    mw, mock = load(run / "middleware.jsonl"), load(run / "mock" / "metrics.jsonl")
    stages = defaultdict(lambda: dict(wall=0.0, model_calls=0, model_secs=0.0, cost=0.0, tin=0, tout=0,
                                      research_calls=0, research_cached=0, research_secs=0.0, searches=0, fetches=0,
                                      turns=0, retries=0, list_cost=0.0, unpriced=0, failed_attempts=0, failed_stage=0))
    for e in mw:
        if e["kind"] == "stage":
            if e.get("failed"):
                stages[e["stage"]]["failed_stage"] += 1
            else:
                stages[e["stage"]]["wall"] += e.get("secs") or 0  # every pass: a stage that asks a question runs twice
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
            if e.get("alias") not in READER_ALIASES:
                s["research_calls"] += 1
            s["research_secs"] += e["secs"]
            s["searches"] += e.get("web_searches") or 0
            s["fetches"] += e.get("web_fetches") or 0
        else:
            s["model_calls"] += 1
            s["model_secs"] += e["secs"]
    for e in mw:
        if e["kind"] != "model":
            continue
        st = STAGE_OF.get(e["purpose"], e["purpose"])
        if e.get("attempt", 1) > 1:
            stages[st]["retries"] += 1
        if e.get("stop") == "error":
            stages[st]["failed_attempts"] += 1
            continue
        c = list_cost(e.get("model"), e.get("input_tokens") or 0, e.get("output_tokens") or 0, e.get("breakdown"))
        if c is None:
            stages[st]["unpriced"] += 1
        else:
            stages[st]["list_cost"] += c
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
    split = defaultdict(lambda: dict(cost=0.0, tin=0, tout=0))
    for e in mock:
        st = "research" if e["family"] == "research" else STAGE_OF.get(e.get("purpose"), e.get("purpose") or "?")
        for m, u in (e.get("by_model") or {}).items():
            x = split[(st, m)]
            x["cost"] += u.get("cost") or 0
            x["tin"] += (u.get("in") or 0) + (u.get("cache_write") or 0) + (u.get("cache_read") or 0)
            x["tout"] += u.get("out") or 0
    for e in mock:
        if e.get("alias") == "jev":   # jev reports no by_model split: its cost and tokens are its own row
            x = split[("research", "jev (TypeSafe)")]
            x["cost"] += e.get("cost_usd") or 0
            x["tin"] += (e.get("reader") or {}).get("jev_tokens") or 0
    by_model = [dict(stage=st, model=m, **v) for (st, m), v in sorted(split.items())]
    tot = {k: sum(s[k] for s in stages.values()) for k in
           ("wall", "model_calls", "cost", "tin", "tout", "research_calls", "research_cached", "searches", "fetches",
            "list_cost", "failed_attempts", "failed_stage", "unpriced")}
    tot["units"] = len(units)
    tot["units_reused"] = sum(1 for u in units if u["reused"])
    tot["units_zero_sources"] = sum(1 for u in units if not u["reused"] and not u["sources"])
    tot["units_with_facts"] = sum(1 for u in units if u["facts"] > 0)
    return {"stages": stages, "units": units, "by_purpose": by_purpose, "by_model": by_model, "totals": tot,
            "reader": reader(run, mock)}


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


def sources_and_yield(run: Path, units: list) -> dict:
    """Source-tier mix from the run's layers database, and how many facts the extraction returned against how
    many survived the middleware's checks (needs the recorded extract_facts replies)."""
    out: dict = {}
    dbp = run / "mock" / "layers.sqlite"
    if dbp.is_file():
        db = sqlite3.connect(dbp)
        out["tiers"] = dict(db.execute("select tier, count(*) from sources group by tier").fetchall())
        out["domains"] = db.execute("select count(distinct domain) from sources").fetchone()[0]
    rec = run / "recordings" / "model_calls.jsonl"
    if rec.is_file():
        per: dict = defaultdict(int)
        for line in rec.read_text().splitlines():
            r = json.loads(line)
            if r.get("purpose") != "extract_facts" or r.get("stop") != "ok" or not r.get("reply"):
                continue
            try:
                per[r.get("unit")] += len(json.loads(r["reply"]).get("facts") or [])
            except (ValueError, TypeError):
                pass
        returned = sum(per.values())
        kept = sum(u.get("facts") or 0 for u in units if u.get("sources"))
        out["extraction_returned"], out["extraction_kept"] = returned, kept
    return out


def table(r: dict) -> str:
    rows = ["| stage | wall s | model calls | research calls (cached) | searches/fetches | tokens in / out | CLI cost $ | list cost $ |",
            "|---|---|---|---|---|---|---|---|"]
    for name in ("extract", "identify", "select", "research", "synthesise", "report"):
        s = r["stages"].get(name)
        if s:
            rows.append(f"| {name} | {s['wall']:.0f} | {s['model_calls']} | {s['research_calls']} ({s['research_cached']}) | "
                        f"{s['searches']}/{s['fetches']} | {s['tin']:,} / {s['tout']:,} | {s['cost']:.2f} | {s['list_cost']:.2f} |")
    t = r["totals"]
    rows.append(f"| **total** | {t['wall']:.0f} | {t['model_calls']} | {t['research_calls']} ({t['research_cached']}) | "
                f"{t['searches']}/{t['fetches']} | {t['tin']:,} / {t['tout']:,} | {t['cost']:.2f} | {t['list_cost']:.2f} |")
    if t["failed_attempts"] or t["failed_stage"] or t["unpriced"]:
        rows.append(f"\nfailed model attempts: {t['failed_attempts']}, failed stages: {t['failed_stage']}, "
                    f"model calls with no price row: {t['unpriced']}")
    rows += ["", f"units: {t['units']}, reused from DB: {t['units_reused']}, zero sources: {t['units_zero_sources']}, "
                 f"with facts: {t['units_with_facts']}", "", "| model purpose | calls | secs | tokens in / out | cost $ |",
             "|---|---|---|---|---|"]
    for k, b in sorted(r["by_purpose"].items()):
        rows.append(f"| {k} | {b['calls']} | {b['secs']:.0f} | {b['tin']:,} / {b['tout']:,} | {b['cost']:.2f} |")
    if r.get("by_model"):
        rows += ["", "| stage | model that ran | tokens in / out | CLI cost $ |", "|---|---|---|---|"]
        for x in r["by_model"]:
            rows.append(f"| {x['stage']} | {x['model']} | {x['tin']:,} / {x['tout']:,} | {x['cost']:.2f} |")
    rd = r.get("reader")
    if rd:
        na = lambda v, f=str: "not recorded" if v is None else f(v)
        why = ", ".join(f"{k} {v}" for k, v in sorted(rd["failed"].items())) or "none"
        rows += ["", "| page reader (search-jev) | value |", "|---|---|",
                 f"| units / fell back to the agent | {rd['units']} / {rd['fallback_units']} |",
                 f"| pages found / read | {rd['candidates']} / {rd['read']} ({rd['read'] / max(1, rd['candidates']):.0%}) |",
                 f"| pages not used, by reason | {why} |",
                 f"| passages scored by jev | {na(rd['passages'], lambda v: f'{v:,}')} |",
                 f"| characters kept / sources returned | {na(rd['kept_chars'], lambda v: f'{v:,}')} / {na(rd['sources'])} |",
                 f"| jev tokens / cost $ | {na(rd['jev_tokens'], lambda v: f'{v:,}')} / {rd['jev_cost']:.3f} |"]
        if not rd["recorded"]:
            rows.append("(recorded before the reader lines existed: counts from the mock log)")
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
    x = r.get("sources")
    if x:
        if x.get("tiers"):
            rows.append(f"\nsource tiers: " + ", ".join(f"{k} {v}" for k, v in sorted(x["tiers"].items()))
                        + f" ({x.get('domains', 0)} distinct domains)")
        if "extraction_returned" in x:
            rows.append(f"extraction yield: {x['extraction_kept']} of {x['extraction_returned']} facts the model returned "
                        f"survived the quote and figure checks")
    inv = r.get("invariants")
    if inv is not None:
        rows.append(f"invariant check: {inv['violations']} violation(s) " + (json.dumps(inv["by_rule"]) if inv["by_rule"] else ""))
    return "\n".join(rows)


if __name__ == "__main__":
    run = Path(sys.argv[1])
    r = build(run)
    r["quality"] = quality(run)
    r["sources"] = sources_and_yield(run, r["units"])
    try:
        import invariants
        vs = invariants.check(run)
        r["invariants"] = {"violations": len(vs), "by_rule": invariants.summarise(vs), "first": vs[:10]}
    except (OSError, ValueError, KeyError):
        r["invariants"] = None
    (run / "report.json").write_text(json.dumps(r, indent=1, default=dict))
    md = table(r)
    (run / "report.md").write_text(md + "\n")
    print(md)
