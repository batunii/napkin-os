"""verify_finding@1 — a person verified a finding: write it to the agency's knowledge.

The host asks this before it records the verification (Contract 3 §6.2, Contract 4
§4): a `human:<id>` source at tier `reviewer-verified`, and the finding written to
the layer as a fact, method `synthesis`, whose source is that person and whose
decision is the verification the host will record (the host chose its id, so the
layer and the document name the same decision). The answer is the pin; the host
records it, the finding's status and the decision as one change. Nothing here
writes the document: the reply's change is null.

The fact takes the strictest licence among the pins it rests on, and sits in the
brand layer when any of them does — a synthesis over client-confidential material
stays at brand scope and never promotes (C3).
"""

from __future__ import annotations

import re

from ..doc import LICENCE_RANK, ctx_facts, ctx_findings
from ..layers import origin_uri
from ..util import TaskError, bad, iso, today
from .. import reasoning as rsn

NAME, TASK, VERSION, KIND, CAPABILITY_MAJOR = "verify_finding", "verify_finding", "1.0", "short", 1


def run(req, caps):
    inp = req.inp if isinstance(req.inp, dict) else {}
    fid, who, did = inp.get("finding"), inp.get("by"), inp.get("decision_id")
    if not isinstance(fid, str) or not fid:
        raise bad("input.finding is required")
    if not isinstance(who, str) or not re.fullmatch(r"human:\S+", who):
        raise bad("input.by must be the person verifying, human:<id>")
    if not isinstance(did, str) or not re.fullmatch(r"d_[0-9A-Z]{6,}", did):
        raise bad("input.decision_id must be the d_ id the host will record")
    finding = next((f for f in ctx_findings(req.clan) if f.get("id") == fid), None)
    if finding is None:
        raise TaskError(404, "unknown_finding", f"finding {fid} is not in this document")
    if finding.get("status") != "proposed":
        raise TaskError(409, "finding_state", f"finding {fid} is already {finding.get('status')}")
    pins = {p["id"]: p for p in ctx_facts(req.clan) if isinstance(p.get("id"), str)}
    cites = [c for c in finding.get("cites") or [] if isinstance(c, str)]
    missing = [c for c in cites if c not in pins]
    if not cites or missing:
        raise bad(f"finding {fid} cites {', '.join(missing) or 'nothing'} not pinned in this document")
    cited = [pins[c] for c in cites]

    brand = next((p for p in cited if p.get("layer") == "brand"), None)
    layer = "brand" if brand else "category"
    entity = (brand or cited[0])["entity"]
    markets = {p.get("market") for p in cited}
    market = next(iter(markets)) if len(markets) == 1 else None
    licence = max((p.get("licence", "open") for p in cited), key=LICENCE_RANK.index)
    as_of = max(p["as_of"] for p in cited)
    learned = today()
    key = "synthesis." + re.sub(r"[^a-z0-9_]", "_", fid.lower())

    src = caps.layers.add_source({"uri": who, "tier": "reviewer-verified", "domain": who, "licence": licence,
                                  "title": "verified in the document"})
    decision = {"id": did, "kind": "verify", "handler": req.handler, "action": "verify_finding",
                "rationale": f"{who} verified finding {fid}: {finding.get('statement', '')}",
                "cites": [fid] + cites,
                "reasoning": rsn.make(
                    f"Wrote finding {fid} to the {layer} layer as reviewed by {who}.",
                    [rsn.point("A person verified the finding in the document", fid),
                     rsn.point("It rests on the pins it cites", *cites)],
                    rsn.certainty("high", "a person read it and said it holds"),
                    "the person reopens it or a cited pin is superseded",
                    only_option="a verified finding is written to the layer; that is what verifying means")}
    row = caps.layers.append({"layer": layer, "entity": entity, "key": key, "market": market,
                              "value": finding["statement"], "unit": "text", "as_of": as_of,
                              "retrieved_at": learned, "sources": [src], "quotes": {},
                              "licence": licence, "method": "synthesis"}, decision)
    pin = {"id": row["id"], "entity": row["entity"], "key": row["key"], "value": row["value"], "unit": row["unit"],
           "as_of": row["as_of"], "retrieved_at": row["retrieved_at"], "sources": [src] + cites,
           "confidence": finding.get("confidence") or "low", "licence": licence,
           "status": "active" if row.get("status") != "contested" else "contested",
           "version": row["version"], "supersedes": row.get("supersedes"),
           "origin": origin_uri(row["layer"], row["entity"], row["key"], row["version"]), "decision": did,
           "pinned_at": iso(), "pin_reason": f"verified finding {fid} ({who})", "layer": row["layer"],
           "method": "synthesis"}
    if row.get("market"):
        pin["market"] = row["market"]
    result = {"summary": f"Finding {fid} written to the {layer} layer as {row['id']}, reviewed by {who}.",
              "pin": pin, "source": src}
    return result, None, []
