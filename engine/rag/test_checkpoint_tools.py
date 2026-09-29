"""Offline tests for phase A item 5 (2026-09-28): brief lists, the noise line, the runner's
grading fixes and the grounding count. No Claude or jev calls."""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import pytest

import checkpoint_run as cr
import grounding

MD = """# Client brief — Brief

## The insight
People dread switching.

## Single-minded proposition
A creative line the document never says.

## Reasons to believe
- A 445MW plant in Whitegate
- TO CONFIRM: taste parity evidence
- Smart meter rollout across Ireland

## Desired response
- **Think:** something
"""


def test_claims_are_reasons_to_believe_only_with_requests_kept():
    got = grounding.claims_from_md(MD)                     # the SMP is creative, not counted
    assert got == [("rtb", "A 445MW plant in Whitegate"), ("rtb", "TO CONFIRM: taste parity evidence"),
                   ("rtb", "Smart meter rollout across Ireland")]


def test_grounding_counts_invented_but_not_requests(monkeypatch):
    import jev_checks
    asked = []

    def fake(text, claims, research=None):
        asked.extend(claims)
        return [("supported", 0.97), ("not_in_brief", 0.99)]
    monkeypatch.setattr(jev_checks, "claims_supported", fake)
    g = grounding.check("brief", MD)
    assert asked == ["A 445MW plant in Whitegate", "Smart meter rollout across Ireland"]   # request not sent
    assert (g["invented"], g["of"], g["to_confirm"]) == (1, 2, 1)


def test_grounding_below_threshold_is_not_invented():
    g = grounding.summarise([("rtb", "x")], [("not_in_brief", 0.6)])
    assert g["invented"] == 0 and g["of"] == 1


def test_grounding_none_when_jev_silent(monkeypatch):
    import jev_checks
    monkeypatch.setattr(jev_checks, "claims_supported", lambda t, c, research=None: None)
    assert grounding.check("brief", MD) is None


def test_resolve_briefs(tmp_path):
    sets = {"test-three": ["a", "b", "c"]}
    assert cr.resolve_briefs("test-three", "x,y", sets) == ["a", "b", "c"]      # a set overrides --briefs
    assert cr.resolve_briefs(None, "x, y", sets) == ["x", "y"]
    assert cr.resolve_briefs(None, None, sets) == cr.DEFAULT_BRIEFS.split(",")
    with pytest.raises(SystemExit, match="unknown brief set"):
        cr.resolve_briefs("nope", None, sets)
    f = tmp_path / "sets.json"
    f.write_text(json.dumps({"sets": sets}))
    assert cr.load_sets(f) == sets and cr.load_sets(tmp_path / "missing.json") == {}


def test_noise_estimate_from_repeat_pairs(tmp_path):
    for d, h in (("cp_a", {"m": 80, "f": 70}), ("cp_b", {"m": 74, "f": 78})):
        (tmp_path / d).mkdir()
        (tmp_path / d / "rows.json").write_text(json.dumps({"after": {k: {"health": v} for k, v in h.items()}}))
    reg = tmp_path / "reg.json"
    reg.write_text(json.dumps({"checkpoints": [{"id": "a", "dir": "cp_a", "arm": "after"},
                                               {"id": "b", "dir": "cp_b", "arm": "after", "repeat_of": "a"}]}))
    n = cr.noise_estimate(reg, tmp_path)
    assert n["diffs"] == [-6, 8] and n["sd"] == 7.1 and n["brief"] == 14 and n["from"] == ["a->b"]
    reg.write_text(json.dumps({"checkpoints": [{"id": "a", "dir": "cp_a", "arm": "after"}]}))
    assert cr.noise_estimate(reg, tmp_path) is None


def test_render_marks_noise_and_never_prints_zero_for_unscored():
    rows = {"before": {"m": {"health": 60, "quality": 60}, "f": {"health": 70, "quality": 70}},
            "after": {"m": {"health": 80, "quality": 80, "grounding": {"invented": 0, "of": 5, "to_confirm": 2}},
                      "f": {"health": None, "quality": None, "grounding": {"error": "jev did not answer"}}}}
    out = cr.render("t", {"before": None, "after": None}, rows, {"brief": 16, "sd": 7.8, "pairs": 6, "from": ["x->y"]})
    assert "+20 (real)" in out and "not scored" in out and "0 of 5 (+2 to confirm)" in out and "invented proof points (RTB)" in out
    assert "Over the 1 of 2 briefs every arm scored." in out
    assert "| after | 80 |" in out                        # the unscored brief is left out of the sum, not added as 0
    small = cr.render("t", {"before": None, "after": None},
                      {"before": {"m": {"health": 70}}, "after": {"m": {"health": 75}}}, {"brief": 16, "sd": 7.8, "pairs": 6, "from": []})
    assert "+5 (noise)" in small


class _GC:
    """golden_critic stand-in: the first `fail` calls judge nothing, then health 77."""
    SCHEMA_PATH = None

    def __init__(self, fail):
        self.fail, self.calls = fail, 0

    def from_brief_object(self, bo): return bo
    def validate(self, schema, gb): return {}

    def run_critic_sampled(self, schema, gb, v, model=None, samples=1):
        self.calls += 1
        if self.calls <= self.fail:
            return {"health": 0, "fields": [], "definition_of_done": []}, 0
        return {"health": 77, "fields": [], "definition_of_done": [], "critic_samples": {"spread": 2}}, 12

    def quality_split(self, schema, gb, v): return {"quality": 70}


def _arm(tmp_path, monkeypatch, fail):
    (tmp_path / "trace_mix_s").mkdir()
    (tmp_path / "trace_mix_s" / "brief_object.json").write_text("{}")
    fake = _GC(fail)
    fake.SCHEMA_PATH = tmp_path / "trace_mix_s" / "brief_object.json"
    monkeypatch.setitem(sys.modules, "golden_critic", fake)
    monkeypatch.setattr(cr, "CRITIC_RETRY_WAIT_S", 0)
    return fake


def test_failed_grading_is_retried_and_not_stamped(tmp_path, monkeypatch):
    fake = _arm(tmp_path, monkeypatch, fail=1)
    r = cr.score_row(tmp_path, "s", {"health": 49}, "m")
    assert fake.calls == 2 and r["health"] == 77 and r["critic"] == "m" and r["health_trace"] == 49


def test_grading_that_never_answers_is_regraded_next_time(tmp_path, monkeypatch):
    fake = _arm(tmp_path, monkeypatch, fail=9)
    r = cr.score_row(tmp_path, "s", {"health": 49}, "m")
    assert r["health"] is None and r["critic"] is None           # not stamped as graded
    fake.fail = 0
    r2 = cr.score_row(tmp_path, "s", {**r, "critic": "m"}, "m")    # even a stamped-but-empty row (old bug)
    assert r2["health"] == 77 and r2["health_trace"] == 49
    assert cr.score_row(tmp_path, "s", r2, "m") is r2             # a graded row is left alone


def test_preflight_stops_without_claude(monkeypatch):
    sys.path.insert(0, str(HERE.parent))
    import parse_brief as pb
    monkeypatch.setenv("BRIEF_CLAUDE_TRANSPORT", "api")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("BRIEF_ALLOW_NONCLAUDE", raising=False)
    with pytest.raises(pb.NoClaudeAvailable):
        cr.preflight("claude-fable-5-1x3")
    cr.preflight("trace")                                           # no critic, nothing to check
