"""A stage the host refuses must not leave the report waiting for ever (and the document locked).

End to end over HTTP; the job-level details (the gap, its wording) are in test_no_halt.py."""

import importlib.util
from pathlib import Path

import pytest

from conftest import contract_suite

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


@pytest.mark.xfail(strict=True, reason=(
    "known bug (edge-case log EC-001, 2026-10-02): when the host refuses the research change, synthesise "
    "still writes findings citing facts the document never received (dangling f_ cites); the contract "
    "driver rejects that change"))
def test_a_refused_stage_is_a_gap_the_job_finishes_and_frees_the_document(server):
    suite = new_suite(server)
    doc, data, inp, facts, chain = ct.start_doc(PROMPT)
    host = RefusingHost(suite, doc, data, facts, chain)
    r = ct.Run(suite, host, inp)
    r.start(ct.pick_first)
    assert r.states[-1] == "done"  # the refused research is a gap; the stages after it land
    # the job is over, so the document is free again: a new campaign is accepted, not refused with 409
    status, _ = suite.c.task("start_campaign", inp, host.clan())
    assert status == 200


def test_a_host_that_applies_every_stage_is_not_cut_off(server):
    suite = new_suite(server)
    doc, data, inp, facts, chain = ct.start_doc(PROMPT)
    host = ct.Host(suite, doc, data, facts, chain)
    r = ct.Run(suite, host, inp)
    r.start(ct.pick_first)
    assert r.states[-1] == "done"
