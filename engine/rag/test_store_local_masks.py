"""The local store's vectorised filter returns exactly the rows filters.matches() does,
for every operator, absent fields, numbers vs strings and lists; results are cached per
filter and dropped on write; get_store keeps one local store per process and picks up a
rebuilt index (2026-09-26, retrieval hole). Offline, tiny temp indexes.
Run: cd engine/rag && python3 -m pytest -q test_store_local_masks.py
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import filters  # noqa: E402
import store_base  # noqa: E402
from store_local import LocalStore  # noqa: E402

VALUES = {"bucket": ["exemplars", "craft", "rules", None],
          "status": ["current", "superseded", None],
          "stage": ["brief", "production", None],
          "scope": ["global", "brand:bmw", "category:automotive", None],
          "category": ["automotive", "fmcg", 3, "3", None],
          "as_of": ["2019-05-01", "2021-01-01", "2024-07-09", None]}


def _index(tmp_path: Path, n: int = 400, seed: int = 7) -> LocalStore:
    """A temp index of n rows with random metadata (None = field absent)."""
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        md = {k: rng.choice(v) for k, v in VALUES.items()}
        md = {k: v for k, v in md.items() if v is not None}
        if i % 17 == 0:
            md["tags"] = ["a", "b"]                      # unhashable value: str() form
        rows.append({"id": f"c{i}", "text": f"t{i}", "vector": [rng.random(), rng.random()], "metadata": md})
    st = LocalStore(tmp_path)
    st.replace_all(rows)
    return st


FILTERS = [
    {"bucket": "craft"},
    {"bucket": "craft", "status": {"ne": "superseded"}, "stage": {"ne": "production"},
     "scope": {"in": ["global", "brand:bmw"]}},
    {"category": 3}, {"category": "3"}, {"category": {"in": [3, "fmcg"]}},
    {"scope": {"nin": ["global"]}}, {"status": {"exists": True}}, {"status": {"exists": False}},
    {"as_of": {"gte": "2021-01-01"}}, {"as_of": {"lt": "2021-01-01"}, "bucket": "rules"},
    {"tags": "['a', 'b']"}, {"bucket": "nothing-has-this"}, {"scope": {"in": []}},
    {"bucket": {"in": ["exemplars", "unknown"]}, "category": {"ne": "automotive"}},
]


def test_masks_match_the_loop_exactly(tmp_path):
    """Every filter: same row indexes, same order, as the matches() loop."""
    st = _index(tmp_path)
    rows = st._rows()
    for where in FILTERS:
        want = [i for i, r in enumerate(rows) if filters.matches(r.get("metadata") or {}, where)]
        assert st._filtered(where) == want, where
        assert st._filtered(where) == want, f"cached: {where}"


def test_bad_filter_still_raises(tmp_path):
    """A malformed filter is a FilterError, not an empty result."""
    st = _index(tmp_path, n=5)
    try:
        st._filtered({"bucket": {"in": "craft"}})
    except filters.FilterError:
        pass
    else:
        raise AssertionError("FilterError expected")


def test_write_drops_the_filter_caches(tmp_path):
    """After a write the columns and cached results are rebuilt from the new rows."""
    st = _index(tmp_path, n=50)
    before = st._filtered({"bucket": "craft"})
    st.upsert([{"id": "new", "text": "x", "vector": [1.0, 0.0], "metadata": {"bucket": "craft"}}])
    after = st._filtered({"bucket": "craft"})
    assert len(after) == len(before) + 1 and st._rows()[after[-1]]["id"] == "new"


def test_search_results_unchanged(tmp_path):
    """Dense and hybrid search over a filter return the same ids as before the change
    would (checked against a brute-force ranking of the loop's rows)."""
    st = _index(tmp_path)
    where = FILTERS[1]
    rows = st._rows()
    idxs = [i for i, r in enumerate(rows) if filters.matches(r["metadata"], where)]
    q = [0.3, 0.9]
    brute = sorted(idxs, key=lambda i: -(rows[i]["vector"][0] * q[0] + rows[i]["vector"][1] * q[1]))[:5]
    assert [r["id"] for _, r in st.search(q, 5, where)] == [rows[i]["id"] for i in brute]
    assert all(filters.matches(r["metadata"], where) for _, r in st.search_hybrid(q, "t1", 5, where))


def test_one_local_store_per_process(tmp_path, monkeypatch):
    """get_store returns the same instance for the same unchanged index, a new one when
    the file changes, and a fresh one each time with RAG_STORE_MEMO=0."""
    monkeypatch.setattr(store_base, "_MEMO", {})
    _index(tmp_path, n=10)
    a = store_base.get_store("local", index_dir=tmp_path)
    assert store_base.get_store("local", index_dir=tmp_path) is a
    LocalStore(tmp_path).upsert([{"id": "z", "text": "z", "vector": [0.0, 1.0], "metadata": {}}])
    b = store_base.get_store("local", index_dir=tmp_path)
    assert b is not a and b.count() == 11
    monkeypatch.setenv("RAG_STORE_MEMO", "0")
    assert store_base.get_store("local", index_dir=tmp_path) is not store_base.get_store("local", index_dir=tmp_path)


def test_missing_index_is_not_memoised(tmp_path, monkeypatch):
    """An index folder with no chunks file yet gives a fresh store each call."""
    monkeypatch.setattr(store_base, "_MEMO", {})
    d = tmp_path / "empty"
    assert store_base.get_store("local", index_dir=d) is not store_base.get_store("local", index_dir=d)
    assert json.dumps(store_base._MEMO) == "{}"


def test_cold_store_is_built_once_under_parallel_searches(tmp_path, monkeypatch):
    """Eight threads searching a cold store read the file once and build BM25 once."""
    import threading
    st = _index(tmp_path, n=60)
    fresh = LocalStore(tmp_path)
    reads, builds = [], []
    real_open, real_build = open, fresh.build_bm25
    monkeypatch.setattr("builtins.open", lambda f, *a, **k: (reads.append(str(f)) if str(f).endswith("chunks.jsonl") else None) or real_open(f, *a, **k))
    fresh.build_bm25 = lambda **k: builds.append(1) or real_build(**k)
    ts = [threading.Thread(target=fresh.search_hybrid, args=([0.5, 0.5], "t3", 3, {"bucket": "craft"})) for _ in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert len(reads) == 1 and len(builds) == 1 and fresh.count() == st.count()


def test_warm_builds_rows_matrix_and_bm25(tmp_path):
    """warm() leaves nothing lazy for the first search, and warm_store never raises."""
    import brief_context
    st = _index(tmp_path, n=20)
    w = st.warm()
    assert w["rows"] == 20 and st._mat is not None and st._bm25 is not None
    assert brief_context.warm_store(tmp_path / "does-not-exist") in ({}, {"rows": 0, "secs": 0.0})
