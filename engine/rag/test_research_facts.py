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


# ---------- C1b: citations checked in code, moved into fact_refs ----------

FACTS = {f["id"]: f for f in rf.current(FIXTURE_FACTS)[0]}


def test_citation_checks():
    fails = rf.citation_failures(["40 shops near you [F:f-1 v2]", "45 shops [F:f-1]", "We run 40 shops", "x [F:f-9]"],
                                 FACTS, "A bakery.")
    assert fails == ["fact citation: 45 is not in the fact it cites (F:f-1) (item 1)",
                     "fact citation: 40 comes from the research (F:f-1) but is not cited (item 2)",
                     "fact citation: cites F:f-9, which was not given (item 3)"]
    assert rf.citation_failures(["Baked before 7am"], FACTS, "Baked before 7am.") == []   # a brief figure needs no cite
    assert rf.citation_failures(["x [F:f-9]"], {}, "") == []                              # no facts, no checks
    assert pb._invents(fails)                                                             # never kept for review


def test_markers_move_into_fact_refs():
    clean, refs = rf.strip(["40 shops near you [F:f-1 v2]", "Baked before 7am"], FACTS)
    assert clean == ["40 shops near you", "Baked before 7am"]
    assert refs == [{"item": 0, "id": "f-1", "version": 2, "scope": "brand", "source_ids": ["s-9"]}]
    assert rf.strip({"think": "x [F:f-2]", "do": "y"}, FACTS)[1][0]["item"] == "think"


def test_the_fill_keeps_refs_and_the_app_passes_them_through(monkeypatch):
    _fake_models(monkeypatch)
    real = pb._json_call
    def cite(user, system=None, **k):
        out = real(user, system=system, **k)
        if isinstance(out, dict) and isinstance(out.get("value"), list):
            out = {**out, "value": ["40 shops put a Hearthstone near you [F:f-1 v2]", "Baked before 7am"]}
        return out
    monkeypatch.setattr(pb, "_json_call", cite)
    gf = {"audience": {"value": "under-30s", "source": "client_stated"},
          "background": {"value": "a bakery", "source": "client_stated"},
          "competitor_context": {"value": "supermarkets", "source": "client_stated"}}
    from test_cannot_fail_silently import GOLDEN_SCHEMA
    pb.fill_derivable_fields(gf, {"loops": {}}, GOLDEN_SCHEMA, brief_text="A bakery, baked before 7am.",
                             research_facts=rf.current(FIXTURE_FACTS)[0])
    rtb = gf["reasons_to_believe"]
    assert rtb["value"] == ["40 shops put a Hearthstone near you", "Baked before 7am"]
    assert rtb["fact_refs"] == [{"item": 0, "id": "f-1", "version": 2, "scope": "brand", "source_ids": ["s-9"]}]
    sys.path.insert(0, str(HERE.parent / "agent-server"))
    import mapping
    out = mapping.map_brief({"meta": {}, "loop1_capture": {}, "loop2_brief": {}, "loop2_golden": {"fields": gf}})
    assert out["fact_refs"]["reasons_to_believe"][0]["id"] == "f-1"
    assert out["reasons_to_believe"][0] == "40 shops put a Hearthstone near you"          # no marker in the app text


# ---------- C1c: the grounding count accepts current facts as support ----------

def test_grounding_counts_research_backed_claims_apart(monkeypatch):
    import grounding
    import jev_checks
    got = {}
    def fake(text, claims, research=None):
        got["research"] = research
        return [("supported_by_research", 0.95), ("not_in_brief", 0.97)] if research else [("not_in_brief", 0.95)] * len(claims)
    monkeypatch.setattr(jev_checks, "claims_supported", fake)
    md = "## Reasons to believe\n- 40 shops put a Hearthstone near you\n- Voted best bakery in Europe\n"
    lines = [rf.line(f) for f in rf.current(FIXTURE_FACTS)[0]]
    g = grounding.check("A bakery.", md, research=lines)
    assert got["research"] == lines and (g["invented"], g["of"], g.get("from_research")) == (1, 2, 1)
    g0 = grounding.check("A bakery.", md)                                   # no facts: exactly as before
    assert got["research"] is None and g0["invented"] == 2 and "from_research" not in g0


def test_the_record_keeps_the_fact_lines_for_later_checks():
    rec = rf.record(*rf.current(FIXTURE_FACTS))
    assert rec["used"][0]["line"].startswith("[F:f-1 v2] brand shops: 40 shops")


def test_report_cell_shows_research_support():
    import checkpoint_run as cr
    assert cr._grounding_cell({"invented": 0, "of": 5, "to_confirm": 1, "from_research": 2}) == "0 of 5 (+1 to confirm, 2 from research)"
    assert cr._grounding_cell({"invented": 1, "of": 4}) == "1 of 4"
