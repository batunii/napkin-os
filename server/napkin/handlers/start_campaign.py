"""start_campaign@1 — the chat intake: one job, six stages (middleware-api.md §8)."""

from ..doc import build_materials, ctx_data
from ..pipeline.campaign import CampaignJob
from ..util import bad, norm_sha

NAME, TASK, VERSION, KIND, CAPABILITY_MAJOR = "start_campaign", "start_campaign", "1.0", "campaign", 1


def start(req, caps, settings, jid):
    """Validate the intake and build the job (the caller starts it)."""
    inp = req.inp
    prompt = inp.get("prompt", "")
    if not isinstance(prompt, str):
        raise bad("input.prompt must be a string")
    atts = inp.get("attachments", [])
    if not isinstance(atts, list):
        raise bad("input.attachments must be a list")
    mats = ctx_data(req.clan).get("materials")
    mats = mats if isinstance(mats, dict) else {}
    for a in atts:
        if not isinstance(a, dict) or not isinstance(a.get("material_id"), str) or not a.get("sha256") \
                or not isinstance(a.get("name"), str):
            raise bad("each attachment needs a material_id, a name and a sha256")
        m = mats.get(a["material_id"])
        if not isinstance(m, dict) or norm_sha(m.get("sha256", "")) != norm_sha(a["sha256"]):
            raise bad("an attachment's material_id is not in clan.data.materials with that sha256 "
                      "(the view indexes a file before it starts the campaign)")
    xinp = {"prompt": prompt, "attachments": [{"name": a["name"], "sha256": a["sha256"], "material_id": a["material_id"],
                                               **({"text": a["text"]} if "text" in a else {})} for a in atts]}
    build_materials(xinp, ctx_data(req.clan))  # validates the hashes
    if not (prompt.strip() or any(isinstance(a.get("text"), str) and a["text"].strip() for a in atts)):
        raise bad("nothing to read: input.prompt is empty and no attachment carries text")
    return CampaignJob(jid, req.doc, req.handler, req.clan, xinp, caps, settings)
