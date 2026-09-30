#!/usr/bin/env python3
"""
golden.py — build and score the retrieval golden set.

Every corpus file carries a "Retrieval Queries" section: 4–6 questions a planner might
ask that THIS file should answer. That is a labelled dataset nobody has to write:
    query  ->  expected doc_id
This module turns it into a test set and scores a store against it.

    python3 golden.py build  --corpus ../reference/rag  --out golden/        # cases + holdout
    python3 golden.py eval   --index ./index  --golden golden/               # recall@k report

Design
  * One case per query line. Expected answer = the file's doc_id (for D&AD, the group id,
    because entries are only indexed inside their discipline+year group).
  * A HOLDOUT. One file in five is picked deterministically (hash of doc_id, not random, so
    the split is identical on every machine and every rebuild). For holdout files the
    build strips the Retrieval Queries from the embedded text. Without that, the index
    contains the literal question and every score is a flattering lie. Holdout recall is
    the honest number; "seen" recall shows the ceiling the RQ text buys you.
  * Filters implied by the file: each case carries {source} so the eval can measure
    retrieval the way the brief pipeline actually calls it (filtered), as well as unfiltered.
  * Metric = recall@k on doc_id: did any of the top-k chunks belong to the expected
    document? Chunk-level exactness would punish returning a different (equally good)
    section of the right case.

Files under golden/ are DERIVED from the proprietary corpus and are gitignored; the
generator is what is committed.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import chunking  # noqa: E402

HOLDOUT_FRACTION = 0.2
_BULLET = re.compile(r"^\s*[-*•]\s*(.+?)\s*$")


# ---- extraction ---------------------------------------------------------------
def rq_lines(text: str) -> list[str]:
    """Query lines from a Retrieval Queries block: one per bullet (or per non-empty line)."""
    out = []
    for line in text.splitlines():
        m = _BULLET.match(line)
        q = (m.group(1) if m else line).strip().strip('"')
        if len(q.split()) >= 3:
            out.append(q)
    return out


def is_holdout(doc_id: str, fraction: float = HOLDOUT_FRACTION) -> bool:
    """Deterministic split: same doc_id -> same answer everywhere."""
    h = int(hashlib.sha1(doc_id.encode()).hexdigest()[:8], 16)
    return (h % 1000) < int(fraction * 1000)


FACET_KEYS = ("sector", "effectiveness_type", "strategic_territory", "award_tier_raw", "year",
              "client", "framework_name", "lions_category", "agency")


def _facets(meta: dict, md: dict) -> dict[str, str]:
    """The labels a templated query could mention, as written in the file (raw spelling —
    the templates use 'Food & Drink', 'Grand Prix', not our snake_case)."""
    out = {}
    for k in FACET_KEYS:
        v = meta.get(k) if k not in ("award_tier_raw",) else md.get("award_tier_raw")
        if v is None:
            v = md.get(k)
        if v and str(v).strip() and str(v).strip().lower() not in ("general", "not recorded", "none"):
            out[k] = str(v).strip().strip('"')
    return out


def cases_for_file(path: Path) -> list[dict]:
    """Golden cases for one corpus file (D&AD handled by cases_for_dandad).
    Two kinds: `templated` = the file's own Retrieval Queries (generated from facets, so
    many files share them); `specific` = "<client> <title>" — what a planner types when
    they know the case. Acceptable-answer sets are filled in by build_golden()."""
    meta, body, _source, strategy = chunking._read(path)
    if strategy in ("skip", "group"):
        return []
    rq_block, _ = chunking.extract_rq(body)
    doc_id = str(meta.get("framework_id") or meta.get("id") or path.stem)
    queries: list[str] = rq_lines(rq_block)
    fm = meta.get("RETRIEVAL_QUERIES") or meta.get("retrieval_queries")
    if fm:
        queries += rq_lines(fm if isinstance(fm, str) else "\n".join(map(str, fm)))
    chunks = chunking.chunk_file(path)
    if not chunks:
        return []
    md = chunks[0]["metadata"]
    facets = _facets(meta, md)
    hold = is_holdout(doc_id)
    base = {"expected_doc_id": doc_id, "source": md["source"], "bucket": md["bucket"],
            "category": md.get("category"), "holdout": hold, "file": path.name, "facets": facets}
    out = [{"query": q, "kind": "templated", **base} for q in dict.fromkeys(queries)]
    title = re.sub(r"\s*\(\d{4}\)\s*$", "", facets.get("framework_name", "")).strip()
    client = facets.get("client", "")
    if client and title and md["source"] in ("ipa", "effie", "cannes"):
        out.append({"query": f"{client} {title}", "kind": "specific", **base})
    return out


def cases_for_dandad(paths: list[Path]) -> list[dict]:
    """D&AD queries point at the GROUP the entry lives in."""
    out = []
    for c in chunking.chunk_dandad_groups(paths):
        md = c["metadata"]
        if md["level"] != "child":
            continue
        gid = md["doc_id"]
        for q in dict.fromkeys(rq_lines(c.get("retrieval_queries") or "")):
            out.append({"query": q, "kind": "templated", "expected_doc_id": gid, "source": "dandad", "bucket": md["bucket"],
                        "category": None, "holdout": is_holdout(gid), "file": c["source"],
                        "facets": {k: str(v) for k, v in (("sector", md.get("sector")), ("year", md.get("year")),
                                                          ("award_tier_raw", md.get("award_tier_raw"))) if v}})
    return out


def build_golden(corpus: Path) -> tuple[list[dict], set[str]]:
    """Build every golden case from a corpus directory: returns (cases, holdout doc_ids).

    Walks every .md file (skipping DROP-ZIPS-HERE placeholders), sends D&AD entries to
    cases_for_dandad() because they are only indexed inside groups, then fills each case's
    acceptable-answer set. The holdout set is what `rag.py build --holdout` reads to embed
    those documents without their retrieval queries."""
    files = [p for p in sorted(corpus.rglob("*.md")) if "DROP-ZIPS-HERE" not in p.name]
    cases: list[dict] = []
    dandad: list[Path] = []
    for p in files:
        _, _, _, strategy = chunking._read(p)
        if strategy == "group":
            dandad.append(p)
        else:
            cases += cases_for_file(p)
    if dandad:
        cases += cases_for_dandad(dandad)
    attach_acceptable(cases)
    holdout = {c["expected_doc_id"] for c in cases if c["holdout"]}
    return cases, holdout


def attach_acceptable(cases: list[dict]) -> None:
    """For each case, `accept` = every doc (same source) whose facets agree on all the
    facet values the query text mentions. A templated query "Examples of Reframing
    strategy achieving Brand Building" is answered correctly by ANY Reframing +
    Brand Building case, not just the file it was written in. A query that mentions no
    facet (or a `specific` one) accepts only its own document."""
    by_source: dict[str, dict[str, dict]] = collections.defaultdict(dict)
    for c in cases:
        by_source[c["source"]][c["expected_doc_id"]] = c.get("facets") or {}
    for c in cases:
        if c.get("kind") == "specific":
            c["accept"] = [c["expected_doc_id"]]; continue
        q = c["query"].lower()
        mentioned = {k: v for k, v in (c.get("facets") or {}).items() if v.lower() in q}
        if not mentioned:
            c["accept"] = [c["expected_doc_id"]]; continue
        docs = by_source[c["source"]]
        c["accept"] = sorted(d for d, f in docs.items() if all(f.get(k) == v for k, v in mentioned.items()))
        if c["expected_doc_id"] not in c["accept"]:
            c["accept"].append(c["expected_doc_id"])


def write_golden(cases: list[dict], holdout: set[str], out: Path) -> None:
    """Write the golden set to `out`: cases.jsonl (one case per line), holdout.json (the
    sorted holdout doc_ids) and summary.json (counts by kind and by source and split, and
    the median acceptable-set size of templated cases). Creates the folder; overwrites
    existing files."""
    out.mkdir(parents=True, exist_ok=True)
    with (out / "cases.jsonl").open("w", encoding="utf-8") as f:
        for c in cases:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    (out / "holdout.json").write_text(json.dumps(sorted(holdout), indent=0) + "\n")
    by = collections.Counter((c["source"], c["holdout"]) for c in cases)
    kinds = collections.Counter(c.get("kind", "templated") for c in cases)
    acc = [len(c.get("accept", [])) for c in cases if c.get("kind") == "templated"]
    (out / "summary.json").write_text(json.dumps({
        "cases": len(cases), "holdout_docs": len(holdout), "by_kind": dict(kinds),
        "templated_accept_set_median": (sorted(acc)[len(acc) // 2] if acc else None),
        "by_source": {f"{s}/{'holdout' if h else 'seen'}": n for (s, h), n in sorted(by.items())}}, indent=1))


def select_cases(cases: list[dict], sources: list[str] | None = None, per_source: int | None = None) -> list[dict]:
    """Restrict to some sources and/or cap cases per source. The cap keeps holdout and
    seen cases in their natural proportion by taking every case in file order until the
    cap — deterministic, so two runs compare like with like."""
    if sources:
        cases = [c for c in cases if c["source"] in sources]
    if per_source:
        seen: dict[str, int] = collections.Counter(); out = []
        for c in cases:
            if seen[c["source"]] < per_source:
                out.append(c); seen[c["source"]] += 1
        cases = out
    return cases


def load_golden(folder: Path) -> tuple[list[dict], set[str]]:
    """Read a folder written by write_golden(): returns (cases, holdout doc_ids). Raises
    if either file is missing."""
    cases = [json.loads(l) for l in (folder / "cases.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    holdout = set(json.loads((folder / "holdout.json").read_text()))
    return cases, holdout


# ---- scoring ---------------------------------------------------------------------
def recall_at(hits: list[dict], accept, k: int) -> int:
    """1 if any of the top-k hits belongs to an acceptable document."""
    ok = set(accept) if not isinstance(accept, str) else {accept}
    return int(any((h.get("metadata") or {}).get("doc_id") in ok for h in hits[:k]))


def evaluate(cases: list[dict], search, ks=(5, 10), filtered: bool = True, limit: int | None = None) -> dict:
    """search(query, k, where) -> [chunk dict, ...] ranked. Returns a report dict.
    `filtered=True` passes where={"source": ...} like the brief pipeline does."""
    kmax = max(ks)
    agg: dict[tuple, list[int]] = collections.defaultdict(lambda: [0] * (len(ks) + 1))
    misses: list[dict] = []
    for c in (cases[:limit] if limit else cases):
        where = {"source": c["source"]} if filtered else None
        hits = search(c["query"], kmax, where)
        accept = c.get("accept") or [c["expected_doc_id"]]
        r = [recall_at(hits, accept, k) for k in ks]
        split = "holdout" if c["holdout"] else "seen"
        kind = c.get("kind", "templated")
        for key in (("all",), ("split", split), ("kind", kind),
                    ("source", c["source"]), ("bucket", c["bucket"]),
                    ("source+split", c["source"], split),
                    ("source+kind+split", c["source"], kind, split)):
            row = agg[key]; row[0] += 1
            for i, v in enumerate(r, 1): row[i] += v
        if not r[-1] and len(misses) < 50:
            misses.append({"query": c["query"], "expected": c["expected_doc_id"], "accept_n": len(accept), "source": c["source"],
                           "kind": kind, "holdout": c["holdout"], "got": [(h.get("metadata") or {}).get("doc_id") for h in hits[:3]]})
    report = {"n": len(cases[:limit] if limit else cases), "ks": list(ks), "filtered": filtered, "groups": {}, "misses": misses}
    for key, row in sorted(agg.items(), key=lambda kv: str(kv[0])):
        n = row[0]
        report["groups"]["/".join(key)] = {"n": n, **{f"recall@{k}": round(row[i] / n, 3) for i, k in enumerate(ks, 1)}}
    return report


def print_report(rep: dict) -> None:
    """Print an evaluate() report: recall@k per group as a table, then up to ten of the
    recorded misses."""
    print(f"golden eval  n={rep['n']}  filtered={rep['filtered']}")
    print(f"{'group':<40} {'n':>6}  " + "  ".join(f"recall@{k:<3}" for k in rep["ks"]))
    for g, row in rep["groups"].items():
        print(f"{g:<40} {row['n']:>6}  " + "  ".join(f"{row[f'recall@{k}']:>9.3f}" for k in rep["ks"]))
    if rep["misses"]:
        print(f"\nfirst misses ({len(rep['misses'])}):")
        for m in rep["misses"][:10]:
            print(f"  [{m['source']} {m['kind']}{' holdout' if m['holdout'] else ''}] {m['query'][:60]!r} -> want {m['expected']} (+{m['accept_n']-1} ok) got {m['got']}")


def rag_mode() -> str:
    """The search mode rag.py uses by default (RAG_SEARCH, else hybrid), recorded in the
    report when --mode is not given. rag is imported here rather than at module top, like
    its other uses in this file, so `golden.py build` never loads the store layer."""
    import rag
    return rag.SEARCH_MODE


def _store_search(index_dir: Path, queries: list[str] | None = None, batch: int = 64, mode: str | None = None):
    """search(q, k, where) over the store at index_dir. If `queries` is given, embed them
    all up front in batches (one HTTP round-trip per 64 instead of per query) and serve
    from that cache; unknown queries fall back to a single embed call."""
    import rag
    store = rag.open_store(index_dir)
    cache: dict[str, list[float]] = {}
    if queries:
        uniq = list(dict.fromkeys(queries))
        for i in range(0, len(uniq), batch):
            chunk = uniq[i:i + batch]
            vecs, _ = rag.embed(chunk, "query")
            cache.update({q: rag._norm(v) for q, v in zip(chunk, vecs)})
            print(f"  embedded {min(i + batch, len(uniq))}/{len(uniq)} queries", file=sys.stderr, flush=True)
    def search(q: str, k: int, where):
        """The search(q, k, where) callable evaluate() expects: ranked rows, scores
        dropped. Uses the pre-embedded vector when the query was supplied up front."""
        qv = cache.get(q)
        if qv is None:
            vecs, _ = rag.embed([q], "query"); qv = rag._norm(vecs[0])
        return [row for _, row in rag.search_vec(store, qv, q, k=k, where=where, mode=mode)]
    return search


def main():
    """CLI: `build` writes the golden set from a corpus; `eval` scores a store against it
    and prints recall@k (optionally saving the report as JSON). Relative paths resolve
    against the rag/ directory, not the working directory."""
    ap = argparse.ArgumentParser(description="RAG retrieval golden set")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build"); b.add_argument("--corpus", default="../reference/rag"); b.add_argument("--out", default="golden")
    e = sub.add_parser("eval"); e.add_argument("--index", default="./index"); e.add_argument("--golden", default="golden")
    e.add_argument("--unfiltered", action="store_true"); e.add_argument("--limit", type=int, default=None)
    e.add_argument("--sources", default=None, help="comma list, e.g. ipa,cannes,playbook (default: all)")
    e.add_argument("--per-source", type=int, default=None, help="cap cases per source (deterministic, first N) so one source cannot dominate")
    e.add_argument("--save", default=None, help="write the report JSON here")
    e.add_argument("--mode", default=None, help="dense | hybrid (default: $RAG_SEARCH or hybrid)")
    a = ap.parse_args()
    def _abs(x):
        """Resolve a relative CLI path against the rag/ directory."""
        return Path(x) if Path(x).is_absolute() else HERE / x
    if a.cmd == "build":
        cases, holdout = build_golden(_abs(a.corpus).resolve())
        write_golden(cases, holdout, _abs(a.out))
        print(json.loads((_abs(a.out) / "summary.json").read_text()))
    else:
        cases, _ = load_golden(_abs(a.golden))
        cases = select_cases(cases, sources=a.sources.split(",") if a.sources else None, per_source=a.per_source)
        cases = cases[:a.limit] if a.limit else cases
        rep = evaluate(cases, _store_search(_abs(a.index), [c["query"] for c in cases], mode=a.mode), filtered=not a.unfiltered)
        rep["mode"] = a.mode or rag_mode()
        print_report(rep)
        if a.save:
            Path(a.save).write_text(json.dumps(rep, indent=1))


if __name__ == "__main__":
    main()
