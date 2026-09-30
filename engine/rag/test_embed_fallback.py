"""Query-embedding fallback: hosted -> local copy of the same model -> keyword-only.
Offline: the hosted call and the local model are faked.
Run: cd engine/rag && RAG_STORE=local RAG_INDEX=./_index_v4 python3 -m pytest -q test_embed_fallback.py
"""
from __future__ import annotations
import sys
import types
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import pytest  # noqa: E402
import rag  # noqa: E402


def _down(*a, **k):
    """A hosted endpoint that fails."""
    raise OSError("connection refused")


@pytest.fixture
def hosted_down(monkeypatch):
    """Hosted embedding fails; a key is present so the hosted path is attempted."""
    monkeypatch.setenv("NVIDIA_API_KEY", "k")
    monkeypatch.delenv("RAG_EMBED", raising=False)
    monkeypatch.setattr(rag, "_nim_embed", _down)
    monkeypatch.setattr(rag, "EMBED_FALLBACK", ["local", "keyword"])


def _fake_local(monkeypatch, fail=False):
    """Install a fake embed_local module."""
    m = types.ModuleType("embed_local")
    m.MODEL = "fake/embed"
    def embed(texts, input_type="passage"):
        """Unit vectors, or a failure."""
        if fail:
            raise RuntimeError("no weights")
        return [[1.0, 0.0] for _ in texts]
    m.embed = embed
    monkeypatch.setitem(sys.modules, "embed_local", m)


def test_query_falls_back_to_the_local_model(hosted_down, monkeypatch):
    """Hosted down: the same model locally answers, and the mode says so."""
    _fake_local(monkeypatch)
    vecs, mode = rag.embed(["q"], "query")
    assert mode == "local:fake/embed" and vecs == [[1.0, 0.0]]


def test_passages_never_fall_back(hosted_down, monkeypatch):
    """An index build must not mix vectors from another source: passages re-raise."""
    _fake_local(monkeypatch)
    with pytest.raises(OSError):
        rag.embed(["doc"], "passage")


def test_both_down_gives_keyword_only(hosted_down, monkeypatch):
    """Hosted and local both fail: embed_query returns None, search_vec goes lexical."""
    _fake_local(monkeypatch, fail=True)
    assert rag.embed_query("q") is None

    class Store:
        """Records a lexical search."""
        def search_lexical(self, q, k=5, where=None):
            """Return one fake keyword hit."""
            return [(1.0, {"id": "kw", "q": q})]
    assert rag.search_vec(Store(), None, "challenger brand", k=3) == [(1.0, {"id": "kw", "q": "challenger brand"})]


def test_keyword_fallback_can_be_disabled(hosted_down, monkeypatch):
    """Without `keyword` in RAG_EMBED_FALLBACK, a total failure raises instead."""
    _fake_local(monkeypatch, fail=True)
    monkeypatch.setattr(rag, "EMBED_FALLBACK", ["local"])
    with pytest.raises(rag.EmbedUnavailable):
        rag.embed_query("q")
