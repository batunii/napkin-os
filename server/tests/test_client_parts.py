"""find_client_parts (middleware-api.md §11): Ellis suggests the parts a
client's answer was about; the code decides which suggestions hold."""

import json
import threading
from types import SimpleNamespace

import httpx
import pytest

from napkin import registry
from napkin.capabilities import Capabilities
from napkin.handlers import find_client_parts as fcp
from napkin.model import ModelError, ModelPort
from napkin.rules.quotes import verbatim_ws
from napkin.util import TaskError

from fakes import FakeModel

DOC = "3f2a0000-2222-4333-8444-555555555555"
SMP, AUD, TONE = f"{DOC}#single_minded_proposition", f"{DOC}#audience", f"{DOC}#tone"
PARTS = [{"address": SMP, "label": "Single-minded proposition", "value": "Summer tastes better without the hangover."},
         {"address": AUD, "label": "Audience", "value": None},
         {"address": TONE, "label": "Tone", "value": {"words": ["warm", "wry"]}}]
PROOF = ("Honestly this isn't the brief we talked about.\nThe single-minded proposition   doesn't feel like us. "
         "The audience is spot on, though.")
SECRET = "CONFIDENTIAL-DATA-NEVER-SENT"


def caps_for(model):
    """The real capability object; the handler must use only its capture view."""
    class NoLayers:
        def open(self, *_):
            return SimpleNamespace()
    return Capabilities(handler="find_client_parts@1.0", scope={"org": "org/a", "brand": "brand/b"},
                        model_port=ModelPort(model, "m", 5), research_port=None, layer_store=NoLayers(),
                        research_semaphore=threading.Semaphore(1))


def req(**inp):
    base = {"answer": "rejected", "proof": PROOF, "parts": PARTS}
    clan = {"id": DOC, "version": "sha256:x", "data": {"note": SECRET}, "facts": [{"id": "f_1", "value": SECRET}],
            "decision_chain": {"decisions": []}, "context": SECRET}
    return SimpleNamespace(doc=DOC, base="sha256:x", clan=clan, handler="find_client_parts@1.0",
                           inp={**base, **inp})


def run(model=None, **inp):
    model = model or FakeModel()
    result, change, hits = fcp.run(req(**inp), caps_for(model))
    return model, result, change, hits


def answering(*suggestions):
    return FakeModel(overrides={"find_client_parts": lambda p: {"suggestions": list(suggestions)}})


def test_suggestions_quoted_verbatim_are_kept_with_the_proofs_own_characters():
    model, result, change, hits = run()
    assert change is None and hits == []
    by = {s["address"]: s for s in result["suggestions"]}
    assert set(by) == {SMP, AUD} and result["dropped"] == 0
    assert by[AUD] == {"address": AUD, "answer": "rejected", "quote": "The audience is spot on, though."}
    # the fake collapsed nothing; a quote that differs only in whitespace is kept as the proof writes it
    m2, r2, _, _ = run(answering({"address": SMP, "answer": "accepted_with_changes",
                                  "quote": "The single-minded proposition doesn't feel like us."}))
    assert r2["suggestions"] == [{"address": SMP, "answer": "accepted_with_changes",
                                  "quote": "The single-minded proposition   doesn't feel like us."}]
    assert r2["suggestions"][0]["quote"] in PROOF


@pytest.mark.parametrize("quote", [
    "The proposition does not feel like us.",               # paraphrased
    "the audience is spot on, though.",                     # case changed
    "Honestly this isn’t the brief we talked about.",       # quote mark changed
    "Honestly this isn't the brief … talked about.",        # joined with an ellipsis
    "",                                                     # empty
    "   ",                                                  # whitespace only
])
def test_a_quote_not_verbatim_in_the_proof_is_dropped(quote):
    _, result, _, _ = run(answering({"address": AUD, "answer": "rejected", "quote": quote}))
    assert result["suggestions"] == [] and result["dropped"] == 1


def test_unknown_address_repeat_and_overlong_quote_are_dropped():
    long_proof = "The audience is wrong. " + "x" * 600
    _, result, _, _ = run(answering(
        {"address": f"{DOC}#budget", "answer": "rejected", "quote": "The audience is spot on, though."},
        {"address": "other-doc#audience", "answer": "rejected", "quote": "The audience is spot on, though."},
        {"address": AUD, "answer": "accepted", "quote": "The audience is spot on, though."},
        {"address": AUD, "answer": "rejected", "quote": "Honestly this isn't the brief we talked about."}))
    assert result["suggestions"] == [{"address": AUD, "answer": "accepted",
                                      "quote": "The audience is spot on, though."}]  # the first one stays
    assert result["dropped"] == 3 and "3 dropped" in result["summary"]
    _, r2, _, _ = run(answering({"address": AUD, "answer": "rejected", "quote": long_proof.strip()}),
                      proof=long_proof)
    assert r2["suggestions"] == [] and r2["dropped"] == 1


def test_a_suggestion_carries_no_key_but_address_answer_and_quote():
    _, result, _, _ = run()
    assert result["suggestions"] and all(set(s) == {"address", "answer", "quote"} for s in result["suggestions"])
    assert set(result) == {"summary", "suggestions", "dropped"}


def test_the_words_may_name_no_part():
    _, result, _, _ = run(proof="Not what we asked for at all.")
    assert result == {"summary": "no part suggested", "suggestions": [], "dropped": 0}


@pytest.mark.parametrize("proof", ["", "  \n\t "])
def test_empty_proof_is_refused_without_a_model_call(proof):
    model = FakeModel()
    with pytest.raises(TaskError) as e:
        run(model, proof=proof)
    assert (e.value.status, e.value.etype) == (400, "invalid_input") and model.calls == []


@pytest.mark.parametrize("inp", [
    {"answer": "accepted"}, {"answer": None}, {"answer": "maybe"},
    {"proof": "x" * 24001}, {"proof": None},
    {"parts": []}, {"parts": None}, {"parts": [{"address": f"{DOC}#p{i}", "label": "P"} for i in range(101)]},
    {"parts": [{"address": SMP}]}, {"parts": [{"label": "Audience"}]},
    {"parts": [PARTS[0], dict(PARTS[1], address=SMP)]},
])
def test_bad_input_is_400_before_any_model_call(inp):
    model = FakeModel()
    with pytest.raises(TaskError) as e:
        run(model, **inp)
    assert (e.value.status, e.value.etype) == (400, "invalid_input") and model.calls == []


def test_the_model_sees_only_the_answer_the_proof_and_the_parts():
    model, _, _, _ = run()
    assert len(model.calls) == 1  # one call, no second pass
    purpose, payload, kw = model.calls[0]
    assert purpose == "find_client_parts" and kw["max_tokens"] == 1024
    assert set(payload) == {"answer", "proof", "parts"}
    assert payload["answer"] == "rejected" and payload["proof"] == PROOF
    assert payload["parts"] == [{"address": SMP, "label": "Single-minded proposition",
                                 "value": "Summer tastes better without the hangover."},
                                {"address": AUD, "label": "Audience", "value": None},
                                {"address": TONE, "label": "Tone", "value": '{"words":["warm","wry"]}'}]
    assert SECRET not in json.dumps(kw, default=str)  # nothing of the document the host sent for transport
    assert "tools" not in kw


def test_values_are_clipped_and_extra_part_keys_never_reach_the_model():
    model, _, _, _ = run(parts=[dict(PARTS[0], value="v" * 5000, confidential=True, hash="sha256:abc")])
    part = model.calls[0][1]["parts"][0]
    assert set(part) == {"address", "label", "value"} and len(part["value"]) == 2000


def test_it_runs_on_the_capture_view_alone():
    """No retrieval, research or layers: a caps object that has only the view is enough."""
    model = FakeModel()
    view = caps_for(model).capture_view()
    result, change, _ = fcp.run(req(), SimpleNamespace(capture_view=lambda: view))
    assert result["suggestions"] and change is None
    assert not hasattr(view, "layers") and not hasattr(view, "retrieval") and not hasattr(view, "research")


def test_a_failed_model_call_is_an_error_never_an_invented_list():
    model = FakeModel(overrides={"find_client_parts": lambda p: "not json"})
    with pytest.raises(ModelError):
        run(model)


def test_verbatim_ws_collapses_whitespace_only():
    text = "One  line,\nthen “another”."
    assert verbatim_ws(text, "One line, then") == "One  line,\nthen"
    assert verbatim_ws(text, " then “another”. ") == "then “another”."
    assert verbatim_ws(text, 'then "another".') is None   # no quote-mark folding
    assert verbatim_ws(text, "one line") is None          # no case folding
    assert verbatim_ws(text, "") is None and verbatim_ws(text, None) is None


def test_it_resolves_for_a_pipeline_that_does_not_declare_it():
    mod, handler = registry.resolve("find_client_parts", {"pipeline": {"tasks": {"draft_brief": "draft_brief@1"}}})
    assert mod is fcp and handler == "find_client_parts@1.0"
    assert registry.resolve("find_client_parts", {})[1] == "find_client_parts@1.0"


def test_over_http_the_reply_is_short_and_its_change_null(server):
    body = {"request_kind": "middleware",
            "payload": {"task": "find_client_parts", "input": {"answer": "rejected", "proof": PROOF, "parts": PARTS}},
            "clan": {"id": DOC, "version": "sha256:x", "pipeline": {"tasks": {"draft_brief": "draft_brief@1"}}}}
    r = httpx.post(server.url + "/v1/tasks", json=body, timeout=10)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["handler"] == "find_client_parts@1.0" and out["job"]["state"] == "done" and out["change"] is None
    assert {s["address"] for s in out["result"]["suggestions"]} == {SMP, AUD}
    assert all(s["quote"] in PROOF for s in out["result"]["suggestions"])
    r = httpx.post(server.url + "/v1/tasks", json={**body, "payload": {"task": "find_client_parts",
                                                                        "input": {"answer": "accepted", "proof": PROOF,
                                                                                  "parts": PARTS}}}, timeout=10)
    assert r.status_code == 400 and r.json()["error"]["type"] == "invalid_input"
