"""synthesise: proposed findings over the pins (Contract 3 §6).

The model reads the pins (never the raw layer) and proposes statements, each
citing the pins it rests on. The rules: every cite is a pin the document
holds; a statement states no figure absent from its cited pins; a cite set
already written up is not written again; confidence is derived (lowest cited,
one lower for a single or stale citation) — the model is never asked; status
is always `proposed` (only a human verifies).
"""

from __future__ import annotations

import logging
import re

from ..doc import (CONF, GATES, ISO_3166, LENSES, ctx_data, ctx_decisions, ctx_facts, ctx_findings, decision,
                   field_value, human_owned, lens_of_key)
from ..rules.cite import clean_claim
from ..rules.confidence import finding_confidence
from ..rules.figures import NUM
from ..util import bad, iso, lid, uid
from .. import reasoning as rsn

log = logging.getLogger("napkin.synthesise")

SYSTEM = """You write research findings for an advertising agency from pinned facts.
Each finding is one or two plain sentences a planner can use, drawn only from the pins it cites
(cite every pin you rely on by its id). Compare markets where the pins allow. Do not introduce any
number that is not a pinned value; prefer describing direction ("higher", "growing") over restating
figures — the view shows the figures from the pins. Propose at most one finding per lens.
Optionally describe the researched audience in one or two sentences with the consumer pins it
rests on (no figures at all), or return null.
Give each finding (and the audience) its own reasoning: cite the pin ids each point rests on, and
reject the readings of the pins you did not choose.""" + rsn.GUIDE.replace(" for your answer as a whole", " for each finding and the audience")


def _obj(props: dict) -> dict:
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


def schema(pin_ids: list[str]) -> dict:
    ids = {"type": "array", "items": {"type": "string", "enum": pin_ids}}
    stmt = _obj({"statement": {"type": "string"}, "fact_ids": ids})
    return _obj({
        "findings": {"type": "array", "items": _obj({
            "lens": {"type": "string", "enum": LENSES}, "statement": {"type": "string"}, "cites": ids,
            "markets": {"type": "array", "items": {"type": "string"}}, "reasoning": rsn.MODEL_SCHEMA})},
        "audience": {"anyOf": [_obj({"definition": {"type": "string"}, "fact_ids": ids,
                                     "behaviours": {"type": "array", "items": stmt},
                                     "attitudes": {"type": "array", "items": stmt},
                                     "reasoning": rsn.MODEL_SCHEMA}), {"type": "null"}]},
    })


def usable_pins(clan: dict) -> list[dict]:
    data = ctx_data(clan)
    excluded = {e.get("fact_id") for e in ((data.get("selection") or {}).get("excluded") or [])}
    return [f for f in ctx_facts(clan) if re.fullmatch(r"f_[0-9A-Z]{6,}", str(f.get("id", "")))
            and f.get("id") not in excluded and f.get("confidence") in CONF]


def run_synthesis(doc, base, clan, inp, handler, caps, seed=None, with_audience=False):
    """-> (result, change, hits). Raises 400 when there is no pin to cite."""
    lenses = inp.get("lenses", LENSES)
    if not isinstance(lenses, list) or not lenses or any(l not in LENSES for l in lenses):
        raise bad(f"input.lenses must be a non-empty list of lens ids from {LENSES}")
    facts = usable_pins(clan)
    if not facts:
        raise bad("no pinned facts in clan.facts to synthesise from (a finding must cite pins)")
    seed = seed if seed is not None else base
    data = ctx_data(clan)
    by_id = {f["id"]: f for f in facts}
    findings_by_id = {f["id"]: f for f in ctx_findings(clan)}
    existing = {tuple(sorted(x.get("cites") or [])) for x in ctx_findings(clan)
                if x.get("status") in ("proposed", "verified")}
    brand = field_value(data, "brand") or {}
    names = [brand.get("name", ""), field_value(data, "name") or ""] + \
        [c.get("name", "") for c in (field_value(data, "competitor_set") or []) if isinstance(c, dict)]
    payload = {"brand": brand.get("name"), "markets": field_value(data, "markets"),
               "problem": field_value(data, "problem"), "objective": field_value(data, "objective"),
               "lenses": lenses,
               "pins": [{"id": f["id"], "lens": lens_of_key(f.get("key")), "entity": f["entity"], "key": f["key"],
                         "value": f["value"], "unit": f.get("unit"), "market": f.get("market"),
                         "as_of": f.get("as_of")} for f in facts]}
    raw = caps.model.structured("synthesise", SYSTEM, payload, schema(sorted(by_id)), max_tokens=6000)
    findings, decs, dropped, reason_notes = [], [], [], []
    t = iso()
    seen = set()
    for item in raw.get("findings") or []:
        cleaned, why = clean_claim(item.get("statement"), item.get("cites"), by_id, {}, names)
        if not cleaned:
            dropped.append(why)
            continue
        cites = sorted(cleaned["cites"])
        lens = item.get("lens") if item.get("lens") in lenses else None
        cited_lenses = {lens_of_key(by_id[c].get("key")) for c in cites} - {None}
        if lens is None or (cited_lenses and lens not in cited_lenses):
            lens = next(iter(sorted(cited_lenses, key=LENSES.index)), None) if cited_lenses else lens
        if lens not in lenses or tuple(cites) in existing or tuple(cites) in seen:
            continue
        seen.add(tuple(cites))
        cited = [by_id[c] for c in cites]
        fid = uid("fi_", doc, lens, cites)
        did = uid("d_", doc, seed, "finding", fid)
        mk = sorted({m for m in (item.get("markets") or []) if m in ISO_3166} |
                    {f["market"] for f in cited if f.get("market")})
        fi = {"id": fid, "statement": cleaned["text"], "cites": cites, "method": "synthesis", "status": "proposed",
              "derived_by": handler, "confidence": finding_confidence(cited), "derived_at": t, "decision": did,
              "lens": lens}
        if mk:
            fi["markets"] = mk
        findings.append(fi)
        r, why_notes = rsn.from_model(
            item.get("reasoning"), decided=f"Proposed a {lens.replace('_', ' ')} finding for a person to verify.",
            known=by_id, certainty_=_derived(cited, fi["confidence"]),
            fallback=[rsn.point(_pin_line(f), f["id"]) for f in cited],
            would_change_if="a cited pin is revised, excluded or goes stale",
            only_option="the statement is what the cited pins say together",
            attention=("It rests on thin evidence (low derived confidence)." if fi["confidence"] == "low" else None))
        reason_notes += why_notes
        decs.append(decision(doc, did, "finding", handler, "synthesise_finding",
                             f"Derived from {len(cites)} pin(s); confidence {fi['confidence']} is the lowest cited, "
                             f"stepped down for a single or stale citation — never the model's. Proposed until a "
                             f"human verifies it.", [f"findings[{fid}]"], cites, timestamp=t, reasoning=r))
    patch, read = {}, {}
    aud = raw.get("audience") if with_audience else None
    if aud and not human_owned(data, ctx_decisions(clan), doc, "audience") and not field_value(data, "audience"):
        value = _audience(aud, by_id, names, [f["id"] for f in findings if f.get("lens") == "consumer_culture"])
        if value:
            fids = sorted({i for i in value.get("size", {}).get("fact_ids", [])} |
                          {i for part in ("behaviours", "attitudes") for s in value.get(part, []) for i in s["fact_ids"]}
                          | set(aud.get("fact_ids") or []) & set(by_id))
            if fids:
                adid = uid("d_", doc, seed, "audience")
                patch = {"campaign": {"audience": {"value": value, "origin": "proposed", "gate": GATES["audience"],
                                                   "fact_ids": fids, "decision": adid}}}
                read = {"campaign.audience": None}
                cited = [by_id[i] for i in fids]
                r, why_notes = rsn.from_model(
                    aud.get("reasoning"), decided="Proposed the researched audience from the consumer pins.",
                    known=by_id, certainty_=_derived(cited, finding_confidence(cited)),
                    fallback=[rsn.point(_pin_line(f), f["id"]) for f in cited],
                    would_change_if="a person states the audience, or the consumer pins are revised",
                    only_option="the audience is what the consumer pins describe",
                    attention="A proposed audience: a person confirms it before the brief.")
                reason_notes += why_notes
                decs.append(decision(doc, adid, "edit", handler, "propose_audience",
                                     "The researched audience, proposed from consumer pins; its statements restate no "
                                     "figure (the view renders figures from the pins).", ["campaign.audience"], fids,
                                     timestamp=t, fields_changed=["campaign.audience"], reasoning=r))
    change = {"doc": doc, "base_version": base, "data_patch": patch, "read": read, "facts_append": [],
              "findings_append": findings, "decisions": decs}
    hits = [{"id": c, "scope": by_id[c].get("layer", ""), "source": by_id[c].get("origin", "")}
            for c in dict.fromkeys(c for fi in findings for c in fi["cites"])]
    result = {"summary": f"{len(findings)} finding(s) proposed from {len(facts)} pin(s)"
                         + (f"; {len(dropped)} statement(s) dropped by the cite rule" if dropped else "") + ".",
              "findings": [f["id"] for f in findings], "dropped": dropped}
    if reason_notes:
        log.info("synthesise: reasoning points dropped by the cite check: %s", "; ".join(reason_notes)[:600])
    return result, change, hits


def _no_figures(text: str, names) -> bool:
    allowed = {n for nm in names for n in NUM.findall(nm or "")}
    return all(n in allowed for n in NUM.findall(text or ""))


def _audience(aud, by_id, names, synth_ids):
    d = (aud.get("definition") or "").strip()
    if not d or not _no_figures(d, names):
        return None
    value = {"definition": d}
    for part in ("behaviours", "attitudes"):
        items = []
        for s in aud.get(part) or []:
            ids = [i for i in dict.fromkeys(s.get("fact_ids") or []) if i in by_id]
            st = (s.get("statement") or "").strip()
            if ids and st and _no_figures(st, names):
                iid = lid("a_", part, st, n=8)
                if iid not in {x["id"] for x in items}:
                    items.append({"id": iid, "statement": st, "fact_ids": ids})
        if items:
            value[part] = items
    if synth_ids:
        value["synthesis_finding_ids"] = synth_ids
    return value


def _pin_line(f) -> str:
    where = f" in {f['market']}" if f.get("market") else ""
    return f"{f['entity']} {f['key']}{where} is {f['value']}"


def _derived(cited, level) -> dict:
    """A finding's certainty: its derived confidence and the basis of it."""
    lowest = min((f.get("confidence", "low") for f in cited), key=CONF.index)
    steps = []
    if len(cited) == 1:
        steps.append("a single citation")
    if any(f.get("stale") for f in cited):
        steps.append("a stale citation")
    basis = f"the lowest cited pin is {lowest} (derived from source tier and corroboration)"
    if steps and level != lowest:
        basis += f", one lower for {' and '.join(steps)}"
    return rsn.certainty(level, basis)
