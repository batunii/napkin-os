"""draft_brief through the brief engine (middleware-api.md §10.14): the request the
engine's agent server expects, its reply as the middleware's change, and the
fallback to the middleware's own drafters when the engine gives nothing. The
engine is a stub behind httpx.MockTransport; everything the middleware decides
runs for real."""

import copy
import hashlib
import json
import threading
import time

import httpx
import jsonschema
import pytest

from napkin.brief import engine
from napkin.brief.fields import KEYS
from napkin.brief.job import BriefJob
from napkin.brief.planned import PlannedBriefJob
from napkin.config import Settings
from napkin.handlers import draft_brief
from napkin.reasoning import problems

from conftest import Server
from fakes import BRIEF_TEXT, FakeModel
from test_brief import BRIEF_SCHEMA, DOC, Host, retrieval, run

ENGINE_URL = "http://engine.test"
PIN = "f_01JXF001"
FI = "fi_01JXF0A1"
FI_FACT = "f_01JXFV99"  # the fact a verified finding became; the document holds the finding
SRC = "src_01JXS01"
ATT_TEXT = "Harbour Tonic brief.\nThe client wants midweek volume up.\n"
ATT_SHA = hashlib.sha256(ATT_TEXT.encode()).hexdigest()

UPSTREAM = {"brand": "Harbour Tonic", "category": "drinks", "competitors": ["Low Beer Co"],
            "facts": [{"id": PIN, "version": 2, "status": "current", "entity": "brand", "key": "market.share",
                       "value": "12", "unit": "pct", "as_of": "2025", "scope": "brand",
                       "sources": [{"id": SRC, "title": "Review 2026"}]}],
            "decisions": [{"id": "d_R1", "kind": "rejected_finding", "who": "A", "role": "person",
                           "about": "shops", "statement": "rejected the footfall finding"}],
            "not_an_engine_key": {"x": 1}}

REPLY = {
    "project_name": "Midweek Tonic", "client": "Harbour Drinks Ltd",
    "background": "Harbour Tonic is losing midweek drinkers.",
    "objectives": {"commercial": "Grow midweek volume by 8%.", "behavioural": "Choose Harbour on a weeknight."},
    "audience": "Lapsed weeknight drinkers.",
    "insight": "A weeknight drink has to earn its place.",
    "single_minded_proposition": "The weeknight tonic.",
    "reasons_to_believe": ["Holds 12% of the market", "Only 20 calories"],
    "desired_response": {"think": "It fits a weeknight.", "feel": "Easy.", "do": "Pick it up midweek."},
    "tone_and_world": ["Warm", "Plain"],
    "mandatories": ["Harbour logo on every asset."],
    "open_questions": ["[high] How will the work be judged? — No criteria given",
                      "[high] The client brief conflicts with verified research ([F:f_01JXF001 v2] brand "
                      "market.share: 12 pct). Which is current? — the brief and the research disagree"],
    "rationale": "Extracted via anthropic:claude-opus-5; no-loss ledger coverage 91%; golden-brief fill 9/12 "
                 "fields; DRAFTS TO REVIEW (failed their checks): smp.",
    "context": "**Brief quality:** clear.",
    "fact_refs": {"reasons_to_believe": [{"item": 0, "id": PIN, "version": 2, "scope": "brand",
                                          "source_ids": [SRC]}],
                  "insight": [{"item": None, "id": FI_FACT, "version": 1, "scope": "research", "source_ids": []}],
                  "desired_response": [{"item": "think", "id": PIN, "version": 2, "scope": "brand",
                                        "source_ids": [SRC]}]},
    "fact_conflicts": [{"id": PIN, "version": 2, "line": "[F:f_01JXF001 v2] brand market.share: 12 pct", "p": 0.93}],
    "meta": {"research_facts": {"given": 1, "used": [{"id": PIN, "version": 2, "scope": "brand"}], "skipped": []},
             "research_decisions": {"given": 2, "used": [{"id": "d_R1", "kind": "rejected_finding"}],
                                    "skipped": [{"id": "d_R2", "why": "not current (superseded)"}]}},
}


class Engine:
    """The engine's agent server, stubbed: records each request, answers with `reply`
    (a dict, a status, an exception to raise, or a function of the request)."""

    def __init__(self, reply=None, status=200, raise_=None, delay=0.0):
        self.reply, self.status, self.raise_, self.delay = copy.deepcopy(reply or REPLY), status, raise_, delay
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append({"url": str(request.url), "headers": dict(request.headers), "body": body})
        if self.delay:
            time.sleep(self.delay)
        if self.raise_ is not None:
            raise self.raise_
        if callable(self.reply):
            return self.reply(body)
        if isinstance(self.reply, (bytes, str)):
            return httpx.Response(self.status, content=self.reply)
        return httpx.Response(self.status, json=self.reply)


@pytest.fixture
def stub(monkeypatch):
    """Point the adapter's transport at a stub engine the test fills in."""
    holder = {}

    def install(e: Engine):
        holder["e"] = e
        monkeypatch.setattr(engine, "TRANSPORT", httpx.MockTransport(e))
        return e
    return install


@pytest.fixture
def eserver(tmp_path):
    s = Server(tmp_path, retrieval=retrieval(), settings_kw={"brief_engine_url": ENGINE_URL})
    yield s
    s.stop()


def research_host(**kw):
    """A brief spun off from research: a pin, a verified finding and the upstream payload."""
    h = Host(**kw)
    h.facts = [{"id": PIN, "entity": "brand", "key": "market.share", "value": 12, "layer": "brand",
                "origin": "fact://brand/brand/market.share@2", "sources": [SRC]}]
    h.findings = [{"id": FI, "status": "verified", "statement": "Weeknights matter.", "cites": [PIN],
                   "verification": {"decision": "d_01JXV0001", "by": "human:1", "at": "2026-09-01", "fact_id": FI_FACT}}]
    base = h.clan

    def clan():
        c = base()
        c["facts"], c["findings"], c["upstream_payload"] = copy.deepcopy(h.facts), copy.deepcopy(h.findings), UPSTREAM
        return c
    h.clan = clan
    return h


def inp(prompt=BRIEF_TEXT, attachment=True):
    atts = [{"name": "Harbour brief.txt", "sha256": ATT_SHA, "text": ATT_TEXT}] if attachment else []
    return {"prompt": prompt, "attachments": atts}


def check_engine_change(host, extra_ok=()):
    """The §10 rules on an engine change: every write named by a decision, never blank,
    reasoning well formed, every cite resolving in the document, the drafter's name."""
    facts = {f["id"] for f in getattr(host, "facts", [])}
    findings = {f["id"] for f in getattr(host, "findings", [])}
    for ch in host.changes:
        targets = {t for d in ch["decisions"] for t in d["targets"]}
        for top, v in ch["data_patch"].items():
            if top == "materials":
                assert all(f"{DOC}#materials[{k}]" in targets for k in v)
                continue
            leaves = [(f"{top}.{k}", x) for k, x in v.items()] if top in ("objectives", "desired_response") else [(top, v)]
            for path, x in leaves:
                assert x not in ("", [], {}, None), f"{path} blanked"
                assert f"{DOC}#{path}" in targets, f"{path} written without a decision naming it"
        for d in ch["decisions"]:
            assert problems(d["reasoning"]) == [], (d["id"], problems(d["reasoning"]))
            # the engine's brief is Dara's; the check loop's review is Jude's (brief_judge=jev)
            assert d["agent"] in ("draft_brief@1.0/drafter", "draft_brief@1.0/judge") and \
                d["handler"] == "draft_brief@1.0"
            for c in list(d["cites"]) + [c for p in d["reasoning"]["because"] for c in p.get("cites") or []]:
                assert (c.startswith("mat_") and c in host.data.get("materials", {})) or c in facts or c in findings \
                    or c in extra_ok, f"{d['id']} cites {c}, which does not resolve"
    jsonschema.Draft7Validator(BRIEF_SCHEMA).validate(host.data)


# ---------------------------------------------------------------------------------------------------------------

def test_the_request_is_the_shape_the_engine_server_reads(eserver, stub):
    e = stub(Engine())
    host = research_host(data={"client": "Harbour Drinks Ltd", "brief_style": "classic"})
    run(eserver, host, inp=inp())
    assert len(e.requests) == 1
    req = e.requests[0]
    assert req["url"] == ENGINE_URL + "/"
    body = req["body"]
    assert body["request_kind"] == "agent" and set(body) == {"request_kind", "payload", "clan"}
    p = body["payload"]
    # the client's material is the brief the engine captures: the prompt, then each
    # material under our own plain heading, never the engine's ATTACHMENT header
    assert p["task"] == "draft_brief"
    assert p["input"] == f"{BRIEF_TEXT.strip()}\n\n--- From the client: Harbour brief.txt ---\n{ATT_TEXT.strip()}"
    assert "ATTACHMENT" not in p["input"] and p["attachments"] == []
    assert p["loops37"] is True  # the engine's own switch for insight, SMP and RTBs
    # the upstream as the extract printed it, narrowed to the keys run(upstream=...) reads
    assert p["upstream"] == {k: UPSTREAM[k] for k in engine.UPSTREAM_KEYS}
    # only the names the document already has: no chain, no facts, no findings, no other data
    assert body["clan"] == {"data": {"client": "Harbour Drinks Ltd"}}
    assert req["headers"]["x-napkin-handler"] == "draft_brief@1.0" and req["headers"]["x-napkin-job"].startswith("job_")


def test_no_upstream_sends_none_and_an_attachment_only_brief_is_the_brief_text(eserver, stub):
    e = stub(Engine())
    run(eserver, Host(), inp=inp(prompt=None))
    p = e.requests[0]["body"]["payload"]
    assert "upstream" not in p and p["attachments"] == []
    assert p["input"] == f"--- From the client: Harbour brief.txt ---\n{ATT_TEXT.strip()}"
    assert e.requests[0]["body"]["clan"] == {"data": {}}


def test_only_a_vision_transcription_goes_as_supporting_context():
    from napkin.brief.capture import Material
    prompt = Material("mat_p", "prompt", "prompt", "s1", text="Launch it.")
    client = Material("mat_c", "document", "brief.pdf", "s2", text="The client's own brief.")
    shot = Material("mat_i", "image", "moodboard.png", "s3", text="A picture of a bottle.")
    shot.transcribed = {"model": "m", "backend": "b"}
    unread = Material("mat_u", "document", "empty.pdf", "s4", text="")
    body = engine.request_body([prompt, client, shot, unread], {}, None)
    p = body["payload"]
    assert p["input"] == "Launch it.\n\n--- From the client: brief.pdf ---\nThe client's own brief."
    assert p["attachments"] == [{"name": "moodboard.png", "extracted_text": "A picture of a bottle."}]
    assert engine.request_body([prompt], {}, None, loops37=False)["payload"].keys() == {"task", "input",
                                                                                        "attachments"}


def test_the_loops37_switch_is_the_settings(monkeypatch):
    s = Settings(brief_engine_url=ENGINE_URL)
    assert engine.port_for(s).loops37 is True
    monkeypatch.setenv("NAPKIN_BRIEF_ENGINE_URL", ENGINE_URL)
    monkeypatch.setenv("NAPKIN_BRIEF_ENGINE_LOOPS37", "0")
    assert engine.port_for(Settings.from_env()).loops37 is False


def _classify(did, target, model=False, superseded=None):
    d = {"id": did, "kind": "classify", "agent": "human", "actor": "human:1", "action": "classify",
         "targets": [target], "licence": {"model": model, "export": False, "corpus": False}}
    if superseded:
        d["superseded_by"] = superseded
    return d


def test_a_name_marked_not_for_models_is_not_sent_or_taken(eserver, stub):
    e = stub(Engine())
    chain = [_classify("d_MARK00001", f"{DOC}#client")]
    host = Host(data={"client": "Harbour Drinks Ltd", "project_name": "Midweek Tonic"}, chain=chain)
    replies = run(eserver, host, inp=inp())
    body = e.requests[0]["body"]
    assert body["clan"] == {"data": {"project_name": "Midweek Tonic"}}
    assert "Harbour Drinks Ltd" not in json.dumps(body["clan"])
    f = replies[-1]["result"]["fields"]
    assert f["client"]["state"] == "kept" and host.data["client"] == "Harbour Drinks Ltd"
    assert all("client" not in ch["data_patch"] for ch in host.changes)
    check_engine_change(host)


def test_the_newest_mark_decides():
    older = _classify("d_OLD", f"{DOC}#client")
    newer = _classify("d_NEW", f"{DOC}#client", model=True)
    assert engine.model_false(DOC, [newer, older]) == set()
    assert engine.model_false(DOC, [older]) == {"client"}
    assert engine.model_false(DOC, [_classify("d_S", f"{DOC}#client", superseded="d_X")]) == set()
    assert engine.model_false(DOC, [_classify("d_O", "other-doc#client")]) == set()
    # a mark on a path above a leaf holds for the leaf
    assert engine.model_false(DOC, [_classify("d_P", f"{DOC}#objectives")]) >= {
        "objectives.commercial", "objectives.behavioural", "objectives.attitudinal"}


def test_a_held_field_the_engine_echoes_is_not_proposed(eserver, stub):
    stub(Engine())
    chain = [{"id": "d_HUMAN00002", "kind": "edit", "agent": "human:1", "actor": "human:1", "action": "edit",
              "targets": [f"{DOC}#project_name", f"{DOC}#client"]}]
    host = Host(data={"project_name": "Midweek Tonic", "client": "Harbour Drinks Ltd"}, chain=chain)
    replies = run(eserver, host, inp=inp())
    last = replies[-1]
    assert [p["field"] for p in last["result"]["proposals"]] == []
    assert not any(x["action"] == "propose" for x in host.chain)
    assert last["result"]["fields"]["project_name"]["state"] == "done"
    check_engine_change(host)


def test_the_reply_lands_as_the_middlewares_change(eserver, stub):
    stub(Engine())
    host = research_host()
    replies = run(eserver, host, inp=inp())
    last = replies[-1]
    assert last["job"]["state"] == "done" and last["job"]["progress"] == {"done": 3, "total": 3}
    assert last["result"]["engine"] == {"state": "used"}
    assert set(last["result"]["fields"]) == set(KEYS)
    for r in replies:
        assert r["job"]["stage"] in ("extract", "draft", "judge")
    d = host.data
    assert d["background"] == REPLY["background"] and d["objectives"] == REPLY["objectives"]
    assert d["desired_response"] == REPLY["desired_response"] and d["reasons_to_believe"] == REPLY["reasons_to_believe"]
    assert d["open_questions"] == REPLY["open_questions"]
    assert "rationale" not in d and "context" not in d and "fact_refs" not in d and "fact_conflicts" not in d
    assert set(d["materials"]) and all(m["licence"] == "client-confidential" for m in d["materials"].values())
    # no Judge ran over the engine's brief: it checked its own
    assert not any(x["kind"] == "verdict" for x in host.chain) and "review" not in d
    fields = last["result"]["fields"]
    assert fields["insight"] == {"state": "done", "by": "drafter"}
    assert fields["competitor_context"] == {"state": "absent", "by": "drafter"}
    assert fields["budget_and_scope"]["state"] == "absent"
    check_engine_change(host)

    by_target = {}
    for x in host.chain:
        for t in x["targets"]:
            by_target.setdefault(t.split("#", 1)[1], []).append(x)
    # one draft decision per written field, by the drafter (Dara), marked as the engine's
    for k in ("background", "objectives.commercial", "insight", "reasons_to_believe", "desired_response.think",
              "desired_response.feel", "tone_and_world"):
        ds = [x for x in by_target[k] if x["action"] == "draft"]
        assert len(ds) == 1 and ds[0]["drafted_by"] == "brief-engine", k
    rtb = next(x for x in by_target["reasons_to_believe"] if x["action"] == "draft")
    assert PIN in rtb["cites"] and rtb["fact_refs"][0]["item"] == 0
    assert any(PIN in (p.get("cites") or []) for p in rtb["reasoning"]["because"])
    # a finding's fact resolves to the finding, so the drawer opens it
    ins = next(x for x in by_target["insight"] if x["action"] == "draft")
    assert FI in ins["cites"] and FI_FACT not in ins["cites"]
    # a desired-response ref lands on its leaf only
    think = next(x for x in by_target["desired_response.think"] if x["action"] == "draft")
    feel = next(x for x in by_target["desired_response.feel"] if x["action"] == "draft")
    assert PIN in think["cites"] and PIN not in feel["cites"]
    # the engine kept the SMP though it failed its checks: low certainty, and it says so
    smp = next(x for x in by_target["single_minded_proposition"] if x["action"] == "draft")
    assert smp["reasoning"]["certainty"]["level"] == "low" and "failed its checks" in smp["reasoning"]["attention"]
    assert rtb["reasoning"]["certainty"]["level"] == "medium" and "attention" not in rtb["reasoning"]
    # open questions and the conflict are attention items
    oq = next(x for x in by_target["open_questions"] if x["action"] == "questions")
    assert oq["reasoning"]["attention"].startswith("Open questions to answer:")
    conflict = next(x for x in host.chain if x["action"] == "flag_conflict")
    assert conflict["targets"] == [f"{DOC}#facts[{PIN}]"] and PIN in conflict["cites"]
    assert "Settle which is current" in conflict["reasoning"]["attention"]
    assert conflict["fact_conflict"] == {"id": PIN, "version": 2, "p": 0.93}
    # the run: what the engine was given and what it left out
    runs = [x for x in host.chain if x["action"] == "draft_run"]
    assert len(runs) == 1
    rd = runs[0]
    assert rd["research_facts"] == REPLY["meta"]["research_facts"]
    assert rd["research_decisions"] == REPLY["meta"]["research_decisions"]
    assert set(rd["abstained"]) >= {"competitor_context", "budget_and_scope", "objectives.attitudinal"}
    assert "1 research decision(s)" in rd["reasoning"]["attention"]
    assert rd["engine_context"] == REPLY["context"]
    assert all(t.startswith(f"{DOC}#materials[") for t in rd["targets"])
    # the pins the drafts cite are in the trace
    assert any(h["id"] == PIN for h in last["trace"]["hits"])


def test_locked_fields_are_kept_and_held_fields_proposed(eserver, stub):
    stub(Engine())
    chain = [{"id": "d_HUMAN00001", "kind": "edit", "agent": "human:1", "actor": "human:1", "action": "edit",
              "targets": [f"{DOC}#audience"]}]
    host = Host(data={"locked_fields": ["insight"], "audience": "My own audience."}, chain=chain)
    replies = run(eserver, host, inp=inp())
    f = replies[-1]["result"]["fields"]
    assert f["insight"]["state"] == "kept" and "insight" not in host.data
    assert host.data["audience"] == "My own audience." and f["audience"]["state"] == "proposed"
    props = replies[-1]["result"]["proposals"]
    assert [p["field"] for p in props] == ["audience"] and props[0]["value"] == REPLY["audience"]
    prop = next(x for x in host.chain if x["action"] == "propose")
    assert prop["proposed_value"] == REPLY["audience"] and prop["targets"] == [f"{DOC}#audience"]
    assert all("insight" not in ch["data_patch"] for ch in host.changes)
    check_engine_change(host)


def test_a_thin_reply_lands_what_it_has_and_names_the_rest(eserver, stub):
    stub(Engine(reply={"background": "Only the background.", "reasons_to_believe": "not a list: kept as one",
                       "insight": 7, "rationale": "Heuristic extraction (no API keys): captured facts only.",
                       "fact_refs": {"insight": [{"id": "f_NOTHELD", "item": None}]}}))
    host = Host()
    replies = run(eserver, host, inp=inp())
    assert replies[-1]["job"]["state"] == "done" and replies[-1]["result"]["engine"]["state"] == "used"
    assert host.data["background"] == "Only the background."
    assert host.data["reasons_to_believe"] == ["not a list: kept as one"]
    assert "insight" not in host.data
    rd = next(x for x in host.chain if x["action"] == "draft_run")
    assert "insight" in rd["abstained"] and "single_minded_proposition" in rd["abstained"]
    a = rd["reasoning"]["attention"]
    assert "Not written, in a shape the field cannot hold: insight." in a
    assert "captured without its model" in a
    assert replies[-1]["result"]["fields"]["open_questions"]["state"] == "absent"
    check_engine_change(host)


def test_a_fact_the_document_does_not_hold_is_not_cited(eserver, stub):
    reply = copy.deepcopy(REPLY)
    reply["fact_refs"] = {"reasons_to_believe": [{"item": 1, "id": "f_ELSEWHERE", "version": 1}]}
    reply["fact_conflicts"] = []
    stub(Engine(reply=reply))
    host = Host()
    run(eserver, host, inp=inp())
    rtb = next(x for x in host.chain if x["action"] == "draft" and f"{DOC}#reasons_to_believe" in x["targets"])
    assert "f_ELSEWHERE" not in rtb["cites"] and rtb["fact_refs"][0]["id"] == "f_ELSEWHERE"
    rd = next(x for x in host.chain if x["action"] == "draft_run")
    assert "1 research fact(s) this document does not hold" in rd["reasoning"]["attention"]
    check_engine_change(host)


@pytest.mark.parametrize("engine_", [
    Engine(status=500, reply={"error": "internal error — see server log"}),
    Engine(status=400, reply={"error": "no brief text"}),
    Engine(raise_=httpx.ReadTimeout("slow")),
    Engine(raise_=httpx.ConnectError("refused")),
    Engine(reply=b"<html>not json</html>"),
    Engine(reply={"error": "a 200 with an error"}),
    Engine(reply={"rationale": "nothing else", "context": "x"}),
], ids=["500", "400", "timeout", "unreachable", "not-json", "200-error", "no-field"])
def test_the_engine_failing_falls_back_to_the_middlewares_drafters(eserver, stub, engine_):
    e = stub(engine_)
    host = Host()
    replies = run(eserver, host, inp=inp())
    last = replies[-1]
    assert len(e.requests) == 1
    assert last["job"]["state"] == "done" and last["job"]["progress"] == {"done": 3, "total": 3}
    eng = last["result"]["engine"]
    assert eng["state"] == "skipped" and eng["reason"]
    assert last["result"]["summary"].endswith(f"The brief engine was not used: {eng['reason']}.")
    # the middleware's own brief, as today: captured, drafted, judged
    assert host.data["background"] == "Harbour Tonic is losing midweek drinkers to low-alcohol beer."
    assert host.data.get("insight") and "review" in host.data
    assert any(x["kind"] == "verdict" for x in host.chain)
    assert not any(x.get("drafted_by") == "brief-engine" for x in host.chain)
    # the reason is recorded where a person sees it: the capture decision's attention
    cap = next(x for x in host.chain if x["action"] == "capture")
    assert f"We wrote this brief ourselves: the brief engine could not be used this time ({eng['reason']})" \
        in cap["reasoning"]["attention"]
    assert "middleware" not in cap["reasoning"]["attention"]
    assert problems(cap["reasoning"]) == []


def test_fallback_reasons_name_what_went_wrong_and_never_the_reply(eserver, stub):
    stub(Engine(status=503, reply={"error": "the client's secret words"}))
    host = Host()
    last = run(eserver, host, inp=inp())[-1]
    assert last["result"]["engine"]["reason"] == "the brief engine returned 503"
    stub(Engine(raise_=httpx.ReadTimeout("slow")))
    last = run(eserver, Host(), inp=inp())[-1]
    assert last["result"]["engine"]["reason"] == f"the brief engine did not answer within {engine.TIMEOUT:.0f}s"
    assert "secret" not in json.dumps(host.chain)


def test_the_view_sees_the_draft_stage_while_the_engine_writes(eserver, stub):
    gate = threading.Event()

    def slow(body):
        gate.wait(5)
        return httpx.Response(200, json=REPLY)
    stub(Engine(reply=slow))
    seen = []

    def on_reply(b):
        seen.append((b["job"]["stage"], b["result"]["engine"]["state"], b["result"]["fields"]["insight"]["state"],
                     b["result"]["summary"]))
        if len(seen) > 3:
            gate.set()
    run(eserver, Host(), inp=inp(), on_reply=on_reply)
    running = [s for s in seen if s[1] == "running" and s[3] != "queued"]  # the first reply is the queued one
    assert running and all(s[0] == "draft" and s[2] == "drafting" for s in running)
    assert running[0][3] == "The brief engine is writing the brief."


def test_a_fallback_that_also_fails_is_a_gap_that_says_both(eserver, stub, tmp_path):
    stub(Engine(status=502))
    model = FakeModel()
    model.overrides["capture"] = lambda p: (_ for _ in ()).throw(RuntimeError("down"))
    s = Server(tmp_path, model=model, retrieval=retrieval(), settings_kw={"brief_engine_url": ENGINE_URL})
    host = Host()
    try:
        last = run(s, host, inp=inp())[-1]
    finally:
        s.stop()
    # No stage halts the brief (the owner, 2026-09-30): the failed fallback capture is a gap, the job ends done.
    assert last["job"]["state"] == "done"
    assert [g["stage"] for g in last["result"]["gaps"]][:1] == ["extract"]
    assert last["result"]["engine"] == {"state": "skipped", "reason": "the brief engine returned 502"}
    assert "The brief engine was not used: the brief engine returned 502." in last["result"]["summary"]
    gap = next(x for x in host.chain if x.get("action") == "capture")
    assert gap["targets"] == [] and "the brief engine returned 502" in gap["reasoning"]["attention"]


def test_unset_is_the_middlewares_own_job_and_never_calls_an_engine(bserver_plain, stub):
    e = stub(Engine())
    host = Host()
    last = run(bserver_plain, host, inp=inp())[-1]
    assert e.requests == [] and "engine" not in last["result"]
    assert last["job"]["state"] == "done" and any(x["kind"] == "verdict" for x in host.chain)
    assert not any(x.get("drafted_by") == "brief-engine" for x in host.chain)


@pytest.fixture
def bserver_plain(tmp_path):
    s = Server(tmp_path, retrieval=retrieval())
    yield s
    s.stop()


def test_the_handler_picks_the_job_by_the_setting():
    class Req:
        doc, base, handler = DOC, "v1", "draft_brief@1.0"
        clan = {"id": DOC, "version": "v1", "data": {}}
        inp = {"prompt": "A brief.", "attachments": []}

    class Caps:
        scope = {"org": "o", "brand": "b"}
    job = draft_brief.start(Req, Caps, Settings(brief_flow="fields"), "job_x")
    assert type(job) is BriefJob
    job = draft_brief.start(Req, Caps, Settings(), "job_p")
    assert type(job) is PlannedBriefJob
    job = draft_brief.start(Req, Caps, Settings(brief_engine_url=ENGINE_URL), "job_y")
    assert isinstance(job, engine.EngineBriefJob) and job.port.url == ENGINE_URL + "/"


def test_the_setting_is_read_from_the_environment(monkeypatch):
    monkeypatch.delenv("NAPKIN_BRIEF_ENGINE_URL", raising=False)
    assert Settings.from_env().brief_engine_url is None
    monkeypatch.setenv("NAPKIN_BRIEF_ENGINE_URL", "http://127.0.0.1:8787")
    assert Settings.from_env().brief_engine_url == "http://127.0.0.1:8787"


def test_mapping_helpers():
    refs = engine.refs_by_key({"desired_response": [{"item": "do", "id": "f_1"}, {"item": "nope", "id": "f_2"}],
                               "reasons_to_believe": [{"item": 2, "id": "f_3"}, {"id": ""}, "junk"],
                               "not_a_field": [{"id": "f_4"}]})
    assert refs == {"desired_response.do": [{"item": "do", "id": "f_1"}],
                    "reasons_to_believe": [{"item": 2, "id": "f_3"}]}
    assert engine.review_drafts("Extracted via x; DRAFTS TO REVIEW (failed their checks): insight, reasons to "
                                "believe, desired response.") == {
        "insight", "reasons_to_believe", "desired_response.think", "desired_response.feel", "desired_response.do"}
    assert engine.review_drafts("Extracted via x.") == set()
    values, bad = engine.reply_values({"objectives.commercial": "Dotted.", "desired_response": {"feel": "F"},
                                       "mandatories": [], "insight": ["a list"], "rationale": "r"})
    assert values == {"objectives.commercial": "Dotted.", "desired_response.feel": "F"} and bad == ["insight"]
    assert engine.upstream_of({"upstream_payload": {"brand": "", "facts": []}}) is None
    assert engine.upstream_of({}) is None


def test_a_reply_that_cannot_be_mapped_falls_back_with_nothing_of_it_left(eserver, stub, monkeypatch):
    stub(Engine())

    def boom(self, c, res, mat_ids):
        raise RuntimeError("a mapping bug")
    monkeypatch.setattr(engine.EngineBriefJob, "_conflict", boom)
    chain = [{"id": "d_HUMAN00001", "kind": "edit", "agent": "human:1", "actor": "human:1", "action": "edit",
              "targets": [f"{DOC}#audience"]}]
    host = Host(data={"audience": "My own audience."}, chain=chain)
    last = run(eserver, host, inp=inp())[-1]
    assert last["job"]["state"] == "done"
    assert last["result"]["engine"] == {"state": "skipped", "reason": "its reply could not be read (RuntimeError)"}
    ids = {x["id"] for x in host.chain}
    props = last["result"]["proposals"]
    assert props and all(p["decision"] in ids for p in props)  # the engine's proposal did not survive
    assert not any(x.get("drafted_by") == "brief-engine" for x in host.chain)


def test_the_engine_brief_goes_through_the_check_loop(tmp_path, stub):
    # Sai's engine writes; jev flags; Jude explains; Dara rewrites the flagged part and fills one the
    # engine left empty; the review is jev's reading.
    from fakes import FakeModel
    from test_planned_brief import FakeJev
    stub(Engine(reply={**REPLY, "insight": "A vague insight about people."}))
    m, seen = FakeModel(), {}

    def explain(p):
        seen["explain"] = p
        return {"parts": {k: {"rework": True, "why": f"the {k} needs work", "instruction": "write it"}
                          for k in list(p["flagged"]) + p["empty"] if k in ("insight", "competitor_context")},
                "chain": {"issue": None, "parts": []}, "dimensions": [], "questions": []}
    m.overrides["explain_flags"] = lambda p: (explain(p) if True else None) and {
        **explain(p), "parts": {**{k: {"rework": False, "why": "fine", "instruction": ""}
                                   for k in list(p["flagged"]) + p["empty"]}, **explain(p)["parts"]}}
    m.overrides["rewrite_brief_parts"] = lambda p: {k: {"value": f"A sharp {k}", "basis": "assumption", "cites": [],
                                                         "builds_on_client": False, "why": "x"} for k in p["parts"]}
    s = Server(tmp_path, model=m, retrieval=retrieval(), jev=FakeJev(),
               settings_kw={"brief_engine_url": ENGINE_URL, "brief_judge": "jev"})
    try:
        host = research_host()
        replies = run(s, host, inp=inp())
    finally:
        s.stop()
    assert replies[-1]["job"]["state"] == "done"
    assert "insight" in seen["explain"]["flagged"] and "competitor_context" in seen["explain"]["empty"]
    assert host.data["insight"] == "A sharp insight" and host.data["competitor_context"] == "A sharp competitor_context"
    rw = [d for d in host.chain if d.get("action") == "regenerate"]
    assert {t.split("#", 1)[1] for d in rw for t in d["targets"]} == {"insight", "competitor_context"}
    assert host.data["review"]["scorecard"]["dimensions"][0]["verdict"] == "pass"
    check_engine_change(host, extra_ok=("review", f"{DOC}#review") + tuple(d["id"] for d in host.chain))
