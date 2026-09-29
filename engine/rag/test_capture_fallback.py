"""The Loop 1 capture fallback (capture_fallback.py, 2026-09-29): the deterministic reader on
a made-up brief, and the jev reader with jev faked. No model or jev calls."""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent)); sys.path.insert(0, str(HERE))
import capture_fallback as cf  # noqa: E402

BRIEF = [
    "Project Name: Acme app relaunch",
    "CONTEXT",
    "Acme Bank has run the same app since 2019.",
    "The Challenge Under-30s see it as their parents' bank.",
    "What do we want to achieve?",
    "We want to grow app sign-ups by 20% in six months.",
    "Who are we talking to?",
    "Students and first jobbers aged 18-25 who bank on their phones.",
    "DELIVERABLES",
    "Two campaign concepts and a key visual.",
    "The logo must appear on every asset.",
    "Deadline: 12.03.2026",
    "Budget: 150,000 EUR",
]


def test_rules_read_sections_labels_and_cues():
    fields, used = cf.rules_capture(BRIEF)
    assert "Acme Bank has run" in fields["background_context"]["value"]
    assert "parents' bank" in fields["business_problem"]["value"]          # heading glued to its sentence
    assert any("20%" in e["value"] for e in fields["objective"])
    assert "aged 18-25" in fields["target_audience"]["value"]
    assert any("key visual" in e["value"] for e in fields["deliverables"])
    assert any("logo must appear" in e["value"] for e in fields["mandatories"])   # a cue inside another section
    assert fields["budget"]["value"] == "150,000 EUR"
    assert any("12.03.2026" in e["value"] for e in fields["timeline"])
    assert used == set(range(len(BRIEF)))


def test_rules_are_deterministic():
    assert cf.rules_capture(BRIEF) == cf.rules_capture(list(BRIEF))


def test_headings_in_romanian_and_questions():
    assert cf.heading_of("Obiective")[0] == "objective"
    assert cf.heading_of("Public țintă")[0] == "target_audience"
    assert cf.heading_of("Who are we talking to?")[0] == "target_audience"
    assert cf.heading_of("What else should we know?") == ("other", "")
    assert cf.heading_of("Sales rose 4% last year.") == (None, "Sales rose 4% last year.")


def test_jev_reader_places_each_sentence_and_skips_other(monkeypatch):
    import jev_checks
    monkeypatch.setattr(jev_checks, "sort_segments",
                        lambda text, segs, labels, hints: [("background_context", 0.9), ("other", 0.8), ("budget", 0.95)])
    fields, used = cf.jev_capture(["Acme is a bank.", "Hello team,", "150k EUR"], "brief")
    assert fields["background_context"]["value"] == "Acme is a bank." and fields["budget"]["confidence"] == 0.95
    assert "other" not in fields and used == {0, 1, 2}


def test_capture_falls_back_to_rules_when_jev_is_silent(monkeypatch):
    monkeypatch.setattr(cf, "jev_capture", lambda segs, text: None)
    fields, used, reader = cf.capture(BRIEF, "brief")
    assert reader == "rules" and fields["budget"]["value"] == "150,000 EUR"
    _f, _u, reader2 = cf.capture(BRIEF, "brief", allow_jev=False)
    assert reader2 == "rules"
