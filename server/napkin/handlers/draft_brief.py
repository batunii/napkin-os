"""draft_brief@1 — Brief Maker: extract -> draft -> judge, one job (middleware-api.md §10).

With NAPKIN_BRIEF_ENGINE_URL set, the brief engine drafts it and the job maps
its answer (§10.14); the middleware's own stages run when the engine gives
nothing. Unset, the job is exactly the middleware's own."""

from ..brief import engine
from ..brief.capture import build_materials
from ..brief.job import BriefJob
from ..doc import ctx_data
from ..util import bad

NAME, TASK, VERSION, KIND, CAPABILITY_MAJOR = "draft_brief", "draft_brief", "1.0", "brief", 1


def start(req, caps, settings, jid):
    """Validate now (a 400 before any job); build the job (the caller starts it)."""
    data = ctx_data(req.clan)
    if data.get("locked") is True:
        raise bad("the brief is locked: nothing may be drafted into it")
    mats = build_materials(req.inp, data)
    port = engine.port_for(settings)
    if port is not None:
        return engine.EngineBriefJob(jid, TASK, req.handler, req, caps, mats=mats, port=port)
    return BriefJob(jid, TASK, req.handler, req, caps, mats=mats)
