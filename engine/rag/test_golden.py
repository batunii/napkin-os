"""Golden-set generator + scorer. Fixtures inline; no corpus needed."""
from __future__ import annotations
import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import chunking  # noqa: E402
import golden  # noqa: E402

LONG = "This sentence is here so the section clears the minimum word count for a chunk. " * 2


def test_rq_lines_take_bullets_and_drop_short():
    """rq_lines() extracts bulleted lines as queries and drops ones too short to be real."""
    assert golden.rq_lines("- What insight worked for FMCG?\n* Examples of humour\n- ok\n\nHow did brands win?") == \
        ["What insight worked for FMCG?", "Examples of humour", "How did brands win?"]


def test_holdout_is_deterministic_and_roughly_one_in_five():
    """is_holdout() gives the same answer for the same id across calls, and holds out
    roughly a fifth of ids overall."""
    ids = [f"ipa_{i:04d}" for i in range(2000)]
    a = {i for i in ids if golden.is_holdout(i)}
    b = {i for i in ids if golden.is_holdout(i)}
    assert a == b
    assert 0.15 < len(a) / len(ids) < 0.25


def test_cases_for_ipa_file_and_holdout_strips_rq_from_embedding(tmp_path):
    """cases_for_file() builds one golden case per retrieval query with the right expected
    doc, source, bucket and category; and when a doc is in the RQ holdout its retrieval
    queries are excluded from the embedding text while the rest of the chunk text remains."""
    f = tmp_path / "ipa" / "ipa_0001.md"; f.parent.mkdir()
    f.write_text("---\nsource: ipa\nframework_id: ipa_0001\nframework_name: Chips\ncategory: ipa_effectiveness_case\n"
                 f"year: 2024\nsector: Food & Drink\naward_tier: Gold\n---\n# Chips\n\n## Insight\n{LONG}\n\n"
                 "## Retrieval Queries\n- What insight worked for Food & Drink brands?\n- IPA Gold case study Food & Drink 2024\n")
    cases = golden.cases_for_file(f)
    assert [c["query"] for c in cases] == ["What insight worked for Food & Drink brands?", "IPA Gold case study Food & Drink 2024"]
    assert all(c["expected_doc_id"] == "ipa_0001" and c["source"] == "ipa" and c["bucket"] == "exemplars"
               and c["category"] == "food_drink" for c in cases)
    chunk = chunking.chunk_file(f)[0]
    assert "What insight worked" in chunking.embed_text_of(chunk)
    chunking.RQ_HOLDOUT = {"ipa_0001"}
    try:
        assert "What insight worked" not in chunking.embed_text_of(chunk)
        assert chunk["text"] in chunking.embed_text_of(chunk)
    finally:
        chunking.RQ_HOLDOUT = set()


def test_dandad_cases_point_at_group(tmp_path):
    """cases_for_dandad() points every case at the shared discipline/year group id, not at
    an individual file."""
    d = tmp_path / "dandad"; d.mkdir()
    for n, t in ((1, "Alpha"), (2, "Beta")):
        (d / f"dandad_{n:04d}.md").write_text(
            f"---\nsource: dandad\nframework_id: dandad_{n:04d}\nframework_name: \"{t} (2026)\"\ncategory: dandad_case\n"
            f"award_tier: WOOD Pencil\nyear: 2026\nsector: Typography\n---\n# {t} (2026)\n\n## Overview\n{LONG}\n\n"
            f"## Retrieval Queries\n- Award-winning Typography work like {t}\n")
    cases = golden.cases_for_dandad(sorted(d.glob("*.md")))
    assert len(cases) == 2 and {c["expected_doc_id"] for c in cases} == {"dandad:typography:2026"}


def test_evaluate_scores_recall_per_group():
    """evaluate() reports overall recall@k plus per-split and per-source breakdowns, and
    lists every missed case."""
    cases = [{"query": "q1", "expected_doc_id": "A", "source": "ipa", "bucket": "exemplars", "holdout": False},
             {"query": "q2", "expected_doc_id": "B", "source": "ipa", "bucket": "exemplars", "holdout": True},
             {"query": "q3", "expected_doc_id": "C", "source": "playbook", "bucket": "craft", "holdout": False}]
    ranked = {"q1": ["A", "X"], "q2": ["X", "Y", "Z", "W", "V", "B"], "q3": ["X", "Y"]}
    def search(q, k, where):
        """Fake search returning the pre-ranked doc ids for q, filtered to k, asserting the
        expected source filter was passed."""
        assert where == {"source": next(c["source"] for c in cases if c["query"] == q)}
        return [{"metadata": {"doc_id": d}} for d in ranked[q][:k]]
    rep = golden.evaluate(cases, search, ks=(5, 10))
    g = rep["groups"]
    assert g["all"] == {"n": 3, "recall@5": 0.333, "recall@10": 0.667}
    assert g["split/holdout"]["recall@5"] == 0.0 and g["split/holdout"]["recall@10"] == 1.0
    assert g["source/playbook"]["recall@10"] == 0.0
    assert rep["misses"][0]["expected"] == "C"


def test_select_cases_filters_and_caps():
    """select_cases() filters to the requested sources, and per_source caps how many cases
    of each source are kept."""
    cases = [{"source": s, "i": i} for s in ("ipa", "dandad") for i in range(5)]
    assert len(golden.select_cases(cases, sources=["ipa"])) == 5
    capped = golden.select_cases(cases, per_source=2)
    assert [c["source"] for c in capped] == ["ipa", "ipa", "dandad", "dandad"]


def test_templated_queries_accept_any_doc_with_the_mentioned_facets():
    """attach_acceptable() accepts any doc sharing a templated query's mentioned facets, but
    a specific query or one that mentions no facet only accepts its own doc."""
    cases = [
        {"query": "Examples of Reframing strategy achieving Brand Building", "kind": "templated", "expected_doc_id": "a",
         "source": "ipa", "facets": {"sector": "Telecoms", "effectiveness_type": "Brand Building", "strategic_territory": "Reframing", "client": "BT"}},
        {"query": "IPA Gold case study Retail 2010", "kind": "templated", "expected_doc_id": "b",
         "source": "ipa", "facets": {"sector": "Retail", "effectiveness_type": "Brand Building", "strategic_territory": "Reframing", "award_tier_raw": "Gold", "year": "2010"}},
        {"query": "BT Big Idea", "kind": "specific", "expected_doc_id": "a", "source": "ipa",
         "facets": {"sector": "Telecoms", "effectiveness_type": "Brand Building", "strategic_territory": "Reframing", "client": "BT"}},
        {"query": "whatever", "kind": "templated", "expected_doc_id": "c", "source": "ipa",
         "facets": {"sector": "Retail", "effectiveness_type": "Turnaround", "strategic_territory": "Humor"}},
    ]
    golden.attach_acceptable(cases)
    assert cases[0]["accept"] == ["a", "b"]          # both Reframing + Brand Building
    assert cases[1]["accept"] == ["b"]               # only b is Gold Retail 2010
    assert cases[2]["accept"] == ["a"]               # specific -> own doc only
    assert cases[3]["accept"] == ["c"]               # mentions no facet -> own doc only
    hits = [{"metadata": {"doc_id": "b"}}]
    assert golden.recall_at(hits, cases[0]["accept"], 5) == 1 and golden.recall_at(hits, cases[2]["accept"], 5) == 0


def test_specific_query_is_client_plus_title(tmp_path):
    """The specific-kind case query is built from client plus title, and its facets carry
    the raw award tier and sector."""
    f = tmp_path / "ipa" / "ipa_0001.md"; f.parent.mkdir()
    f.write_text("---\nsource: ipa\nframework_id: ipa_0001\nframework_name: \"When the chips are down (2024)\"\ncategory: ipa_effectiveness_case\n"
                 f"year: 2024\nsector: Food & Drink\nclient: McCain Foods Ltd\naward_tier: Grand Prix\n---\n# Chips\n\n## Insight\n{LONG}\n\n"
                 "## Retrieval Queries\n- IPA Grand Prix case study Food & Drink 2024\n")
    cases = golden.cases_for_file(f)
    kinds = {c["kind"]: c["query"] for c in cases}
    assert kinds["specific"] == "McCain Foods Ltd When the chips are down"
    assert cases[0]["facets"]["award_tier_raw"] == "Grand Prix" and cases[0]["facets"]["sector"] == "Food & Drink"
