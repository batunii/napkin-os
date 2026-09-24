"""report: data.report, composed from the document as the request holds it
(middleware-api.md §8.5, Contract 3 §17).

The model writes the prose (headline, summary, per-lens claims) as
structured output. The structure — sections, pins / finding / gap / contest
blocks, `confirm`, `not_researched`, `based_on` — is built here from the
document. Every claim the model wrote is checked in code before the change is
sent: its cites must be pins or non-rejected findings the document holds, and
it may state no figure absent from what it cites. A claim that fails is
dropped; a headline or summary that fails is replaced by a figure-free line
built here. The model's own confidence is never read.
"""

from __future__ import annotations

import json
import logging
import re

from ..doc import (CAMPAIGN_FIELDS, LENS_TITLES, LENSES, ctx_data, ctx_facts, ctx_findings, lens_of_key,
                   market_list)
from ..rules.cite import clean_claim
from ..util import bad, canon_sha, iso

log = logging.getLogger("napkin.report")

SYSTEM = """You write the research report an advertising planner reads first, from pinned facts
and proposed findings. Plain, specific sentences. Every sentence cites the pin ids and/or finding
ids it rests on. State no number that is not the value of a pin you cite (or written in a finding
you cite); the view renders every figure from the pins, so describing ("the larger market",
"growing fastest") is usually better than restating. Never cite anything not in the input. Write a
headline, two to four summary lines, and for each lens section one to three claims."""

FIELD_LABELS = {"brand": "Brand", "client_org": "Client", "categories": "Categories", "markets": "Markets",
                "competitor_set": "Comparators", "audience": "The researched audience", "in_market": "In market",
                "constraints": "Constraints", "campaign_type": "Campaign type"}


def _obj(props: dict) -> dict:
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


def schema(cite_ids: list[str], lenses: list[str]) -> dict:
    claim = _obj({"text": {"type": "string"}, "cites": {"type": "array", "items": {"type": "string",
                                                                                   "enum": cite_ids}}})
    return _obj({"headline": claim, "summary": {"type": "array", "items": claim},
                 "sections": {"type": "array", "items": _obj({"lens": {"type": "string", "enum": lenses or LENSES},
                                                              "claims": {"type": "array", "items": claim}})}})


def compose(doc, clan, handler, caps):
    """-> (report, cites, hits). 400 when there is nothing to cite."""
    data = ctx_data(clan)
    camp = data.get("campaign") or {}
    sel = data.get("selection") or {}
    pins = [f for f in ctx_facts(clan) if re.fullmatch(r"f_[0-9A-Z]{6,}", str(f.get("id", "")))]
    pin_by = {f["id"]: f for f in pins}
    findings = [f for f in ctx_findings(clan) if f.get("status") in ("proposed", "verified")
                and re.fullmatch(r"fi_[0-9A-Z]{6,}", str(f.get("id", "")))]
    fi_by = {f["id"]: f for f in findings}
    if not pins and not findings:
        raise bad("nothing to cite: the document holds no pin and no finding, so no claim could be sourced")
    fval = lambda f: (camp.get(f) or {}).get("value")
    brand = (fval("brand") or {}).get("name") or fval("name") or "The campaign"
    markets = list(fval("markets") or [])
    names = [fval("name") or "", (fval("brand") or {}).get("name", "")] + \
        [c.get("name", "") for c in (fval("competitor_set") or []) if isinstance(c, dict)]

    roster = [f for f in pins if str(f.get("key", "")).startswith("roster.")]
    by_lens = {l: {"pins": [], "findings": [], "gaps": [], "contests": []} for l in LENSES}
    for f in pins:
        l = lens_of_key(f.get("key"))
        if l:
            by_lens[l]["pins"].append(f)
    for fi in findings:
        l = fi.get("lens") if fi.get("lens") in LENSES else next(
            (lens_of_key(pin_by[c]["key"]) for c in fi.get("cites") or [] if c in pin_by and
             lens_of_key(pin_by[c]["key"])), None)
        if l:
            by_lens[l]["findings"].append(fi)
    for g in sel.get("gaps") or []:
        l = g.get("lens") if g.get("lens") in LENSES else lens_of_key(str(g.get("key", "")).partition(":")[2])
        if l and re.fullmatch(r"[a-z0-9_]+", str(g.get("id", ""))):
            by_lens[l]["gaps"].append(g)
    for c in sel.get("contested") or []:
        l = lens_of_key(str(c.get("key", "")).partition(":")[2].partition("@")[0])
        if l and re.fullmatch(r"[a-z0-9_]+", str(c.get("id", ""))):
            by_lens[l]["contests"].append(c)
    lenses_with = [l for l in LENSES if by_lens[l]["pins"] or by_lens[l]["findings"]]

    # -- the model writes the prose ----------------------------------------------
    written = {"headline": None, "summary": [], "sections": {}}
    dropped = []
    if lenses_with:
        payload = {"brand": brand, "markets": markets, "problem": fval("problem"), "objective": fval("objective"),
                   "lenses": [{"lens": l, "title": LENS_TITLES[l],
                               "pins": [{"id": p["id"], "entity": p["entity"], "key": p["key"], "value": p["value"],
                                         "unit": p.get("unit"), "market": p.get("market"),
                                         "confidence": p.get("confidence")} for p in by_lens[l]["pins"]],
                               "findings": [{"id": f["id"], "statement": f["statement"], "status": f["status"]}
                                            for f in by_lens[l]["findings"]],
                               "open_contests": [c["key"] for c in by_lens[l]["contests"] if c.get("status") == "open"],
                               "gaps": [g.get("searched") for g in by_lens[l]["gaps"]]}
                              for l in lenses_with]}
        try:
            raw = caps.model.structured("report", SYSTEM, payload, schema(sorted(pin_by) + sorted(fi_by), lenses_with),
                                        max_tokens=6000)
        except Exception as e:  # the structure still composes; the prose falls back to lines built here
            log.warning("report prose unavailable: %s", e)
            dropped.append(f"model: {e}")
            raw = {}
        h = raw.get("headline")
        if h:
            written["headline"], why = clean_claim(h.get("text"), h.get("cites"), pin_by, fi_by, names)
            if why:
                dropped.append(f"headline: {why}")
        for s in raw.get("summary") or []:
            c, why = clean_claim(s.get("text"), s.get("cites"), pin_by, fi_by, names)
            written["summary"].append(c) if c else dropped.append(f"summary: {why}")
        for sec in raw.get("sections") or []:
            for s in sec.get("claims") or []:
                c, why = clean_claim(s.get("text"), s.get("cites"), pin_by, fi_by, names)
                if c:
                    written["sections"].setdefault(sec.get("lens"), []).append(c)
                else:
                    dropped.append(f"{sec.get('lens')}: {why}")

    def fcites(fi):
        return [fi["id"]] + [c for c in fi.get("cites") or [] if c in pin_by]

    sections, used = [], set()
    if roster:
        ids = [f["id"] for f in roster]
        sections.append({"id": "s_identity", "title": "Brand and category", "blocks": [
            {"kind": "claim", "text": f"The categories and the client come from {brand}'s roster row in the brand "
                                      f"layer.", "cites": ids}, {"kind": "pins", "fact_ids": ids}]})
        used |= set(ids)
    for l in LENSES:
        g = by_lens[l]
        if not any(g.values()):
            continue
        blocks = [{"kind": "claim", **c} for c in written["sections"].get(l, [])[:3]]
        pm = sorted({p["market"] for p in g["pins"] if p.get("market")})
        if not blocks:
            if g["findings"]:
                fi = g["findings"][0]
                blocks.append({"kind": "claim", "text": fi["statement"], "cites": fcites(fi)})
            elif g["pins"]:
                where = f" for {market_list(pm)}" if pm else ""
                blocks.append({"kind": "claim", "text": f"{LENS_TITLES[l]}: what research pinned{where}; the values "
                                                        f"are shown below.", "cites": [p["id"] for p in g["pins"]]})
        if g["pins"]:
            blocks.append({"kind": "pins", "fact_ids": [p["id"] for p in g["pins"]]})
        blocks += [{"kind": "finding", "finding_id": fi["id"]} for fi in g["findings"]]
        blocks += [{"kind": "contest", "contest_id": c["id"]} for c in g["contests"]]
        blocks += [{"kind": "gap", "gap_id": x["id"]} for x in g["gaps"]]
        sec = {"id": "s_" + l, "title": LENS_TITLES[l], "lens": l, "blocks": blocks}
        mk = {x.get("market") for x in g["pins"] + g["gaps"]} - {None}
        if len(mk) == 1 and not any(not p.get("market") for p in g["pins"]):
            sec["market"] = mk.pop()
        sections.append(sec)
        for b in blocks:
            used |= set(b.get("cites", [])) | set(b.get("fact_ids", []))
            if b["kind"] == "finding":
                used.add(b["finding_id"])

    summary = written["summary"][:4]
    if not summary:
        summary = [{"text": fi["statement"], "cites": fcites(fi)} for l in LENSES for fi in by_lens[l]["findings"][:1]][:4]
    if not summary:
        for l in LENSES:
            if by_lens[l]["pins"] and len(summary) < 4:
                summary.append({"text": f"{LENS_TITLES[l]} rests on pinned facts; nothing has been synthesised from "
                                        f"them yet.", "cites": [p["id"] for p in by_lens[l]["pins"]]})
    if not summary:
        summary = [{"text": f"Only {brand}'s roster row is pinned so far; nothing has been researched yet.",
                    "cites": [f["id"] for f in roster] or [pins[0]["id"]]}]
    open_ct = [c for c in sel.get("contested") or [] if c.get("status") == "open"]
    headline = written["headline"] or {
        "text": f"{brand}{' in ' + market_list(markets) if markets else ''}: what the research found"
                + (", with values still contested" if open_ct else "") + ".",
        "cites": [findings[0]["id"]] if findings else [pins[0]["id"]]}

    report = {"built_at": iso(), "handler": handler, "based_on": based_on(clan),
              "headline": headline, "summary": summary, "sections": sections,
              "confirm": confirm_list(doc, camp), "not_researched": not_researched(sel, markets)}
    cites = list(dict.fromkeys(list(headline["cites"]) + [c for s in summary for c in s["cites"]] + sorted(used)))
    hits = [{"id": c, "scope": pin_by[c].get("layer", ""), "source": pin_by[c].get("origin", "")}
            for c in cites if c in pin_by]
    if dropped:
        log.info("report: dropped %d claim(s) by the cite rule: %s", len(dropped), json.dumps(dropped)[:600])
    return report, cites, hits


def based_on(clan) -> dict:
    data = ctx_data(clan)
    bf = (data.get("projection") or {}).get("built_from") or {}
    ok = lambda v: v if re.fullmatch(r"sha256:[0-9a-f]{64}", str(v)) else None
    return {"version": str(clan.get("version")),
            "facts_sha256": ok(bf.get("facts_sha256")) or canon_sha({"facts": ctx_facts(clan)}),
            "findings_sha256": ok(bf.get("findings_sha256")) or canon_sha({"findings": ctx_findings(clan)})}


def confirm_list(doc, camp) -> list:
    """D3's four while extracted or proposed, then every other proposed field."""
    d3 = ["brand", "categories", "markets", "competitor_set"]
    order = [f for f in d3 if (camp.get(f) or {}).get("origin") in ("extracted", "proposed")] + \
        [f for f in CAMPAIGN_FIELDS if f not in d3 and (camp.get(f) or {}).get("origin") == "proposed"]
    out = []
    for f in order:
        env = camp[f]
        v = env.get("value")
        if f in ("brand", "client_org"):
            shown = v.get("name") if isinstance(v, dict) else str(v)
        elif f == "markets":
            shown = market_list(v or [])
        elif f == "competitor_set":
            shown = ", ".join(c.get("name", c.get("ref", "")) for c in v or [])
        elif f == "categories":
            shown = ", ".join(v or [])
        else:
            shown = None
        label = FIELD_LABELS.get(f, f.replace("_", " ").capitalize()) + (f": {shown}" if shown else "")
        why = ("Read from the material; a person has not confirmed it yet." if env.get("origin") == "extracted"
               else "Proposed from pinned facts; a person has not confirmed it yet.")
        out.append({"address": f"{doc}#campaign.{f}", "label": label, "why": why})
    return out


def not_researched(sel, markets) -> list:
    skipped = [s for s in sel.get("lenses_skipped") or [] if s.get("lens") in LENSES]
    nr = [{"lens": s["lens"], **({"market": s["market"]} if s.get("market") else {}), "reason": s["reason"]}
          for s in skipped]
    ran = {(r.get("lens"), r.get("market")) for r in sel.get("lenses_run") or []}
    for l in LENSES:
        for m in markets:
            if (l, m) in ran or any(s["lens"] == l and s.get("market") in (None, m) for s in skipped):
                continue
            nr.append({"lens": l, "market": m, "reason": "No research run covers it yet."})
    return list({json.dumps(x, sort_keys=True): x for x in nr}.values())
