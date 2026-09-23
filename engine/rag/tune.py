#!/usr/bin/env python3
"""
tune.py — sweep retrieval settings against the golden set, one knob at a time.

    python3 tune.py ./_index_v3 --per-source 250

Two rules, both there to stop the sweep lying to us:

1.  EMBED ONCE. Every configuration is scored on the same query vectors, computed up
    front. Otherwise each run pays for embedding again and the sweep is too slow to
    actually run, which is how tuning quietly stops happening.

2.  DECIDE ON HOLDOUT. "Seen" cases have their own retrieval queries in the indexed
    text, so they score high whatever the settings are and would flatter every
    configuration equally. Holdout recall is the number that moves for real reasons.

Only knobs that need no re-embedding are swept here: the fusion constant, the weighting
between the two retrievers, the candidate pool, and the keyword scorer's parameters.
Chunk size is the one that would matter most and it costs a full rebuild, so it belongs
in a separate, deliberate experiment rather than a sweep.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import golden  # noqa: E402
import lexical  # noqa: E402
import rag  # noqa: E402

# name -> kwargs for LocalStore.search_hybrid, plus optional bm25 params.
# Each sweep changes ONE thing from the baseline so a difference has one cause.
def focus_configs() -> dict[str, dict]:
    """Round two: confirm the round-one winners on a bigger sample, sweep the one
    parameter that moved most (BM25 `b`) more finely, and test the winners TOGETHER —
    single-knob sweeps cannot tell you whether two gains are the same gain counted twice."""
    base = {"n": 50, "rrf_k": 60, "weights": (1.0, 1.0)}
    out: dict[str, dict] = {"baseline (b0.75 k1.5 rrf60)": dict(base)}
    for b in (0.1, 0.3, 0.5):
        out[f"b={b}"] = {**base, "_bm25": {"b": b}}
    out["b=0.3 k1=1.2"] = {**base, "_bm25": {"b": 0.3, "k1": 1.2}}
    out["b=0.3 rrf_k=10"] = {**base, "rrf_k": 10, "_bm25": {"b": 0.3}}
    out["b=0.3 k1=1.2 rrf_k=10"] = {**base, "rrf_k": 10, "_bm25": {"b": 0.3, "k1": 1.2}}
    return out


def configs() -> dict[str, dict]:
    """Round one of the sweep: a baseline plus variants that each change one knob from it
    (the RRF constant, the dense:lexical weighting, the candidate pool `n`, or one BM25
    parameter). Keys starting with `_` are not search_hybrid() arguments: main() uses
    `_bm25` to rebuild the keyword index for that run."""
    base = {"n": 50, "rrf_k": 60, "weights": (1.0, 1.0)}
    out: dict[str, dict] = {"baseline (n50 k60 1:1)": dict(base)}
    for k in (10, 30, 120):
        out[f"rrf_k={k}"] = {**base, "rrf_k": k}
    for w in ((2.0, 1.0), (1.0, 2.0)):
        out[f"weights dense:lex={w[0]:g}:{w[1]:g}"] = {**base, "weights": w}
    for n in (20, 100):
        out[f"n={n}"] = {**base, "n": n}
    out["bm25 b=0.3"] = {**base, "_bm25": {"b": 0.3}}
    out["bm25 k1=1.2"] = {**base, "_bm25": {"k1": 1.2}}
    return out


def score(store, cases, qvecs, cfg, ks=(5, 10)) -> dict:
    """Golden-set recall for one configuration: every case goes through
    store.search_hybrid() with the config's non-underscore keys, filtered by source, using
    the query vectors pre-computed in `qvecs` (a query missing from it raises KeyError).
    Returns golden.evaluate()'s report."""
    hy = {k: v for k, v in cfg.items() if not k.startswith("_")}

    def search(q, k, where):
        """The search(q, k, where) callable golden.evaluate() expects, over the cached
        query vector."""
        rows = store.search_hybrid(qvecs[q], q, k=k, where=where, **hy)
        return [r for _, r in rows]

    return golden.evaluate(cases, search, ks=ks, filtered=True)


def main() -> None:
    """CLI: embed every selected golden query once, score each configuration (configs(),
    or focus_configs() with --focus), and print recall with each row's change in holdout
    recall@5 against the first row. Needs a store with search_hybrid() and bm25(), i.e.
    the local store.

    A `_bm25` configuration swaps a freshly built BM25 onto the store for its run, and the
    store's own index is put back at the end. That rebuild passes an empty holdout set to
    _lexical_text(), so unlike LocalStore.bm25() it indexes the holdout documents'
    retrieval queries too."""
    ap = argparse.ArgumentParser()
    ap.add_argument("index")
    ap.add_argument("--golden", default="golden")
    ap.add_argument("--sources", default="ipa,cannes,playbook")
    ap.add_argument("--per-source", type=int, default=250)
    ap.add_argument("--save", default=None)
    ap.add_argument("--focus", action="store_true", help="round two: confirm winners, test them together")
    a = ap.parse_args()

    index = Path(a.index) if Path(a.index).is_absolute() else HERE / a.index
    cases, _ = golden.load_golden(HERE / a.golden)
    cases = golden.select_cases(cases, sources=a.sources.split(","), per_source=a.per_source)
    cfgs = focus_configs() if a.focus else configs()
    print(f"{len(cases)} cases · {len(cfgs)} configurations", file=sys.stderr)

    store = rag.open_store(index)
    uniq = list(dict.fromkeys(c["query"] for c in cases))
    qvecs: dict[str, list[float]] = {}
    for i in range(0, len(uniq), 64):
        chunk = uniq[i:i + 64]
        vecs, _ = rag.embed(chunk, "query")
        qvecs.update({q: rag._norm(v) for q, v in zip(chunk, vecs)})
        print(f"  embedded {min(i+64, len(uniq))}/{len(uniq)}", file=sys.stderr, flush=True)

    rows, base_bm25 = [], store.bm25()
    for name, cfg in cfgs.items():
        if cfg.get("_bm25"):
            store._bm25 = lexical.BM25(
                ((r["id"], store._lexical_text(r, set())) for r in store.scroll()), **cfg["_bm25"])
        else:
            store._bm25 = base_bm25
        rep = score(store, cases, qvecs, cfg)
        g = rep["groups"]
        rows.append({"config": name,
                     "all@5": g["all"]["recall@5"], "all@10": g["all"]["recall@10"],
                     "holdout@5": g["split/holdout"]["recall@5"],
                     "holdout@10": g["split/holdout"]["recall@10"],
                     "n_holdout": g["split/holdout"]["n"]})
        print(f"  {name:<28} holdout@5 {rows[-1]['holdout@5']:.3f}  holdout@10 {rows[-1]['holdout@10']:.3f}",
              file=sys.stderr, flush=True)
    store._bm25 = base_bm25

    base = rows[0]
    print(f"\n{'configuration':<28}{'all@5':>8}{'all@10':>8}{'hold@5':>9}{'hold@10':>9}{'Δhold@5':>9}")
    for r in rows:
        d = r["holdout@5"] - base["holdout@5"]
        mark = "  <<" if d > 0.004 else ("  --" if d < -0.004 else "")
        print(f"{r['config']:<28}{r['all@5']:>8.3f}{r['all@10']:>8.3f}"
              f"{r['holdout@5']:>9.3f}{r['holdout@10']:>9.3f}{d:>+9.3f}{mark}")
    print(f"\nholdout cases: {base['n_holdout']} of {len(cases)}")
    if a.save:
        Path(a.save).write_text(json.dumps(rows, indent=1))


if __name__ == "__main__":
    main()
