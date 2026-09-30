"""A stage the host refuses must not leave the report waiting for ever (and the document locked)."""

import importlib.util
from pathlib import Path

import pytest

from conftest import contract_suite
from napkin.pipeline import campaign

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("contract_test", contract_suite())
ct = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ct)

PROMPT = "BMW is trying to enter the Ev hybrid market in Ireland. Make a campaign clan on that"


class RefusingHost(ct.Host):
    """A host that refuses the research stage's change (it never applies it), as it would on a stale base."""

    def apply(self, ch):
        if ch.get("facts_append"):
            return None
        return super().apply(ch)


def new_suite(server):
    return ct.Suite(ct.Client(server.url, None), REPO / "app" / "templates" / "campaign-research", 30)


def test_a_refused_stage_fails_the_job_naming_the_stage_and_frees_the_document(server, monkeypatch):
    monkeypatch.setattr(campaign, "REPORT_STALL_SECONDS", 1.0)
    monkeypatch.setattr(campaign, "REPORT_STALL_POLLS", 3)  # the test driver polls slowly; production waits for 10
    suite = new_suite(server)
    doc, data, inp, facts, chain = ct.start_doc(PROMPT)
    host = RefusingHost(suite, doc, data, facts, chain)
    with pytest.raises(ct.Fail, match=r"host did not apply the change for: .*research"):
        ct.Run(suite, host, inp).start(ct.pick_first)
    # the job is over, so the document is free again: a new campaign is accepted, not refused with 409
    status, _ = suite.c.task("start_campaign", inp, host.clan())
    assert status == 200


def test_a_host_that_applies_every_stage_is_not_cut_off(server, monkeypatch):
    monkeypatch.setattr(campaign, "REPORT_STALL_SECONDS", 1.0)
    monkeypatch.setattr(campaign, "REPORT_STALL_POLLS", 3)
    suite = new_suite(server)
    doc, data, inp, facts, chain = ct.start_doc(PROMPT)
    host = ct.Host(suite, doc, data, facts, chain)
    r = ct.Run(suite, host, inp)
    r.start(ct.pick_first)
    assert r.states[-1] == "done"
