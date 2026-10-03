"""REQ-01: every field filled. Generated fields get one repair, then the best draft flagged; fields the
client never gave get a labelled proposal, never shown as the client's words."""
import json
from pathlib import Path

import brief_render as br
import parse_brief as pb

SCHEMA = json.loads((Path(pb.__file__).resolve().parent / "golden-brief" / "golden_brief.schema.json").read_text())


def test_an_empty_generated_field_is_repaired_once_with_the_reason():
    golden = {"reasons_to_believe": {"value": None, "source": "missing", "reason": "fact citation: 28 not cited",
                                     "rejected_attempt": ["first"]}}
    notes = []

    def one(fid, note):
        notes.append((fid, note))
        return {"value": ["fixed, cited"], "source": "inferred"}, [{"question": "q", "blocks_field": fid}]
    fills = {"insight": {"value": "i"}, "smp": {"value": "s"}, "desired_response": {"value": {"do": "x"}}}
    qs = pb._fill_empty_generated(fills, [{"question": "Agree the reasons", "blocks_field": "reasons_to_believe"}],
                                  golden, SCHEMA, one, lambda e: e)
    assert [f for f, _n in notes] == ["reasons_to_believe"] and "28 not cited" in notes[0][1]
    assert fills["reasons_to_believe"]["fill_repair"]["outcome"] == "repaired"
    assert [q["question"] for q in qs] == ["q"]                     # the stale 'agree' question is replaced


def test_when_the_repair_fails_the_best_draft_ships_flagged_never_blank():
    golden = {"desired_response": {"value": None, "source": "missing", "reason": "fact citation: 116 not cited",
                                   "rejected_attempt": {"do": "Ring 116"}}}

    def one(fid, note):
        golden[fid] = {"value": None, "source": "missing", "reason": "research decision: 4 from a decision",
                       "rejected_attempt": {"do": "Ring now"}}
        return None, []
    fills = {"insight": {"value": "i"}, "smp": {"value": "s"}, "reasons_to_believe": {"value": ["r"]}}
    qs = pb._fill_empty_generated(fills, [], golden, SCHEMA, one, lambda e: e)
    dr = fills["desired_response"]
    assert dr["value"] == {"do": "Ring now"} and dr["review"]["status"] == "failed_checks"
    assert dr["review"]["failed"] == ["research decision"] and dr["fill_repair"]["outcome"] == "shipped_flagged"
    assert qs[0]["blocks_field"] == "desired_response" and "failed research decision" in qs[0]["question"]


def test_a_clients_own_line_is_never_touched():
    golden = {"smp": {"value": "Client line", "source": "client_stated"}}
    called = []
    pb._fill_empty_generated({}, [], golden, SCHEMA, lambda f, n: called.append(f) or (None, []), lambda e: e)
    assert "smp" not in called


def test_missing_fields_get_labelled_proposals_with_a_question(monkeypatch):
    sent = {}

    def fake(user, system=None, **kw):
        sent["user"], sent["route"] = user, kw.get("route")
        return {"fields": {"competitor_context": {"value": "Lyons is the rival [F:f_1]", "basis": "the research",
                                                  "question": "Is Lyons the main rival?"},
                           "tone_world_assets": {"value": "Warm, Irish, everyday", "basis": "category norms",
                                                 "question": "Any tone rules?"}}}
    monkeypatch.setattr(pb, "_json_call", fake)
    golden = {"background": {"value": "Barry's wants younger drinkers", "source": "client_stated"}}
    facts = [{"id": "f_1", "version": 1, "entity": "brand/lyons", "key": "market.share", "value": 0.36, "unit": "proportion"}]
    qs = pb.propose_missing(golden, SCHEMA, "brief text", research=facts)
    cc = golden["competitor_context"]
    assert sent["route"] == "grounded_writer" and "competitor_context" in sent["user"]
    assert cc["proposed"] is True and cc["source"] == "inferred" and cc["method"] == "proposal"
    assert cc["value"] == "Lyons is the rival" and cc["fact_refs"][0]["id"] == "f_1"    # [F:id] moved to fact_refs
    assert "budget_scope" not in golden                                                   # left out by the reply: stays empty
    assert {q["blocks_field"] for q in qs} == {"competitor_context", "tone_world_assets"}
    md = br.render_client_brief({"meta": {"project": "P"}, "loop2_brief": {}, "loop2_golden": {"fields": golden}})
    assert "_Proposed (not given by the client), based on the research. To confirm: Is Lyons the main rival?_" in md


def test_a_failed_proposal_call_never_fails_the_brief(monkeypatch):
    monkeypatch.setattr(pb, "_json_call", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    golden = {}
    assert pb.propose_missing(golden, SCHEMA, "brief") == []
    assert golden["competitor_context"]["proposal_error"] == "RuntimeError"


def test_the_fill_step_runs_in_a_real_fill_when_switched_on(monkeypatch):
    """With BRIEF_FILL_ALL=1 the generated-field step is called from fill_derivable_fields."""
    monkeypatch.setenv("BRIEF_FILL_ALL", "1")
    seen = []
    monkeypatch.setattr(pb, "_fill_empty_generated", lambda fills, qs, *a: seen.append(sorted(fills)) or qs)
    pb.fill_derivable_fields({}, {}, {"fields": []}, brief_text="x")
    assert seen == [[]]


def test_proof_requests_leave_the_reasons_to_believe_and_show_as_proof_still_needed():
    """ADR 0020: 'TO CONFIRM' items are requests, not proof; they are shown apart and asked as questions."""
    golden = {"reasons_to_believe": {"value": ["No boiling: 0 minutes vs 10-20"], "source": "inferred",
                                     "proof_needed": ["TO CONFIRM: a blind taste test against boiled polenta"]}}
    md = br.render_client_brief({"meta": {"project": "P"}, "loop2_brief": {}, "loop2_golden": {"fields": golden}})
    rtb = md.split("## Reasons to believe")[1].split("##")[0]
    assert "- No boiling: 0 minutes vs 10-20" in rtb and "_Proof still needed:_" in rtb
    assert "- a blind taste test against boiled polenta" in rtb and "TO CONFIRM" not in rtb.split("_Proof still needed:_")[1]
