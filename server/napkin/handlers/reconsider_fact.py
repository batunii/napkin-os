"""reconsider_fact@1 — a person marked a fact wrong: take it out, check what the crew concluded
from it, and work out again what no longer stands without it (long task)."""

from ..pipeline.reconsider import Reconsider, validate

NAME, TASK, VERSION, KIND, CAPABILITY_MAJOR = "reconsider_fact", "reconsider_fact", "1.0", "long", 1


def prepare(req, caps, settings):
    fact_id, why, verdict = validate(req.clan, req.inp)
    r = Reconsider(req.doc, req.base, req.clan, req.handler, caps, fact_id, why, verdict, settings)

    def work():
        return r.run(progress=lambda done: caps.jobs.progress(done))
    return 2, work
