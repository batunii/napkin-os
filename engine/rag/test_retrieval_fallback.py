"""When the RAG store is unreachable, Loops 3-7 fall back to the pack digests. That must be
loud: a warning on stderr and a `fallback` record in the result, because on 2026-09-24 a
whole comparison run quietly used digests and nothing in its output said so.
Run: cd engine/rag && python3 -m pytest -q test_retrieval_fallback.py
"""
from __future__ import annotations
import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import parse_brief as pb  # noqa: E402


class _Retriever:
    """A retriever whose store is down."""
    @staticmethod
    def index_available(index_dir=None):
        """Test stub: stands in for `index_available` in a retriever whose store is down."""
        return False

    @staticmethod
    def index_label(index_dir=None):
        """Test stub: stands in for `index_label` in a retriever whose store is down."""
        return "qdrant:unavailable (ConnectError)"


def test_digest_fallback_is_announced_and_recorded(monkeypatch, capsys):
    """Store down -> digests are used, stderr says so, and the result carries the reason."""
    monkeypatch.setattr(pb, "_load_retriever", lambda: _Retriever)
    monkeypatch.setattr(pb, "_loops37_from_digests",
                        lambda loop2, fields, synthesize=True: {"enabled": True, "index": "digests:packs_dist"})
    out = pb.loops_3_7({}, {}, synthesize=False)
    assert out["fallback"] == {"to": "digests", "reason": "RAG store unavailable: qdrant:unavailable (ConnectError)"}
    assert "RAG store unavailable" in capsys.readouterr().err


def test_no_digests_either_is_a_disabled_stub(monkeypatch, capsys):
    """Store down and no digests -> the disabled stub, still announced."""
    monkeypatch.setattr(pb, "_load_retriever", lambda: _Retriever)
    monkeypatch.setattr(pb, "_loops37_from_digests", lambda loop2, fields, synthesize=True: None)
    out = pb.loops_3_7({}, {}, synthesize=False)
    assert out["enabled"] is False
    assert "RAG store unavailable" in capsys.readouterr().err


def test_a_brief_without_precedent_retrieval_says_so_or_stops(monkeypatch, tmp_path):
    """EC-048: a missing store is recorded in meta.degraded; BRIEF_REQUIRE_STORE=1 stops the run."""
    import parse_brief as pb
    import pytest
    src = (pb.HERE / "parse_brief.py").read_text()
    assert 'out["meta"]["degraded"]' in src and "BRIEF_REQUIRE_STORE" in src
    monkeypatch.setenv("BRIEF_REQUIRE_STORE", "1")
    monkeypatch.setattr(pb, "loops_3_7", lambda *a, **k: {"enabled": True, "fallback": {"to": "digests", "reason": "RAG store unavailable: x"}})
    monkeypatch.setattr(pb, "_json_call", lambda *a, **k: {})
    with pytest.raises(RuntimeError, match="BRIEF_REQUIRE_STORE=1 and RAG store unavailable"):
        pb.run(None, loops37=True, golden=False, raw_text="A brand wants a campaign in Ireland.", source_name="t")
