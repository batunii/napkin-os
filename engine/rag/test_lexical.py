"""BM25 + RRF unit tests. Run: cd engine/rag && python3 -m pytest test_lexical.py -q"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from lexical import BM25, rrf, tokenize  # noqa: E402


def test_tokenize_keeps_brand_tokens_and_digits():
    assert tokenize("McCain 'We are family' (2024) — adam&eveDDB, FCB Grid") == \
        ["mccain", "we", "are", "family", "2024", "adam", "eveddb", "fcb", "grid"]


DOCS = [
    ("mccain", "McCain frozen chips We are family campaign sustained success price premium"),
    ("xero", "Xero UK accountants small business software trust B2B launch"),
    ("fcb", "FCB Grid think feel involvement matrix message strategy Vaughn 1980"),
    ("generic", "brand strategy campaign success message"),
]


def test_bm25_exact_brand_token_wins():
    idx = BM25(DOCS)
    assert idx.search("McCain chips", k=2)[0][1] == "mccain"
    assert idx.search("FCB grid", k=1)[0][1] == "fcb"
    assert idx.search("xero accountants", k=1)[0][1] == "xero"


def test_bm25_rare_tokens_outweigh_common_ones():
    idx = BM25(DOCS)
    # "campaign" appears in two docs, "Vaughn" in one: the rare token should dominate
    top = idx.search("campaign Vaughn", k=1)[0][1]
    assert top == "fcb"


def test_bm25_allowed_restricts_results():
    idx = BM25(DOCS)
    assert [d for _, d in idx.search("campaign", k=5, allowed={"generic"})] == ["generic"]
    assert idx.search("nonexistenttoken", k=5) == []


def test_rrf_rewards_agreement_and_top_ranks():
    fused = rrf([["a", "b", "c"], ["b", "a", "d"]])
    order = [d for _, d in fused]
    assert order[:2] == ["a", "b"] or order[:2] == ["b", "a"]     # both agree a,b are top
    assert order.index("c") > order.index("a") and order.index("d") > order.index("b")
    # The two regimes, both now meaningful since the default moved from 60 to 10.
    lists = [["solo"] + [f"x{i}" for i in range(40)], [f"y{i}" for i in range(30)] + ["x20"]]
    # large k flattens the curve: a doc in BOTH lists beats one ranked first in only one
    assert rrf(lists, k=60)[0][1] == "x20"
    # the tuned default is sharper: a confident top rank wins
    assert rrf(lists, k=10)[0][1] == "solo"
    assert rrf(lists)[0][1] == "solo"


def test_rrf_k_controls_how_much_a_top_rank_is_worth():
    # small k: rank 1 in one list beats mid-table agreement in both
    small = [d for _, d in rrf([["solo"] + [f"x{i}" for i in range(40)],
                                [f"y{i}" for i in range(30)] + ["x20"]], k=1)]
    assert small[0] == "solo"
    # large k flattens the curve, so agreement wins
    large = [d for _, d in rrf([["solo"] + [f"x{i}" for i in range(40)],
                                [f"y{i}" for i in range(30)] + ["x20"]], k=60)]
    assert large[0] == "x20"


def test_rrf_weights_let_one_retriever_count_for_more():
    even = [d for _, d in rrf([["a"], ["b"]])]
    assert set(even[:2]) == {"a", "b"}
    tilted = [d for _, d in rrf([["a"], ["b"]], weights=[3.0, 1.0])]
    assert tilted[0] == "a"


def test_tuned_defaults_are_locked_to_the_measured_winners():
    """These two values were chosen by sweeping the golden set, not by convention. If
    someone restores the textbook defaults, held-out recall@5 drops from 0.974 to 0.906,
    so the change should be deliberate and re-measured rather than tidy-looking."""
    import inspect
    assert inspect.signature(BM25.__init__).parameters["b"].default == 0.3
    assert inspect.signature(BM25.__init__).parameters["k1"].default == 1.5
    assert inspect.signature(rrf).parameters["k"].default == 10
