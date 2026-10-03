"""What the Loop-1 capture read but no golden field carries is shown in the client brief (EC-043)."""
import brief_render as br


def _brief(cap, golden=None):
    return {"meta": {"project": "P"}, "loop2_brief": {}, "loop1_capture": {"fields": cap},
            "loop2_golden": {"fields": golden or {}}}


def test_captured_measures_scoring_and_timings_reach_the_client_brief():
    b = _brief({"success_metrics": [{"value": "Spontaneous awareness among 18-34s (baseline 5%)", "status": "stated",
                                     "source_quote": "baseline 5%"}],
                "evaluation_criteria": [{"value": "Strategy 50%, minimum 300 of 500", "status": "assumption"}],
                "timeline": [{"value": None, "status": "gap"}]})
    md = br.render_client_brief(b)
    assert "## Also in the client's documents" in md
    assert "- Spontaneous awareness among 18-34s (baseline 5%)\n" in md          # quoted: no tag
    assert "- Strategy 50%, minimum 300 of 500 _(inferred)_" in md              # no quote, assumed: tagged
    assert "**Timings**" not in md                                              # a gap is not shown
    assert md.index("Also in the client's documents") < md.index("Single-minded") or "Open questions" not in md


def test_an_item_a_golden_field_already_says_is_not_repeated():
    b = _brief({"key_message": {"value": "Call any time about anything", "status": "stated", "source_quote": "x"},
                "deliverables": [{"value": "A landing page", "status": "stated", "source_quote": "y"}]},
               golden={"smp": {"value": "You can call any time, about anything at all"}})
    extras = dict(br.captured_extras(b))
    assert "The client's key message" not in extras and extras["Deliverables"] == ["A landing page"]


def test_no_capture_no_section():
    assert "Also in the client's documents" not in br.render_client_brief(_brief({}))
