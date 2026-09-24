"""The retrieval port client (peripherals.md §3) against a fake service."""

import hashlib
import json

import httpx
import pytest

from napkin.capabilities import Capabilities
from napkin.retrieval import RetrievalError, RetrievalPort, passage_id, passage_uri

from fakes import FakeRetrievalService

SCOPE = {"org": "org/test-agency", "brand": "brand/test"}
ATTR = {"handler": "draft_brief@1.0", "job": "job_x"}


def port(svc, **kw):
    return RetrievalPort("http://retrieval.test", token=kw.get("token"), transport=httpx.MockTransport(svc),
                         sleep=lambda s: None)


def test_passage_identity_formula():
    text = "Great briefs name one thing."
    sha = hashlib.sha256(text.encode()).hexdigest()
    assert passage_uri("cannes", "digest.md", "Craft rules for a brief", sha) == \
        f"passage://cannes/digest.md#craft-rules-for-a-brief@{sha[:16]}"
    assert passage_id("cannes", "digest.md", "X", text) == \
        "psg_" + hashlib.sha256(b"cannes\ndigest.md\nX\n" + text.encode()).hexdigest()[:20]


def test_packs_and_retrieve_with_scope_headers_and_closed_body():
    svc = FakeRetrievalService()
    p = port(svc, token="tok")
    packs = p.packs(SCOPE, ATTR)
    assert {x["tag"] for x in packs} == {"playbooks", "cannes"}
    out = p.retrieve(SCOPE, ATTR, "find the  human\ninsight", 3, packs=["playbooks"], purpose="loop4_insight")
    assert out["passages"] and all(x["pack"] == "playbooks" for x in out["passages"])
    assert out["trace"]["packs"]["playbooks"].startswith("sha256:")
    for r in svc.requests:
        assert r.headers["x-napkin-org"] == "org/test-agency" and r.headers["x-napkin-brand"] == "brand/test"
        assert r.headers["x-napkin-handler"] == "draft_brief@1.0" and r.headers["x-napkin-job"] == "job_x"
        assert r.headers["authorization"] == "Bearer tok"
    body = json.loads(svc.requests[-1].content)
    assert set(body) <= {"query", "k", "packs", "where", "purpose"} and body["query"] == "find the human insight"


def test_a_passage_that_does_not_recompute_is_dropped():
    svc = FakeRetrievalService(tamper=True)
    out = port(svc).retrieve(SCOPE, ATTR, "anything", 5)
    assert out["dropped"] >= 1 and all(x["text"] != "TAMPERED" for x in out["passages"])


def test_errors_are_loud_and_reads_retry_once():
    svc = FakeRetrievalService(fail=[503])
    assert port(svc).retrieve(SCOPE, ATTR, "q", 2)["passages"]  # retried once
    svc = FakeRetrievalService(fail=[502, 502])
    with pytest.raises(RetrievalError):
        port(svc).retrieve(SCOPE, ATTR, "q", 2)
    svc = FakeRetrievalService(fail=[404])
    with pytest.raises(RetrievalError, match="404"):
        port(svc).retrieve(SCOPE, ATTR, "q", 2, packs=["someone-elses"])


def test_the_capability_is_scoped_and_capture_has_no_retrieval(store):
    import threading
    from napkin.model import ModelPort
    from fakes import FakeModel
    svc = FakeRetrievalService()
    caps = Capabilities(handler="draft_brief@1.0", scope=SCOPE, model_port=ModelPort(FakeModel(), "m", 5),
                        research_port=None, layer_store=store, research_semaphore=threading.Semaphore(1),
                        retrieval_port=port(svc))
    caps.bind_job("job_42")
    assert caps.retrieval.retrieve("q", 2)["passages"]
    assert svc.requests[-1].headers["x-napkin-job"] == "job_42"
    view = caps.capture_view()
    for attr in ("retrieval", "research", "layers"):
        assert not hasattr(view, attr)
    with pytest.raises(AttributeError):
        view.retrieval = caps.retrieval  # slots: nothing can be attached later
