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
    name = "template"

    def __init__(self, **_kwargs):
        self.dsn = os.environ.get("TEMPLATE_STORE_URL")
        if not self.dsn:
            raise StoreConfigError("Set TEMPLATE_STORE_URL for RAG_STORE=template")
        # import your client here, not at module top

    def available(self) -> bool:
        return self.count() > 0

    def ensure(self, dim: int) -> None:
        raise NotImplementedError

    def upsert(self, rows: list[dict]) -> int:
        raise NotImplementedError

    def search(self, qvec: list[float], k: int = 5,
               where: dict | None = None) -> list[tuple[float, dict]]:
        raise NotImplementedError

    def scroll(self, batch: int = 512) -> Iterator[dict]:
        raise NotImplementedError

    def count(self) -> int:
        raise NotImplementedError

    def describe(self) -> dict:
        return {"store": self.name, "label": f"{self.name}:{self.dsn}"}


# register at import time if you prefer not to edit store_base.REGISTRY:
# from store_base import register; register("template", "store_template:TemplateStore")
