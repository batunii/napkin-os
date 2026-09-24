"""regenerate_field@1 — Brief Maker: redraft one field, then judge it (middleware-api.md §10.10)."""

from ..brief.fields import KEYS, locked
from ..brief.job import BriefJob
from ..doc import ctx_data
from ..util import bad

NAME, TASK, VERSION, KIND, CAPABILITY_MAJOR = "regenerate_field", "regenerate_field", "1.0", "brief", 1


def start(req, caps, settings, jid):
    data = ctx_data(req.clan)
    if data.get("locked") is True:
        raise bad("the brief is locked: nothing may be redrafted")
    field = req.inp.get("field")
    if not isinstance(field, str) or field not in KEYS:
        raise bad(f"input.field must be one of the eighteen Brief Maker fields")
    if locked(data, field):
        raise bad(f"the field is locked (data.locked_fields)")
    guidance = req.inp.get("guidance")
    if guidance is not None and not isinstance(guidance, str):
        raise bad("input.guidance must be a string")
    return BriefJob(jid, TASK, req.handler, req, caps, field=field, guidance=(guidance or "").strip() or None)
