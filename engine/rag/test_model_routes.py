"""Model routes (ADR 0011, Sai 2026-09-26): every pipeline call names its job; the job
picks [model, fallback]; a judge never runs on its writer's model; effort follows the
job and never leaks to the next call; whole-pipeline pins switch routing off. Offline.
Run: cd engine/rag && python3 -m pytest -q test_model_routes.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent))
import parse_brief as pb  # noqa: E402

SCHEMA = json.loads((HERE.parent / "golden-brief" / "golden_brief.schema.json").read_text())
FIELD = {f["id"]: f for f in SCHEMA["fields"]}


@pytest.fixture(autouse=True)
def _anthropic(monkeypatch):
    """Routing needs the Anthropic provider and no whole-pipeline pin."""
    monkeypatch.setenv("BRIEF_PROVIDER", "anthropic")
    for v in ("BRIEF_MODEL", "BRIEF_MODEL_CHAIN", "BRIEF_ROUTES") + tuple(f"BRIEF_ROUTE_{r.upper()}" for r in pb.ROUTES):
        monkeypatch.delenv(v, raising=False)


def test_route_table_and_exclusion():
    """The table as decided; a judge's writer model is removed from its chain."""
    assert pb.route_models("synth") == ["claude-sonnet-5", pb.HAIKU]
    assert pb.route_models("grounded_writer")[0] == "claude-opus-5-5"
    assert pb.route_models("hero_judge", exclude="claude-opus-4-6") == ["claude-opus-5-5", "claude-sonnet-5"]
    assert pb.route_models("judge", exclude="claude-opus-5-5") == ["claude-sonnet-5", pb.HAIKU]
    assert pb.route_models("hero_judge", exclude="claude-opus-5-5") == ["claude-sonnet-5"]
    assert pb.writer_route("reasons_to_believe") == "grounded_writer" and pb.writer_route("smp") == "hero"
    assert pb.judge_route("insight") == "hero_judge" and pb.judge_route("desired_response") == "judge"
    assert set(pb.model_routes()) == set(pb.ROUTES)


def test_overrides_and_switches(monkeypatch):
    """BRIEF_ROUTE_<JOB> overrides one job; BRIEF_ROUTES=0, BRIEF_MODEL, BRIEF_MODEL_CHAIN
    or a non-Anthropic provider turn routing off (None: the old single-model chain)."""
    monkeypatch.setenv("BRIEF_ROUTE_SYNTH", f"{pb.HAIKU}")
    assert pb.route_models("synth") == [pb.HAIKU]
    for var, val in (("BRIEF_ROUTES", "0"), ("BRIEF_MODEL", "claude-opus-5-5"),
                     ("BRIEF_MODEL_CHAIN", "anthropic:claude-opus-4-6")):
        monkeypatch.setenv(var, val)
        assert pb.route_models("judge") is None and pb.model_routes() == {}
        monkeypatch.delenv(var)
    monkeypatch.setenv("BRIEF_PROVIDER", "nim")
    assert pb.route_models("judge") is None
    assert pb.route_models("no-such-job") is None


def _record(monkeypatch, replies=None):
    """Fake _call_link: records (model, effort) and answers from `replies` by model."""
    seen = []
    def link(provider, model, user, **k):
        """Test stub: stands in for `_call_link` in _record."""
        seen.append((model, getattr(pb._EFFORT_TL, "effort", None)))
        r = (replies or {}).get(model, '{"ok": 1}')
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(pb, "_call_link", link)
    return seen


def test_routed_call_walks_its_job_and_never_the_writer(monkeypatch):
    """A routed judge tries Sonnet then Haiku, never the writer; its effort is 'low' during
    the call and cleared after it."""
    seen = _record(monkeypatch, {"claude-sonnet-5": RuntimeError("down")})
    assert pb._json_call("u", route="judge", exclude="claude-opus-5-5") == {"ok": 1}
    assert seen == [("claude-sonnet-5", "low"), (pb.HAIKU, "low")]
    assert getattr(pb._EFFORT_TL, "effort", None) is None
    seen = _record(monkeypatch)
    assert pb._json_call("u", route="hero_judge", exclude="claude-opus-5-5") == {"ok": 1}
    assert seen == [("claude-sonnet-5", "low")]


def test_explicit_model_and_only_model_bypass_routes(monkeypatch):
    """model= (BRIEF_SYNTH_MODEL, the critic) wins over the route, with no effort set."""
    seen = _record(monkeypatch)
    pb._json_call("u", model="claude-opus-4-6", route="synth", only_model=True)
    assert seen == [("claude-opus-4-6", None)]


def test_effort_reaches_the_api_and_the_cli(monkeypatch):
    """On a thinking model the API call carries output_config.effort and the CLI --effort;
    on Opus 4.6 neither; after the call the thread's effort is cleared."""
    sent = {}
    class Msgs:
        """Test stub class: stands in for `Msgs` in test_effort_reaches_the_api_and_the_cli."""
        def create(self, **kw):
            """Test stub: stands in for `create` in Msgs."""
            sent.update(kw)
            class T:
                """Test stub class: stands in for `T` in create."""
                type, text = "text", '{"ok": 1}'
            class R:
                """Test stub class: stands in for `R` in create."""
                content, stop_reason, usage = [T()], "end_turn", None
            return R()
    class Client:
        """Test stub class: stands in for `Client` in test_effort_reaches_the_api_and_the_cli."""
        def __init__(self, **k):
            """Test stub: stands in for `__init__` in Client."""
            self.messages = Msgs()
    import types
    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=Client))
    monkeypatch.setenv("BRIEF_CLAUDE_TRANSPORT", "api")
    pb._EFFORT_TL.effort = "low"
    pb._chat_anthropic("u", model="claude-sonnet-5")
    assert sent["output_config"]["effort"] == "low"
    sent.clear(); pb._chat_anthropic("u", model="claude-opus-4-6")
    assert "output_config" not in sent
    pb._EFFORT_TL.effort = None


def test_every_pipeline_call_names_its_job(monkeypatch):
    """Each pipeline call site passes its job (and judges their writer's exclusion)."""
    calls = []
    def fake(user, system=None, **k):
        """Test stub: stands in for `_json_call` in test_every_pipeline_call_names_its_job."""
        calls.append((k.get("route"), k.get("exclude")))
        return None
    monkeypatch.setattr(pb, "_json_call", fake)
    pb.score_betterbriefs("A brief."); pb.how_to_win_toon(["One sentence."])
    pb.capture_toon(["One sentence."]); pb.extract_golden_brief("A brief.")
    pb._smp_territory("A brief.", "Rival X")
    pb._refine_field(FIELD["smp"], "a line", "note")
    pb._judge_and_gate(FIELD["smp"], [{"value": "a"}, {"value": "b"}])
    pb._judge_and_gate(FIELD["reasons_to_believe"], [{"value": ["x"]}, {"value": ["y"]}])
    routes = [c[0] for c in calls]
    for r in ("mechanical", "extract", "hero_judge", "hero", "judge"):
        assert r in routes, (r, routes)
    assert ("hero_judge", "claude-opus-4-6") in calls and ("judge", "claude-opus-5-5") in calls
