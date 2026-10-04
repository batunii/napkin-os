"""ask_research: a person's question answered in its section; jev decides whether to search."""

import threading
from types import SimpleNamespace

import pytest

from napkin import registry
from napkin.capabilities import Capabilities
from napkin.model import ModelPort
from napkin.pipeline.ask import Asker, validate
from napkin.pipeline.research import Researcher
from napkin.util import TaskError

from fakes import FakeModel, FakeResearch

SCOPE = {"org": "org/test-agency", "brand": "brand/test"}
DOC = "11111111-2222-4333-8444-555555555555"
SETTINGS = SimpleNamespace(reuse_days=30, research_concurrency=2)


class FakeJev:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def ask(self, state, questions):
        self.calls.append((state, questions))
        a = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return {n: (0.9 if n == "grounded" else a) for n in questions}


def stated(v, gate):
    return {"value": v, "origin": "stated", "gate": gate, "by": "human:u", "decision": "d_TESTSTATED1"}


def caps_for(store, model=None, research=None, jev=None):
    return Capabilities(handler="t@1.0", scope=SCOPE, model_port=ModelPort(model or FakeModel(), "claude-opus-5", 30),
                        research_port=research or FakeResearch(), layer_store=store,
                        research_semaphore=threading.Semaphore(4), jev_port=jev)


def researched(store):
    clan = {"id": DOC, "version": "3", "facts": [], "findings": [], "decision_chain": {"decisions": []},
            "data": {"campaign": {"brand": stated({"ref": "brand/bmw", "name": "BMW"}, "created"),
                                  "categories": stated(["automotive.ev_charging"], "research"),
                                  "markets": stated(["IE"], "research")}}}
    _, ch, _ = Researcher(DOC, "3", clan, "t@1.0", caps_for(store), ["media_spend"], ["IE"],
                          ["automotive.ev_charging"]).run()
    return dict(clan, facts=ch["facts_append"])


def answering(missing=(), figure=False):
    def r(p):
        pid = p["pins"][0]["id"]
        s = [{"text": "Spend leans on the channel the research names.", "cites": [pid]}]
        if figure:
            s.append({"text": "It is 87% of all spend.", "cites": [pid]})
        return {"sentences": s, "missing": list(missing), "lens": p["lens"] or "media_spend", "topic": "Where spend goes"}
    return r


def ask(store, clan, q="Where does the money go?", lens="media_spend", jev=None, model=None, research=None):
    caps = caps_for(store, model=model or FakeModel({"ask_answer": answering()}), research=research, jev=jev)
    return Asker(DOC, "3", clan, "t@1.0", caps, q, lens, SETTINGS).run()


def test_an_answer_the_facts_hold_is_written_in_its_section_without_a_search(store):
    clan = researched(store)
    research, jev = FakeResearch(), FakeJev(0.83)
    result, change, _ = ask(store, clan, jev=jev, research=research)
    assert research.calls == [] and not result["searched"]
    rec = next(iter(change["data_patch"]["asks"].values()))
    assert rec["lens"] == "media_spend" and rec["in_section"] and rec["status"] == "answered"
    assert rec["answer"][0]["cites"] and not rec["searched"] and rec["new_facts"] == []
    assert "0.83" not in str(rec)  # jev's scores are never on the page
    acts = [d["action"] for d in change["decisions"]]
    assert acts == ["check_answer", "ask_research"]
    assert "0.83" in str(change["decisions"][0]["reasoning"])  # they are in the record
    assert all(t.endswith(f"#asks[{rec['id']}]") for d in change["decisions"] for t in d["targets"])
    assert change["facts_append"] == [] and "selection" not in change["data_patch"]


def test_when_jev_says_it_does_not_answer_the_researchers_search_with_the_question(store):
    clan = researched(store)
    research, jev = FakeResearch(), FakeJev(0.2, 0.7)
    result, change, _ = ask(store, clan, q="What share of spend is on TV?", jev=jev, research=research)
    assert research.calls == [("media_spend", "IE")] and result["searched"]
    rec = next(iter(change["data_patch"]["asks"].values()))
    assert rec["searched"] and rec["status"] == "answered" and len(jev.calls) == 2
    acts = [d["action"] for d in change["decisions"]]
    assert acts[0] == "check_answer" and acts[-1] == "ask_research" and "research_run" in acts
    # a search on one question is not the lens's run: its runs, coverage and gaps stay
    assert not set(change["data_patch"].get("selection") or {}) - {"contested"}
    for d in change["decisions"]:
        assert not [t for t in d["targets"] if "#selection.lenses_run" in t or "#selection.coverage" in t]


def test_without_jev_what_the_answer_says_is_missing_decides(store):
    clan = researched(store)
    r1 = FakeResearch()
    _, ch1, _ = ask(store, clan, research=r1, model=FakeModel({"ask_answer": answering(missing=["TV share of spend"])}))
    # still partial after the first search: a second one, on what the answer says is missing
    assert r1.calls == [("media_spend", "IE"), ("media_spend", "IE")]
    rec = next(iter(ch1["data_patch"]["asks"].values()))
    assert len(rec["searches"]) == 2 and "Specifically: TV share of spend" in rec["searches"][1]["focus"]
    assert rec["web_sources"] >= 1
    r2 = FakeResearch()
    _, change, _ = ask(store, clan, research=r2)
    assert r2.calls == []
    assert "Not checked by jev" in str(change["decisions"][0]["reasoning"])


def test_a_sentence_with_a_figure_nothing_cited_holds_is_dropped(store):
    clan = researched(store)
    _, change, _ = ask(store, clan, jev=FakeJev(0.9), model=FakeModel({"ask_answer": answering(figure=True)}))
    rec = next(iter(change["data_patch"]["asks"].values()))
    assert len(rec["answer"]) == 1 and "87" not in str(rec["answer"])


def test_a_question_outside_the_sections_gets_its_own(store):
    clan = researched(store)
    _, change, _ = ask(store, clan, q="Are there grants for home chargers?", lens=None, jev=FakeJev(0.9))
    rec = next(iter(change["data_patch"]["asks"].values()))
    assert not rec["in_section"] and rec["topic"] == "Where spend goes" and rec["lens"] == "media_spend"


def test_the_question_is_checked_before_any_job(store):
    clan = researched(store)
    with pytest.raises(TaskError):
        validate(clan, {"question": "  "})
    with pytest.raises(TaskError):
        validate(clan, {"question": "Why?", "lens": "astrology"})
    assert validate(clan, {"question": " Why? "}) == ("Why?", None)


def test_a_document_made_before_the_task_can_still_ask():
    clan = {"pipeline": {"tasks": {"compose_report": "compose_report@1"}}}
    mod, handler = registry.resolve("ask_research", clan)
    assert handler == "ask_research@1.0"
