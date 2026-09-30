"""LocalStore dense vs hybrid on a tiny synthetic index. Run: python3 -m pytest test_store_local_hybrid.py -q"""
from __future__ import annotations
import sys, json
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import store_local  # noqa: E402


def _row(i, vec, text, doc, rq=""):
    """Build a store row with the given vector, text and optional retrieval queries."""
    return {"id": f"c{i}", "source": f"f{i}.md", "section": "S", "chunk_index": 0, "vector": vec,
            "metadata": {"source": "ipa", "doc_id": doc, "level": "child"}, "text": text, "header": "", "retrieval_queries": rq}


def _store(tmp_path, holdout=None):
    """Build a LocalStore of three fixture rows (two generic, one McCain), optionally with
    a holdout manifest, and return a fresh instance reading it back from disk."""
    st = store_local.LocalStore(tmp_path)
    st.ensure(3)
    st.replace_all([
        _row(0, [1.0, 0.0, 0.0], "generic brand strategy campaign", "generic"),
        _row(1, [0.9, 0.1, 0.0], "another generic strategy campaign text", "generic2"),
        _row(2, [0.0, 0.0, 1.0], "McCain frozen chips We are family", "mccain", rq="chips brand results"),
    ])
    man = {"chunks": 3, "dim": 3}
    if holdout is not None:
        hf = tmp_path / "holdout.json"; hf.write_text(json.dumps(holdout)); man["holdout_file"] = str(hf)
    st.manifest_path.write_text(json.dumps(man))
    return store_local.LocalStore(tmp_path)


def test_dense_alone_misses_the_brand_hybrid_finds_it(tmp_path):
    """A query whose vector points at a generic doc but whose words name a brand is missed
    by dense search alone but found by search_hybrid()."""
    st = _store(tmp_path)
    q = [1.0, 0.0, 0.0]                      # vector says "generic"; words say "McCain"
    dense = [r["metadata"]["doc_id"] for _, r in st.search(q, k=1)]
    assert dense == ["generic"]
    hybrid = [r["metadata"]["doc_id"] for _, r in st.search_hybrid(q, "McCain chips", k=1)]
    assert hybrid == ["mccain"]


def test_numpy_and_python_paths_agree(tmp_path):
    """search() gives identical ranked results whether the numpy-accelerated path or the
    pure-Python fallback is used."""
    st = _store(tmp_path)
    q = [0.7, 0.7, 0.1]
    fast = [(round(s, 5), r["id"]) for s, r in st.search(q, k=3)]
    saved = store_local._np; store_local._np = None
    try:
        slow = [(round(s, 5), r["id"]) for s, r in store_local.LocalStore(tmp_path).search(q, k=3)]
    finally:
        store_local._np = saved
    assert fast == slow


def test_where_filter_applies_to_both_sides(tmp_path):
    """search_hybrid()'s where filter is applied to both the dense and lexical results,
    even when both would otherwise agree on a doc it excludes."""
    st = _store(tmp_path)
    # vector AND words both point at McCain, but the filter excludes it (local filter = substring match)
    hits = st.search_hybrid([0.0, 0.0, 1.0], "McCain chips", k=3, where={"doc_id": "generic"})
    assert hits and all(r["metadata"]["doc_id"].startswith("generic") for _, r in hits)
    assert st.search_hybrid([1.0, 0, 0], "x", k=3, where={"doc_id": "nope"}) == []


def test_bm25_respects_holdout_from_manifest(tmp_path):
    """bm25() excludes a held-out document's retrieval-query text from the index, and
    includes it once the manifest names no holdout."""
    st = _store(tmp_path, holdout=["mccain"])
    # the RQ text "results" is only in the held-out doc's retrieval_queries: BM25 must not see it
    assert st.bm25().search("results", k=3) == []
    st2 = _store(tmp_path, holdout=[])
    assert [d for _, d in st2.bm25().search("results", k=3)] == ["c2"]


# ---- retag: metadata refresh without re-embedding ------------------------------------------
def test_retag_updates_metadata_only_when_text_is_unchanged(tmp_path, monkeypatch):
    """rag.retag() previews and then applies a metadata-only refresh (leaving vectors
    untouched) when the source text is unchanged, and refuses when the text has changed
    since a rebuild would be needed to keep the vector current."""
    import rag, chunking
    corpus = tmp_path / "corpus" / "cannes"; corpus.mkdir(parents=True)
    body = ("---\nsource: cannes\nframework_id: cannes_0001\nframework_name: \"X (2026)\"\ncategory: cannes_case\n"
            "award_tier: Gold Cannes Lions\nyear: 2026\nclient: APPLE\nsector: general\nlions_category: Film\n---\n"
            "# X (2026)\n\n## Why is this work relevant for Film?\n" + "Long enough body sentence here. " * 5 + "\n")
    (corpus / "c1.md").write_text(body)
    idx = tmp_path / "idx"
    rows = [{**c, "vector": [0.1, 0.2]} for c in chunking.chunk_corpus([corpus / "c1.md"])]
    for r in rows:                                    # simulate an index built before enrichment
        r["metadata"] = {**r["metadata"], "category": None}
    st = store_local.LocalStore(idx); st.ensure(2); st.replace_all(rows)
    st.manifest_path.write_text(json.dumps({"chunks": len(rows), "dim": 2}))

    dry = rag.retag(corpus, idx)
    assert dry["metadata_changed"] == len(rows) and dry["applied"] is False
    assert all(r["metadata"]["category"] is None for r in store_local.LocalStore(idx).scroll())

    done = rag.retag(corpus, idx, apply=True)
    assert done["metadata_changed"] == len(rows) and done["text_changed_needs_rebuild"] == 0
    after = list(store_local.LocalStore(idx).scroll())
    assert all(r["metadata"]["category"] == "technology" for r in after)
    assert all(r["vector"] == [0.1, 0.2] for r in after)          # vectors untouched

    (corpus / "c1.md").write_text(body.replace("Long enough body sentence here.", "Completely different text now."))
    assert rag.retag(corpus, idx)["text_changed_needs_rebuild"] > 0   # refuses: vector would be stale


def test_rebuilt_keyword_index_still_excludes_holdout_questions(tmp_path):
    """tune.py sweeps BM25 parameters by rebuilding the index. A rebuild that skipped the
    holdout exclusion scored every variant on the held-out questions' own text while the
    baseline was not — the defect that inflated the original sweep."""
    import json as _json
    from store_local import LocalStore
    hold = tmp_path / "holdout.json"
    hold.write_text(_json.dumps(["held"]))
    st = LocalStore(tmp_path / "ix")
    st._rows_cache = None
    rows = [{"id": "held#0", "text": "body", "retrieval_queries": "zebra quokka",
             "metadata": {"doc_id": "held"}},
            {"id": "seen#0", "text": "body", "retrieval_queries": "zebra quokka",
             "metadata": {"doc_id": "seen"}}]
    st._rows = lambda: rows
    st.manifest = lambda: {"holdout_file": str(hold)}
    assert st.holdout_ids() == {"held"}
    for params in ({}, {"b": 0.75, "k1": 1.5}, {"b": 0.1}):
        hits = [d for _, d in st.build_bm25(**params).search("zebra quokka", k=5)]
        assert hits == ["seen#0"], (params, hits)
