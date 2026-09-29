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


def test_the_review_note_of_a_kept_draft_is_not_a_claim():
    """The note brief_render puts above a kept draft is skipped; the draft's items are counted
    (2026-09-29: the note was counted as an invented reason to believe)."""
    md = "## Reasons to believe\n_Draft — to review: it failed supports_smp. See open questions._\n- A real item\n"
    assert grounding.claims_from_md(md) == [("rtb", "A real item")]
    render = (Path(__file__).resolve().parent.parent / "brief_render.py").read_text()
    assert "_Draft — to review: it failed" in render                        # the note's wording holds


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


OQ_MD = ("## Open questions to resolve before research\n- **[important]** What is the budget?\n"
         "- Who approves the work?\n\n## Other\n- not a question\n")


def test_open_questions_are_read_from_the_client_brief():
    assert grounding.questions_from_md(OQ_MD) == ["What is the budget?", "Who approves the work?"]


def test_open_questions_already_answered_are_counted(monkeypatch):
    """JL-13: a question the brief already answers at p >= 0.9 is counted; below is not."""
    import jev_checks
    monkeypatch.setattr(jev_checks, "questions_answered", lambda t, qs: [0.95, 0.4])
    g = grounding.open_questions("brief", OQ_MD)
    assert (g["answered"], g["of"]) == (1, 2)
    monkeypatch.setattr(jev_checks, "questions_answered", lambda t, qs: None)
    assert grounding.open_questions("brief", OQ_MD) is None


def test_the_report_cell_shows_answered_questions():
    cell = cr._grounding_cell({"invented": 0, "of": 3, "open_questions": {"answered": 0, "of": 7}})
    assert cell == "0 of 3 (0 of 7 questions already answered)"


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


# ---- cost and speed per brief (2026-09-29) --------------------------------------------
EVENTS = [
    {"kind": "llm", "step": "capture_toon", "start": 0.5, "secs": 40.0, "usd": 0.07},
    {"kind": "llm", "step": "extract_golden_brief", "start": 0.5, "secs": 35.0, "usd": 0.08},
    {"kind": "embed", "start": 36.0, "secs": 0.3},
    {"kind": "validator", "start": 36.4, "secs": 0.6}, {"kind": "validator", "start": 37.0, "secs": 0.5},
    {"kind": "llm", "step": "one", "start": 40.0, "secs": 12.0, "usd": 0.01},
    {"kind": "llm", "step": "_one", "start": 40.0, "secs": 20.0, "usd": 0.02},          # insight drafts
    {"kind": "llm", "step": "_judge_and_gate", "start": 60.0, "secs": 9.0, "usd": 0.02},
    {"kind": "llm", "step": "_one", "start": 69.0, "secs": 20.0, "usd": 0.03},          # SMP drafts
    {"kind": "llm", "step": "_refine_field", "start": 89.0, "secs": 7.0, "usd": 0.01},
    {"kind": "llm", "step": "_one", "start": 96.0, "secs": 7.0, "usd": 0.01},           # RTB
    {"kind": "llm", "step": "_one", "start": 96.0, "secs": 8.0, "usd": 0.01},           # desired response
    {"kind": "llm", "step": "_judge_and_gate", "start": 104.0, "secs": 3.0, "usd": 0.005},
    {"kind": "llm", "step": "_pinned_judge", "start": 110.0, "secs": 40.0, "usd": 0.05},  # the grader, left out
]


def test_stage_breakdown_by_step_and_order():
    st = cr.stage_breakdown(EVENTS, brief_secs=108.0)
    assert st["reading"] == {"span_s": 40.0, "calls": 2, "usd": 0.15}
    assert st["retrieval"]["calls"] == 3 and st["strategy notes"]["calls"] == 1
    assert st["insight + SMP"]["calls"] == 4 and st["insight + SMP"]["span_s"] == 56.0     # 40 -> 96
    assert st["proof points"]["calls"] == 3 and st["proof points"]["span_s"] == 11.0       # 96 -> 107
    assert "other" not in st


def test_routes_win_over_the_order_heuristic_when_recorded():
    ev = [{"kind": "llm", "step": "_one", "route": "grounded_writer", "start": 1.0, "secs": 2.0, "usd": 0.01}]
    assert "proof points" in cr.stage_breakdown(ev)


def test_trace_numbers_count_jev_and_embeddings():
    n = cr.trace_numbers({"events": EVENTS, "brief_secs": 108.0})
    assert (n["jev_calls"], n["embed_calls"]) == (2, 1)


def test_costs_claude_jev_and_total():
    ev = EVENTS + [{"kind": "jev", "start": 36.4, "secs": 0.6, "tokens": 25000},
                   {"kind": "jev", "start": 97.0, "secs": 0.4, "tokens": 5000}]
    n = cr.trace_numbers({"events": ev, "brief_secs": 108.0})
    assert n["claude_usd"] == 0.265 and n["jev_tokens"] == 30000 and n["jev_calls"] == 2
    assert n["jev_usd"] == 0.0012 and n["total_usd"] == 0.2662
    old = cr.trace_numbers({"events": EVENTS, "brief_secs": 108.0})          # a trace from before jev was logged
    assert old["jev_usd"] is None and old["total_usd"] is None and old["jev_calls"] == 2


def test_reports_show_tokens_and_the_three_costs():
    rows = {"before": {"m": {"health": 70, "claude_usd": 0.5, "jev_usd": 0.001, "total_usd": 0.501,
                             "tokens": {"in": 12000, "out": 9000}}},
            "after": {"m": {"health": 72, "claude_usd": 0.4, "jev_usd": None, "total_usd": None,
                            "tokens": {"in": 10000, "out": 8000}}}}
    out = cr.render("t", {"before": None, "after": None}, rows, {"brief": 16, "sd": 7.8, "pairs": 6, "from": []})
    assert "| input tokens (incl. cache) / output | Claude $ | jev $ | total $ |" in out
    assert "12,000 / 9,000 | 0.500 | 0.0010 | 0.501 |" in out
    assert "10,000 / 8,000 | 0.400 |  |  |" in out          # jev cost not logged for this run: blank, not 0


def test_a_stem_with_a_converted_copy_reads_the_clients_document(tmp_path):
    """omv-btl-brief has a .docx and a converted .md: every tool reads the .docx."""
    from e2e_eval import brief_files
    for n in ("omv.md", "omv.docx", "solo.md", "b.pdf", ".DS_Store"):
        (tmp_path / n).write_text("x")
    got = {k: v.name for k, v in brief_files(tmp_path).items()}
    assert got == {"omv": "omv.docx", "solo": "solo.md", "b": "b.pdf"}
