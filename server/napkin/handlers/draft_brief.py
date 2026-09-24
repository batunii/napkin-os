"""draft_brief@1 — Brief Maker: extract -> draft -> judge, one job (middleware-api.md §10)."""

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
    return BriefJob(jid, TASK, req.handler, req, caps, mats=mats)
