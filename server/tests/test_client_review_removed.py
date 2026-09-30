"""Client review asks nothing of the middleware (middleware-api.md §11,
2026-09-30): the host matches the client's words to the parts itself, so
`find_client_parts` is no task here, and no rule orders decisions by their
stamps (Contract 4 §3)."""

import httpx
import pytest

from napkin import registry
from napkin.doc import human_owned

DOC = "3f2a"


def test_find_client_parts_is_no_task_and_no_handler():
    assert "find_client_parts" not in registry.TASKS
    assert "find_client_parts" not in registry.REGISTRY
    assert "find_client_parts" not in registry.BUILTIN_PIPELINE
    assert "find_client_parts" not in registry.REVIEW_TASKS
    with pytest.raises(Exception):
        registry.lookup("find_client_parts@1", "find_client_parts")


def test_over_http_it_is_an_unknown_task(server):
    body = {"request_kind": "middleware",
            "payload": {"task": "find_client_parts",
                        "input": {"answer": "rejected", "proof": "The audience is wrong.",
                                  "parts": [{"address": f"{DOC}#audience", "label": "Audience"}]}},
            "clan": {"id": DOC, "version": "sha256:x"}}
    r = httpx.post(server.url + "/v1/tasks", json=body, timeout=10)
    assert r.status_code == 400, r.text
    assert r.json()["error"]["type"] == "unknown_task"


def verdict(i, ts):
    return {"id": f"d_v{i}", "kind": "verdict", "polarity": "bad", "timestamp": ts,
            "targets": [f"{DOC}#campaign.audience"], "actor": "human:aoife"}


def edit(i, ts):
    return {"id": f"d_e{i}", "kind": "edit", "timestamp": ts, "targets": [f"{DOC}#campaign.audience"],
            "actor": "human:aoife"}


def test_an_edit_written_after_the_verdict_answers_it_whatever_its_stamp():
    # Newest first, as the host sends the chain: the edit came after.
    chain = [edit(1, "2001-01-01T00:00:00Z"), verdict(1, "2099-12-31T23:59:59Z")]
    assert human_owned({}, chain, DOC, "audience") is None


def test_an_edit_written_before_the_verdict_does_not_answer_it_whatever_its_stamp():
    chain = [verdict(1, "2001-01-01T00:00:00Z"), edit(1, "2099-12-31T23:59:59Z")]
    assert human_owned({}, chain, DOC, "audience") == "an unanswered bad verdict stands on it"


def test_brief_holder_reads_the_chain_newest_first():
    """Brief Maker's human-held rule (§10.5) takes the latest writer from the
    chain as the host sends it, newest first — never the stamps."""
    from napkin.brief.fields import holder, last_writer
    D = "b1"
    person = {"id": "d_p", "kind": "edit", "actor": "human:aoife", "targets": [f"{D}#audience"],
              "timestamp": "2020-01-01T00:00:00Z"}  # a clock that is wrong: older stamp, newer in the chain
    drafter = {"id": "d_m", "kind": "edit", "actor": "process:middleware", "targets": [f"{D}#audience"],
               "timestamp": "2026-09-30T00:00:00Z"}
    data = {"audience": "x"}
    assert holder(D, data, [person, drafter], "audience") == "a person wrote it last"
    assert last_writer(D, [person, drafter], "audience") == "d_p"
    assert holder(D, data, [drafter, person], "audience") is None
    assert last_writer(D, [drafter, person], "audience") == "d_m"
    bad = {"id": "d_v", "kind": "verdict", "polarity": "bad", "actor": "human:aoife", "targets": [f"{D}#audience"]}
    # an edit newer than the person's bad verdict answers it; one older does not
    assert holder(D, data, [drafter, bad], "audience") is None
    assert holder(D, data, [bad, drafter], "audience") == "a person's bad verdict on it is unanswered"
