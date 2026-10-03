"""EC-020: the engine's own record reaches the middleware as reply.meta."""
import mapping


def test_engine_meta_carries_research_gap_fill_and_field_markers():
    brief = {"meta": {"research_facts": {"given": 3, "used": [], "skipped": []},
                      "research_decisions": {"given": 2, "used": ["d_1"]}, "gap_fill": {"report": {}}, "parsed_at": "x"},
             "loop2_golden": {"fields": {
                 "competitor_context": {"value": "Lyons", "proposed": True, "basis": "research", "confirm": "Lyons?"},
                 "reasons_to_believe": {"value": ["a"], "review": {"status": "failed_checks", "failed": ["supports_smp"]},
                                        "decision_refs": [{"item": 0, "id": "d_1"}]},
                 "smp": {"value": "line"}}}}
    em = mapping.engine_meta(brief)
    assert em["research_decisions"]["given"] == 2 and "parsed_at" not in em
    assert em["field_flags"]["competitor_context"] == {"proposed": True, "basis": "research", "confirm": "Lyons?"}
    assert em["field_flags"]["reasons_to_believe"]["review"]["status"] == "failed_checks"
    assert em["field_flags"]["reasons_to_believe"]["decision_refs"] == [{"item": 0, "id": "d_1"}]
    assert "single_minded_proposition" not in em["field_flags"]       # nothing to mark


def test_a_plain_brief_gives_an_empty_meta():
    assert mapping.engine_meta({"meta": {}, "loop2_golden": {"fields": {"smp": {"value": "x"}}}}) == {}
