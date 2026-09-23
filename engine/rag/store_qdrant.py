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
import sys
import os
import urllib.error
import urllib.request
import time as _time
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
    """Read the Qdrant connection from the environment: (url without a trailing slash, api
    key or '', collection name, default 'napkin_rag'). Raises RuntimeError when no URL is
    set; QdrantStore turns that into StoreConfigError. Read on every call, never cached."""
    # accept QDRANT_URL or QDRANT_CLUSTER_ENDPOINT (the name Qdrant Cloud's dashboard uses)
    url = (os.environ.get("QDRANT_URL") or os.environ.get("QDRANT_CLUSTER_ENDPOINT") or "").rstrip("/")
    if not url:
        raise RuntimeError("Set QDRANT_URL (or QDRANT_CLUSTER_ENDPOINT) for RAG_STORE=qdrant")
    # Use the URL as given. Qdrant Cloud serves REST on 443 (https default) AND 6333;
    # 443 is firewall-friendly, so we do NOT force :6333. For self-hosted, include the
    # port explicitly (e.g. http://localhost:6333). Override port with QDRANT_URL if needed.
    return url, os.environ.get("QDRANT_API_KEY", ""), os.environ.get("QDRANT_COLLECTION", "napkin_rag")


# urllib opens a fresh TCP connection per call, so a burst of requests — a migration in
# 256-row batches, or an eval firing two searches per query — can exhaust connections and
# come back "connection refused" from a server that is perfectly healthy. The embedding
# path in rag.py already retries transient failures for exactly this reason; this one did
# not, and a single blip anywhere would abort a whole migration. Retries cover connection
# errors and 429/5xx; a 4xx is a bug in the request and retrying it just hides it.
_RETRY_STATUS = (429, 500, 502, 503, 504)
_ATTEMPTS = 4


def _req(method: str, path: str, body: dict | None = None, timeout: int = 60):
    """Send one JSON request to Qdrant and return the parsed JSON response.

    Connection errors, timeouts and HTTP 429/5xx are retried, four attempts in all,
    waiting 0.4, 0.8 then 1.6 seconds; any other HTTP status fails at once. Every failure
    is raised as RuntimeError, which the functions in this module treat as "unavailable"
    or "absent"."""
    url, key, _ = _cfg()
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if key:
        headers["api-key"] = key
    last = None
    for attempt in range(_ATTEMPTS):
        req = urllib.request.Request(url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:300]
            if e.code in _RETRY_STATUS and attempt < _ATTEMPTS - 1:
                last = RuntimeError(f"Qdrant {method} {path} -> HTTP {e.code}: {detail}")
            else:
                raise RuntimeError(f"Qdrant {method} {path} -> HTTP {e.code}: {detail}")
        except (urllib.error.URLError, OSError, TimeoutError) as e:  # DNS, reset, TLS, timeout
            last = RuntimeError(f"Qdrant {method} {path} -> unreachable: {e}")
            if attempt == _ATTEMPTS - 1:
                raise last from e
        _time.sleep(0.4 * (2 ** attempt))                            # 0.4s, 0.8s, 1.6s
    raise last if last else RuntimeError(f"Qdrant {method} {path} -> failed")


def point_id(chunk: dict) -> str:
    """Deterministic UUID so re-push upserts the same point (idempotent)."""
    seed = f"{chunk.get('source')}:{chunk.get('chunk_index')}:{chunk.get('id')}"
    return str(uuid.uuid5(_NS, seed))


def collection_name() -> str:
    """The configured collection name (QDRANT_COLLECTION, default 'napkin_rag'). Raises
    RuntimeError if Qdrant is not configured at all."""
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
    """Create the collection, if absent, for `dim`-sized cosine vectors plus the BM25
    sparse vector, then make sure the payload indexes exist.

    `recreate=True` deletes the collection first, and every point with it. An existing
    collection is left as it is, so one created before sparse support stays dense-only
    (see has_sparse())."""
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
    name = collection_name()
    if name in _SPARSE_CACHE:
        return _SPARSE_CACHE[name]
    try:
        r = _req("GET", f"/collections/{name}")
    except RuntimeError as e:
        # A failed request is NOT "no sparse vector": say so and do not cache, so the next
        # search asks again instead of pinning hybrid to dense for the whole process.
        print(f"[!] qdrant: could not read collection config ({e}); hybrid falls back to "
              f"dense for this search", file=sys.stderr)
        return False
    cfg = (r.get("result", {}).get("config", {}).get("params", {}) or {})
    _SPARSE_CACHE[name] = SPARSE_NAME in (cfg.get("sparse_vectors") or {})
    return _SPARSE_CACHE[name]


# collection -> has sparse vector. The answer cannot change during a run, and it was being
# asked 42 times per brief (47% of RAG calls, measured 2026-09-23).
_SPARSE_CACHE: dict[str, bool] = {}


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
    """Create a keyword index on `id` and `parent_id` and one per indexed contract field
    (see _index_fields()). Every failure is swallowed so an index that already exists is
    not an error, which also means a genuine failure here is silent."""
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
    """Number of points in the collection, or 0 when it is missing or unreachable."""
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
    """Delete the whole collection and every point in it. A missing or unreachable
    collection is ignored."""
    try:
        _req("DELETE", f"/collections/{collection_name()}")
    except RuntimeError:
        pass


class QdrantStore(VectorStore):
    """VectorStore adapter over the REST primitives above."""
    name = "qdrant"

    def __init__(self, **_ignored):
        """Read the Qdrant config once, for describe(). Raises StoreConfigError if no URL
        is set, as the VectorStore contract requires, and makes no network call.
        Constructor kwargs (such as index_dir) are accepted and ignored."""
        try:
            self.url, _, self.collection = _cfg()
        except RuntimeError as e:
            raise StoreConfigError(str(e)) from e

    def available(self) -> bool:
        """True if the collection exists and holds points. See available()."""
        return available()
    def ensure(self, dim: int) -> None:
        """Create the collection and payload indexes if missing. See ensure_collection()."""
        ensure_collection(dim)
    def upsert(self, rows: list[dict]) -> int:
        """Write rows as points, with sparse vectors when supported. See upsert()."""
        return upsert(rows)
    def get(self, chunk_id):
        """One chunk payload by chunk id, or None. See get()."""
        return get(chunk_id)

    def search(self, qvec, k=5, where=None):
        """Dense top-k as [(score, payload)]. See search()."""
        return search(qvec, k=k, where=where)

    def search_hybrid(self, qvec, qtext, k=5, where=None, n=50, rrf_k=10, weights=(1.0, 1.0)):
        """Dense + BM25 fused by RRF, with the same defaults as the local store. See
        search_hybrid()."""
        return search_hybrid(qvec, qtext, k=k, where=where, n=n, rrf_k=rrf_k, weights=weights)
    def scroll(self, batch: int = 512):
        """Yield every payload with its vector. See scroll()."""
        return scroll(batch)
    def count(self) -> int:
        """Points in the collection, 0 if unreachable. See count()."""
        return count()
    def delete_all(self) -> None:
        """Drop the whole collection, for migrate --replace. See delete_collection()."""
        delete_collection()

    def describe(self) -> dict:
        """Identity for run metadata: `qdrant:<collection>` as the label, plus the URL and
        collection name. The API key is never included."""
        return {"store": "qdrant", "label": f"qdrant:{self.collection}",
                "url": self.url, "collection": self.collection}
