"""compose_report@1 — Refresh report: recompose data.report whole (short task)."""

from ..doc import ctx_data, decision, read_of
from ..pipeline.report import compose
from ..util import iso, uid, ulid_like

NAME, TASK, VERSION, KIND, CAPABILITY_MAJOR = "compose_report", "compose_report", "1.0", "short", 1


def run(req, caps, jid):
    report, cites, hits, why = compose(req.doc, req.clan, req.handler, caps)
    did = uid("d_", req.doc, req.base, "compose_report", jid)
    mid = ulid_like("msg_", 1, req.doc, jid, "report")
    text = "Report refreshed from the document as it stands."
    patch = {"report": report, "intake": {"messages": {mid: {"role": "agent", "text": text, "at": iso(),
                                                             "job_id": jid, "stage": "report"}}}}
    d = decision(req.doc, did, "edit", req.handler, "compose_report",
                 "Refresh report: recomposed from the document as it stands; every claim cites a pin or a finding "
                 "and states no figure they do not hold.", ["report", f"intake.messages[{mid}]"], cites,
                 fields_changed=["report", "intake.messages"], reasoning=why)
    change = {"doc": req.doc, "base_version": req.base, "read": read_of(ctx_data(req.clan), patch),
              "data_patch": patch, "facts_append": [], "findings_append": [], "decisions": [d]}
    result = {"summary": f"Report composed: {len(report['sections'])} section(s), {len(report['confirm'])} to confirm.",
              "messages": [{"id": mid, "text": text, "stage": "report"}]}
    return result, change, hits
