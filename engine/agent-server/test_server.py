"""agent-server draft path: a degraded run says so, and never silently re-runs without
the golden extraction (audit CC13/C4/N6, batch 1). Offline: parse_brief.run is stubbed.
Run: cd engine/agent-server && python3 -m pytest -q test_server.py
"""
from __future__ import annotations
import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import server  # noqa: E402
import parse_brief  # noqa: E402
from mapping import build_rationale, build_context  # noqa: E402

MINIMAL = {"meta": {"extraction_mode": "anthropic:claude-opus-4-6"},
           "loop1_capture": {"fields": {}, "how_to_win": {}, "no_loss_ledger": {"coverage_pct": 90}},
           "loop2_brief": {"open_questions": []},
           "loop2_golden": {"fields": {"background": {"value": "b", "source": "client_stated"}}}}


def test_draft_degrade_keeps_golden_and_says_so(monkeypatch):
    """run() raising with loops37 on: the retry keeps golden=True, and the draft's rationale
    starts 'Degraded run (<reason>)'."""
    calls = []
    def fake_run(path, **kw):
        calls.append(kw)
        if kw["loops37"]:
            raise RuntimeError("store stalled")
        return {**MINIMAL, "meta": dict(MINIMAL["meta"])}
    monkeypatch.setattr(parse_brief, "run", fake_run)
    monkeypatch.setattr(parse_brief, "_json_call", lambda *a, **k: {})
    monkeypatch.setattr(server, "research", None)
    code, fields = server.do_draft({"input": "A brief.", "loops37": True}, {"data": {}})
    assert code == 200
    assert [c["loops37"] for c in calls] == [True, False] and calls[1]["golden"] is True
    assert fields["rationale"].startswith("Degraded run (retrieval and strategy fill failed: RuntimeError: store stalled)")


def test_no_claude_is_a_clear_error_not_a_retry(monkeypatch):
    """NoClaudeAvailable propagates (a non-2xx for the app), with no second run."""
    calls = []
    def fake_run(path, **kw):
        calls.append(kw)
        raise parse_brief.NoClaudeAvailable("no Claude")
    monkeypatch.setattr(parse_brief, "run", fake_run)
    monkeypatch.setattr(server, "research", None)
    try:
        server.do_draft({"input": "A brief.", "loops37": True}, {"data": {}})
    except parse_brief.NoClaudeAvailable:
        pass
    else:
        raise AssertionError("NoClaudeAvailable was swallowed")
    assert len(calls) == 1


def test_rationale_and_context_name_fallbacks_and_degradation():
    """A non-Claude answer, a retrieval fallback and unvalidated loops all reach the planner."""
    brief = {**MINIMAL, "meta": {**MINIMAL["meta"], "fallback_links": ["nim:openai/gpt-oss-20b"], "degraded": "x"},
             "loops3_7": {"enabled": True, "sources_used": [], "fallback": {"to": "digests", "reason": "mix stalled"},
                          "validation_degraded": ["loop4_insight"]}}
    r = build_rationale(brief)
    assert r.startswith("Degraded run (x)") and "NOT a Claude brief: answered by nim:openai/gpt-oss-20b" in r
    ctx = build_context(brief)
    assert "fell back to digests" in ctx and "mix stalled" in ctx and "Unvalidated evidence" in ctx
