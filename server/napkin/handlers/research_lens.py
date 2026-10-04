"""research_lens@1 — one research run per lens x market, merged (long task)."""

from ..doc import build_materials, ctx_data
from ..pipeline.research import Researcher, validate_research

NAME, TASK, VERSION, KIND, CAPABILITY_MAJOR = "research_lens", "research_lens", "1.0", "long", 1


def prepare(req, caps, settings):
    """Validate now (400s before any job); return (total units, work)."""
    lenses, markets, cats = validate_research(req.clan, req.inp)
    # the documents attached to the campaign are read as sources (the host adds their text)
    mats, _ = build_materials({"attachments": req.inp.get("attachments") or []}, ctx_data(req.clan))
    r = Researcher(req.doc, req.base, req.clan, req.handler, caps, lenses, markets, cats,
                   reuse_days=settings.reuse_days, concurrency=settings.research_concurrency, materials=mats)

    def work():
        return r.run(progress=lambda done: caps.jobs.progress(done))
    return len(r.pairs), work
