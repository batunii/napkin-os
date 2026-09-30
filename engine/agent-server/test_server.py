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
        """Test stub: stands in for `fake_run` in test_draft_degrade_keeps_golden_and_says_so."""
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
        """Test stub: stands in for `fake_run` in test_no_claude_is_a_clear_error_not_a_retry."""
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


def test_regen_is_grounded_and_rule_checked(monkeypatch):
    """The regeneration prompt carries the client brief as data; an RTB with a figure the
    brief never gave is retried once with the failure named, then rejected with 422; an
    empty list is not accepted; a clean value passes first time."""
    seen = []
    replies = iter([{"reasons_to_believe": ["73% of buyers repurchased"], "rationale": "r"},
                    {"reasons_to_believe": ["Still 73% repurchased"], "rationale": "r"}])
    def fake(user, system=None, accept=None, **k):
        """Test stub: stands in for `fake` in test_regen_is_grounded_and_rule_checked."""
        seen.append(user)
        obj = next(replies)
        return obj if accept(obj) else None
    monkeypatch.setattr(parse_brief, "_json_call", fake)
    clan = {"data": {"brief_input": "Acme sells 2 million packs a year. Every asset carries the disclaimer.",
                     "single_minded_proposition": "Only Acme."}}
    code, out = server.do_regen({"task": "regenerate_field", "field": "reasons_to_believe"}, clan)
    assert code == 422 and "figures not in the brief: 73" in out["error"]
    assert "<client_brief>" in seen[0] and "2 million packs" in seen[0]
    assert "BROKE THESE RULES" in seen[1] and len(seen) == 2
    monkeypatch.setattr(parse_brief, "_json_call", lambda user, system=None, accept=None, **k:
                        {"reasons_to_believe": ["2 million packs a year"], "rationale": "r"})
    code, out = server.do_regen({"task": "regenerate_field", "field": "reasons_to_believe"}, clan)
    assert code == 200 and out["reasons_to_believe"] == ["2 million packs a year"]
    assert "single_sentence: 2 sentences" in server._regen_rule_failures("single_minded_proposition", "One. Two.", None)
    empty_ok = False
    def empty(user, system=None, accept=None, **k):
        """Test stub: stands in for `empty` in test_regen_is_grounded_and_rule_checked."""
        nonlocal empty_ok
        empty_ok = accept({"reasons_to_believe": []})
        return None
    monkeypatch.setattr(parse_brief, "_json_call", empty)
    code, _ = server.do_regen({"task": "regenerate_field", "field": "reasons_to_believe"}, clan)
    assert code == 502 and empty_ok is False


# ---- the draft path's extra work (audit C2/critic-G10, Sai 2026-09-29) --------------------

class _FakeResearch:
    """Stands in for the research module; counts gather() calls."""
    def __init__(self):
        self.calls = 0

    def gather(self, text, clan_data):
        """Record the call; return a one-line summary."""
        self.calls += 1
        return "dossier", "- Precedent: ipa_0001"


def _draft(monkeypatch, loops37: bool, web: str = "off"):
    """One stubbed draft; returns (gather calls, the naming call's kwargs, fields)."""
    fake = _FakeResearch()
    naming = {}

    def fake_json(user, **k):
        """The naming call: record its kwargs."""
        naming.update(k)
        return {"project_name": "P", "client": "C"}
    monkeypatch.setattr(parse_brief, "run", lambda path, **kw: {**MINIMAL, "meta": dict(MINIMAL["meta"])})
    monkeypatch.setattr(parse_brief, "_json_call", fake_json)
    monkeypatch.setattr(server, "research", fake)
    monkeypatch.setenv("RESEARCH_WEB", web)
    monkeypatch.delenv("BRIEF_RESEARCH", raising=False)
    code, fields = server.do_draft({"input": "A brief.", "loops37": loops37}, {"data": {}})
    assert code == 200
    return fake.calls, naming, fields


def test_the_dossier_is_skipped_when_it_would_repeat_the_brief(monkeypatch):
    """Loops 3-7 on, web off (the app default): no second retrieval, no gist call."""
    calls, _n, fields = _draft(monkeypatch, loops37=True)
    assert calls == 0 and "**Research:**" not in fields.get("context", "")


def test_the_dossier_runs_when_it_is_the_only_precedent_or_the_web_is_on(monkeypatch):
    """Loops 3-7 off, or RESEARCH_WEB on: the dossier runs as before."""
    assert _draft(monkeypatch, loops37=False)[0] == 1
    calls, _n, fields = _draft(monkeypatch, loops37=True, web="claude")
    assert calls == 1 and "**Research:**" in fields["context"]


def test_naming_runs_on_the_mechanical_route(monkeypatch):
    """The project name is a small job: the mechanical route, not the writer chain."""
    _c, naming, fields = _draft(monkeypatch, loops37=True)
    assert naming.get("route") == "mechanical" and fields["project_name"] == "P"
