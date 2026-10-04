"""No stage halts a job (the owner, 2026-09-30: "no stage should halt a brief or
Research being produced, that's the worst thing we can do").

Every way a start_campaign or draft_brief job used to stall or stop — a stage
that raises, one the model refuses or answers malformed, one that outlives its
time, a change the host refuses, no poll bringing the earlier stages, a
question nobody answers — now degrades to a recorded gap, and the job ends
`done` with what it has. The campaign job is driven directly with the fakes and
a small host that applies each reply's change (and refuses what a test says).
"""

import copy
import importlib.util
import threading
import time
from types import SimpleNamespace

import pytest

from napkin import jobs as jobs_mod
from napkin.capabilities import Capabilities
from napkin.doc import apply_patch
from napkin.handlers import start_campaign
from napkin.model import ModelPort
from napkin.pipeline import campaign
from napkin.reasoning import problems
from napkin.util import TaskError

from conftest import Server, contract_suite
from fakes import FakeModel, FakeResearch

spec = importlib.util.spec_from_file_location("contract_test", contract_suite())
ct = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ct)

SCOPE = {"org": "org/test-agency", "brand": "brand/test"}
BMW = "BMW is trying to enter the Ev hybrid market in Ireland. Make a campaign clan on that"
LABELLED = "Brand: Harbour Tonic\nLaunch the tonic in Ireland this spring."
TONIC = ["soft_drinks.carbonates"]
TWO_BRANDS = "The brands in this brief: Alpha One and Beta Two. Launch it in Ireland."


class Model(FakeModel):
    """The fakes, plus what a real model does when it will not answer: `refuse`
    purposes come back as a refusal (stop_reason refusal), `garble` purposes as
    output that never validates."""

    def __init__(self, refuse=(), garble=(), overrides=None):
        super().__init__(overrides)
        self.refuse, self.garble = set(refuse), set(garble)

    def create(self, **kw):
        import json
        import re
        user = kw["messages"][0]["content"]
        if isinstance(user, list):
            user = next(p["text"] for p in user if p.get("type") == "text")
        purpose = re.match(r"Task: (\w+)", user).group(1)
        if purpose in self.refuse:
            self.calls.append((purpose, {}, kw))
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="")], stop_reason="refusal",
                                   usage=SimpleNamespace(input_tokens=1, output_tokens=1))
        if purpose in self.garble:
            self.calls.append((purpose, {}, kw))
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps({"nope": 1}))],
                                   stop_reason="end_turn", usage=SimpleNamespace(input_tokens=1, output_tokens=1))
        return super().create(**kw)


def caps_for(store, model=None, research=None):
    return Capabilities(handler="start_campaign@1.0", scope=SCOPE,
                        model_port=ModelPort(model or FakeModel(), "claude-opus-5", 30),
                        research_port=research or FakeResearch(), layer_store=store,
                        research_semaphore=threading.Semaphore(4))


def settings(**kw):
    base = dict(reuse_days=30, research_concurrency=4, question_wait=30.0, report_wait=30.0, stage_timeout=30.0)
    base.update(kw)
    return SimpleNamespace(**base)


class MiniHost:
    """One campaign document: applies a change whole, idempotently, unless
    `refuse(change)` says the host would refuse it (as the real host refuses a
    change whole)."""

    def __init__(self, prompt, refuse=None, categories=None):
        self.doc, self.data, self.inp, self.facts, self.chain = ct.start_doc(prompt)
        self.findings, self.version, self.refuse = [], 1, refuse or (lambda ch: False)
        self.refused = 0
        if categories:  # the person stated them before starting: no question
            self.data["campaign"]["categories"] = {"value": list(categories), "origin": "stated", "by": "human:u",
                                                   "gate": "research", "decision": "d_TESTSTATED01"}
            self.chain.insert(0, {"id": "d_TESTSTATED01", "kind": "edit", "agent": "human", "action": "edit",
                                  "targets": [f"{self.doc}#campaign.categories"], "rationale": "stated"})

    def clan(self):
        return {"id": self.doc, "version": f"v{self.version}", "data": copy.deepcopy(self.data),
                "facts": copy.deepcopy(self.facts), "findings": copy.deepcopy(self.findings),
                "decision_chain": {"decisions": copy.deepcopy(self.chain)}}

    def apply(self, ch):
        if not ch:
            return
        have = {d["id"] for d in self.chain}
        new = [d for d in ch["decisions"] if d["id"] not in have]
        if not new:
            return
        if self.refuse(ch):
            self.refused += 1
            return
        self.data = apply_patch(self.data, ch["data_patch"] or {})
        self.facts += [f for f in ch.get("facts_append") or [] if f["id"] not in {x["id"] for x in self.facts}]
        self.findings += [f for f in ch.get("findings_append") or []
                          if f["id"] not in {x["id"] for x in self.findings}]
        self.chain = new + self.chain
        self.version += 1

    def actions(self):
        return [d.get("action") for d in self.chain]

    def messages(self):
        return [m["text"] for m in (self.data.get("intake") or {}).get("messages", {}).values() if m["role"] == "agent"]


def start(store, host, model=None, research=None, **kw):
    req = SimpleNamespace(doc=host.doc, clan=host.clan(), inp=host.inp, handler="start_campaign@1.0")
    job = start_campaign.start(req, caps_for(store, model, research), settings(**kw), "job_test0001")
    job.start()
    return job


def drive(job, host, answer=None, timeout=30, poll=True):
    """Poll the way the view does, applying each reply's change, to the end."""
    t0 = time.monotonic()
    while True:
        assert time.monotonic() - t0 < timeout, f"the job did not finish: {job.view()}"
        if poll:
            host.apply(job.reply_change(host.clan()))
        v = job.view()
        if v["state"] == "needs_input" and answer:
            answer(job, host, v["question"])
        if v["state"] in ("done", "failed"):
            for _ in range(2 * campaign.REFUSED_AFTER + 2):  # what finished since the last poll
                host.apply(job.reply_change(host.clan()))
            return v
        time.sleep(0.005)


def reasoned(host):
    for d in host.chain:
        if d.get("agent") != "human":
            assert problems(d.get("reasoning")) == [], (d["action"], problems(d.get("reasoning")))


# -- the known deadlock ------------------------------------------------------------

def test_a_refused_stage_no_longer_deadlocks_the_job(store):
    """The report stage waited for a poll whose document held every earlier
    stage; a change the host refused never got there, so the job stayed
    `running` for ever and the document was 409 until a restart (the audit,
    research-agents.html §4, item 3). Now the refused change is found, sent on
    its own, dropped, and its stage is a gap; the stages after it land."""
    host = MiniHost(BMW, refuse=lambda ch: any(d["action"] == "research_merge" for d in ch["decisions"]))
    def answer(job, h, q):  # the categories question: the person picks both, as the view writes it
        assert q["address"].endswith("#campaign.categories")
        h.data["campaign"]["categories"] = {"value": ["automotive.ev_charging", "automotive.hybrid"],
                                            "origin": "confirmed", "confirmed_from": "extracted", "by": "human:u",
                                            "gate": "research", "decision": "d_TESTANSWER01"}
        h.chain.insert(0, {"id": "d_TESTANSWER01", "kind": "edit", "agent": "human", "action": "answer",
                           "targets": [f"{h.doc}#campaign.categories"], "rationale": "picked both"})
        job.answer(h.clan(), {"question_id": q["id"], "option_id": "both"})
    job = start(store, host, report_wait=3600)  # the report's own wait cannot be what frees it
    v = drive(job, host, answer)
    assert v["state"] == "done" and v["error"] is None
    assert host.refused >= 2 * campaign.REFUSED_AFTER  # sent with the others, then alone
    assert "research_merge" not in host.actions()
    assert {"stage": "research", "reason": "not_applied"} in job.gaps
    gap = next(d for d in host.chain if d["action"] == "research" and "did not apply" in d["rationale"])
    assert gap["reasoning"]["attention"].startswith("We couldn't finish looking it up")
    # what came after it landed: synthesis and the report stage (with or without a report)
    assert "report" in host.actions()
    assert any("the document did not take what it wrote" in m for m in host.messages())
    # the chat speaks the crew's words: no stage names, no internal terms
    for m in host.messages():
        assert "stage" not in m and "middleware" not in m and "lens x market" not in m, m
    assert job.summary().startswith(("Campaign ready", "Campaign finished")) and "research" in job.summary()
    reasoned(host)


def test_a_poll_that_never_comes_does_not_hold_the_report(store):
    """Nobody polls (the laptop closed): the report composes from the job's own
    copy once REPORT_WAIT passes, and lands with everything else on the next poll."""
    host = MiniHost(LABELLED, categories=TONIC)
    job = start(store, host, report_wait=0.2)
    v = drive(job, host, poll=False)
    assert v["state"] == "done"
    host.apply(job.reply_change(host.clan()))
    rpt = host.data["report"]
    assert rpt["headline"]["cites"] and host.facts
    d = next(d for d in host.chain if d["action"] == "report")
    assert "own copy" in d["reasoning"]["attention"]
    # its cites resolve in the document it landed in
    ids = {f["id"] for f in host.facts} | {f["id"] for f in host.findings}
    assert set(rpt["headline"]["cites"]) <= ids
    reasoned(host)


# -- stages that fail -------------------------------------------------------------------

def test_a_stage_that_raises_is_a_gap_and_the_next_stage_runs(store, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("synthesis fell over")
    monkeypatch.setattr(campaign.synth_stage, "run_synthesis", boom)
    host = MiniHost(LABELLED, categories=TONIC)
    job = start(store, host)
    v = drive(job, host)
    assert v["state"] == "done" and v["error"] is None
    assert {"stage": "synthesise", "reason": "raised"} in job.gaps
    assert job.gap_list() == job.gaps  # result.gaps: the view shows the step as skipped
    gap = next(d for d in host.chain if d["action"] == "synthesise")
    a = gap["reasoning"]["attention"]
    assert a.startswith("We couldn't finish working out the points: something went wrong on our side.")
    assert "(What went wrong: RuntimeError" in a
    assert gap["reasoning"]["decided"] == "Went on without the synthesise stage: it stopped with an error."
    assert gap["reasoning"]["rejected"][0]["option"] == "stop the campaign at the synthesise stage"
    assert host.data["report"]["headline"]["cites"]  # the report stands on the pins
    assert "stage_failed" not in host.actions()
    reasoned(host)


def test_a_research_stage_that_raises_names_every_pair_as_a_gap(store, monkeypatch):
    class Broken:
        def __init__(self, *a, **kw):
            pass

        def run(self):
            raise ConnectionError("research service gone")
    monkeypatch.setattr(campaign, "Researcher", Broken)
    host = MiniHost(LABELLED, categories=TONIC)
    job = start(store, host)
    v = drive(job, host)
    assert v["state"] == "done"
    gaps = host.data["selection"]["gaps"]
    assert len(gaps) == len(campaign.LENSES) and all("research did not run" in g["note"] for g in gaps)
    runs = [d for d in host.chain if d["action"] == "research_run"]
    assert len(runs) == len(gaps)
    # each one is what the view's "Ask to look again" runs: its lens and market
    assert all(any("#selection.lenses_run[" in t for t in d["targets"]) for d in runs)
    # nothing to cite: no report, and why
    assert "report" not in host.data
    none = next(d for d in host.chain if d["action"] == "report")
    assert none["reasoning"]["decided"] == "Wrote no report: the document holds nothing to cite."
    assert job.summary().startswith("Campaign finished without a report")
    reasoned(host)


def test_a_refused_or_malformed_select_runs_the_default_plan(store):
    for model in (Model(refuse={"select"}), Model(garble={"select"})):
        host = MiniHost(LABELLED, categories=TONIC)
        job = start(store, host, model=model)
        v = drive(job, host)
        assert v["state"] == "done"
        kind = "refusal" if model.refuse else "invalid_output"
        assert {"stage": "select", "reason": kind} in job.gaps
        sel = next(d for d in host.chain if d["action"] == "select")
        assert sel["reasoning"]["certainty"]["level"] == "medium"
        assert "could not plan the research" in sel["reasoning"]["attention"]
        assert len(host.data["selection"]["lenses_run"]) == len(campaign.LENSES)  # every lens, the one market


def test_a_refused_identify_asks_the_person_instead(store):
    host = MiniHost(LABELLED, categories=TONIC)
    job = start(store, host, model=Model(refuse={"identify"}))
    t0 = time.monotonic()
    while job.view()["state"] != "needs_input":
        assert time.monotonic() - t0 < 10, job.view()
        host.apply(job.reply_change(host.clan()))
        time.sleep(0.005)
    q = job.view()["question"]
    assert q["address"].endswith("#campaign.brand") and q["options"] == []
    assert any("could not read the material for the brand" in m for m in host.messages() + [
        c.message["text"] for c in job.chunks if c.message])
    assert {"stage": "identify", "reason": "refusal"} in job.gaps
    with job.lock:
        job.settings.question_wait = 0.01
        job.cond.notify_all()
    assert drive(job, host)["state"] == "done"


def test_a_stage_that_outlives_its_time_is_left_behind(store, monkeypatch):
    release = threading.Event()
    real = campaign.synth_stage.run_synthesis

    def slow(*a, **kw):
        release.wait(10)
        return real(*a, **kw)
    monkeypatch.setattr(campaign.synth_stage, "run_synthesis", slow)
    host = MiniHost(LABELLED, categories=TONIC)
    job = start(store, host, stage_timeout=0.3)
    v = drive(job, host)
    assert v["state"] == "done"
    assert {"stage": "synthesise", "reason": "timeout"} in job.gaps
    n = len(job.chunks)
    release.set()  # the stage left behind finishes now: nothing it writes lands
    time.sleep(0.3)
    assert len(job.chunks) == n
    assert not [c for c in job.chunks if c.stage == "synthesise" and not c.gap]
    assert host.data["report"]["headline"]["cites"]


def _after_the_left_thread_dies(module, monkeypatch):
    """Wrap the module's `bounded` so each stage's thread starts only once the
    thread left behind before it has died: Python then hands the new thread the
    dead one's ident, the case a mark by ident got wrong."""
    real, left_behind, idents = module.bounded, [], []

    def wrapped(fn, seconds, name, stop=None):
        for t in left_behind:
            t.join(5)
        value, err, left = real(fn, seconds, name, stop=stop)
        if left is not None:
            left_behind.append(left)
            idents.append(left.ident)
        return value, err, left
    monkeypatch.setattr(module, "bounded", wrapped)
    return idents


def test_a_stage_after_one_left_behind_still_lands(store, monkeypatch):
    """The stage after a timed-out one ran in a thread that reused the dead
    thread's ident: it was taken for the abandoned one, dropped, and rerun for
    ever. The mark is on the Thread now, so it lands and the job ends."""
    real = campaign.synth_stage.run_synthesis

    def slow(*a, **kw):
        time.sleep(0.4)
        return real(*a, **kw)
    monkeypatch.setattr(campaign.synth_stage, "run_synthesis", slow)
    idents = _after_the_left_thread_dies(campaign, monkeypatch)
    host = MiniHost(LABELLED, categories=TONIC)
    job = start(store, host, stage_timeout=0.3)
    v = drive(job, host, timeout=15)
    assert v["state"] == "done" and idents
    assert job.gaps == [{"stage": "synthesise", "reason": "timeout"}]
    assert host.data["report"]["headline"]["cites"] and "report" in host.actions()
    assert any(c.stage == "report" and not c.gap for c in job.chunks)


# -- a question nobody answers ------------------------------------------------------------

def test_an_unanswered_category_goes_on_with_the_materials_reading(store):
    host = MiniHost(BMW)
    job = start(store, host, question_wait=0.2)
    v = drive(job, host)
    assert v["state"] == "done"
    cats = host.data["campaign"]["categories"]
    assert cats["value"] == ["automotive.ev_charging"] and cats["origin"] == "extracted" and cats["source"]
    d = next(d for d in host.chain if d["id"] == cats["decision"])
    assert d["reasoning"]["certainty"]["level"] == "low" and "confirm the categories" in d["reasoning"]["attention"]
    assert {"stage": "identify", "reason": "unanswered"} in job.gaps
    assert any(m.startswith("Nobody answered in") for m in host.messages())
    # the report asks the person to confirm what the job went on with
    assert any(c["address"].endswith("#campaign.categories") for c in host.data["report"]["confirm"])
    # a late answer is told why the job moved on
    with pytest.raises(TaskError) as e:
        job.answer(host.clan(), {"question_id": "q_x", "option_id": "both"})
    assert e.value.status == 409 and "nobody answered in time" in e.value.message
    reasoned(host)


def test_the_brand_is_never_guessed_when_nobody_answers(store):
    host = MiniHost(TWO_BRANDS)
    job = start(store, host, question_wait=0.2)
    v = drive(job, host)
    assert v["state"] == "done"
    assert "brand" not in host.data["campaign"]
    d = next(d for d in host.chain if d["action"] == "identify" and d["reasoning"]["decided"].startswith(
        "Went on without the brand"))
    assert any(r["option"] == "take Alpha One as the client's brand" for r in d["reasoning"]["rejected"])
    # the next question (the category, with no roster row) goes on at once: nobody is there
    assert "categories" in job.skipped_fields or "categories" in host.data["campaign"]
    assert job.unattended
    reasoned(host)


def test_nothing_settled_still_ends_the_job(store):
    """A model that refuses every call: no brand, no category read, questions
    nobody answers — the job still ends, each gap named."""
    host = MiniHost("Launch it in Ireland this spring.")
    model = Model(refuse={"extract", "identify", "select", "synthesise", "report", "layout"})
    job = start(store, host, model=model, question_wait=0.1)
    v = drive(job, host)
    assert v["state"] == "done" and v["error"] is None
    stages = {g["stage"] for g in job.gaps}
    assert {"extract", "identify", "select", "report"} <= stages
    assert "report" not in host.data and job.summary().startswith("Campaign finished without a report")
    reasoned(host)


# -- the job store ------------------------------------------------------------------------

def test_a_job_whose_worker_died_no_longer_holds_its_document():
    store = jobs_mod.JobStore()
    dead = threading.Thread(target=lambda: None)
    dead.start()
    dead.join()
    j = SimpleNamespace(id="job_dead", task="start_campaign", doc="d1", scope=SCOPE, state="running", thread=dead)
    store.add(j)
    assert store.unfinished_campaign(SCOPE, "d1") is None
    live = threading.Event()
    t = threading.Thread(target=live.wait)
    t.start()
    try:
        store.add(SimpleNamespace(id="job_live", task="start_campaign", doc="d1", scope=SCOPE, state="running",
                                  thread=t))
        assert store.unfinished_campaign(SCOPE, "d1").id == "job_live"
    finally:
        live.set()


def test_bounded_returns_what_ran_what_raised_and_what_it_left_behind():
    assert jobs_mod.bounded(lambda: 7, 1, "t")[:2] == (7, None)
    _, err, left = jobs_mod.bounded(lambda: 1 / 0, 1, "t")
    assert isinstance(err, ZeroDivisionError) and left is None
    gate = threading.Event()
    _, err, left = jobs_mod.bounded(lambda: gate.wait(5), 0.05, "t")
    assert err is None and left is not None and left.is_alive()
    jobs_mod.abandon(left)
    gate.set()
    left.join(5)
    # a later thread (which may carry the dead one's ident) is not the one left behind
    assert jobs_mod.bounded(lambda: jobs_mod.abandoned(), 1, "t")[:2] == (False, None)
    # an Abandoned raised in a thread nobody left behind is an error, never a clean None

    def stray():
        raise jobs_mod.Abandoned("x")
    _, err, left = jobs_mod.bounded(stray, 1, "t")
    assert isinstance(err, jobs_mod.Abandoned) and left is None


# -- Brief Maker ---------------------------------------------------------------------------

from test_brief import Host as BriefHost, check_change_rules, retrieval, run as brief_run  # noqa: E402


def _brief(tmp_path, model=None):
    return Server(tmp_path, model=model or FakeModel(), retrieval=retrieval())


def test_a_draft_stage_that_raises_leaves_the_judge_to_review_what_there_is(tmp_path, monkeypatch):
    from napkin.brief import job as bj

    def boom(*a, **kw):
        raise RuntimeError("the drafters fell over")
    monkeypatch.setattr(bj, "run_drafters", boom)
    s = _brief(tmp_path)
    try:
        host = BriefHost()
        last = brief_run(s, host)[-1]
    finally:
        s.stop()
    assert last["job"]["state"] == "done" and last["job"]["error"] is None
    assert last["result"]["gaps"][0]["stage"] == "draft"
    f = last["result"]["fields"]
    assert f["insight"]["state"] == "failed" and "insight" not in host.data
    assert host.data["background"]  # the capture landed
    assert any(d["action"] == "draft" and d["targets"] == [] for d in host.chain)
    assert host.data["review"]["judge"]  # the Judge reviewed what the brief holds
    check_change_rules(host)


def test_a_judge_stage_that_raises_writes_no_draft_unjudged(tmp_path, monkeypatch):
    from napkin.brief import job as bj

    def boom(*a, **kw):
        raise KeyError("rubric")
    monkeypatch.setattr(bj, "coherence", boom)
    s = _brief(tmp_path)
    try:
        host = BriefHost()
        last = brief_run(s, host)[-1]
    finally:
        s.stop()
    assert last["job"]["state"] == "done"
    assert last["result"]["gaps"][0] == {"stage": "judge", "reason": "raised", "detail": "KeyError: 'rubric'"}
    assert "insight" not in host.data and last["result"]["fields"]["insight"]["state"] == "failed"
    gap = next(d for d in host.chain if d["action"] == "review" and d["targets"] == [])
    assert gap["agent"].endswith("/judge") and "no draft is written unjudged" in gap["reasoning"]["attention"]
    check_change_rules(host)


def test_a_scorecard_that_fails_leaves_the_capture_standing(tmp_path):
    model = Model(refuse={"scorecard"})
    s = _brief(tmp_path, model)
    try:
        host = BriefHost()
        last = brief_run(s, host)[-1]
    finally:
        s.stop()
    assert last["job"]["state"] == "done"
    assert host.data["capture"]["items"] and host.data["background"]
    assert last["result"]["gaps"][0]["stage"] == "extract" and last["result"]["gaps"][0]["reason"] == "refusal"
    cap = next(d for d in host.chain if d["action"] == "capture")
    assert "scorecard could not be made" in cap["reasoning"]["attention"]
    assert not any(d["action"] == "score" for d in host.chain)
    check_change_rules(host)


def test_a_brief_stage_that_outlives_its_time_is_left_behind(tmp_path, monkeypatch):
    from napkin.brief import job as bj
    release = threading.Event()
    real = bj.run_drafters

    def slow(*a, **kw):
        release.wait(10)
        return real(*a, **kw)
    monkeypatch.setattr(bj, "run_drafters", slow)
    monkeypatch.setattr(bj, "STAGE_TIMEOUT", 0.3)
    s = _brief(tmp_path)
    try:
        host = BriefHost()
        last = brief_run(s, host)[-1]
        assert last["job"]["state"] == "done"
        assert last["result"]["gaps"][0]["stage"] == "draft" and last["result"]["gaps"][0]["reason"] == "timeout"
        release.set()
        time.sleep(0.3)
        st, again = __import__("test_brief").post(s, "job_status", {"job_id": last["job"]["id"]}, host.clan())
        assert again["result"]["fields"]["insight"]["state"] == "failed"  # the late drafters moved nothing
        assert "insight" not in host.data
    finally:
        release.set()
        s.stop()


def test_a_brief_stage_after_one_left_behind_still_lands(tmp_path, monkeypatch):
    from napkin.brief import job as bj
    real = bj.run_drafters

    def slow(*a, **kw):
        time.sleep(0.4)
        return real(*a, **kw)
    monkeypatch.setattr(bj, "run_drafters", slow)
    monkeypatch.setattr(bj, "STAGE_TIMEOUT", 0.3)
    idents = _after_the_left_thread_dies(bj, monkeypatch)
    s = _brief(tmp_path)
    try:
        host = BriefHost()
        last = brief_run(s, host)[-1]
    finally:
        s.stop()
    assert last["job"]["state"] == "done" and idents
    assert [g["stage"] for g in last["result"]["gaps"]] == ["draft"]
    assert host.data["review"]["judge"]  # the Judge after it landed
    check_change_rules(host)
