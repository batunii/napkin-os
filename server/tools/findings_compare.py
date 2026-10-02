#!/usr/bin/env python3
"""Compare the findings different model settings wrote from the same facts.

    python3 server/tools/findings_compare.py --facts runs/metrics/<recorded run>/checkpoints/after_research.json \\
        opus=runs/metrics/trial-f-opus-... sonnet=runs/metrics/trial-f-sonnet-... [--show market_structure]

Each LABEL=DIR names a stage_trial.py run that saved findings_<n>.json. Scores are code counts, not opinions:
  findings      how many, and how many lenses they cover
  facts cited   distinct facts the findings cite, as a share of all facts in the checkpoint
  cites/finding the evidence each finding rests on
  multi-market  findings that compare two or more markets
  words         mean words per statement
  confidence    the split of high / medium / low the rules derived
--show LENS prints that lens's statements side by side so the wording can be read.
"""
import argparse
import json
import statistics as st
from pathlib import Path


def load(d: Path):
    return [json.loads(p.read_text()) for p in sorted(d.glob("findings_*.json"))]


def score(runs, n_facts):
    rows = []
    for fs in runs:
        cited = {c for f in fs for c in f.get("cites") or [] if str(c).startswith("f_")}
        conf = {k: sum(1 for f in fs if f.get("confidence") == k) for k in ("high", "medium", "low")}
        rows.append({"findings": len(fs), "lenses": len({f.get("lens") for f in fs}),
                     "facts_cited": len(cited), "share": len(cited) / max(1, n_facts),
                     "cites_each": st.mean(len(f.get("cites") or []) for f in fs) if fs else 0,
                     "multi_market": sum(1 for f in fs if len(f.get("markets") or []) > 1),
                     "words": st.mean(len((f.get("statement") or "").split()) for f in fs) if fs else 0, **conf})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--facts", required=True, help="the checkpoint the trials started from")
    ap.add_argument("--show", default=None)
    ap.add_argument("runs", nargs="+", metavar="LABEL=DIR")
    a = ap.parse_args()
    n_facts = len(json.loads(Path(a.facts).read_text()).get("facts") or [])
    loaded = {}
    for r in a.runs:
        label, d = r.split("=", 1)
        loaded[label] = load(Path(d))
    print(f"facts in checkpoint: {n_facts}\n")
    print("| setting | repeat | findings | lenses | facts cited (share) | cites/finding | multi-market | words | high/med/low |")
    print("|---|---|---|---|---|---|---|---|---|")
    for label, runs in loaded.items():
        for i, x in enumerate(score(runs, n_facts), 1):
            print(f"| {label} | {i} | {x['findings']} | {x['lenses']} | {x['facts_cited']} ({x['share']:.0%}) | "
                  f"{x['cites_each']:.1f} | {x['multi_market']} | {x['words']:.0f} | {x['high']}/{x['medium']}/{x['low']} |")
    if a.show:
        for label, runs in loaded.items():
            print(f"\n== {label} / {a.show}")
            for f in (runs[0] if runs else []):
                if f.get("lens") == a.show:
                    print(f"- [{','.join(f.get('markets') or [])}] ({f.get('confidence')}, {len(f.get('cites') or [])} cites) {f.get('statement')}")


if __name__ == "__main__":
    main()
