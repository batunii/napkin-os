"""A fact marked wrong: it is excluded, what rested on it is checked without it, and what
no longer stands is worked out again (findings, answers, the report)."""

import json
import threading
from types import SimpleNamespace

import pytest

from napkin.capabilities import Capabilities
from napkin.model import ModelPort
from napkin.pipeline.reconsider import Reconsider, validate
from napkin.pipeline.research import Researcher
from napkin.util import TaskError

from fakes import FakeModel, FakeResearch
from test_ask import FakeJev, stated

SCOPE = {"org": "org/test-agency", "brand": "brand/test"}
DOC = "11111111-2222-4333-8444-555555555555"
SETTINGS = SimpleNamespace(reuse_days=30, research_concurrency=2)


def caps_for(store, model=None, jev=None):
    return Capabilities(handler="t@1.0", scope=SCOPE, model_port=ModelPort(model or FakeModel(), "claude-opus-5", 30),
                        research_port=FakeResearch(), layer_store=store, research_semaphore=threading.Semaphore(4),
                        jev_port=jev)


def doc_with_findings(store):
    clan = {"id": DOC, "version": "3", "facts": [], "findings": [], "decision_chain": {"decisions": []},
            "data": {"campaign": {"brand": stated({"ref": "brand/bmw", "name": "BMW"}, "created"),
                                  "categories": stated(["automotive.ev_charging"], "research"),
                                  "markets": stated(["IE"], "research")}}}
    _, ch, _ = Researcher(DOC, "3", clan, "t@1.0", caps_for(store), ["media_spend", "market_structure"], ["IE"],
                          ["automotive.ev_charging"]).run()
    pins = ch["facts_append"]
    media = [p for p in pins if p["key"].startswith("media.")]
    market = [p for p in pins if p["key"].startswith("market.")]
    wrong, other = media[0]["id"], market[0]["id"]
    f = lambda fid, cites, st: {"id": fid, "statement": st, "cites": cites, "method": "synthesis", "status": "proposed",
                                "derived_by": "t@1.0", "confidence": "medium", "derived_at": "2026-10-02T00:00:00Z",
                                "decision": "d_TESTFIND01", "lens": "media_spend"}
    findings = [f("fi_ONLYWRONG01", [wrong], "Spend leans on one channel."),
                f("fi_BOTHPINS01", [wrong, other], "The market and its spend move together.")]
    data = dict(clan["data"], asks={"ask_A": {"id": "ask_A", "question": "Where does the money go?", "lens": "media_spend",
                                              "in_section": True, "answer": [{"text": "Mostly one channel.", "cites": [wrong]}],
                                              "status": "answered", "searched": False, "new_facts": []}},
                report={"headline": {"text": "x", "cites": [wrong]}, "sections": []})
    return dict(clan, facts=pins, findings=findings, data=data), wrong, other


def rework_to(other):
    return lambda p: {"findings": [{"id": x["id"], "statement": "The category's own measure is what remains.",
                                    "cites": [other], "why": "only the market figure is left"} for x in p["findings"]]}


def answer_from(p):
    pid = p["pins"][0]["id"]
    return {"sentences": [{"text": "What remains points elsewhere.", "cites": [pid]}], "missing": [], "lens": "media_spend",
            "topic": "Spend"}


def test_a_fact_marked_wrong_is_taken_out_and_what_rested_on_it_is_worked_out_again(store):
    clan, wrong, other = doc_with_findings(store)
    model = FakeModel({"rework_findings": rework_to(other), "ask_answer": answer_from})
    jev = FakeJev(0.85)  # the finding with another fact still stands; the answer is checked too
    caps = caps_for(store, model=model, jev=jev)
    result, change, _ = Reconsider(DOC, "3", clan, "t@1.0", caps, wrong, "The source is a press release.", None,
                                   SETTINGS).run()
    patch = change["data_patch"]
    assert patch["selection"]["excluded"] == [{"fact_id": wrong, "reason": "The source is a press release.",
                                               "decision": change["decisions"][0]["id"]}]
    checked = {c["id"]: c for c in result["checked"]}
    assert checked["fi_ONLYWRONG01"]["stands"] is False   # nothing else held it up
    assert checked["fi_BOTHPINS01"]["stands"] is True     # jev: the other fact still supports it
    new = change["findings_append"]
    assert len(new) == 1 and new[0]["cites"] == [other] and new[0]["status"] == "proposed"
    assert result["reworked"]["fi_ONLYWRONG01"]["new"] == new[0]["id"]
    # the answer that cited it is answered again, without it
    assert wrong not in json.dumps(patch["asks"]["ask_A"]["answer"])
    # the report is recomposed and cites nothing that rests on the fact
    rep = json.dumps(patch["report"])
    assert wrong not in rep and "fi_ONLYWRONG01" not in rep and "fi_BOTHPINS01" not in rep
    # one decision says what happened; it answers the person's verdict on the fact
    d = change["decisions"][0]
    assert d["action"] == "reconsider_fact" and any(t.endswith(f"#facts[{wrong}]") for t in d["targets"])
    assert "still stands" in json.dumps(d["reasoning"]) and "worked out again" in json.dumps(d["reasoning"])
    acts = [x["action"] for x in change["decisions"]]
    assert acts.count("synthesise_finding") == 1 and "ask_research" in acts and "compose_report" in acts
    # no finding is rejected here: that is a person's
    assert all(f.get("status") == "proposed" for f in new)


def test_without_jev_everything_that_cited_it_is_reworked(store):
    clan, wrong, other = doc_with_findings(store)
    caps = caps_for(store, model=FakeModel({"rework_findings": rework_to(other), "ask_answer": answer_from}))
    result, change, _ = Reconsider(DOC, "3", clan, "t@1.0", caps, wrong, "Wrong year.", None, SETTINGS).run()
    assert all(not c["stands"] for c in result["checked"])


def test_the_fact_and_a_reason_are_required(store):
    clan, wrong, _ = doc_with_findings(store)
    with pytest.raises(TaskError):
        validate(clan, {"fact_id": wrong})
    with pytest.raises(TaskError):
        validate(clan, {"fact_id": "f_NOTHERE001", "why": "x"})
    assert validate(clan, {"fact_id": wrong, "why": " x "})[:2] == (wrong, "x")
