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

from store_base import VectorStore

HERE = Path(__file__).resolve().parent


def default_index_dir() -> Path:
    env = os.environ.get("RAG_INDEX")
    if env:
        return Path(env) if os.path.isabs(env) else HERE / env
    return HERE / "index"


class LocalStore(VectorStore):
    name = "local"

    def __init__(self, index_dir: Path | str | None = None):
        d = Path(index_dir) if index_dir else default_index_dir()
        self.index_dir = d if d.is_absolute() else HERE / d
        self._cache: list[dict] | None = None

    # -- files --------------------------------------------------------------
    @property
    def chunks_path(self) -> Path:
        return self.index_dir / "chunks.jsonl"

    @property
    def manifest_path(self) -> Path:
        return self.index_dir / "manifest.json"

    def _rows(self) -> list[dict]:
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
        self.index_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.chunks_path.with_suffix(".jsonl.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
        tmp.replace(self.chunks_path)
        self._cache = rows

    def write_manifest(self, extra: dict) -> None:
        rows = self._rows()
        man = {"chunks": len(rows), "dim": len(rows[0]["vector"]) if rows else 0, **extra}
        self.manifest_path.write_text(json.dumps(man, indent=2))

    def manifest(self) -> dict:
        if self.manifest_path.exists():
            try:
                return json.loads(self.manifest_path.read_text())
            except json.JSONDecodeError:
                pass
        return {}

    # -- contract -----------------------------------------------------------
    def available(self) -> bool:
        return self.chunks_path.exists() and self.count() > 0

    def ensure(self, dim: int) -> None:
        self.index_dir.mkdir(parents=True, exist_ok=True)

    def upsert(self, rows: list[dict]) -> int:
        by_id = {r["id"]: r for r in self._rows()}
        for r in rows:
            by_id[r["id"]] = r
        self._write(list(by_id.values()))
        return len(rows)

    def replace_all(self, rows: list[dict]) -> int:
        """Fast path for a full build: overwrite instead of merge."""
        self._write(list(rows))
        return len(rows)

    def search(self, qvec: list[float], k: int = 5,
               where: dict | None = None) -> list[tuple[float, dict]]:
        rows = self._rows()
        if where:
            rows = [r for r in rows if all(
                str(v).lower() in str((r.get("metadata") or {}).get(kk, "")).lower()
                for kk, v in where.items())]
            if not rows:
                return []
        scored = sorted(((_dot(qvec, r["vector"]), r) for r in rows),
                        key=lambda x: x[0], reverse=True)
        return scored[:k]

    def scroll(self, batch: int = 512) -> Iterator[dict]:
        yield from self._rows()

    def count(self) -> int:
        return len(self._rows())

    def describe(self) -> dict:
        m = self.manifest()
        return {"store": "local", "label": str(self.index_dir), "path": str(self.index_dir),
                "embed_mode": m.get("embed_mode"), "dim": m.get("dim"), "chunks": m.get("chunks")}

    def delete_all(self) -> None:
        if self.chunks_path.exists():
            self.chunks_path.unlink()
        self._cache = []


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))   # vectors are L2-normalised at store time
