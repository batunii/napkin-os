#!/usr/bin/env python3
"""
store_local.py — file-backed VectorStore (index/chunks.jsonl + manifest.json).

The default and the canonical artefact: `rag.py build` always writes here first,
then other stores are filled by `migrate`/`push`. Zero dependencies, fine up to a
few hundred thousand rows (brute-force cosine in Python).

Config: index_dir (constructor) or $RAG_INDEX; defaults to ./index next to rag.py.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Iterator

import filters as _filters
from lexical import BM25, rrf
from store_base import VectorStore

try:                       # optional: 50-100x faster dense scoring when installed; never required
    import numpy as _np
except ImportError:        # pragma: no cover
    _np = None

HERE = Path(__file__).resolve().parent


CURRENT_INDEX = "_index_v4"


def default_index_dir() -> Path:
    """Where the local index lives when no index_dir is given: $RAG_INDEX (absolute, or
    relative to the rag/ directory), else rag/index, else rag/_index_v4, the current index
    (EC-048: rag/index was never built, so a run without RAG_INDEX had no evidence)."""
    env = os.environ.get("RAG_INDEX")
    if env:
        return Path(env) if os.path.isabs(env) else HERE / env
    return HERE / "index" if (HERE / "index").exists() else HERE / CURRENT_INDEX


class LocalStore(VectorStore):
    """VectorStore over index/chunks.jsonl, held fully in memory after the first read.

    Also offers hybrid search (dense + BM25) and get() by chunk id, which the Qdrant store
    mirrors so local and remote retrieval agree. The row list, BM25 index, NumPy matrix,
    id map, per-field filter columns and filter results are each built lazily and thrown
    away on every write.

    Filtering (2026-09-26): a metadata filter used to run filters.matches() over every
    row for every query and widening round - ~110k calls and 20-27 s per brief on the
    ~20k-chunk index, the largest single wait in the pipeline. Each filtered field is now
    factorised once into integer codes (one per distinct str() value, -1 for absent), so
    eq / ne / in / nin / exists become NumPy comparisons, and the resulting row list is
    cached per filter. Range operators (gt/gte/lt/lte) keep the exact matches() loop, on
    that field only, because they compare raw values, not their str() form. The result
    is the same row list, in the same order, as the loop gave (test_store_local_masks)."""
    name = "local"

    def __init__(self, index_dir: Path | str | None = None):
        """Point the store at `index_dir` (default: default_index_dir()). A relative path
        resolves against the rag/ directory, not the working directory. Nothing is read
        until first use, so a missing directory is not an error here."""
        d = Path(index_dir) if index_dir else default_index_dir()
        self.index_dir = d if d.is_absolute() else HERE / d
        self._cache: list[dict] | None = None
        self._bm25: BM25 | None = None
        self._mat = None                       # numpy matrix of all vectors, built lazily
        self._by_id: dict[str, dict] | None = None
        self._cols: dict[str, tuple] = {}      # field -> (codes array, {str value: code})
        self._where_cache: dict[str, list[int]] = {}
        # build_multi searches five fields in parallel threads: without this lock each
        # thread read the 356 MB file and built its own BM25 on a cold store (2026-09-26)
        self._build_lock = threading.RLock()

    # -- files --------------------------------------------------------------
    @property
    def chunks_path(self) -> Path:
        """The JSONL file holding one row per line."""
        return self.index_dir / "chunks.jsonl"

    @property
    def manifest_path(self) -> Path:
        """The JSON manifest recording how the index was built."""
        return self.index_dir / "manifest.json"

    def _rows(self) -> list[dict]:
        """All rows, read from chunks.jsonl on the first call and cached. A missing file
        is an empty index, not an error."""
        if self._cache is None:
            with self._build_lock:
                if self._cache is None:
                    rows: list[dict] = []
                    if self.chunks_path.exists():
                        with open(self.chunks_path, encoding="utf-8") as fh:
                            for line in fh:
                                if line.strip():
                                    rows.append(json.loads(line))
                    self._cache = rows
        return self._cache

    def _write(self, rows: list[dict]) -> None:
        """Replace chunks.jsonl with `rows` atomically (write a temp file, then rename),
        so a crash mid-write never leaves a half-written index. The new rows become the
        cache and the BM25, matrix and id caches are dropped to rebuild on next use."""
        self.index_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.chunks_path.with_suffix(".jsonl.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
        tmp.replace(self.chunks_path)
        self._cache = rows
        self._bm25 = None
        self._mat = None
        self._by_id = None
        self._cols = {}
        self._where_cache = {}

    def write_manifest(self, extra: dict) -> None:
        """Write manifest.json: the current row count (`chunks`) and vector `dim`, plus
        `extra`, whose keys win. The whole file is replaced, so a caller that wants to
        keep existing keys passes them back in `extra`, as rag.retag() does."""
        rows = self._rows()
        man = {"chunks": len(rows), "dim": len(rows[0]["vector"]) if rows else 0, **extra}
        self.manifest_path.write_text(json.dumps(man, indent=2))

    def manifest(self) -> dict:
        """The parsed manifest, or {} when it is missing or not valid JSON."""
        if self.manifest_path.exists():
            try:
                return json.loads(self.manifest_path.read_text())
            except json.JSONDecodeError:
                pass
        return {}

    # -- contract -----------------------------------------------------------
    def available(self) -> bool:
        """True if chunks.jsonl exists and holds at least one row."""
        return self.chunks_path.exists() and self.count() > 0

    def ensure(self, dim: int) -> None:
        """Create the index directory. `dim` is ignored: a JSONL file has no fixed vector
        size."""
        self.index_dir.mkdir(parents=True, exist_ok=True)

    def upsert(self, rows: list[dict]) -> int:
        """Merge `rows` into the index by id (a row replaces any existing row with the
        same id), then rewrite the whole file. Returns the number of rows passed in, not
        the number that changed. A full build uses replace_all() instead."""
        by_id = {r["id"]: r for r in self._rows()}
        for r in rows:
            by_id[r["id"]] = r
        self._write(list(by_id.values()))
        return len(rows)

    def replace_all(self, rows: list[dict]) -> int:
        """Fast path for a full build: overwrite instead of merge."""
        self._write(list(rows))
        return len(rows)

    WHERE_CACHE_MAX = 512

    def _filtered(self, where: dict | None) -> list[int]:
        """Row indexes passing the metadata filter (all rows when no filter), ascending.
        Vectorised over per-field codes when NumPy is installed, cached per filter; the
        plain matches() loop otherwise. Same rows either way."""
        rows = self._rows()
        if not where:
            return list(range(len(rows)))
        norm = _filters.normalise(where)                 # raises FilterError as before
        if _np is None or not rows:
            return [i for i, r in enumerate(rows) if _filters.matches(r.get("metadata") or {}, where)]
        key = json.dumps(sorted((f, op, sorted(map(str, v)) if op in ("in", "nin") else v)
                                for f, (op, v) in norm.items()), default=str)
        hit = self._where_cache.get(key)
        if hit is not None:
            return list(hit)
        mask = _np.ones(len(rows), dtype=bool)
        for field, (op, value) in norm.items():
            mask &= self._field_mask(field, op, value)
            if not mask.any():
                break
        out = _np.flatnonzero(mask).tolist()
        if len(self._where_cache) >= self.WHERE_CACHE_MAX:
            self._where_cache.clear()
        self._where_cache[key] = out
        return list(out)

    def _column(self, field: str) -> tuple:
        """(codes, vocab) for one metadata field over all rows: codes[i] is the integer
        for str(value) of row i, or -1 when the field is absent or None - the same
        presence rule filters.matches() uses. Built once per field, under the lock."""
        col = self._cols.get(field)
        if col is not None:
            return col
        rows = self._rows()
        with self._build_lock:
            col = self._cols.get(field)
            if col is None:
                vocab: dict[str, int] = {}
                codes = _np.empty(len(rows), dtype=_np.int32)
                for i, r in enumerate(rows):
                    v = (r.get("metadata") or {}).get(field)
                    codes[i] = -1 if v is None else vocab.setdefault(str(v), len(vocab))
                col = self._cols[field] = (codes, vocab)
        return col

    def _field_mask(self, field: str, op: str, value) -> "object":
        """Boolean mask of rows whose `field` passes (op, value), matching filters.matches()."""
        rows = self._rows()
        if op in ("gt", "gte", "lt", "lte"):             # raw-value comparison: exact loop
            return _np.fromiter((_filters.matches(r.get("metadata") or {}, {field: {op: value}})
                                 for r in rows), dtype=bool, count=len(rows))
        codes, vocab = self._column(field)
        present = codes != -1
        if op == "exists":
            return present if bool(value) else ~present
        if op in ("eq", "ne"):
            c = vocab.get(str(value), -2)
            same = codes == c
            return same if op == "eq" else ~same     # ne: absent rows pass (-1 != c)
        wanted = [vocab[str(v)] for v in value if str(v) in vocab]
        inside = _np.isin(codes, wanted) if wanted else _np.zeros(len(rows), dtype=bool)
        return inside if op == "in" else ~inside     # nin: absent rows pass

    def _dense_top(self, qvec: list[float], idxs: list[int], n: int) -> list[tuple[float, int]]:
        """Top-n (score, row index) by cosine over the given row indexes. NumPy when
        available (one matrix-vector product), pure Python otherwise — same result."""
        rows = self._rows()
        if _np is not None and rows:
            if self._mat is None:
                with self._build_lock:
                    if self._mat is None:
                        self._mat = _np.asarray([r["vector"] for r in rows], dtype=_np.float32)
            sub = self._mat[idxs] if len(idxs) != len(rows) else self._mat
            scores = sub @ _np.asarray(qvec, dtype=_np.float32)
            top = _np.argsort(-scores)[:n]
            return [(float(scores[t]), idxs[int(t)]) for t in top]
        scored = sorted(((_dot(qvec, rows[i]["vector"]), i) for i in idxs), key=lambda x: x[0], reverse=True)
        return scored[:n]

    def search(self, qvec: list[float], k: int = 5,
               where: dict | None = None) -> list[tuple[float, dict]]:
        """Dense-only top-k by cosine over the rows that pass `where`. Returns [] when
        nothing passes the filter."""
        idxs = self._filtered(where)
        if not idxs:
            return []
        rows = self._rows()
        return [(s, rows[i]) for s, i in self._dense_top(qvec, idxs, k)]

    # -- hybrid (dense + BM25, fused by reciprocal rank) ------------------------
    def _lexical_text(self, r: dict, holdout: set[str]) -> str:
        """Same text the vector saw: header + section + body (+ retrieval queries unless the
        doc is in the golden holdout — otherwise BM25 would leak the question text the
        dense side was deliberately built without)."""
        parts = [r.get("header") or "", r.get("section") or "", r.get("text") or ""]
        if r.get("retrieval_queries") and (r.get("metadata") or {}).get("doc_id") not in holdout:
            parts.append(r["retrieval_queries"])
        return "\n".join(parts)

    def holdout_ids(self) -> set[str]:
        """Doc ids in the golden holdout this index was built against (manifest
        `holdout_file`), or an empty set when the index has none. The one place that reads
        it, so every keyword index built over this store excludes the same documents'
        retrieval queries."""
        hf = self.manifest().get("holdout_file")
        if hf and Path(hf).exists():
            return set(json.loads(Path(hf).read_text()))
        return set()

    def build_bm25(self, **params) -> BM25:
        """A fresh keyword index over every row, holdout-safe, with BM25 `params` (k1, b)
        overriding the defaults. Used by bm25() and by tune.py, which sweeps parameters —
        a sweep that built its own index without the holdout exclusion would score its
        variants on leaked question text while the baseline did not."""
        holdout = self.holdout_ids()
        return BM25(((r["id"], self._lexical_text(r, holdout)) for r in self._rows()), **params)

    def bm25(self) -> BM25:
        """Built lazily from the rows on first use; rebuilt after any write."""
        if self._bm25 is None:
            with self._build_lock:
                if self._bm25 is None:
                    self._bm25 = self.build_bm25()
        return self._bm25

    def warm(self) -> dict:
        """Build everything a search needs - rows, vector matrix, BM25 index - once, so the
        first brief's retrieval does not pay it on the critical path. Returns what it
        built with seconds; parse_brief.run() calls it beside the opening Claude calls."""
        import time as _time
        t = _time.time()
        n = self.count()
        if _np is not None and n:
            with self._build_lock:
                if self._mat is None:
                    self._mat = _np.asarray([r["vector"] for r in self._rows()], dtype=_np.float32)
        self.bm25()
        return {"rows": n, "secs": round(_time.time() - t, 1)}

    def search_hybrid(self, qvec: list[float], qtext: str, k: int = 5, where: dict | None = None,
                      n: int = 50, rrf_k: int = 10, weights: tuple[float, float] = (1.0, 1.0)
                      ) -> list[tuple[float, dict]]:
        """Dense top-n and BM25 top-n over the same filtered rows, fused by RRF, top-k.
        n=50 candidates per side: wide enough that a doc ranked 40th by cosine but 1st by
        keyword still gets in; narrow enough to stay cheap — 20 and 100 both scored worse.
        `rrf_k` and `weights` are exposed so tuning can sweep them; the defaults here are
        the swept winners, not the paper's untested values (see lexical.rrf)."""
        idxs = self._filtered(where)
        if not idxs:
            return []
        rows = self._rows()
        by_id = {rows[i]["id"]: rows[i] for i in idxs}
        dense = [rows[i]["id"] for _, i in self._dense_top(qvec, idxs, n)]
        lex = [d for _, d in self.bm25().search(qtext, k=n, allowed=set(by_id))]
        return [(score, by_id[d]) for score, d in rrf([dense, lex], k=rrf_k, weights=weights)[:k]]

    def search_lexical(self, qtext: str, k: int = 5, where: dict | None = None
                       ) -> list[tuple[float, dict]]:
        """BM25 only, over the same filtered rows: the keyword-only fallback used when no
        query vector could be produced (rag.embed_query returned None)."""
        idxs = self._filtered(where)
        if not idxs:
            return []
        rows = self._rows()
        by_id = {rows[i]["id"]: rows[i] for i in idxs}
        return [(score, by_id[d]) for score, d in self.bm25().search(qtext, k=k, allowed=set(by_id))]

    def get_many(self, chunk_ids: list[str]) -> dict[str, dict]:
        """Several rows by chunk id: {id: row}, missing ids absent (dict lookups)."""
        out = {}
        for i in chunk_ids:
            r = self.get(i) if i else None
            if r:
                out[i] = r
        return out

    def get(self, chunk_id: str) -> dict | None:
        """One row by chunk id. Used to present a parent case after one of its sections
        matched — the index holds both, so this is a dict lookup, not a second search."""
        if self._by_id is None:
            self._by_id = {r["id"]: r for r in self._rows()}
        return self._by_id.get(chunk_id)

    def scroll(self, batch: int = 512) -> Iterator[dict]:
        """Yield every row with its vector. `batch` is ignored; the rows are already in
        memory."""
        yield from self._rows()

    def count(self) -> int:
        """Number of rows in the index (reads the file on the first call)."""
        return len(self._rows())

    def describe(self) -> dict:
        """Identity for run metadata: the index path as the label, plus embed mode, dim
        and chunk count from the manifest (None where the manifest lacks them)."""
        m = self.manifest()
        return {"store": "local", "label": str(self.index_dir), "path": str(self.index_dir),
                "embed_mode": m.get("embed_mode"), "dim": m.get("dim"), "chunks": m.get("chunks")}

    def delete_all(self) -> None:
        """Delete chunks.jsonl and empty the cached rows. The manifest is left in place,
        and the id map behind get() is not reset, so get() on this same instance can still
        return a deleted row until the next write."""
        if self.chunks_path.exists():
            self.chunks_path.unlink()
        self._cache = []


def _dot(a: list[float], b: list[float]) -> float:
    """Dot product of two vectors, equal to cosine because rows and queries are
    L2-normalised. The scoring path when NumPy is not installed."""
    return sum(x * y for x, y in zip(a, b))   # vectors are L2-normalised at store time
