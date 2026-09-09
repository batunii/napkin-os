#!/usr/bin/env python3
"""
store_base.py — the one contract every vector-store backend implements.

Why: the brain is one logical store, but WHERE it lives must be swappable
(local JSONL today, Qdrant Cloud tomorrow, pgvector on AWS or a self-hosted
engine on Nebius after that) without touching rag.py, retrieve.py or
parse_brief.py. Everything above this file talks to `VectorStore` only.

Switching store = change RAG_STORE (and that backend's env vars). Moving data
between stores = `python3 rag.py migrate --from local --to qdrant` (copies the
rows *with* their vectors, so no re-embedding).

Row shape (what upsert() receives and search()/scroll() return):
    {id, source, section, chunk_index, metadata{...}, text, retrieval_queries,
     vector[float] (L2-normalised)}
`where` is a flat {metadata_key: value} filter on `metadata.<key>`.

Adding a backend:
    1. copy store_template.py → store_<name>.py, implement the methods
    2. add "<name>": "store_<name>:<ClassName>" to REGISTRY below (or call
       register() at import time)
    3. `python3 rag.py store-check` runs the contract against it offline
"""
from __future__ import annotations

import importlib
import os
from abc import ABC, abstractmethod
from typing import Iterator


class VectorStore(ABC):
    """Minimal contract. All methods must be safe to call with no network when
    misconfigured: raise StoreConfigError from the constructor, never later."""

    name: str = "base"

    # -- lifecycle ----------------------------------------------------------
    @abstractmethod
    def available(self) -> bool:
        """True if the store is reachable AND holds at least one row."""

    @abstractmethod
    def ensure(self, dim: int) -> None:
        """Create the collection/table/file for vectors of `dim`. Idempotent."""

    # -- data ---------------------------------------------------------------
    @abstractmethod
    def upsert(self, rows: list[dict]) -> int:
        """Insert-or-replace rows keyed by row['id']. Returns rows written."""

    @abstractmethod
    def search(self, qvec: list[float], k: int = 5,
               where: dict | None = None) -> list[tuple[float, dict]]:
        """Top-k by cosine on a normalised query vector → [(score, row), ...].
        Rows may omit 'vector'."""

    @abstractmethod
    def scroll(self, batch: int = 512) -> Iterator[dict]:
        """Yield every row WITH its vector. Used by migrate and store-check."""

    @abstractmethod
    def count(self) -> int:
        ...

    # -- introspection ------------------------------------------------------
    def describe(self) -> dict:
        """Human/meta-friendly identity, e.g. {"store": "qdrant", "label": "qdrant:napkin_rag"}."""
        return {"store": self.name, "label": self.name}

    def delete_all(self) -> None:          # optional; migrate --replace uses it
        raise NotImplementedError(f"{self.name} does not support delete_all")


class StoreConfigError(RuntimeError):
    """Raised by a backend constructor when its env/config is missing."""


# name → "module:Class". Lazy import so an unused backend's deps are never needed.
REGISTRY: dict[str, str] = {
    "local":  "store_local:LocalStore",
    "qdrant": "store_qdrant:QdrantStore",
    # "pgvector":   "store_pgvector:PgVectorStore",     # see store_template.py
    # "opensearch": "store_opensearch:OpenSearchStore",
}


def register(name: str, path: str) -> None:
    REGISTRY[name.lower()] = path


def store_name(explicit: str | None = None) -> str:
    return (explicit or os.environ.get("RAG_STORE") or "local").lower().strip()


def get_store(name: str | None = None, **kwargs) -> VectorStore:
    """Instantiate a backend by name (default: $RAG_STORE, else 'local').
    kwargs are passed to the constructor (e.g. index_dir= for local)."""
    n = store_name(name)
    if n not in REGISTRY:
        raise StoreConfigError(f"Unknown RAG_STORE={n!r}. Known: {', '.join(sorted(REGISTRY))}")
    mod_name, cls_name = REGISTRY[n].split(":")
    cls = getattr(importlib.import_module(mod_name), cls_name)
    store = cls(**kwargs)
    store.name = n
    return store


def list_stores() -> list[str]:
    return sorted(REGISTRY)
