"""synthesise_findings@1 — proposed findings over the pins (long task)."""

from ..pipeline.synthesise import run_synthesis, usable_pins
from ..doc import LENSES
from ..util import bad

NAME, TASK, VERSION, KIND, CAPABILITY_MAJOR = "synthesise_findings", "synthesise_findings", "1.0", "long", 1


def prepare(req, caps, settings):
    lenses = req.inp.get("lenses", LENSES)
    if not isinstance(lenses, list) or not lenses or any(l not in LENSES for l in lenses):
        raise bad(f"input.lenses must be a non-empty list of lens ids from {LENSES}")
    if not usable_pins(req.clan):
        raise bad("no pinned facts in clan.facts to synthesise from (a finding must cite pins)")

    def work():
        out = run_synthesis(req.doc, req.base, req.clan, req.inp, req.handler, caps)
        caps.jobs.progress(1)
        return out
    return 1, work
