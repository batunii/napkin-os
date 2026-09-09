#!/usr/bin/env python3
"""
store_qdrant.py — remote vector store backend (Qdrant) for the RAG index.

Dependency-free REST client (urllib), matching how rag.py calls NIM — the `claws`
env has no pip packages, and this way the SAME code talks to Qdrant Cloud *and* a
self-hosted Qdrant (Docker/VPS): only QDRANT_URL changes.

Why this exists: so the code can be shared WITHOUT shipping the index or the corpus.
The vectors + payload live in Qdrant; the repo carries only this adapter + config.

Config (env, typically from briefing/.env):
    QDRANT_URL         e.g. https://xxxx.qdrant.io:6333  or  http://localhost:6333
    QDRANT_API_KEY     required for Qdrant Cloud; omit for local/self-host
    QDRANT_COLLECTION  default "napkin_rag"

Activate by setting RAG_STORE=qdrant (see rag.py). Points are keyed by a deterministic
UUID from source+chunk so re-pushing is idempotent (upsert, not duplicate).

The module-level functions are the REST primitives; `QdrantStore` at the bottom is
the VectorStore adapter that rag.py actually uses (see store_base.py).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import uuid
from typing import Iterator

from store_base import StoreConfigError, VectorStore

_NS = uuid.UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")  # stable namespace for point ids


def _cfg():
    # accept QDRANT_URL or QDRANT_CLUSTER_ENDPOINT (the name Qdrant Cloud's dashboard uses)
    url = (os.environ.get("QDRANT_URL") or os.environ.get("QDRANT_CLUSTER_ENDPOINT") or "").rstrip("/")
    if not url:
        raise RuntimeError("Set QDRANT_URL (or QDRANT_CLUSTER_ENDPOINT) for RAG_STORE=qdrant")
    # Use the URL as given. Qdrant Cloud serves REST on 443 (https default) AND 6333;
    # 443 is firewall-friendly, so we do NOT force :6333. For self-hosted, include the
    # port explicitly (e.g. http://localhost:6333). Override port with QDRANT_URL if needed.
    return url, os.environ.get("QDRANT_API_KEY", ""), os.environ.get("QDRANT_COLLECTION", "napkin_rag")


def _req(method: str, path: str, body: dict | None = None, timeout: int = 60):
    url, key, _ = _cfg()
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if key:
        headers["api-key"] = key
    req = urllib.request.Request(url + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Qdrant {method} {path} -> HTTP {e.code}: {e.read().decode()[:300]}")
    except (urllib.error.URLError, OSError, TimeoutError) as e:      # DNS, reset, TLS, timeout
        raise RuntimeError(f"Qdrant {method} {path} -> unreachable: {e}") from e


def point_id(chunk: dict) -> str:
    """Deterministic UUID so re-push upserts the same point (idempotent)."""
    seed = f"{chunk.get('source')}:{chunk.get('chunk_index')}:{chunk.get('id')}"
    return str(uuid.uuid5(_NS, seed))


def collection_name() -> str:
    return _cfg()[2]


def available() -> bool:
    """True if the configured collection exists and has points."""
    try:
        _cfg()
    except RuntimeError:
        return False
    try:
        r = _req("GET", f"/collections/{collection_name()}")
        return (r.get("result", {}).get("points_count") or 0) > 0
    except RuntimeError:
        return False


def ensure_collection(dim: int, recreate: bool = False):
    name = collection_name()
    if recreate:
        try:
            _req("DELETE", f"/collections/{name}")
        except RuntimeError:
            pass
    # create only if absent
    try:
        _req("GET", f"/collections/{name}")
        exists = True
    except RuntimeError:
        exists = False
    if not exists:
        _req("PUT", f"/collections/{name}",
             {"vectors": {"size": dim, "distance": "Cosine"}})
    ensure_payload_indexes()


# Qdrant needs a payload index on any field used in a filter. These are the metadata
# keys the pipeline filters on (where={"source": ...} etc.). Idempotent.
_INDEX_FIELDS = ("metadata.source", "metadata.category", "metadata.award_tier", "metadata.year",
                 "metadata.level", "metadata.parent_id", "metadata.doc_id", "metadata.sector")


def ensure_payload_indexes():
    name = collection_name()
    for field in _INDEX_FIELDS:
        try:
            _req("PUT", f"/collections/{name}/index?wait=true",
                 {"field_name": field, "field_schema": "keyword"})
        except RuntimeError:
            pass  # already exists / non-fatal


def upsert(rows: list[dict], batch: int = 256) -> int:
    """rows are chunk dicts that include a normalised `vector`. Payload = the chunk
    minus the vector (so retrieve.py gets source/section/metadata/text unchanged)."""
    name = collection_name()
    n = 0
    for i in range(0, len(rows), batch):
        pts = []
        for c in rows[i:i + batch]:
            payload = {k: v for k, v in c.items() if k != "vector"}
            pts.append({"id": point_id(c), "vector": c["vector"], "payload": payload})
        _req("PUT", f"/collections/{name}/points?wait=true", {"points": pts})
        n += len(pts)
    return n


def _filter(where: dict | None):
    """Map a {key: value} metadata filter to a Qdrant nested-field filter.
    Keys are metadata keys (e.g. 'source'), stored under payload.metadata.<key>."""
    if not where:
        return None
    return {"must": [{"key": f"metadata.{k}", "match": {"value": v}}
                     for k, v in where.items()]}


def search(qvec: list[float], k: int = 5, where: dict | None = None) -> list[tuple[float, dict]]:
    """Return [(score, chunk_payload), ...] — mirrors rag.search()'s shape."""
    name = collection_name()
    body = {"vector": qvec, "limit": k, "with_payload": True}
    flt = _filter(where)
    if flt:
        body["filter"] = flt
    r = _req("POST", f"/collections/{name}/points/search", body)
    return [(float(p["score"]), p.get("payload", {})) for p in r.get("result", [])]


def count() -> int:
    try:
        r = _req("GET", f"/collections/{collection_name()}")
        return int(r.get("result", {}).get("points_count") or 0)
    except RuntimeError:
        return 0


def scroll(batch: int = 512) -> Iterator[dict]:
    """Yield every payload with its vector (for migrate / store-check)."""
    name = collection_name()
    offset = None
    while True:
        body = {"limit": batch, "with_payload": True, "with_vector": True}
        if offset is not None:
            body["offset"] = offset
        r = _req("POST", f"/collections/{name}/points/scroll", body, timeout=120)
        res = r.get("result", {})
        for p in res.get("points", []):
            yield {**p.get("payload", {}), "vector": p.get("vector")}
        offset = res.get("next_page_offset")
        if offset is None:
            break


def delete_collection():
    try:
        _req("DELETE", f"/collections/{collection_name()}")
    except RuntimeError:
        pass


class QdrantStore(VectorStore):
    """VectorStore adapter over the REST primitives above."""
    name = "qdrant"

    def __init__(self, **_ignored):
        try:
            self.url, _, self.collection = _cfg()
        except RuntimeError as e:
            raise StoreConfigError(str(e)) from e

    def available(self) -> bool:            return available()
    def ensure(self, dim: int) -> None:     ensure_collection(dim)
    def upsert(self, rows: list[dict]) -> int:  return upsert(rows)
    def search(self, qvec, k=5, where=None):    return search(qvec, k=k, where=where)
    def scroll(self, batch: int = 512):     return scroll(batch)
    def count(self) -> int:                 return count()
    def delete_all(self) -> None:           delete_collection()

    def describe(self) -> dict:
        return {"store": "qdrant", "label": f"qdrant:{self.collection}",
                "url": self.url, "collection": self.collection}
