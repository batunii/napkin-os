"""C1a (2026-09-29): verified research facts reach the hero writers as cited lines and count as
allowed for the figure check; superseded ones are skipped; nothing reaches the Loop 1 capture;
a run without facts is unchanged. The facts below are made-up fixtures, not research."""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent)); sys.path.insert(0, str(HERE))
import parse_brief as pb  # noqa: E402
import research_facts as rf  # noqa: E402
from test_cannot_fail_silently import _fake_models, _fill  # noqa: E402

FIXTURE_FACTS = [   # fixtures only
    {"id": "f-1", "version": 2, "brand_id": "b-1", "entity": "brand", "key": "shops", "value": "40", "unit": "shops",
     "as_of": "2026-06", "status": "current", "sources": [{"id": "s-9", "title": "Fixture annual report"}]},
    {"id": "f-2", "version": 1, "brand_id": None, "entity": "category", "key": "loaves bought weekly per household",
     "value": "3.1", "status": "current", "sources": [{"uri": "https://example.org/fixture"}]},
    {"id": "f-3", "version": 1, "brand_id": "b-1", "entity": "brand", "key": "shops", "value": "35", "status": "superseded"},
    {"id": "f-4", "value": ""},
]


def test_only_current_well_formed_facts_are_used():
    usable, skipped = rf.current(FIXTURE_FACTS)
    assert [f["id"] for f in usable] == ["f-1", "f-2"]
    assert {s["id"]: s["why"] for s in skipped} == {"f-3": "not current (superseded)", "f-4": "missing id or value"}


def test_a_fact_line_carries_id_version_scope_and_source():
    assert rf.line(FIXTURE_FACTS[0]) == "[F:f-1 v2] brand shops: 40 shops (brand research, as of 2026-06; Fixture annual report)"
    assert rf.line(FIXTURE_FACTS[1]).startswith("[F:f-2 v1] category loaves bought weekly per household: 3.1 (category research")
    rec = rf.record(*rf.current(FIXTURE_FACTS))
    assert rec["given"] == 4 and [u["id"] for u in rec["used"]] == ["f-1", "f-2"] and len(rec["skipped"]) == 2


def test_every_hero_writer_gets_the_facts_and_the_figure_check_allows_them(monkeypatch):
    calls = _fake_models(monkeypatch)
    lines = [rf.line(f) for f in rf.current(FIXTURE_FACTS)[0]]
    real = pb._numbers_not_in
    seen = []
    monkeypatch.setattr(pb, "_numbers_not_in", lambda value, allowed: seen.append(allowed) or real(value, allowed))
    gf = {"audience": {"value": "under-30s", "source": "client_stated"},
          "background": {"value": "a bakery", "source": "client_stated"},
          "competitor_context": {"value": "supermarkets", "source": "client_stated"}}
    pb.fill_derivable_fields(gf, {"loops": {}}, pb.json.loads((HERE.parent / "golden-brief" / "golden_brief.schema.json").read_text()),
                             brief_text="A bakery. Under-30s buy supermarket bread.", research_facts=lines)
    gen_prompts = [u for k, u, s in calls if k in ("gen", "gen_batch")]
    assert gen_prompts and all("[F:f-1 v2]" in u and "VERIFIED RESEARCH FACTS" in u for u in gen_prompts)
    assert "35" not in " ".join(gen_prompts)                                  # the superseded fact never appears
    assert seen and all("[F:f-1 v2]" in a for a in seen)                     # figures may come from the facts
    assert pb._numbers_not_in(["We have 40 shops"], seen[0]) == []


def test_without_facts_the_prompts_are_unchanged(monkeypatch):
    calls_a = _fake_models(monkeypatch)
    _fill(monkeypatch)
    a = [u for k, u, s in calls_a if k in ("gen", "gen_batch")]
    calls_b = _fake_models(monkeypatch)
    gf = {"audience": {"value": "under-30s", "source": "client_stated"},
          "background": {"value": "app relaunch", "source": "client_stated"},
          "competitor_context": {"value": "RivalBank", "source": "client_stated"}}
    from test_cannot_fail_silently import GOLDEN_SCHEMA, BRIEF
    pb.fill_derivable_fields(gf, {"loops": {}}, GOLDEN_SCHEMA, brief_text=BRIEF, research_facts=[])
    b = [u for k, u, s in calls_b if k in ("gen", "gen_batch")]
    assert a == b and not any("VERIFIED RESEARCH FACTS" in u for u in a)


def test_run_records_the_facts_and_keeps_them_out_of_the_capture(monkeypatch):
    seen = {}
    def cap(segs):
        seen["capture"] = " ".join(segs)
        return {"fields": {"business_problem": {"value": "p", "status": "fact"}}, "how_to_win": {}, "open_questions": []}
    monkeypatch.setattr(pb, "capture_toon", cap)
    monkeypatch.setattr(pb, "how_to_win_toon", lambda segs: {})
    monkeypatch.setattr(pb, "score_betterbriefs", lambda text, fields=None: {})
    out = pb.run(None, raw_text="A bakery. Under-30s buy supermarket bread.", upstream={"facts": FIXTURE_FACTS})
    assert [u["id"] for u in out["meta"]["research_facts"]["used"]] == ["f-1", "f-2"]
    assert "F:f-1" not in seen["capture"] and "40 shops" not in seen["capture"]
    plain = pb.run(None, raw_text="A bakery. Under-30s buy supermarket bread.")
    assert "research_facts" not in plain["meta"]
