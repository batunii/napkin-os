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


def default_index_dir() -> Path:
    """Where the local index lives when no index_dir is given: $RAG_INDEX (absolute, or
    relative to the rag/ directory), else rag/index."""
    env = os.environ.get("RAG_INDEX")
    if env:
        return Path(env) if os.path.isabs(env) else HERE / env
    return HERE / "index"


class LocalStore(VectorStore):
    """VectorStore over index/chunks.jsonl, held fully in memory after the first read.

    Also offers hybrid search (dense + BM25) and get() by chunk id, which the Qdrant store
    mirrors so local and remote retrieval agree. The row list, BM25 index, NumPy matrix
    and id map are each built lazily and thrown away on every write."""
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

    def _filtered(self, where: dict | None) -> list[int]:
        """Row indexes passing the metadata filter (all rows when no filter)."""
        rows = self._rows()
        if not where:
            return list(range(len(rows)))
        return [i for i, r in enumerate(rows) if _filters.matches(r.get("metadata") or {}, where)]

    def _dense_top(self, qvec: list[float], idxs: list[int], n: int) -> list[tuple[float, int]]:
        """Top-n (score, row index) by cosine over the given row indexes. NumPy when
        available (one matrix-vector product), pure Python otherwise — same result."""
        rows = self._rows()
        if _np is not None and rows:
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
            self._bm25 = self.build_bm25()
        return self._bm25

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
