"""Open questions read like a planner wrote them, once each (2026-10-03). The Barry's Tea brief asked
"What is the budget?" next to the capture's own "What is the campaign budget, and does it cover media…",
and "What is the mandatories?"; the Oatly brief printed its mandatories as a Python list and left
"[F:id]" and "finding;,)" in the text."""
import brief_render as br
import parse_brief as pb


def _texts(qs):
    return [q["question"] if isinstance(q, dict) else q for q in qs]


def test_a_generic_question_is_not_asked_when_the_capture_already_asks_it():
    llm = [{"question": "What is the campaign budget, and does it cover media as well as production?"},
           {"question": "Who on the client side signs off the work?"}]
    qs = _texts(pb.shape_loop2({}, llm)["open_questions"])
    assert not any(q.startswith("What is the budget") for q in qs)
    assert sum("budget" in q.lower() for q in qs) == 1
    assert sum("sign" in q.lower() for q in qs) == 1


def test_generic_questions_are_worded_for_a_client():
    qs = _texts(pb.shape_loop2({}, [])["open_questions"])
    assert not any(q.startswith("What is the mandatories") or q.startswith("What is the decision makers") for q in qs)
    assert pb.CORE_QUESTIONS["mandatories"] in qs and pb.CORE_QUESTIONS["decision_makers"] in qs


def test_a_proposal_question_already_asked_is_dropped_and_a_new_one_kept():
    qs = [{"question": "What is the budget for production and media?"},
          {"question": "To confirm (budget & scope): What is the total budget, and does it cover media?"},
          {"question": "To confirm (competitor context): Which rivals should we position against?"}]
    out = _texts(pb.tidy_open_questions(qs))
    assert out == [qs[0]["question"], qs[2]["question"]]


def test_a_proposal_question_with_a_topic_not_yet_asked_is_kept():
    qs = [{"question": "What is the budget?"},
          {"question": "To confirm (mandatories): What budget and which legal lines must we work within?"}]
    assert len(pb.tidy_open_questions(qs)) == 2


def test_a_list_field_renders_as_a_list_and_ids_never_reach_the_reader():
    brief = {"meta": {"project": "P"}, "loop2_brief": {},
             "loop2_golden": {"fields": {
                 "mandatories": {"value": ["No dairy jabs.", "Clear claims with ASAI."]},
                 "tone_world_assets": {"value": "Warm (verified regulation finding [F:f_A]; [F:f_B],) and wry [D:d_1]."}}}}
    md = br.render_client_brief(brief)
    assert "- No dairy jabs." in md and "- Clear claims with ASAI." in md and "['" not in md
    assert "[F:" not in md and "[D:" not in md and ";,)" not in md
    assert "Warm (verified regulation finding) and wry." in md
