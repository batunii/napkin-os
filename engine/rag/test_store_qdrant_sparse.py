"""Qdrant sparse-vector wiring, with the HTTP layer stubbed — no live instance needed.
Run: cd engine/rag && python3 -m pytest test_store_qdrant_sparse.py -q
"""
from __future__ import annotations
import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import lexical  # noqa: E402
import store_qdrant as q  # noqa: E402


class _Fake:
    """Records requests and replays canned responses."""
    def __init__(self, sparse=True, dense_ids=("a", "b"), lex_ids=("b", "c")):
        """Configure whether the fake collection has a sparse vector and what each search
        endpoint returns."""
        self.sparse, self.dense_ids, self.lex_ids, self.seen = sparse, dense_ids, lex_ids, []

    def __call__(self, method, path, body=None, timeout=60):
        """Record the call and reply as the real Qdrant HTTP API would for that path."""
        self.seen.append((method, path, body))
        if method == "GET" and path.startswith("/collections/"):
            cfg = {"sparse_vectors": {q.SPARSE_NAME: {"modifier": "idf"}}} if self.sparse else {}
            return {"result": {"config": {"params": cfg}}}
        if path.endswith("/points/search"):
            v = (body or {}).get("vector")
            ids = self.lex_ids if isinstance(v, dict) and v.get("name") == q.SPARSE_NAME else self.dense_ids
            return {"result": [{"score": 1.0 - i / 10, "payload": {"id": x, "text": x}}
                               for i, x in enumerate(ids)]}
        return {"result": {}}


def _patch(monkeypatch, fake):
    """Point store_qdrant at the fake HTTP layer and a fixed collection name."""
    monkeypatch.setattr(q, "_req", fake)
    monkeypatch.setattr(q, "collection_name", lambda: "test")
    q.has_sparse.__wrapped__ if hasattr(q.has_sparse, "__wrapped__") else None


def test_collection_is_created_with_the_bm25_sparse_vector(monkeypatch):
    """ensure_collection() creates a new collection with a BM25 (idf-modifier) sparse
    vector configured."""
    fake = _Fake(sparse=False)
    _patch(monkeypatch, fake)
    monkeypatch.setattr(q, "_req", lambda m, p, b=None, timeout=60: (_ for _ in ()).throw(RuntimeError())
                        if m == "GET" else fake(m, p, b))
    q.ensure_collection(2048)
    created = [b for m, p, b in fake.seen if m == "PUT" and p == "/collections/test"]
    assert created and q.SPARSE_NAME in created[0]["sparse_vectors"]
    assert created[0]["sparse_vectors"][q.SPARSE_NAME]["modifier"] == "idf"


def test_upsert_attaches_a_sparse_vector_per_point(monkeypatch):
    """upsert() attaches both the dense and a sparse vector to each point when the
    collection supports sparse."""
    fake = _Fake(sparse=True)
    _patch(monkeypatch, fake)
    rows = [{"id": "c1", "vector": [0.1, 0.2], "header": "Xero UK", "section": "Insight",
             "text": "accountants trust", "retrieval_queries": "", "metadata": {"doc_id": "x"}}]
    assert q.upsert(rows) == 1
    pts = [b for m, p, b in fake.seen if p.startswith("/collections/test/points?")][0]["points"]
    vec = pts[0]["vector"]
    assert set(vec) == {"", q.SPARSE_NAME}
    assert vec[""] == [0.1, 0.2]
    assert vec[q.SPARSE_NAME]["indices"] and len(vec[q.SPARSE_NAME]["indices"]) == len(vec[q.SPARSE_NAME]["values"])


def test_upsert_stays_dense_on_a_collection_without_sparse(monkeypatch):
    """upsert() sends a plain dense vector, unchanged, for an old collection with no sparse
    vector configured."""
    fake = _Fake(sparse=False)
    _patch(monkeypatch, fake)
    q.upsert([{"id": "c1", "vector": [0.1, 0.2], "text": "t", "metadata": {}}])
    pts = [b for m, p, b in fake.seen if p.startswith("/collections/test/points?")][0]["points"]
    assert pts[0]["vector"] == [0.1, 0.2]          # old collections keep working


def test_hybrid_fuses_both_lists_the_same_way_the_local_store_does(monkeypatch):
    """search_hybrid() runs one dense and one sparse search and fuses them with the same
    rrf() the local store uses, so remote and local ranking agree."""
    fake = _Fake(sparse=True, dense_ids=("a", "b"), lex_ids=("b", "c"))
    _patch(monkeypatch, fake)
    out = q.search_hybrid([0.1, 0.2], "xero accountants", k=3)
    got = [p["id"] for _, p in out]
    expected = [d for _, d in lexical.rrf([["a", "b"], ["b", "c"]], k=10)][:3]
    assert got == expected
    assert got[0] == "b"                            # in both lists
    searches = [b for m, p, b in fake.seen if p.endswith("/points/search")]
    assert len(searches) == 2                       # one dense, one sparse
    assert searches[1]["vector"]["name"] == q.SPARSE_NAME


def test_hybrid_falls_back_to_dense_when_the_collection_has_no_sparse_vector(monkeypatch):
    """search_hybrid() runs only a dense search, with a single HTTP call, when the
    collection has no sparse vector configured."""
    fake = _Fake(sparse=False, dense_ids=("a", "b"))
    _patch(monkeypatch, fake)
    out = q.search_hybrid([0.1, 0.2], "xero", k=2)
    assert [p["id"] for _, p in out] == ["a", "b"]
    assert len([b for m, p, b in fake.seen if p.endswith("/points/search")]) == 1


def test_get_looks_a_chunk_up_by_payload_id_so_parents_can_be_expanded(monkeypatch):
    """Without this the remote store hands the brief one section where local hands it the
    whole case — a silent local/remote divergence in what the model actually reads."""
    seen = []

    def fake(method, path, body=None, timeout=60):
        """Record the call and answer a scroll-by-payload-id lookup with the parent chunk."""
        seen.append((method, path, body))
        if path.endswith("/points/scroll"):
            return {"result": {"points": [{"payload": {"id": "p1", "text": "the whole case"}}]}}
        return {"result": {}}

    monkeypatch.setattr(q, "_req", fake)
    monkeypatch.setattr(q, "collection_name", lambda: "test")
    got = q.get("p1")
    assert got["text"] == "the whole case"
    body = [b for m, p, b in seen if p.endswith("/points/scroll")][0]
    assert body["filter"]["must"][0] == {"key": "id", "match": {"value": "p1"}}


def test_get_returns_none_when_the_chunk_is_absent(monkeypatch):
    """get() returns None when the scroll finds no matching point."""
    monkeypatch.setattr(q, "_req", lambda m, p, b=None, timeout=60: {"result": {"points": []}})
    monkeypatch.setattr(q, "collection_name", lambda: "test")
    assert q.get("nope") is None


def test_the_chunk_id_is_indexed_so_parent_expansion_is_not_a_scan(monkeypatch):
    """ensure_payload_indexes() indexes the chunk id field get() filters on, alongside the
    contract's own filtered metadata fields."""
    seen = []
    monkeypatch.setattr(q, "_req", lambda m, p, b=None, timeout=60: seen.append((m, p, b)) or {"result": {}})
    monkeypatch.setattr(q, "collection_name", lambda: "test")
    q.ensure_payload_indexes()
    indexed = {b["field_name"] for m, p, b in seen if "/index" in p}
    assert "id" in indexed                       # get() filters on it
    assert "metadata.bucket" in indexed          # and the contract's own filtered fields


# ---- transient failures must not abort a migration ---------------------------
def test_connection_errors_are_retried_then_succeed(monkeypatch):
    """_req() retries a connection error and returns the eventual successful response."""
    import urllib.error
    calls = {"n": 0}

    def flaky(req, timeout=60):
        """Raise a connection error on the first two calls, then succeed."""
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.URLError("Connection refused")
        class R:
            """A context-manager response object wrapping a canned JSON body."""
            def __enter__(self):
                """Return self as the context value."""
                return self
            def __exit__(self, *a):
                """Do not suppress exceptions."""
                return False
            def read(self):
                """Return the canned successful JSON body."""
                return b'{"result": {"ok": true}}'
        return R()

    monkeypatch.setattr(q.urllib.request, "urlopen", flaky)
    monkeypatch.setattr(q._time, "sleep", lambda s: None)
    monkeypatch.setattr(q, "_cfg", lambda: ("http://x", None, "c"))
    monkeypatch.setattr(q.json, "load", lambda r: __import__("json").loads(r.read()))
    assert q._req("GET", "/x") == {"result": {"ok": True}}
    assert calls["n"] == 3


def test_a_client_error_is_not_retried_because_retrying_hides_the_bug(monkeypatch):
    """A 400 response raises immediately with no retry, since retrying a client error
    would mask a genuine bug (a malformed filter) as a transient failure."""
    import urllib.error, io, pytest
    calls = {"n": 0}

    def bad_request(req, timeout=60):
        """Always raise HTTP 400, as for a malformed request."""
        calls["n"] += 1
        raise urllib.error.HTTPError("u", 400, "Bad Request", {}, io.BytesIO(b"malformed filter"))

    monkeypatch.setattr(q.urllib.request, "urlopen", bad_request)
    monkeypatch.setattr(q._time, "sleep", lambda s: None)
    monkeypatch.setattr(q, "_cfg", lambda: ("http://x", None, "c"))
    with pytest.raises(RuntimeError, match="HTTP 400"):
        q._req("POST", "/x", {})
    assert calls["n"] == 1


def test_a_rate_limit_is_retried(monkeypatch):
    """A 429 rate-limit response is retried and the eventual successful response is
    returned."""
    import urllib.error, io
    calls = {"n": 0}

    def limited(req, timeout=60):
        """Raise HTTP 429 on the first call, then succeed."""
        calls["n"] += 1
        if calls["n"] < 2:
            raise urllib.error.HTTPError("u", 429, "Too Many", {}, io.BytesIO(b"slow down"))
        class R:
            """A context-manager response object wrapping a canned JSON body."""
            def __enter__(self):
                """Return self as the context value."""
                return self
            def __exit__(self, *a):
                """Do not suppress exceptions."""
                return False
            def read(self):
                """Return the canned successful JSON body."""
                return b'{"result": 1}'
        return R()

    monkeypatch.setattr(q.urllib.request, "urlopen", limited)
    monkeypatch.setattr(q._time, "sleep", lambda s: None)
    monkeypatch.setattr(q, "_cfg", lambda: ("http://x", None, "c"))
    monkeypatch.setattr(q.json, "load", lambda r: __import__("json").loads(r.read()))
    assert q._req("GET", "/x") == {"result": 1}
    assert calls["n"] == 2
