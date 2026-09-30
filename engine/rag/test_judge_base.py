"""The shared validation-backend contract.
Run: cd engine/rag && python3 -m pytest test_judge_base.py -q
"""
from __future__ import annotations
import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import pytest  # noqa: E402
import judge_base as jb  # noqa: E402


def test_score_must_be_a_probability_not_a_logit():
    """A logit passed as `score` is refused; None is allowed."""
    with pytest.raises(ValueError):
        jb.Verdict(value=True, score=7.4, why=None, backend="x")
    assert jb.Verdict(value=True, score=None, why=None, backend="x").score is None


def test_unknown_unavailable_kind_collapses_to_error():
    """An unknown failure kind becomes 'error'; known kinds and the status are kept."""
    assert jb.BackendUnavailable("x", kind="nonsense").kind == "error"
    e = jb.BackendUnavailable("gone", kind="retired", status=410)
    assert (e.kind, e.status) == ("retired", 410)


def test_query_context_is_truncated_before_the_query():
    """combined() clips context before it ever clips the query itself."""
    q = jb.Query(text="QUERY", context="C" * 100)
    assert q.combined(max_chars=10) == "QUERY\n\nBri"
    assert jb.Query(text="only").combined() == "only"


def test_calibration_maps_logits_monotonically_and_keeps_unknown_keys():
    """Platt probabilities rise with the logit, and extra JSON keys survive in `extra`."""
    c = jb.Calibration.from_dict({"a": 0.5, "b": -1.0, "threshold": 0.4, "fitted_on": "g",
                                  "n": 10, "provisional": True, "auc": 0.98})
    assert c.probability(-10) < c.probability(0) < c.probability(10)
    assert c.extra == {"auc": 0.98}


def test_sigmoid_is_safe_at_extremes():
    """No overflow at +/-1000."""
    assert jb.sigmoid(1000) == 1.0 and jb.sigmoid(-1000) == 0.0


def test_check_verdicts_refuses_short_or_mislabelled_output():
    """A short list or a verdict labelled with another backend is bad_response."""
    ps = [jb.Passage("a", "x"), jb.Passage("b", "y")]
    ok = [jb.Verdict(True, None, None, "n"), jb.Verdict(False, None, None, "n")]
    assert jb.check_verdicts("n", ps, ok) == ok
    with pytest.raises(jb.BackendUnavailable) as e:
        jb.check_verdicts("n", ps, ok[:1])
    assert e.value.kind == "bad_response"
    with pytest.raises(jb.BackendUnavailable):
        jb.check_verdicts("n", ps, [ok[0], jb.Verdict(False, None, None, "other")])


def test_a_conforming_class_satisfies_the_protocol():
    """Any class with the three attributes and score() is a Backend."""
    class B:
        """Minimal conforming backend."""
        name, capacity, calibrated = "b", 4, False
        def score(self, query, passages, *, deadline_s):
            """Pass everything."""
            return [jb.Verdict(True, None, None, "b") for _ in passages]
    assert isinstance(B(), jb.Backend)


# ---- shared loader, classifier, contract shape ------------------------------------
import json  # noqa: E402


def _cal_file(tmp_path, **cfg):
    """A calibration file recording `cfg` as the backend_config it was fitted with."""
    p = tmp_path / "cal.json"
    p.write_text(json.dumps({"a": 0.5, "b": -0.1, "threshold": 0.4, "backend_config": cfg}))
    return p


def test_load_calibration_absent_is_uncalibrated_and_junk_is_a_hard_error(tmp_path):
    """No file: None. An unreadable file: BackendNotConfigured, never silently uncalibrated."""
    assert jb.load_calibration(tmp_path / "none.json", backend="x") is None
    bad = tmp_path / "bad.json"; bad.write_text("{not json")
    with pytest.raises(jb.BackendNotConfigured):
        jb.load_calibration(bad, backend="x")
    out = tmp_path / "out.json"; out.write_text(json.dumps({"a": 1, "b": 0, "threshold": 1.5}))
    with pytest.raises(jb.BackendNotConfigured):
        jb.load_calibration(out, backend="x")


def test_load_calibration_refuses_a_fit_for_another_model_or_input_shape(tmp_path):
    """The critic's reproduction: switching the model kept the old model's Platt numbers."""
    p = _cal_file(tmp_path, model="BAAI/bge-reranker-v2-m3", max_length=512)
    assert jb.load_calibration(p, backend="local",
                               expect={"model": "BAAI/bge-reranker-v2-m3", "max_length": 512}).a == 0.5
    with pytest.raises(jb.BackendNotConfigured, match="model"):
        jb.load_calibration(p, backend="local", expect={"model": "BAAI/bge-reranker-base"})
    with pytest.raises(jb.BackendNotConfigured, match="max_length"):
        jb.load_calibration(p, backend="local", expect={"max_length": 1024})
    # a key the file never recorded is not compared, so older files still load
    assert jb.load_calibration(p, backend="local", expect={"passage_chars": 999}) is not None


@pytest.mark.parametrize("status,body,kind", [
    (410, "", "retired"), (404, "This endpoint has reached its end of life", "retired"),
    (401, "", "not_entitled"), (403, "Authorization failed", "not_entitled"),
    (404, "Not found for account 'x'", "not_entitled"), (404, "404 page not found", "http_error"),
    (429, "", "rate_limited"), (500, "", "http_error"), (None, "", "http_error")])
def test_classify_http_gives_one_kind_per_owner(status, body, kind):
    """The same HTTP failure maps to the same kind for every backend."""
    assert jb.classify_http(status, body) == kind


def test_verdict_as_contract_matches_the_rag_io_relevance_shape():
    """as_contract() validates against $defs/relevance; as_dict() carries raw and why for the trace."""
    import rag_io
    v = jb.Verdict(True, 0.8, None, "nemotron", raw=5.1)
    assert rag_io.validate(v.as_contract(), "relevance") == []
    assert v.as_dict()["raw"] == 5.1 and "why" in v.as_dict()
