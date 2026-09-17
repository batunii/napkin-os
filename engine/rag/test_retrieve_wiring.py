"""The pipeline's retrieval surface: brief-safe defaults, the brief_context entry point,
and the grounding check. Run: python3 -m pytest test_retrieve_wiring.py -q
"""
from __future__ import annotations
import json, sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import store_local  # noqa: E402
import retrieve  # noqa: E402
import filters as F  # noqa: E402


def _row(i, doc, vec, text, **md):
    return {"id": f"c{i}", "source": f"{doc}.md", "section": "S", "chunk_index": i, "vector": vec,
            "text": text, "header": "", "retrieval_queries": "",
            "metadata": {"doc_id": doc, "level": "parent", "source": "ipa", **md}}


def _local(monkeypatch):
    """engine/.env sets RAG_STORE=qdrant and rag.py loads it, so every store call would
    otherwise go over the network. Tests pin the local store explicitly."""
    monkeypatch.setenv("RAG_STORE", "local")
    monkeypatch.setattr(retrieve.rag, "embed", lambda t, i="query": ([[1.0, 0.0]], "stub"))


def _index(tmp_path):
    st = store_local.LocalStore(tmp_path); st.ensure(2)
    st.replace_all([
        _row(0, "keep", [1.0, 0.0], "a strategy case about challenger brands", status="active"),
        _row(1, "dandad_group", [1.0, 0.0], "a design craft reference", source="dandad", stage="production"),
        _row(2, "overruled", [1.0, 0.0], "a superseded finding", status="superseded"),
        _row(3, "legacy", [1.0, 0.0], "an older chunk written before these fields existed"),
    ])
    st.manifest_path.write_text(json.dumps({"chunks": 4, "dim": 2}))
    return tmp_path


def test_brief_safe_hides_production_and_superseded_but_keeps_legacy_chunks(tmp_path, monkeypatch):
    _local(monkeypatch); idx = _index(tmp_path)
    got = {h["doc_id"] for h in retrieve.retrieve("challenger", k=10, index_dir=idx)}
    assert got == {"keep", "legacy"}          # legacy has neither field and must survive
    allp = {h["doc_id"] for h in retrieve.retrieve("challenger", k=10, index_dir=idx, brief_safe=False)}
    assert allp == {"keep", "legacy", "dandad_group", "overruled"}


def test_an_explicit_filter_still_wins_over_the_safe_default(tmp_path, monkeypatch):
    _local(monkeypatch); idx = _index(tmp_path)
    got = {h["doc_id"] for h in retrieve.retrieve("x", k=10, index_dir=idx,
                                                  where={"stage": "production"})}
    assert got == {"dandad_group"}            # the production clan can ask for its own material


def test_brief_safe_narrows_nothing_that_predates_the_fields():
    assert F.matches({"source": "ipa"}, retrieve.BRIEF_SAFE)


# ---- grounding check ---------------------------------------------------------
def test_grounding_flags_invented_citations():
    allowed = {"ipa_0409", "pb_fcb-grid#process"}
    good = retrieve.check_grounding("Pre-selling worked for [ipa_0409].", allowed)
    assert good["grounded"] and good["cited"] == ["ipa_0409"] and not good["invented"]

    bad = retrieve.check_grounding("As shown in [ipa_0999] and [ipa_0409].", allowed)
    assert not bad["grounded"] and bad["invented"] == ["ipa_0999"]
    assert bad["cited"] == ["ipa_0999", "ipa_0409"]


def test_grounding_notices_a_confident_paragraph_with_no_citation_at_all():
    r = retrieve.check_grounding("Award-winning launches always pre-sell.", {"ipa_0409"})
    assert r["uncited"] and not r["grounded"] and r["cited"] == []


def test_grounding_handles_section_suffixes_and_repeats():
    allowed = {"pb_fcb-grid#process"}
    r = retrieve.check_grounding("See [pb_fcb-grid#process], and again [pb_fcb-grid#process].", allowed)
    assert r["cited"] == ["pb_fcb-grid#process"] and r["grounded"]


def test_grounding_accepts_a_context_object_citations_map():
    import brief_context as bc
    hit = bc.Hit(cite="ipa_0409", doc_id="ipa_0409", source="ipa", bucket="exemplars",
                 title="t", section="s", header="h", text="x", score=1.0)
    ctx = bc.BriefContext(blocks={"exemplars": bc.Block("exemplars", [hit], 100)},
                          query="q", keywords=[], filters={})
    assert retrieve.check_grounding("[ipa_0409] supports this.", ctx.citations())["grounded"]
