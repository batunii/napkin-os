"""Calibration maths and plumbing on synthetic data only: no index, no network, no model.
Run: cd engine/rag && RAG_STORE=local RAG_INDEX=./_index_v3 python3 -m pytest test_calibrate.py -q
"""
from __future__ import annotations

import json
import math
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import pytest  # noqa: E402

import calibrate as cal  # noqa: E402
from judge_base import (BackendUnavailable, Calibration, Passage, Verdict,  # noqa: E402
                        sigmoid)


# ---- synthetic data -----------------------------------------------------------------
def _platt_sample(a: float, b: float, n: int, seed: int = 1) -> tuple[list[float], list[bool]]:
    """Raws uniform on [-8, 8], labels drawn from sigmoid(a*raw + b): the model Platt
    scaling assumes, so the fit should recover (a, b)."""
    rng = random.Random(seed)
    xs = [rng.uniform(-8, 8) for _ in range(n)]
    ys = [rng.random() < sigmoid(a * x + b) for x in xs]
    return xs, ys


def _brute_auc(scores, labels) -> float:
    """O(n^2) AUC by definition, ties a half: the reference for cal.auc."""
    pos = [s for s, y in zip(scores, labels) if y]
    neg = [s for s, y in zip(scores, labels) if not y]
    return sum((p > q) + 0.5 * (p == q) for p in pos for q in neg) / (len(pos) * len(neg))


def _brute_best_f1(probs, labels) -> float:
    """Best F1 over every threshold in the data, by exhaustive search."""
    best = 0.0
    for t in set(probs):
        m = cal.metrics_at(probs, labels, t)
        best = max(best, m["f1"] or 0.0)
    return best


def _synthetic_scored(n_cases: int = 120, pool: int = 20, seed: int = 3) -> list[dict]:
    """Scored pools shaped like score_pools() output: per case the top two fused
    positions are mostly relevant with high logits, the rest mostly not."""
    rng = random.Random(seed)
    out = []
    for c in range(n_cases):
        labels = [(i < 2) != (rng.random() < 0.1) for i in range(pool)]
        raws = [rng.gauss(4.0, 2.0) if y else rng.gauss(-5.0, 2.0) for y in labels]
        out.append({"key": f"{'ipa' if c % 3 else 'playbook'}|q{c}", "query": f"q{c}",
                    "source": "ipa" if c % 3 else "playbook", "labels": labels,
                    "raws": raws, "ranks": list(range(pool))})
    return out


# ---- fit_platt ----------------------------------------------------------------------
def test_fit_platt_recovers_known_parameters():
    """Labels drawn from sigmoid(0.7x - 0.4) give back a ~ 0.7, b ~ -0.4."""
    xs, ys = _platt_sample(0.7, -0.4, 20000)
    a, b = cal.fit_platt(xs, ys)
    assert a == pytest.approx(0.7, abs=0.05)
    assert b == pytest.approx(-0.4, abs=0.08)


def test_fit_platt_stays_finite_on_separable_data():
    """Perfectly separated classes have no finite unsmoothed MLE; Platt's targets keep
    the slope finite and positive, and the probabilities short of 0 and 1."""
    xs = [-3.0, -2.0, -1.5, -1.0, 1.0, 1.5, 2.0, 3.0]
    ys = [False] * 4 + [True] * 4
    a, b = cal.fit_platt(xs, ys)
    assert math.isfinite(a) and math.isfinite(b) and a > 0
    assert 0.0 < sigmoid(a * -3 + b) < 0.5 < sigmoid(a * 3 + b) < 0.99
    # First-order conditions at the optimum: fitted probabilities match the smoothed
    # targets (5/6 for 4 positives, 1/6 for 4 negatives) in sum and in x-weighted sum.
    ts = [1 / 6] * 4 + [5 / 6] * 4
    ps = [sigmoid(a * x + b) for x in xs]
    assert sum(ps) == pytest.approx(sum(ts), abs=1e-6)
    assert sum(p * x for p, x in zip(ps, xs)) == pytest.approx(sum(t * x for t, x in zip(ts, xs)), abs=1e-6)


def test_fit_platt_reversed_backend_gets_a_negative_slope():
    """A backend whose raw score runs the wrong way is fitted, not hidden: a < 0."""
    xs, ys = _platt_sample(-0.5, 0.0, 5000, seed=4)
    a, _ = cal.fit_platt(xs, ys)
    assert a < 0


def test_fit_platt_minimises_the_smoothed_loss():
    """The fitted point has no neighbour with lower smoothed cross-entropy."""
    xs, ys = _platt_sample(1.2, 0.8, 2000, seed=5)
    n_pos = sum(ys)
    n_neg = len(ys) - n_pos
    ts = [(n_pos + 1) / (n_pos + 2) if y else 1 / (n_neg + 2) for y in ys]

    def loss(a, b):
        """Smoothed cross-entropy, written independently of calibrate.py."""
        return -sum(t * math.log(sigmoid(a * x + b)) + (1 - t) * math.log(1 - sigmoid(a * x + b))
                    for x, t in zip(xs, ts))

    a, b = cal.fit_platt(xs, ys)
    f = loss(a, b)
    for da, db in ((1e-3, 0), (-1e-3, 0), (0, 1e-3), (0, -1e-3)):
        assert loss(a + da, b + db) >= f - 1e-9


@pytest.mark.parametrize("xs,ys", [([1.0, 2.0], [True, True]), ([1.0, 2.0], [False, False]),
                                   ([], []), ([1.0], [True, False])])
def test_fit_platt_refuses_one_class_or_mismatched_input(xs, ys):
    """A single class says nothing about the other; mismatched lengths are a bug."""
    with pytest.raises(ValueError):
        cal.fit_platt(xs, ys)


# ---- threshold ----------------------------------------------------------------------
def test_best_f1_threshold_is_a_midpoint_between_data_points():
    """Clean split: positives 0.9, 0.8, negatives 0.3, 0.1 -> cut between 0.8 and 0.3."""
    t = cal.best_f1_threshold([0.9, 0.8, 0.3, 0.1], [True, True, False, False])
    assert t == pytest.approx(0.55)
    assert cal.metrics_at([0.9, 0.8, 0.3, 0.1], [True, True, False, False], t)["f1"] == 1.0


def test_best_f1_threshold_ties_pass_more():
    """Passing the 0.5 negative and the 0.4 positive together leaves F1 at 2/3 (2 TP, 1 FP,
    0 FN = 4/6 against 1 TP, 0 FP, 1 FN = 2/3): equal, so the lower cut wins."""
    probs, labels = [0.9, 0.5, 0.4, 0.1], [True, False, True, False]
    t = cal.best_f1_threshold(probs, labels)
    assert t == pytest.approx(0.25)


def test_best_f1_threshold_never_splits_tied_probabilities():
    """Equal probabilities pass or fail together: no cut can fall between them."""
    probs, labels = [0.7, 0.7, 0.7, 0.2], [True, False, True, False]
    t = cal.best_f1_threshold(probs, labels)
    assert t == pytest.approx(0.45)


def test_best_f1_threshold_matches_exhaustive_search():
    """On noisy data the chosen threshold achieves the best F1 any threshold does."""
    xs, ys = _platt_sample(0.6, -1.0, 3000, seed=9)
    probs = [sigmoid(0.6 * x - 1.0) for x in xs]
    t = cal.best_f1_threshold(probs, ys)
    assert cal.metrics_at(probs, ys, t)["f1"] == pytest.approx(_brute_best_f1(probs, ys), abs=1e-4)


def test_best_f1_threshold_all_pass_is_below_the_lowest_probability():
    """When passing everything is best, the threshold still sits below every point."""
    t = cal.best_f1_threshold([0.4, 0.2], [True, True])
    assert 0.0 <= t < 0.2


def test_best_f1_threshold_needs_a_positive():
    """F1 is undefined without positives."""
    with pytest.raises(ValueError):
        cal.best_f1_threshold([0.4, 0.2], [False, False])


def test_metrics_at_counts_and_undefined_values():
    """Known confusion counts; precision is None when nothing passes."""
    m = cal.metrics_at([0.9, 0.6, 0.4, 0.2], [True, False, True, False], 0.5)
    assert (m["tp"], m["fp"], m["fn"], m["tn"]) == (1, 1, 1, 1)
    assert (m["precision"], m["recall"], m["f1"], m["pass_rate"]) == (0.5, 0.5, 0.5, 0.5)
    empty = cal.metrics_at([0.1, 0.2], [True, False], 0.9)
    assert empty["precision"] is None and empty["recall"] == 0.0 and empty["pass_rate"] == 0.0


# ---- AUC and ECE --------------------------------------------------------------------
def test_auc_extremes_and_ties():
    """1 for perfect order, 0 for reversed, 0.5 when every score is equal."""
    ys = [False, False, True, True]
    assert cal.auc([1, 2, 3, 4], ys) == 1.0
    assert cal.auc([4, 3, 2, 1], ys) == 0.0
    assert cal.auc([1, 1, 1, 1], ys) == 0.5
    assert cal.auc([1, 2], [True, True]) is None


def test_auc_matches_brute_force_with_ties():
    """Rank-based AUC equals the pairwise definition, including tied scores."""
    rng = random.Random(11)
    xs = [round(rng.gauss(0, 1), 1) for _ in range(400)]
    ys = [rng.random() < sigmoid(2 * x) for x in xs]
    assert cal.auc(xs, ys) == pytest.approx(_brute_auc(xs, ys), abs=1e-12)


def test_ece_is_zero_when_calibrated_and_correct_when_not():
    """Bin 0.2-0.3 with p=0.25 and 1 in 4 positive is calibrated; a bin predicting 0.9
    with half positive is off by 0.4 for its share of the data."""
    probs = [0.25] * 4
    assert cal.ece(probs, [True, False, False, False]) == pytest.approx(0.0)
    probs = [0.25] * 4 + [0.9] * 4
    labels = [True, False, False, False, True, True, False, False]
    assert cal.ece(probs, labels) == pytest.approx(0.5 * 0.0 + 0.5 * 0.4)
    assert cal.ece([], []) is None


def test_ece_puts_probability_one_in_the_top_bin():
    """p = 1.0 is not an eleventh bin."""
    rel = cal.reliability([1.0, 0.95], [True, True])
    assert rel == [{"bin": "0.9-1.0", "n": 2, "mean_p": 0.975, "pos_rate": 1.0}]


def test_platt_fit_reduces_ece_on_miscalibrated_raws():
    """Logits from sigmoid(0.5x) read as if a=1 are overconfident; the fit fixes that."""
    xs, ys = _platt_sample(0.5, 0.0, 8000, seed=13)
    before = cal.ece([sigmoid(x) for x in xs], ys)
    a, b = cal.fit_platt(xs, ys)
    after = cal.ece([sigmoid(a * x + b) for x in xs], ys)
    assert after < before / 2 and after < 0.03


# ---- split ------------------------------------------------------------------------
def test_split_is_deterministic_case_level_and_about_seventy_percent():
    """Same key, same side, every time; ~70% of many keys train."""
    keys = [f"ipa|query number {i}" for i in range(4000)]
    first = [cal.in_train(k) for k in keys]
    assert first == [cal.in_train(k) for k in keys]
    assert 0.67 < sum(first) / len(first) < 0.73


def test_fit_calibration_keeps_every_pair_of_a_case_on_one_side():
    """Train pairs are exactly the pairs of the train cases: the split is by case."""
    scored = _synthetic_scored()
    d = cal.fit_calibration(scored, backend="x", fitted_on="synthetic")
    train = [p for p in scored if cal.in_train(p["key"])]
    assert d["n"] == sum(len(p["raws"]) for p in train)
    assert d["split"]["train_cases"] + d["split"]["test_cases"] == len(scored)


# ---- the JSON ---------------------------------------------------------------------
def test_fit_calibration_writes_what_the_backends_load(tmp_path):
    """The file round-trips through Calibration.from_dict and both backends' loaders,
    is provisional, names its data, and carries every metric the gate is judged on."""
    d = cal.fit_calibration(_synthetic_scored(), backend="nemotron", fitted_on="synthetic pools",
                            capacity=10, extra={"run": {"scored": 120}})
    p = tmp_path / "nemotron.json"
    cal.write_calibration(p, d)
    loaded = json.loads(p.read_text())
    c = Calibration.from_dict(loaded)
    assert c.provisional is True and c.fitted_on == "synthetic pools" and c.n == d["n"]
    assert 0.0 < c.threshold < 1.0 and c.a > 0
    for block in ("train", "test", "test_within_capacity"):
        assert set(loaded[block]["at"]) == {"0.3", "0.5", "0.7", "chosen"}
        for k in ("auc", "ece_10", "n_pos", "n_neg"):
            assert loaded[block][k] is not None
    assert loaded["test_within_capacity"]["capacity"] == 10
    assert loaded["test"]["auc"] > 0.9 and loaded["run"] == {"scored": 120}
    assert set(loaded["test_by_source"]) == {"ipa", "playbook"}
    import judge_local
    import judge_nemotron
    assert judge_nemotron.load_calibration(p).a == c.a
    assert judge_local.load_calibration(p).threshold == c.threshold
    assert not list(tmp_path.glob("*.tmp"))


def test_fit_calibration_threshold_is_best_f1_on_train_not_test():
    """The chosen threshold is the train optimum; the test block only reports it."""
    scored = _synthetic_scored(seed=21)
    d = cal.fit_calibration(scored, backend="x", fitted_on="s")
    xs, ys, _ = cal.flatten([p for p in scored if cal.in_train(p["key"])])
    probs = [sigmoid(d["a"] * x + d["b"]) for x in xs]
    assert d["train"]["at"]["chosen"]["f1"] == pytest.approx(_brute_best_f1(probs, ys), abs=1e-3)


def test_fit_calibration_refuses_a_split_without_both_classes():
    """No negatives anywhere: nothing to fit, and no file should be written."""
    scored = [{"key": f"k{i}", "source": "ipa", "labels": [True] * 3, "raws": [1.0, 2.0, 3.0],
               "ranks": [0, 1, 2]} for i in range(20)]
    with pytest.raises(ValueError):
        cal.fit_calibration(scored, backend="x", fitted_on="s")


# ---- pools and scoring through a backend -----------------------------------------
class _FakeBackend:
    """A Backend double: returns raw = len(text) per passage, or raises per call from a
    scripted list of kinds (None = succeed)."""

    name = "fake"
    capacity = 40
    calibrated = False

    def __init__(self, script=None, raw=None):
        """`script` gives, per call, None to answer or a kind to raise; `raw` overrides the
        per-passage raw function."""
        self.script = list(script or [])
        self.raw = raw or (lambda p: float(len(p.text)))
        self.seen = []

    def score(self, query, passages, *, deadline_s):
        """Record what was passed, then answer or fail as scripted."""
        self.seen.append((query, list(passages), deadline_s))
        kind = self.script.pop(0) if self.script else None
        if kind:
            raise BackendUnavailable(f"scripted {kind}", kind=kind)
        return [Verdict(value=True, score=None, why=None, backend=self.name, raw=self.raw(p))
                for p in passages]


def _pool(key="ipa|q", n=3):
    """One pool as build_pools() makes it."""
    return {"key": key, "query": key.split("|")[1], "source": "ipa", "kind": "specific",
            "accept": ["d0"], "passages": [Passage(f"c{i}", "x" * (i + 1)) for i in range(n)],
            "doc_ids": [f"d{i}" for i in range(n)], "labels": [i == 0 for i in range(n)]}


def test_build_pools_labels_by_accept_set_and_filters_by_source():
    """Passage text is header + newline + body, unclipped; label = doc_id in accept;
    the search is filtered by the case's source and asked for `pool` rows."""
    calls = []

    def search(q, k, where):
        """Fake golden._store_search callable."""
        calls.append((q, k, where))
        if q == "empty":
            return []
        return [{"id": "c1", "header": "H1", "text": "B" * 3000, "metadata": {"doc_id": "a"}},
                {"id": "c2", "header": "", "text": "t2", "metadata": {"doc_id": "z"}}]

    cases = [{"query": "q1", "source": "cannes", "kind": "specific", "expected_doc_id": "a", "accept": ["a"]},
             {"query": "empty", "source": "ipa", "kind": "specific", "expected_doc_id": "e"}]
    pools = cal.build_pools(cases, search, pool=40)
    assert calls[0] == ("q1", 40, {"source": "cannes"})
    assert len(pools) == 1
    p = pools[0]
    assert p["labels"] == [True, False] and p["key"] == "cannes|q1"
    assert p["passages"][0] == Passage("c1", "H1\n" + "B" * 3000)
    assert p["passages"][1].text == "\nt2"
    assert cal.pools_from_json(json.loads(json.dumps(cal.pools_to_json(pools)))) == pools


def test_select_cases_uses_held_out_cases_only():
    """Seen cases never enter the calibration set, whatever their kind."""
    cases = ([{"query": f"s{i}", "source": "ipa", "kind": "specific", "holdout": i % 2 == 0}
              for i in range(10)]
             + [{"query": f"p{i}", "source": "playbook", "kind": "templated", "holdout": i % 2 == 0}
                for i in range(100)])
    sel = cal.select_cases(cases, n_playbook=40)
    assert all(c["holdout"] for c in sel)
    assert sum(c["kind"] == "specific" for c in sel) == 5
    assert sum(c["source"] == "playbook" for c in sel) == 40
    assert sel == cal.select_cases(cases, n_playbook=40)


def test_score_pools_passes_passages_unchanged_and_keeps_order():
    """The backend receives the full Passage objects (its own clipping applies) and the
    raws come back in input order."""
    b = _FakeBackend()
    scored, stats = cal.score_pools(b, [_pool(n=4)], deadline_s=7.0, sleep=lambda s: None)
    assert b.seen[0][1] == _pool(n=4)["passages"] and b.seen[0][2] == 7.0
    assert b.seen[0][0].text == "q" and b.seen[0][0].context == ""
    assert scored[0]["raws"] == [1.0, 2.0, 3.0, 4.0] and scored[0]["ranks"] == [0, 1, 2, 3]
    assert stats["scored"] == 1 and stats["skipped"] == {}


def test_score_pools_skips_and_counts_timeouts_and_backs_off():
    """A timeout skips that case, is counted, and lengthens the next pause."""
    b = _FakeBackend(script=[None, "timeout", None])
    pauses = []
    scored, stats = cal.score_pools(b, [_pool(f"ipa|q{i}") for i in range(3)], deadline_s=1,
                                    pause_s=0.1, sleep=pauses.append)
    assert [p["key"] for p in scored] == ["ipa|q0", "ipa|q2"]
    assert stats["skipped"] == {"timeout": 1} and stats["attempted"] == 3
    assert pauses == [0.1, 2.0]


def test_score_pools_stops_on_a_retired_model_or_a_run_of_failures():
    """Waiting cannot fix `retired`; a long run of timeouts means the endpoint is down."""
    scored, stats = cal.score_pools(_FakeBackend(script=["retired"]), [_pool(f"ipa|q{i}") for i in range(5)],
                                    deadline_s=1, sleep=lambda s: None)
    assert scored == [] and stats["aborted"] == 4
    n = cal.MAX_CONSECUTIVE_FAILURES
    scored, stats = cal.score_pools(_FakeBackend(script=["timeout"] * (n + 5)),
                                    [_pool(f"ipa|q{i}") for i in range(n + 3)],
                                    deadline_s=1, sleep=lambda s: None)
    assert stats["skipped"] == {"timeout": n} and stats["aborted"] == 3


def test_score_pools_treats_a_misaligned_or_rawless_answer_as_bad_response():
    """Short output and a missing raw are skipped as bad_response, never attached."""
    class Short(_FakeBackend):
        """Drops the last verdict."""

        def score(self, query, passages, *, deadline_s):
            """Answer one passage short."""
            return super().score(query, passages, deadline_s=deadline_s)[:-1]

    _, stats = cal.score_pools(Short(), [_pool()], deadline_s=1, sleep=lambda s: None)
    assert stats["skipped"] == {"bad_response": 1}
    _, stats = cal.score_pools(_FakeBackend(raw=lambda p: None), [_pool()], deadline_s=1,
                               sleep=lambda s: None)
    assert stats["skipped"] == {"bad_response": 1}


def test_check_environment_refuses_the_shared_store_and_offline_embeddings():
    """Calibration never runs against Qdrant or with hash embeddings."""
    assert cal.check_environment({"RAG_STORE": "local"}) == []
    assert cal.check_environment({}) == []
    assert len(cal.check_environment({"RAG_STORE": "qdrant"})) == 1
    assert len(cal.check_environment({"RAG_STORE": "local", "RAG_EMBED": "offline"})) == 1
