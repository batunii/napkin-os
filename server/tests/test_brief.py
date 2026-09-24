"""Brief Maker on the middleware (middleware-api.md §10): draft_brief and
regenerate_field, end to end over HTTP with fake peripherals and a small host
that applies each reply's change the way the real host does."""

import base64
import copy
import hashlib
import json
import threading
import time
from pathlib import Path

import httpx
import jsonschema
import pytest

from napkin.brief import capture as cap_stage
from napkin.brief.fields import KEYS
from napkin.doc import apply_patch
from napkin.reasoning import problems
from napkin.retrieval import RetrievalPort

from conftest import Server
from fakes import BRIEF_TEXT, INSIGHT_SHARP, SMPS, FakeModel, FakeRetrievalService

REPO = Path(__file__).resolve().parents[2]
BRIEF_SCHEMA = json.loads((REPO / "app/templates/brief-maker/schema.json").read_text())
DOC = "bbbbbbbb-2222-4333-8444-555555555555"
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"1" * 32).decode()


def retrieval(svc=None):
    svc = svc or FakeRetrievalService()
    p = RetrievalPort("http://retrieval.test", transport=httpx.MockTransport(svc), sleep=lambda s: None)
    p.service = svc
    return p


@pytest.fixture
def bserver(tmp_path):
    s = Server(tmp_path, retrieval=retrieval())
    yield s
    s.stop()


class Host:
    """Holds one Brief Maker document and applies changes (§5), idempotently."""

    def __init__(self, data=None, chain=None, pipeline=None):
        self.data = copy.deepcopy(data or {})
        self.chain = list(chain or [])
        self.version = 1
        self.pipeline = pipeline
        self.changes = []

    def clan(self):
        c = {"id": DOC, "version": f"v{self.version}", "revision": "r", "data": copy.deepcopy(self.data), "facts": [],
             "findings": [], "decision_chain": {"decisions": copy.deepcopy(self.chain)}}
        if self.pipeline:
            c["pipeline"] = self.pipeline
        return c

    def apply(self, change):
        if not change:
            return
        have = {d["id"] for d in self.chain}
        new = [d for d in change["decisions"] if d["id"] not in have]
        if not new:
            return
        self.changes.append(change)
        self.data = apply_patch(self.data, change["data_patch"])
        self.chain += new
        self.version += 1


def post(server, task, inp, clan):
    r = httpx.post(server.url + "/v1/tasks", json={"request_kind": "middleware", "payload": {"task": task, "input": inp},
                                                   "clan": clan}, timeout=30)
    return r.status_code, r.json()


def run(server, host, task="draft_brief", inp=None, on_reply=None, timeout=30):
    st, body = post(server, task, inp if inp is not None else {"prompt": BRIEF_TEXT, "attachments": []}, host.clan())
    assert st == 200, body
    replies = [body]
    jid = body["job"]["id"]
    t0 = time.time()
    while body["job"]["state"] in ("queued", "running"):
        assert time.time() - t0 < timeout, "the job did not finish"
        if on_reply:
            on_reply(body)
        time.sleep(0.02)
        st, body = post(server, "job_status", {"job_id": jid}, host.clan())
        assert st == 200, body
        replies.append(body)
        host.apply(body["change"])
    return replies


def decisions(host):
    return {d["id"]: d for d in host.chain}


def check_change_rules(host):
    """Rules every Brief Maker change keeps (§10.3, §10.7, §3)."""
    data = host.data
    for ch in host.changes:
        targets = {t for d in ch["decisions"] for t in d["targets"]}
        for top, v in ch["data_patch"].items():
            leaves = [(f"{top}.{k}", x) for k, x in v.items()] if top in ("objectives", "desired_response") else [(top, v)]
            for path, x in leaves:
                assert x not in ("", [], {}, None), f"{path} blanked"
                if top in ("materials", "passages"):
                    for k in v:
                        assert f"{DOC}#{top}[{k}]" in targets, f"{top}[{k}] written without a decision"
                else:
                    assert f"{DOC}#{path}" in targets, f"{path} written without a decision naming it"
        for p in ch["data_patch"]:
            assert p in KEYS or p.split(".")[0] in ("objectives", "desired_response", "materials", "capture", "review",
                                                    "passages", "open_questions") or p in [k.split(".")[0] for k in KEYS]
    ids = decisions(host)
    for d in [d for ch in host.changes for d in ch["decisions"]]:
        assert problems(d.get("reasoning")) == [], (d["id"], problems(d.get("reasoning")))
        for p in d["reasoning"]["because"]:
            for c in p.get("cites") or []:
                ok = (c.startswith("cap_") and c in (data.get("capture") or {}).get("items", {})) or \
                     (c.startswith("psg_") and c in (data.get("passages") or {})) or \
                     (c.startswith("mat_") and c in (data.get("materials") or {})) or \
                     (c.startswith("d_") and c in ids) or c.startswith(f"{DOC}#")
                assert ok, f"{d['id']} cites {c}, which does not resolve"
        assert d["handler"].startswith(("draft_brief@1", "regenerate_field@1"))
        assert d["agent"].rsplit("/", 1)[-1] in ("extract", "drafter", "judge")
    # the document validates against Brief Maker's schema, the §10.4 blocks included
    jsonschema.Draft7Validator(BRIEF_SCHEMA).validate(data)


# ---------------------------------------------------------------------------------------------------------------

def test_draft_brief_end_to_end(bserver):
    host = Host()
    replies = run(bserver, host)
    last = replies[-1]
    assert last["job"]["state"] == "done" and last["task"] == "draft_brief" and last["handler"] == "draft_brief@1.0"
    assert last["job"]["progress"] == {"done": 3, "total": 3} and last["job"]["question"] is None
    assert replies[0]["job"]["state"] == "queued" and replies[0]["change"] is None  # a long task, polled
    for r in replies:
        assert set(r["result"]["fields"]) == set(KEYS) and r["job"]["stage"] in ("extract", "draft", "judge")
        assert r["task"] == "draft_brief" and r["handler"] == "draft_brief@1.0"
    d = host.data
    # captured fields: the client's words
    assert d["background"] == "Harbour Tonic is losing midweek drinkers to low-alcohol beer."
    assert d["objectives"] == {"commercial": "Grow midweek volume by 8% by Q4 2026.",
                               "behavioural": "Get lapsed drinkers to choose Harbour Tonic on a weeknight."}
    assert d["client"] == "Harbour Drinks Ltd" and d["project_name"] == "Midweek Tonic"
    assert d["mandatories"] == ["Harbour logo on every asset."]
    assert "Two social films" in d["budget_and_scope"] and "200k" in d["budget_and_scope"]
    # drafted fields, judged
    assert d["insight"] == INSIGHT_SHARP and d["single_minded_proposition"] == SMPS[0]
    assert d["reasons_to_believe"][0] == "Only 20 calories per serve"
    assert set(d["desired_response"]) == {"think", "feel", "do"}
    assert "[important] How will the work be judged? — No criteria given" in d["open_questions"]
    # capture: the unverified "fact" was dropped, the inference kept as an assumption
    items = d["capture"]["items"]
    assert not any(i["key"] == "decision_makers" for i in items.values())
    assert any(i["key"] == "strategic_angle" and i["status"] == "assumption" for i in items.values())
    for i in items.values():
        if i["status"] == "fact":
            assert i["quote"] in BRIEF_TEXT
    assert d["capture"]["ledger"]["total_segments"] > 0 and "decision_makers" in d["capture"]["gaps"]
    # the scorecard: evidence verbatim or dropped
    dims = {x["dimension"]: x for x in d["review"]["scorecard"]["dimensions"]}
    assert dims["objectives_quality"]["evidence"] in BRIEF_TEXT and "evidence" not in dims["audience_vividness"]
    assert isinstance(d["review"]["judge"]["health"], int)
    assert d["review"]["judge"]["fields"]["insight"]["outcome"] == "passed"
    # every captured field's decision cites only cap_ and mat_ ids
    for dd in host.chain:
        if dd.get("action") == "extract":
            assert all(c.startswith(("cap_", "mat_")) for c in dd["cites"]), dd["cites"]
    # every judged field has a verdict
    verdicts = [x for x in host.chain if x["kind"] == "verdict"]
    assert verdicts and all(v["polarity"] in ("good", "bad") and v["taxonomy_version"] == "reason-codes/1"
                            for v in verdicts)
    assert any(f"{DOC}#insight" in v["targets"] for v in verdicts)
    assert not any("#passages[" in t for v in verdicts for t in v["targets"])  # verdicts target fields only
    check_change_rules(host)
    fields = last["result"]["fields"]
    assert fields["insight"] == {"state": "done", "by": "judge"} and fields["background"]["state"] == "done"
    # trace: passages read, usage reported
    assert any(h["id"].startswith("psg_") and h["scope"] == "house" for h in last["trace"]["hits"])
    assert last["trace"]["usage"]["input_tokens"] > 0


def test_passages_cited_are_written_with_their_version(bserver):
    host = Host()
    run(bserver, host)
    ps = host.data["passages"]
    assert ps
    for pid, p in ps.items():
        assert hashlib.sha256(p["text"].encode()).hexdigest() == p["text_sha256"]
        assert p["pack_version"].startswith("sha256:") and p["citation"] == f"{p['source']} › {p['section']}"
    cited = {c for d in host.chain for pt in d["reasoning"]["because"] for c in pt.get("cites") or []
             if c.startswith("psg_")}
    assert cited and cited <= set(ps)
    draft = next(d for d in host.chain if d.get("action") == "draft" and f"{DOC}#insight" in d["targets"])
    # two packs cited and every check passed first time -> high (derived, never the model's)
    assert draft["reasoning"]["certainty"]["level"] == "high"
    assert not any(c == "psg_00000000000000000000" for c in draft["cites"])  # the invented cite was dropped
    assert not any("99%" in p["point"] for p in draft["reasoning"]["because"])  # a figure without a pin: dropped
    assert draft["reasoning"]["rejected"]  # the losing tournament candidates, with why


def test_extract_is_rag_free(bserver):
    host = Host()
    run(bserver, host)
    calls = bserver.model.calls
    cap = next(c for c in calls if c[0] == "capture")
    assert set(cap[1]) == {"materials"}  # the materials and nothing retrieved
    sc = next(c for c in calls if c[0] == "scorecard")
    assert set(sc[1]) == {"materials"}
    # no captured field cites a passage
    for d in host.chain:
        if d.get("action") in ("extract", "capture", "score"):
            assert not any(c.startswith("psg_") for c in d["cites"])
    # retrieval never ran before the capture was done: its first request follows the scorecard call
    first_draft = next(i for i, c in enumerate(calls) if c[0].startswith("draft_"))
    assert all(c[0] in ("capture", "scorecard", "transcribe") for c in calls[:first_draft] if not c[0].startswith("draft"))


def test_capture_view_cannot_reach_retrieval(store):
    import threading as th
    from napkin.capabilities import Capabilities
    from napkin.model import ModelPort
    caps = Capabilities(handler="draft_brief@1.0", scope={"org": "org/a", "brand": "brand/b"},
                        model_port=ModelPort(FakeModel(), "m", 5), research_port=None, layer_store=store,
                        research_semaphore=th.Semaphore(1), retrieval_port=retrieval())
    view = caps.capture_view()
    mats = cap_stage.build_materials({"prompt": BRIEF_TEXT}, {})
    out = cap_stage.run_capture(view, mats)  # runs on the view alone
    assert out["items"] and not hasattr(view, "retrieval") and not hasattr(view, "research")


def test_insight_then_smp_chain_others_parallel(bserver):
    host = Host()
    run(bserver, host)
    calls = [(p, pl) for p, pl, _ in bserver.model.calls]
    names = [p for p, _ in calls]
    smp = next(pl for p, pl in calls if p == "draft_smp")
    assert smp["insight"] == INSIGHT_SHARP  # the SMP drafts from the CHOSEN insight
    assert names.index("draft_smp") > names.index("sharpen_insight")
    rtb = next(pl for p, pl in calls if p == "draft_reasons_to_believe")
    dr = next(pl for p, pl in calls if p == "draft_desired_response")
    assert "insight" not in rtb and "insight" not in dr  # the rest never see a draft


def test_judge_failure_gets_one_revision(tmp_path):
    model = FakeModel()
    model.fail_checks[("judge_insight", "is_tension")] = 1
    s = Server(tmp_path, model=model, retrieval=retrieval())
    try:
        host = Host()
        replies = run(s, host)
    finally:
        s.stop()
    rev = next(pl for p, pl, _ in model.calls if p == "revise_insight")
    assert rev["failed_checks"][0]["check"] == "is_tension" and rev["failed_checks"][0]["fix"]
    assert "judge" not in json.dumps(rev.get("current_draft", ""))  # the judge's reasoning never reaches the drafter
    assert host.data["insight"].startswith("Midweek drinkers cut back because they fear losing the week")
    v = next(d for d in host.chain if d["kind"] == "verdict" and d["targets"] == [f"{DOC}#insight"])
    assert v["polarity"] == "good" and "after one revision" in v["reasoning"]["decided"]
    draft = next(d for d in host.chain if d.get("action") == "draft" and f"{DOC}#insight" in d["targets"])
    assert draft["reasoning"]["certainty"]["level"] == "low"  # passed only after revision
    assert host.data["review"]["judge"]["fields"]["insight"]["outcome"] == "revised"


LONG_REASON = ("€1.2m spread across digital and OOH in two national markets (Ireland and Great Britain) is thin for "
               "the stated ambition of reversing a three-year shelf-share decline against two well-capitalised "
               "modern-whisky leaders (Suntory-backed Hibiki, Diageo-backed Roe & Co); GB OOH alone would consume most "
               "of the budget before Ireland sees a single poster")
LONG_FIX = "Either narrow geography to one market (lead with Ireland, phase GB later) or reduce the channel mix"


def test_a_coherence_verdict_keeps_its_whole_reason_and_its_fix_apart(tmp_path):
    def coherence(p):
        return {r["id"]: ({"verdict": "fail", "reason": LONG_REASON, "fix": LONG_FIX} if r["id"] == "backbone_balance"
                          else {"verdict": "pass", "reason": "holds together", "fix": None}) for r in p["rules"]}
    model = FakeModel({"judge_coherence": coherence})
    s = Server(tmp_path, model=model, retrieval=retrieval())
    try:
        host = Host()
        run(s, host)
    finally:
        s.stop()
    v = next(d for d in host.chain if d["kind"] == "verdict" and d["reasoning"]["decided"].startswith(
        "The brief does not hold together: backbone balance"))
    assert len({t.split("#", 1)[1].split(".")[0] for t in v["targets"]}) > 1  # one check across several fields
    r = v["reasoning"]
    # the reason whole, as a sentence, and first (the rationale older readers show is decided + it)
    assert r["because"][0]["point"] == LONG_REASON + "."
    assert r["because"][1]["point"].startswith("The rule: Budget ↔ objectives ↔ audience")
    # the fix after it, apart, never glued onto a clipped reason
    assert r["attention"] == f"Fix: {LONG_FIX}. A person answers this before the brief locks."
    assert "Fix:" not in r["because"][0]["point"]
    assert "across budget and scope, objectives, audience" in r["decided"]
    check_change_rules(host)


def test_clip_cuts_at_a_sentence_never_mid_word():
    from napkin.brief.judge import clip, sentence
    assert clip("short", 50) == "short"
    t = "One whole sentence here. " + "word " * 40
    assert clip(t, 40) == "One whole sentence here. …"
    assert clip(t, 60).endswith("word…") and len(clip(t, 60)) <= 60
    assert clip("alpha beta gamma delta", 14) == "alpha beta…"
    assert sentence("no stop") == "no stop." and sentence("a stop.") == "a stop." and sentence("") == ""


def test_failing_twice_is_not_written(tmp_path):
    model = FakeModel()
    model.fail_checks[("judge_smp", "not_a_tagline")] = 2
    s = Server(tmp_path, model=model, retrieval=retrieval())
    try:
        host = Host()
        replies = run(s, host)
    finally:
        s.stop()
    assert "single_minded_proposition" not in host.data
    assert replies[-1]["result"]["fields"]["single_minded_proposition"]["state"] == "failed"
    v = next(d for d in host.chain if d["kind"] == "verdict" and f"{DOC}#single_minded_proposition" in d["targets"]
             and d.get("action") == "judge" and d["polarity"] == "bad")
    assert v["reason_code"] == "cliche" and "not written" in v["reasoning"]["attention"]
    assert any(q.startswith("Agree the single-minded proposition.") for q in host.data["open_questions"])
    check_change_rules(host)


def test_locked_fields_are_kept_and_human_held_fields_proposed(bserver):
    human = {"id": "d_HUMAN00001", "kind": "edit", "actor": "human:shrey", "agent": "human:shrey", "action": "edit",
             "targets": [f"{DOC}#insight"], "rationale": "my insight", "timestamp": "2026-09-24T09:00:00Z"}
    host = Host(data={"locked_fields": ["audience", "desired_response"], "insight": "A person's insight.",
                      "tone_and_world": ["set by hand, no decision"]}, chain=[human])
    replies = run(bserver, host)
    last = replies[-1]
    for ch in host.changes:
        assert "audience" not in ch["data_patch"] and "desired_response" not in ch["data_patch"]
        assert "insight" not in ch["data_patch"] and "tone_and_world" not in ch["data_patch"]
    f = last["result"]["fields"]
    assert f["audience"] == {"state": "kept", "by": None} and f["desired_response.do"]["state"] == "kept"
    assert f["insight"]["state"] == "proposed" and f["tone_and_world"]["state"] == "proposed"
    props = {p["field"]: p for p in last["result"]["proposals"]}
    assert props["insight"]["value"] == INSIGHT_SHARP
    pd = decisions(host)[props["insight"]["decision"]]
    assert pd["action"] == "propose" and pd["proposed_value"] == INSIGHT_SHARP and pd["targets"] == [f"{DOC}#insight"]
    assert host.data["insight"] == "A person's insight."
    assert not any(c[0] == "draft_desired_response" for c in bserver.model.calls)  # locked: not even drafted
    check_change_rules(host)


def test_omitted_not_blank(bserver):
    host = Host(data={"background": "kept as it was"})
    replies = run(bserver, host, inp={"prompt": "Problem: Nobody buys our tonic on a Tuesday.", "attachments": []})
    f = replies[-1]["result"]["fields"]
    for k in ("audience", "competitor_context", "mandatories", "client", "budget_and_scope"):
        assert f[k]["state"] == "absent" and k not in host.data
    cap = next(d for d in host.chain if d.get("action") == "capture")
    assert "audience" in cap["abstained"] and cap["material_read"]
    check_change_rules(host)


def test_stage_reporting_shows_workers(tmp_path):
    gate_extract, gate_draft = threading.Event(), threading.Event()
    model = FakeModel()
    from fakes import brief_responder
    cap_r, dr_r = brief_responder(model, "capture"), brief_responder(model, "draft_desired_response")
    model.overrides["capture"] = lambda p: (gate_extract.wait(10), cap_r(p))[1]
    model.overrides["draft_desired_response"] = lambda p: (gate_draft.wait(10), dr_r(p))[1]
    s = Server(tmp_path, model=model, retrieval=retrieval())
    seen = []

    def watch(body):
        f = body["result"]["fields"]
        seen.append((body["job"]["stage"], f["background"]["state"], f["desired_response.do"]["state"],
                     f["reasons_to_believe"]["state"], body["change"] is not None))
        if body["job"]["stage"] == "extract" and f["background"] == {"state": "extracting", "by": "extract"}:
            gate_extract.set()
        if body["job"]["stage"] == "draft" and f["desired_response.do"] == {"state": "drafting", "by": "drafter"}:
            gate_draft.set()

    try:
        host = Host()
        run(s, host, on_reply=watch)
    finally:
        gate_extract.set()
        gate_draft.set()
        s.stop()
    assert ("extract", "extracting", "waiting", "waiting", False) in seen
    assert any(st == "draft" and dr == "drafting" for st, _, dr, _, _ in seen)
    # the extract stage's change arrives while the job still drafts (staged, §10.9)
    assert host.changes[0]["data_patch"].get("capture") and "insight" not in host.changes[0]["data_patch"]


def test_without_retrieval_drafts_are_low_certainty(tmp_path):
    s = Server(tmp_path)  # NAPKIN_RETRIEVAL_URL unset
    try:
        host = Host()
        run(s, host)
    finally:
        s.stop()
    draft = next(d for d in host.chain if d.get("action") == "draft" and f"{DOC}#insight" in d["targets"])
    assert draft["reasoning"]["certainty"]["level"] == "low"
    assert "no retrieval service" in draft["reasoning"]["attention"]
    assert "passages" not in host.data
    check_change_rules(host)


def test_retrieval_failure_does_not_fail_the_job(tmp_path):
    s = Server(tmp_path, retrieval=retrieval(FakeRetrievalService(fail=[502] * 40)))
    try:
        host = Host()
        replies = run(s, host)
    finally:
        s.stop()
    assert replies[-1]["job"]["state"] == "done" and host.data.get("insight")
    draft = next(d for d in host.chain if d.get("action") == "draft" and f"{DOC}#insight" in d["targets"])
    assert "retrieval failed" in draft["reasoning"]["attention"]


def test_a_drafter_model_failure_fails_only_its_field(tmp_path):
    model = FakeModel()

    def boom(p):
        raise RuntimeError("the model fell over")
    model.overrides["draft_reasons_to_believe"] = boom

    class Raising(FakeModel):
        pass
    s = Server(tmp_path, model=model, retrieval=retrieval())
    try:
        host = Host()
        replies = run(s, host)
    finally:
        s.stop()
    f = replies[-1]["result"]["fields"]
    assert replies[-1]["job"]["state"] == "done"
    assert f["reasons_to_believe"]["state"] == "failed" and "reasons_to_believe" not in host.data
    assert host.data.get("insight") and host.data.get("single_minded_proposition")


def test_a_model_failure_in_extract_fails_the_job(tmp_path):
    model = FakeModel()
    model.overrides["capture"] = lambda p: (_ for _ in ()).throw(RuntimeError("down"))
    s = Server(tmp_path, model=model, retrieval=retrieval())
    try:
        host = Host()
        replies = run(s, host)
    finally:
        s.stop()
    last = replies[-1]
    assert last["job"]["state"] == "failed" and last["job"]["error"]["type"]
    assert last["job"]["stage"] == "extract" and not host.changes


def test_image_attachment_is_transcribed_first(bserver):
    sha = hashlib.sha256(b"img").hexdigest()
    host = Host()
    inp = {"attachments": [{"name": "brief-card.png", "sha256": sha, "media_type": "image/png",
                            "asset": "human/assets/brief-card.png", "image": {"media_type": "image/png", "data": PNG}}]}
    run(bserver, host, inp=inp)
    tr = next(c for c in bserver.model.calls if c[0] == "transcribe")
    assert tr[2]["model"] == "claude-opus-5" and tr[2]["messages"][0]["content"][0]["type"] == "image"
    mat = host.data["materials"][f"mat_{sha[:16]}"]
    assert mat["kind"] == "image" and mat["transcribed"]["model"] and mat["asset"] == "human/assets/brief-card.png"
    bg = next(d for d in host.chain if d.get("action") == "extract" and f"{DOC}#background" in d["targets"])
    assert bg["reasoning"]["certainty"]["level"] == "medium"  # a quote from a transcribed image
    check_change_rules(host)


def test_done_poll_repeats_the_change(bserver):
    host = Host()
    replies = run(bserver, host)
    jid = replies[0]["job"]["id"]
    stale = Host()  # a host that applied nothing: every stage comes again, same ids
    _, b1 = post(bserver, "job_status", {"job_id": jid}, stale.clan())
    _, b2 = post(bserver, "job_status", {"job_id": jid}, stale.clan())
    assert b1["change"] == b2["change"] and b1["change"]["decisions"]
    ids = {d["id"] for ch in host.changes for d in ch["decisions"]}
    assert {d["id"] for d in b1["change"]["decisions"]} == ids
    _, b3 = post(bserver, "job_status", {"job_id": jid}, host.clan())  # all landed: the last change again
    assert b3["change"] is not None


def test_regenerate_field_redrafts_with_guidance(bserver):
    host = Host()
    run(bserver, host)
    replies = run(bserver, host, task="regenerate_field", inp={"field": "desired_response.do", "guidance": "make it a ritual"})
    last = replies[-1]
    assert last["task"] == "regenerate_field" and last["handler"] == "regenerate_field@1.0"
    assert last["job"]["progress"]["total"] == 2 and last["job"]["state"] == "done"
    pl = [pl for p, pl, _ in bserver.model.calls if p == "draft_desired_response"][-1]
    assert pl["planner_guidance"] == "make it a ritual" and pl["redraft_only"] == "do"
    assert host.data["desired_response"]["do"] == "Choose a Harbour Tonic after work."
    regen = host.changes[-1]
    assert set(regen["data_patch"].get("desired_response", {})) == {"do"}  # the named leaf only
    assert any(d["action"] == "regenerate" for d in regen["decisions"])
    assert any(d["kind"] == "verdict" for d in regen["decisions"])
    check_change_rules(host)


def test_regenerate_a_captured_field_rederives_from_the_capture(bserver):
    host = Host()
    run(bserver, host)
    host.data["audience"] = "scribbled over"
    host.chain.append({"id": "d_HUMAN00002", "kind": "edit", "actor": "human:shrey", "action": "edit",
                       "targets": [f"{DOC}#audience"], "timestamp": "2026-09-24T10:00:00Z"})
    replies = run(bserver, host, task="regenerate_field", inp={"field": "audience"})
    # the latest decision that wrote it is a person's: proposed, not written
    assert replies[-1]["result"]["fields"]["audience"]["state"] == "proposed"
    assert replies[-1]["result"]["proposals"][0]["value"].startswith("Thirty-something")
    assert not any(c[0].startswith("draft_") for c in bserver.model.calls[-3:])


def test_regenerate_open_questions_recomposes_without_judging(tmp_path):
    model = FakeModel()
    model.fail_checks[("judge_smp", "not_a_tagline")] = 2
    s = Server(tmp_path, model=model, retrieval=retrieval())
    try:
        host = Host()
        run(s, host)
        host.data["open_questions"] = ["wiped by a person"]
        host.chain.append({"id": "d_HUMAN00003", "kind": "edit", "actor": "process:middleware", "action": "x",
                           "targets": [f"{DOC}#open_questions"], "timestamp": "2026-09-24T10:00:00Z"})
        n = len(model.calls)
        replies = run(s, host, task="regenerate_field", inp={"field": "open_questions"})
        assert not any(p.startswith(("judge_", "draft_")) for p, _, _ in model.calls[n:])
        assert any(q.startswith("Agree the single-minded proposition.") for q in host.data["open_questions"])
        replies = run(s, host, task="regenerate_field", inp={"field": "project_name"})
        assert replies[-1]["result"]["fields"]["project_name"]["state"] == "absent"
        assert "Nothing supports" in replies[-1]["result"]["summary"]
    finally:
        s.stop()


def test_errors_and_concurrency(tmp_path):
    gate = threading.Event()
    model = FakeModel()
    from fakes import brief_responder
    cap_r = brief_responder(model, "capture")
    model.overrides["capture"] = lambda p: (gate.wait(10), cap_r(p))[1]
    s = Server(tmp_path, model=model, retrieval=retrieval())
    try:
        host = Host(data={"locked_fields": ["insight"]})
        st, b = post(s, "draft_brief", {"prompt": BRIEF_TEXT}, host.clan())
        assert st == 200
        st, b2 = post(s, "draft_brief", {"prompt": BRIEF_TEXT}, host.clan())
        assert st == 409 and b2["error"]["type"] == "job_state"
        st, b3 = post(s, "regenerate_field", {"field": "audience"}, host.clan())
        assert st == 409
        assert post(s, "regenerate_field", {"field": "nope"}, host.clan())[0] == 400
        assert post(s, "regenerate_field", {"field": "insight"}, host.clan())[0] == 400  # locked
        assert post(s, "draft_brief", {"prompt": ""}, host.clan())[0] == 400  # nothing to read
        big = base64.b64encode(b"0" * (5 * 1024 * 1024 + 10)).decode()
        assert post(s, "draft_brief", {"attachments": [{"name": "x.png", "sha256": "a" * 64,
                                                        "image": {"media_type": "image/png", "data": big}}]},
                    host.clan())[0] == 400
        locked = Host(data={"locked": True})
        assert post(s, "draft_brief", {"prompt": BRIEF_TEXT}, locked.clan())[0] == 400
    finally:
        gate.set()
        s.stop()


def test_the_pipeline_declaration_resolves():
    from napkin import registry
    path = REPO / "app/templates/brief-maker/app/pipeline.yaml"
    assert registry.parse_pipeline(path.read_text()) == {"draft_brief": "draft_brief@1",
                                                         "regenerate_field": "regenerate_field@1"}
    resolved = registry.resolve_at_startup([str(path)])
    assert set(resolved.values()) == {"draft_brief@1.0", "regenerate_field@1.0"}
