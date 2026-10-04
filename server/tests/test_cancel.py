"""A person cancels a job: it is `cancelled` at once, frees its document, keeps
what it already sent, writes nothing more, and makes no further paid call."""

import threading
import time
from types import SimpleNamespace

import pytest

from napkin.jobs import Cancelled, LongJob, _blocking

from fakes import FakeResearch
from test_no_halt import TONIC, LABELLED, MiniHost, caps_for, start


class SlowResearch(FakeResearch):
    """Research that waits until the test lets it go: a job caught mid-research."""

    def __init__(self):
        super().__init__()
        self.entered, self.go = threading.Event(), threading.Event()

    def search(self, *a, **kw):
        self.entered.set()
        self.go.wait(10)
        return super().search(*a, **kw)


def test_a_cancelled_campaign_stops_where_it_is_and_says_so(store):
    host = MiniHost(LABELLED, categories=TONIC)
    research = SlowResearch()
    job = start(store, host, research=research)
    t0 = time.monotonic()
    while not research.entered.is_set():  # poll the way the view does until research is under way
        host.apply(job.reply_change(host.clan()))
        assert time.monotonic() - t0 < 10, job.view()
        time.sleep(0.005)
    assert job.cancel()
    assert job.view()["state"] == "cancelled" and not job.cancel()  # a second cancel changes nothing
    assert job.caps.jobs.cancelled
    # it holds the document no longer: a new campaign may start on it
    assert not _blocking(job)
    research.go.set()
    job.thread.join(5)
    assert not job.thread.is_alive()
    for _ in range(4):
        host.apply(job.reply_change(host.clan()))
    acts = host.actions()
    assert "cancel_job" in acts and "research_merge" not in acts  # the stage it stopped in wrote nothing
    assert any(m.startswith("Stopped: you cancelled it at the research step") for m in host.messages())
    assert "report" not in host.data
    assert job.summary().startswith("Cancelled at research")
    # and it makes no paid call after it was cancelled
    with pytest.raises(Cancelled):
        job.caps.model.structured("x", "s", {}, {"type": "object"})
    with pytest.raises(Cancelled):
        job.caps.research.search("q", "media_spend", "IE")


def test_a_cancelled_long_job_sends_nothing(store):
    gate = threading.Event()
    caps = caps_for(store)

    def work():
        gate.wait(10)
        return {"summary": "x"}, {"decisions": [{"id": "d_X"}]}, []
    job = LongJob("job_long0001", "research_lens", "research_lens@1.0", "doc", {}, caps, 2, work)
    job.start()
    assert job.cancel() and job.view()["state"] == "cancelled"
    gate.set()
    job.thread.join(5)
    assert job.state == "cancelled" and job.change is None and job.result is None


def test_a_finished_job_is_not_cancelled(store):
    caps = caps_for(store)
    job = LongJob("job_long0002", "research_lens", "research_lens@1.0", "doc", {}, caps, 1,
                  lambda: ({"summary": "x"}, None, []))
    job.start()
    job.thread.join(5)
    assert job.state == "done" and not job.cancel() and job.state == "done"
