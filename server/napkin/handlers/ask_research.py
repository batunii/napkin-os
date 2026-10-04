"""ask_research@1 — a person's question about the research, answered in the section it was
asked in; jev decides whether the facts already here answer it or the researchers search (long task)."""

from ..pipeline.ask import Asker, validate

NAME, TASK, VERSION, KIND, CAPABILITY_MAJOR = "ask_research", "ask_research", "1.0", "long", 1


def prepare(req, caps, settings):
    question, lens = validate(req.clan, req.inp)
    a = Asker(req.doc, req.base, req.clan, req.handler, caps, question, lens, settings,
              attachments=req.inp.get("attachments") or [])

    def work():
        return a.run(progress=lambda done: caps.jobs.progress(done))
    return 2, work
