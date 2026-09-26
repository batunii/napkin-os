#!/usr/bin/env python3
"""
replay_validators.py — the retrieval-only validator A/B: same recorded queries, local
store, validator none / jev / nemotron, no Anthropic calls (audit critic-G14, batch 3).

    RAG_INDEX=_index_v4 python3 replay_validators.py <trace_dir>... [--validators none,jev,nemotron]
    RAG_INDEX=_index_v4 python3 replay_validators.py ../outputs/e2e/batch1_check_2026_09_25/run2

Each <trace_dir> holds a brief_object.json (a recorded run) whose
loops3_7.retrieval_trace.queries are replayed through brief_context.build_multi with the
gist as validator context, once per validator. For every brief x validator the script
records the served cites per field, the overlap with the no-validator arm and with the
recorded run, the validator's backend_used / fell_back / seconds per field, and the
validation contract; then a summary across briefs. Output:
outputs/e2e/replay_validators_<date>/replay.json and replay.md.

Why replay and not live runs: every jev-vs-nemotron comparison before 2026-09-25 was A/A,
because build_multi discarded the validator's result (RAG-1) — and the live runs also
differed in their generated queries, so their 38-52% evidence overlap measured query
drift. Fixing the queries isolates the validator. What this cannot tell you is whether a
better-ordered evidence set makes a better brief; that is the 3-brief (or 6+) A/B with
the writers on.
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
ENGINE = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ENGINE))


def _load_env() -> None:
    """engine/.env keys the store and validators need, without overriding the shell."""
    env = ENGINE / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _gist_context(bo: dict) -> str:
    """The validator context parse_brief._loops_via_mix builds, rebuilt from the record."""
    l37 = bo.get("loops3_7") or {}
    gist = l37.get("gist") or {}
    fields = (bo.get("loop1_capture") or {}).get("fields") or {}

    def val(k):
        """A captured field's value as text."""
        v = fields.get(k)
        if isinstance(v, list):
            return "; ".join(str(x.get("value")) for x in v if isinstance(x, dict) and x.get("value"))
        return str(v.get("value") or "") if isinstance(v, dict) else ""
    parts = [("problem", gist.get("problem")), ("objective", gist.get("objective")),
             ("audience", gist.get("audience")), ("key_message", gist.get("key_message")),
             ("background", val("background_context")), ("competitors", val("competitors_market"))]
    return "\n".join(f"{k}: {v}" for k, v in parts if v)[:3000]


def replay_one(bo: dict, validator: str, index_dir: Path) -> dict:
    """One brief through build_multi with `validator` (none | jev | nemotron | ...)."""
    import brief_context as bc
    import judge
    trace_rec = (bo.get("loops3_7") or {}).get("retrieval_trace") or {}
    queries = trace_rec.get("queries") or {}
    if not queries:
        raise SystemExit("the record has no loops3_7.retrieval_trace.queries")
    names = [] if validator == "none" else [validator]
    chain = judge.Chain([judge.build_backend(n) for n in names], requested=validator)
    t0 = time.time()
    mc = bc.build_multi({}, queries, index_dir=index_dir, chain=chain, context=_gist_context(bo))
    secs = round(time.time() - t0, 2)
    val = (mc.trace.get("validation") or {}).get("per_field") or {}
    per_field = {}
    for f in queries:
        rec = val.get(f) or {}
        per_field[f] = {"cites": mc.trace["fields"][f],
                        "backend_used": rec.get("backend_used"), "fell_back": rec.get("fell_back"),
                        "contract": rec.get("contract"), "per_bucket": rec.get("per_bucket"),
                        "attempts": rec.get("attempts")}
    return {"validator": validator, "seconds": secs, "per_field": per_field,
            "recorded_cites": trace_rec.get("fields") or {}, "embed": mc.trace.get("embed"),
            "notes": mc.trace.get("notes")}


def _overlap(a: list, b: list) -> float:
    """|a ∩ b| / |a| (0 when a is empty)."""
    return round(len(set(a) & set(b)) / len(a), 2) if a else 0.0


def summarise(results: dict) -> dict:
    """Per brief x validator: mean overlap with the none arm and with the recorded run,
    fallback counts; then means across briefs."""
    out = {"briefs": {}, "across": {}}
    for stem, arms in results.items():
        none = arms.get("none", {}).get("per_field", {})
        out["briefs"][stem] = {}
        for v, r in arms.items():
            pf = r["per_field"]
            ov_none = [_overlap(pf[f]["cites"], none.get(f, {}).get("cites", [])) for f in pf]
            ov_rec = [_overlap(pf[f]["cites"], r["recorded_cites"].get(f, [])) for f in pf]
            same_order = sum(1 for f in pf if pf[f]["cites"] == none.get(f, {}).get("cites"))
            others = {o: arms[o]["per_field"] for o in arms if o not in (v, "none")}
            ov_other = {o: round(sum(_overlap(pf[f]["cites"], opf.get(f, {}).get("cites", [])) for f in pf)
                                 / max(1, len(pf)), 2) for o, opf in others.items()}
            out["briefs"][stem][v] = {
                "seconds": r["seconds"],
                "overlap_with_none": round(sum(ov_none) / max(1, len(ov_none)), 2),
                "fields_in_same_order_as_none": f"{same_order}/{len(pf)}",
                "overlap_with_recorded": round(sum(ov_rec) / max(1, len(ov_rec)), 2),
                "overlap_with_other_validators": ov_other,
                "validated_fields": sum(1 for f in pf if pf[f]["backend_used"]),
                "fell_back_fields": sum(1 for f in pf if pf[f]["fell_back"]),
                "hits_served": sum(len(pf[f]["cites"]) for f in pf)}
    validators = {v for arms in results.values() for v in arms}
    for v in validators:
        rows = [b[v] for b in out["briefs"].values() if v in b]
        if rows:
            out["across"][v] = {k: round(sum(r[k] for r in rows) / len(rows), 2)
                                for k in ("seconds", "overlap_with_none", "overlap_with_recorded",
                                          "validated_fields", "fell_back_fields", "hits_served")}
    return out


def render_md(summary: dict, results: dict) -> str:
    """A short markdown report."""
    L = ["# Retrieval-only validator replay", "",
         "Same recorded queries and gist, local store, no Anthropic calls. "
         "`overlap_with_none` = share of a validator arm's served cites also served with no validator "
         "(1.0 = the validator changed nothing); `fields_in_same_order_as_none` counts fields whose "
         "served order is identical.", ""]
    L += ["| validator | briefs | s/brief | overlap with none | overlap with recorded | validated fields | fell back | hits |",
          "|---|---|---|---|---|---|---|---|"]
    for v, a in summary["across"].items():
        n = sum(1 for b in summary["briefs"].values() if v in b)
        L.append(f"| {v} | {n} | {a['seconds']} | {a['overlap_with_none']} | {a['overlap_with_recorded']} | "
                 f"{a['validated_fields']} | {a['fell_back_fields']} | {a['hits_served']} |")
    L.append("")
    for stem, arms in summary["briefs"].items():
        L.append(f"## {stem}")
        L.append("| validator | s | overlap none | same order | overlap recorded | overlap other validators | validated | fell back |")
        L.append("|---|---|---|---|---|---|---|---|")
        for v, r in arms.items():
            ov = ", ".join(f"{o} {x}" for o, x in r["overlap_with_other_validators"].items()) or "-"
            L.append(f"| {v} | {r['seconds']} | {r['overlap_with_none']} | {r['fields_in_same_order_as_none']} | "
                     f"{r['overlap_with_recorded']} | {ov} | {r['validated_fields']} | {r['fell_back_fields']} |")
        L.append("")
        for v, r in results[stem].items():
            for f, pf in r["per_field"].items():
                if pf["attempts"]:
                    bad = [a for a in pf["attempts"] if a.get("outcome") != "ok"]
                    if bad:
                        L.append(f"- {v} · {f}: " + "; ".join(f"{a['backend']} {a['outcome']} ({a.get('detail') or ''})"[:160] for a in bad))
        L.append("")
    return "\n".join(L)


def main() -> None:
    """Replay every trace dir through every validator and write the report."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("traces", nargs="+", help="dirs holding brief_object.json (recorded runs)")
    ap.add_argument("--validators", default="none,jev,nemotron")
    ap.add_argument("--index", default=None, help="local index dir (default RAG_INDEX or rag/_index_v4)")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    _load_env()
    os.environ["RAG_STORE"] = "local"
    index = Path(a.index or os.environ.get("RAG_INDEX") or (HERE / "_index_v4"))
    if not index.is_absolute():
        index = HERE / index
    out_dir = Path(a.out) if a.out else ENGINE / "outputs" / "e2e" / f"replay_validators_{dt.date.today().isoformat()}"
    out_dir.mkdir(parents=True, exist_ok=True)
    results: dict = {}
    for t in a.traces:
        p = Path(t)
        bo = json.loads((p / "brief_object.json" if p.is_dir() else p).read_text())
        # Keyed by run AND brief: the same brief is recorded under several runs.
        stem = (f"{p.parent.name}/{p.name.replace('trace_mix_', '')}" if p.is_dir() else p.stem)
        results[stem] = {}
        for v in [x.strip() for x in a.validators.split(",") if x.strip()]:
            try:
                results[stem][v] = replay_one(bo, v, index)
                print(f"  {stem} [{v}] {results[stem][v]['seconds']} s", file=sys.stderr)
            except Exception as e:  # noqa: BLE001 — one arm failing must not lose the others
                print(f"  {stem} [{v}] FAILED {e.__class__.__name__}: {e}", file=sys.stderr)
                results[stem][v] = {"validator": v, "error": f"{e.__class__.__name__}: {e}",
                                    "seconds": None, "per_field": {}, "recorded_cites": {}}
        results[stem] = {v: r for v, r in results[stem].items() if r.get("per_field") or "error" in r}
    ok = {s: {v: r for v, r in arms.items() if "error" not in r} for s, arms in results.items()}
    summary = summarise(ok)
    (out_dir / "replay.json").write_text(json.dumps({"summary": summary, "results": results}, indent=1))
    (out_dir / "replay.md").write_text(render_md(summary, ok))
    print(json.dumps(summary["across"], indent=1))
    print(f"-> {out_dir}", file=sys.stderr)


if __name__ == "__main__":
    main()
