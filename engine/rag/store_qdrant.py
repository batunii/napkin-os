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

import contract
import filters
import lexical

# Document-side BM25 weights need the collection's mean document length. A remote store
# cannot compute it while it is being filled, so it is a measured constant rather than a
# guess: 191.2 tokens across the 7,315-chunk corpus (p10 76, p50 136, p90 382). It only
# sets relative length weighting, and b=0.3 already limits how much that matters, so a
# corpus that shifts moderately does not need a rebuild. Override if it shifts a lot.
SPARSE_AVG_LEN = float(os.environ.get("RAG_SPARSE_AVG_LEN", "191.2"))
SPARSE_NAME = "bm25"
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
        # `modifier: idf` makes Qdrant supply the query-side inverse document frequency
        # from its own collection statistics, so only the document-side weights are
        # stored. See lexical.sparse_document for the split.
        _req("PUT", f"/collections/{name}",
             {"vectors": {"size": dim, "distance": "Cosine"},
              "sparse_vectors": {SPARSE_NAME: {"modifier": "idf"}}})
    ensure_payload_indexes()


def has_sparse() -> bool:
    """Whether this collection was created with the BM25 sparse vector. A collection
    built before sparse support exists and cannot answer keyword queries, so hybrid has
    to fall back to dense rather than silently return half a result set."""
    try:
        r = _req("GET", f"/collections/{collection_name()}")
        cfg = (r.get("result", {}).get("config", {}).get("params", {}) or {})
        return SPARSE_NAME in (cfg.get("sparse_vectors") or {})
    except RuntimeError:
        return False


# Qdrant needs a payload index on any field used in a filter. Which fields those are
# is the contract's decision (schema/rag_metadata.v1.json, `indexed: true`), not this
# file's: the filter in _filter() and the index list must never drift apart, and the
# contract is the single place both are read from. Idempotent.
def _index_fields() -> list[tuple[str, str]]:
    """[(qdrant field path, qdrant index type)] for every indexed contract field.
    Dates get a `datetime` index so range filters (as_of > ...) work; everything else
    is `keyword` (exact match on a string)."""
    return [(f"metadata.{f.name}", "datetime" if f.type == "date" else "keyword")
            for f in contract.SCHEMA.fields.values() if f.indexed]


def ensure_payload_indexes():
    name = collection_name()
    # `id` is the chunk's own id, not a contract metadata field, so it is not in
    # _index_fields(). It still needs an index: get() looks a chunk up by it to expand a
    # matched section to its whole parent case, and without the index that is a scan.
    for field, kind in [("id", "keyword"), ("parent_id", "keyword"), *_index_fields()]:
        try:
            _req("PUT", f"/collections/{name}/index?wait=true",
                 {"field_name": field, "field_schema": kind})
        except RuntimeError:
            pass  # already exists / non-fatal


def _lexical_text(c: dict) -> str:
    """The same text the local store indexes for BM25 — header, section, body, and the
    retrieval queries. Kept identical so remote and local keyword search agree."""
    parts = [c.get("header") or "", c.get("section") or "", c.get("text") or "",
             c.get("retrieval_queries") or ""]
    return "\n".join(p for p in parts if p)


def upsert(rows: list[dict], batch: int = 256) -> int:
    """rows are chunk dicts that include a normalised `vector`. Payload = the chunk
    minus the vector (so retrieve.py gets source/section/metadata/text unchanged).
    Each point also carries its BM25 sparse vector when the collection supports one."""
    name = collection_name()
    sparse = has_sparse()
    n = 0
    for i in range(0, len(rows), batch):
        pts = []
        for c in rows[i:i + batch]:
            payload = {k: v for k, v in c.items() if k != "vector"}
            pt = {"id": point_id(c), "payload": payload}
            if sparse:
                pt["vector"] = {"": c["vector"],
                                SPARSE_NAME: lexical.sparse_document(_lexical_text(c), SPARSE_AVG_LEN)}
            else:
                pt["vector"] = c["vector"]
            pts.append(pt)
        _req("PUT", f"/collections/{name}/points?wait=true", {"points": pts})
        n += len(pts)
    return n


def get(chunk_id: str) -> dict | None:
    """One chunk payload by its chunk id. Used to present a whole parent case after one
    of its sections matched, which the local store does via a dict lookup. Without this
    the remote store would hand the brief a single section where local hands it the whole
    case — the exact local/remote divergence the shared filter and scoring code exists to
    prevent. Qdrant point ids are UUIDs derived from the chunk (see point_id), so this
    looks the chunk up by its payload id rather than by point id."""
    flt = {"must": [{"key": "id", "match": {"value": chunk_id}}]}
    try:
        r = _req("POST", f"/collections/{collection_name()}/points/scroll",
                 {"filter": flt, "limit": 1, "with_payload": True})
    except RuntimeError:
        return None
    pts = (r.get("result") or {}).get("points") or []
    return pts[0].get("payload") if pts else None


def _filter(where: dict | None):
    """Map the shared filter language (filters.py) to Qdrant's filter JSON. Keys are
    metadata keys (e.g. 'source'), stored under payload.metadata.<key>."""
    return filters.to_qdrant(where)


def search(qvec: list[float], k: int = 5, where: dict | None = None) -> list[tuple[float, dict]]:
    """Return [(score, chunk_payload), ...] — mirrors rag.search()'s shape."""
    name = collection_name()
    body = {"vector": qvec, "limit": k, "with_payload": True}
    flt = _filter(where)
    if flt:
        body["filter"] = flt
    if has_sparse():
        body["vector"] = {"name": "", "vector": qvec}     # named-vector form
    r = _req("POST", f"/collections/{name}/points/search", body)
    return [(float(p["score"]), p.get("payload", {})) for p in r.get("result", [])]


def _search_sparse(qtext: str, k: int, where: dict | None) -> list[dict]:
    """Keyword half of hybrid: BM25 via the sparse vector, Qdrant supplying idf."""
    q = lexical.sparse_query(qtext)
    if not q["indices"]:
        return []
    body = {"vector": {"name": SPARSE_NAME, "vector": q}, "limit": k, "with_payload": True}
    flt = _filter(where)
    if flt:
        body["filter"] = flt
    r = _req("POST", f"/collections/{collection_name()}/points/search", body)
    return [p.get("payload", {}) for p in r.get("result", [])]


def search_hybrid(qvec: list[float], qtext: str, k: int = 5, where: dict | None = None,
                  n: int = 50, rrf_k: int = 10, weights: tuple[float, float] = (1.0, 1.0)
                  ) -> list[tuple[float, dict]]:
    """Dense + BM25, fused by reciprocal rank — the same two lists and the same fusion as
    the local store, so local and remote rank alike. Fusion happens here rather than in
    Qdrant's own query API deliberately: it reuses one tested implementation, and parity
    between the store you tune against and the store you serve from is worth one extra
    round trip. A collection with no sparse vector falls back to dense and says so by
    returning dense-only results rather than pretending to be hybrid."""
    if not has_sparse():
        return search(qvec, k=k, where=where)
    dense = [p for _, p in search(qvec, k=n, where=where)]
    lex = _search_sparse(qtext, k=n, where=where)
    by_id = {}
    for p in dense + lex:
        by_id[str(p.get("id") or id(p))] = p
    ranks = [[str(p.get("id") or id(p)) for p in dense], [str(p.get("id") or id(p)) for p in lex]]
    fused = lexical.rrf(ranks, k=rrf_k, weights=weights)
    return [(score, by_id[d]) for score, d in fused[:k] if d in by_id]


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
    def get(self, chunk_id):                    return get(chunk_id)

    def search(self, qvec, k=5, where=None):    return search(qvec, k=k, where=where)

    def search_hybrid(self, qvec, qtext, k=5, where=None, n=50, rrf_k=10, weights=(1.0, 1.0)):
        return search_hybrid(qvec, qtext, k=k, where=where, n=n, rrf_k=rrf_k, weights=weights)
    def scroll(self, batch: int = 512):     return scroll(batch)
    def count(self) -> int:                 return count()
    def delete_all(self) -> None:           delete_collection()

    def describe(self) -> dict:
        return {"store": "qdrant", "label": f"qdrant:{self.collection}",
                "url": self.url, "collection": self.collection}
