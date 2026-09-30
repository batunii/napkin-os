#!/usr/bin/env python3
"""
store_template.py — copy this to store_<name>.py to add a new backend.

Everything above the store layer (rag.py build/query/migrate, retrieve.py,
parse_brief.py Loops 3–7) only calls the VectorStore methods below, so a new
backend is: implement six methods, register one line, run `rag.py store-check`.

Candidates and the one-liner for each:
  pgvector (AWS RDS/Aurora, Nebius Postgres):
      table(id text pk, source, section, chunk_index int, metadata jsonb,
            text, retrieval_queries, embedding vector(DIM));
      search: ORDER BY embedding <=> $1 LIMIT k   with WHERE metadata->>key = value
  AWS OpenSearch Serverless: knn_vector field + bool filter on metadata.*
  Milvus / Zilliz, Weaviate, Pinecone, LanceDB: equivalent upsert/search/scroll.

Rules:
  * Raise StoreConfigError from __init__ if env is missing. Never raise later
    for config reasons — the pipeline treats runtime errors as "RAG unavailable".
  * Vectors arrive L2-normalised; use cosine or dot, they are equal here.
  * Filters are {metadata_key: value}; index metadata.source / category /
    award_tier / year (and later level / parent_id) for speed.
  * Keep ids = row["id"] (stable, deterministic) so re-upsert is idempotent.
  * Do not add pip deps to rag.py; import them inside this module only.
"""
from __future__ import annotations

import os
from typing import Iterator

from store_base import StoreConfigError, VectorStore


class TemplateStore(VectorStore):
    """Skeleton backend: copy it, rename it, and replace every NotImplementedError.

    Set `name` to the RAG_STORE value it is registered under (get_store() also overwrites
    it with that name). Each method's docstring says what it must do;
    store_base.VectorStore is the contract, and `rag.py store-check <name>` runs it
    against a live instance."""
    name = "template"

    def __init__(self, **_kwargs):
        """Read config from the environment and create the client. Raise StoreConfigError
        here, and only here, when config is missing: the pipeline treats any later
        exception as "RAG unavailable", not as a setup mistake. Accept and ignore unknown
        kwargs, because get_store() passes index_dir= to every backend. Do not contact the
        server here; reachability is available()'s job."""
        self.dsn = os.environ.get("TEMPLATE_STORE_URL")
        if not self.dsn:
            raise StoreConfigError("Set TEMPLATE_STORE_URL for RAG_STORE=template")
        # import your client here, not at module top

    def available(self) -> bool:
        """Return True only if the store is reachable AND holds at least one row.
        Delegating to count() is fine once count() is implemented. Prefer returning False
        over raising when the server is down, as store_qdrant does: migrate calls this
        without a guard."""
        return self.count() > 0

    def ensure(self, dim: int) -> None:
        """Create the collection or table for `dim`-dimensional vectors if it is absent,
        with indexes on `id`, `parent_id` and every contract.indexed_fields() entry so
        filters stay fast. Must be idempotent: build and migrate call it on every run
        against an existing store."""
        raise NotImplementedError

    def upsert(self, rows: list[dict]) -> int:
        """Insert or replace `rows` keyed by row["id"], so re-running a build or migrate
        never duplicates. Store the vector (already L2-normalised) and keep every other
        field as payload, returned unchanged by search() and scroll(). Return the number
        of rows written."""
        raise NotImplementedError

    def search(self, qvec: list[float], k: int = 5,
               where: dict | None = None) -> list[tuple[float, dict]]:
        """Return up to `k` (score, row) pairs, best first, by cosine (or dot, equal on
        normalised vectors) against `qvec`, over rows matching `where`.

        `where` is the shared filter language on metadata.<key>: read it through
        filters.normalise() and raise filters.FilterError for any operator the backend
        cannot express rather than ignore it. Returned rows may omit "vector". Return []
        when nothing matches."""
        raise NotImplementedError

    def scroll(self, batch: int = 512) -> Iterator[dict]:
        """Yield every row WITH its vector, fetching `batch` at a time. migrate copies
        stores through this and store-check verifies writes with it, so a row yielded
        without its vector is lost in a migration."""
        raise NotImplementedError

    def count(self) -> int:
        """Return the number of rows stored. store-check asserts it includes the rows it
        just wrote."""
        raise NotImplementedError

    def describe(self) -> dict:
        """Identity for run metadata: {"store": name, "label": ...}. The label lands in
        run records and logs, so keep secrets out of it; the DSN used here is only safe if
        yours carries no password."""
        return {"store": self.name, "label": f"{self.name}:{self.dsn}"}


# register at import time if you prefer not to edit store_base.REGISTRY:
# from store_base import register; register("template", "store_template:TemplateStore")
