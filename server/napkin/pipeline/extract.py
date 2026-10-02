"""extract: the ask, read from the material (Contract 3 §3).

1. Deterministic lookups first: roster pins the document holds for the
   subject brand give `categories` and `client_org`, origin proposed, citing
   the pins.
2. ONE structured-output call over the prompt and the attachments. The model
   returns candidate values with the verbatim quote each came from.
3. The rules decide what is written: every quote is verified against its
   material (a value whose quote is not there is dropped); markets must be
   ISO codes; the budget band is computed here from the amount read (the
   figure never enters the document); an in-market window needs explicit
   dates; nothing is written over a confirmed / stated field or one under an
   unanswered bad verdict. What is not supported is absent and listed as
   abstained.
"""

from __future__ import annotations

import logging

import json
import re

from ..doc import (CAMPAIGN_FIELDS, GATES, LIST_FIELDS, NOT_EXTRACTED, build_materials, ctx_data, ctx_decisions,
                   ctx_facts, decision, field_value, human_owned, read_of)
from ..rules import budget as budget_rules
from ..rules import markets as market_rules
from ..rules.quotes import find_quote
from ..util import bad, iso, lid, slug, uid
from .. import reasoning as rsn

SYSTEM = """You read a client's campaign ask for an advertising agency's research tool.
You are given the person's prompt and any attached material, each with a material_id.
Report only what the material actually says. For every value give the exact quote (copied
character for character from that material) it was read from, and the material_id.
When the material does not support a field, return null (or an empty list). Never infer,
never default, never use outside knowledge. Relative dates ("next spring") are not dates.
Markets are countries: return ISO 3166-1 alpha-2 codes (the United Kingdom is GB).
Budget: report the amount and currency exactly as stated; do not convert.
Slugs are lowercase snake_case (tv, bvod, ooh, audio, social_meta, tvc_30, social_cutdowns, ooh_6sheet).
The grounds are for what was filled and what was left out: cite the material_id each point rests on.""" + rsn.GUIDE

_Q = {"quote": {"type": "string"}, "material_id": {"type": "string"}}


log = logging.getLogger("napkin.extract")


def _obj(props: dict) -> dict:
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


def _nullable(schema: dict) -> dict:
    return {"anyOf": [schema, {"type": "null"}]}


SCHEMA = _obj({
    "brand": _nullable(_obj({"name": {"type": "string"}, **_Q})),
    "client_org": _nullable(_obj({"name": {"type": "string"}, **_Q})),
    "markets": {"type": "array", "items": _obj({"code": {"type": "string"}, **_Q})},
    "campaign_type": _nullable(_obj({"value": {"type": "string", "enum": ["launch", "always_on", "seasonal",
                                                                          "rebrand"]}, **_Q})),
    "problem": _nullable(_obj(_Q)),
    "objective": _nullable(_obj(_Q)),
    "audience_stated": _nullable(_obj(_Q)),
    "competitor_set": {"type": "array", "items": _obj({"name": {"type": "string"}, **_Q})},
    "success_measures": {"type": "array", "items": _obj({"text": {"type": "string"}, **_Q})},
    "budget": _nullable(_obj({"amount": {"type": "number"},
                              "currency": {"type": "string", "enum": ["EUR", "GBP", "USD", "other"]}, **_Q})),
    "in_market": _nullable(_obj({"from": {"type": "string"}, "to": {"type": "string"}, **_Q})),
    "channels_mandated": {"type": "array", "items": _obj({"slug": {"type": "string"}, **_Q})},
    "deliverables": {"type": "array", "items": _obj({"slug": {"type": "string"}, **_Q})},
    "constraints": {"type": "array", "items": _obj({"kind": {"type": "string", "enum": ["legal", "brand",
                                                                                         "mandatory"]},
                                                    "text": {"type": "string"}, **_Q})},
    "grounds": rsn.MODEL_SCHEMA,
})

FIELD_GUIDE = {
    "brand": "the client's own brand, only if the material labels it (e.g. 'Brand: X') or calls it ours",
    "client_org": "the client company (e.g. from an email signature)",
    "markets": "countries the campaign runs in",
    "campaign_type": "launch | always_on | seasonal | rebrand, only when exactly one is signalled",
    "problem": "the client's problem, as a verbatim quote",
    "objective": "the business outcome wanted, as a verbatim quote",
    "audience_stated": "who the client says it is for, as a verbatim quote",
    "competitor_set": "brands named as competitors or comparators (never the client's own brand)",
    "success_measures": "how the client will judge success",
    "budget": "the stated budget amount",
    "in_market": "the in-market window, only with explicit dates (YYYY-MM-DD from/to)",
    "channels_mandated": "channels the client REQUIRES (must / mandatory)",
    "deliverables": "what the agency must hand over",
    "constraints": "legal, brand and mandatory constraints",
}


def _span(mats_by_id: dict, material_id: str, quote: str, keep_quote=True) -> dict | None:
    """A verified span {material_id, locator, quote?} or None."""
    m = mats_by_id.get(material_id)
    cands = [m] if m else list(mats_by_id.values())
    for mat in cands:
        pos = find_quote(mat.text, quote)
        if pos:
            sp = {"material_id": mat.id, "locator": mat.locator(pos[0])}
            if keep_quote:
                sp["quote"] = mat.text[pos[0]:pos[1]]
            return sp
    return None


def _date(s) -> bool:
    return isinstance(s, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", s) is not None


def verify(raw: dict, mats: list, subject_name: str | None) -> tuple[dict, list]:
    """Model candidates -> {field: {value, span | item_spans}} that the rules
    accept, plus notes on what was dropped."""
    by_id = {m.id: m for m in mats}
    out, notes = {}, []

    def one(field, item, keep_quote=True):
        sp = _span(by_id, item.get("material_id"), item.get("quote"), keep_quote)
        if sp is None:
            notes.append(f"{field}: the quote given is not in the material; dropped")
        return sp

    for f in ("problem", "objective", "audience_stated"):
        it = raw.get(f)
        if it:
            sp = one(f, it)
            if sp and len(sp["quote"]) >= 4:
                out[f] = {"value": sp["quote"], "span": sp}
    it = raw.get("campaign_type")
    if it:
        sp = one("campaign_type", it)
        if sp:
            out["campaign_type"] = {"value": it["value"], "span": sp}
    items = {}
    for it in raw.get("markets") or []:
        code = market_rules.normalise(it.get("code"))
        if not code:
            notes.append(f"markets: {it.get('code')!r} is not an ISO 3166-1 alpha-2 code; dropped")
            continue
        sp = one("markets", it)
        if sp and code not in items:
            items[code] = sp
    if items:
        out["markets"] = {"value": list(items), "item_spans": items}
    it = raw.get("budget")
    if it:
        sp = one("budget_band", it, keep_quote=False)
        if sp:
            b, why = budget_rules.band(it.get("amount"), it.get("currency"))
            if b:
                out["budget_band"] = {"value": b, "span": sp}
            else:
                notes.append(why)
    it = raw.get("in_market")
    if it:
        sp = one("in_market", it)
        if sp and _date(it.get("from")) and _date(it.get("to")) and it["from"] <= it["to"] \
                and re.search(r"\b(19|20)\d{2}\b", sp["quote"]) and it["from"][:4] in sp["quote"] + it["to"][:4]:
            out["in_market"] = {"value": {"from": it["from"], "to": it["to"]}, "span": sp}
        elif sp:
            notes.append("in_market: no explicit dated window in the quote; abstained")
    for f, key in (("channels_mandated", "slug"), ("deliverables", "slug")):
        items = {}
        for it in raw.get(f) or []:
            s = re.sub(r"-", "_", slug(it.get(key, "")))
            sp = one(f, it) if s else None
            if s and sp and s not in items:
                items[s] = sp
        if items:
            out[f] = {"value": list(items), "item_spans": items}
    items, names = {}, {}
    for it in raw.get("competitor_set") or []:
        nm = (it.get("name") or "").strip()
        ref = "brand/" + slug(nm)
        if not slug(nm) or (subject_name and slug(nm) == slug(subject_name)):
            continue
        sp = one("competitor_set", it)
        if sp and ref not in items:
            items[ref], names[ref] = sp, nm
    if items:
        out["competitor_set"] = {"value": [{"ref": r, "name": names[r]} for r in items], "item_spans": items}
    items = {}
    for it in raw.get("success_measures") or []:
        txt = (it.get("text") or "").strip()
        sp = one("success_measures", it) if txt else None
        if sp:
            items[lid("sm_", txt, n=8)] = (txt, sp)
    if items:
        out["success_measures"] = {"value": [{"id": k, "text": v[0]} for k, v in items.items()],
                                   "item_spans": {k: v[1] for k, v in items.items()}}
    items = {}
    for it in raw.get("constraints") or []:
        txt = (it.get("text") or "").strip()
        sp = one("constraints", it) if txt else None
        if sp:
            items[lid("c_", txt, n=8)] = (it["kind"], txt, sp)
    if items:
        out["constraints"] = {"value": [{"id": k, "kind": v[0], "text": v[1]} for k, v in items.items()],
                              "item_spans": {k: v[2] for k, v in items.items()}}
    for f, prefix in (("client_org", "org/"), ("brand", "brand/")):
        it = raw.get(f)
        if it and slug(it.get("name", "")):
            sp = one(f, it)
            if sp:
                out[f] = {"value": {"ref": prefix + slug(it["name"]), "name": it["name"].strip()}, "span": sp}
    return out, notes


def item_key(fname, item):
    return item["ref"] if fname == "competitor_set" else item["id"] if isinstance(item, dict) else item


def merge_list_field(fname, old, new):
    """Union a re-extraction with an earlier extracted/proposed list, keeping per-item provenance."""
    def prov(env, key):
        ip = (env.get("item_provenance") or {}).get(key)
        if ip:
            return ip
        p = {"origin": env["origin"]}
        if env["origin"] == "extracted" and env.get("source"):
            p["source"] = env["source"]
        if env["origin"] == "proposed" and env.get("fact_ids"):
            p["fact_ids"] = env["fact_ids"]
        return p
    items, provs = [], {}
    for env in (old, new):
        for it in env.get("value") or []:
            k = item_key(fname, it)
            if k not in provs:
                items.append(it)
                provs[k] = prov(env, k)
    origins = {p["origin"] for p in provs.values()}
    origin = "proposed" if "proposed" in origins else "extracted"
    out = {"value": items, "origin": origin, "gate": new["gate"], "decision": new["decision"]}
    fact_ids = list(dict.fromkeys(f for p in provs.values() for f in p.get("fact_ids", [])))
    if origin == "proposed":
        out["fact_ids"] = fact_ids
    else:
        out["source"] = next(p["source"] for p in provs.values() if "source" in p)
    if len(origins) > 1 or len({json.dumps(p, sort_keys=True) for p in provs.values()}) > 1:
        out["item_provenance"] = provs
    return out


def roster_proposals(facts: list, subject_ref: str | None) -> dict:
    """Step 1: the subject's roster pins -> proposed categories / client_org."""
    roster = [f for f in facts if str(f.get("key", "")).startswith("roster.")
              and (subject_ref is None or f.get("entity") == subject_ref)
              and f.get("status", "active") == "active"]
    out = {}
    cats = sorted([f for f in roster if f.get("key") in ("roster.categories.primary", "roster.categories.secondary")
                   and re.fullmatch(r"[a-z0-9_]+\.[a-z0-9_]+", str(f.get("value")))],
                  key=lambda f: f["key"] != "roster.categories.primary")
    if cats:
        out["categories"] = {"value": list(dict.fromkeys(f["value"] for f in cats))[:2],
                             "fact_ids": [f["id"] for f in cats]}
    co = next((f for f in roster if f.get("key") == "roster.client_org"
               and re.fullmatch(r"org/[a-z0-9][a-z0-9-]*", str(f.get("value")))), None)
    if co:
        out["client_org"] = {"value": {"ref": co["value"], "name": co["value"][4:].replace("-", " ").title()},
                             "fact_ids": [co["id"]]}
    return out


def material_payload(mats) -> list:
    return [{"material_id": m.id, "name": m.name, "kind": m.kind, "text": m.text} for m in mats]


def run_extract(doc, base, clan, inp, handler, caps, skip=(), did=None, action="extract_ask"):
    """-> (result, change, hits). `skip`: fields another stage owns."""
    # a PDF or a deck goes to the model whole first: its charts, layout and image-only pages are read
    # (reading.py), and the reading is the attachment's text from here on
    from .. import reading
    for n in reading.read_inputs(caps.model, inp, caps.runlog.note):
        log.warning("%s: %s", handler, n)
    data = ctx_data(clan)
    facts = ctx_facts(clan)
    decisions = ctx_decisions(clan)
    mats, unread = build_materials(inp, data)
    if not mats:
        raise bad("nothing to read: input.prompt is empty and no attachment carries text")
    did = did or uid("d_", doc, base, "extract", [m.sha for m in mats])
    subject = field_value(data, "brand")
    subject_name = subject.get("name") if isinstance(subject, dict) else None
    subject_ref = subject.get("ref") if isinstance(subject, dict) else None

    proposed = {k: v for k, v in roster_proposals(facts, subject_ref).items() if k not in skip}
    wanted = [f for f in CAMPAIGN_FIELDS if f not in NOT_EXTRACTED and f not in skip and f not in proposed
              and f != "categories"]
    raw = caps.model.structured(
        "extract", SYSTEM,
        {"materials": material_payload(mats), "subject_brand": subject_name,
         "fields": {f: FIELD_GUIDE[f if f != "budget_band" else "budget"] for f in wanted}},
        SCHEMA, max_tokens=8000)
    cands, notes = verify(raw, mats, subject_name)
    ext_client = cands.get("client_org")
    if "client_org" in proposed and ext_client and ext_client["value"]["ref"] == proposed["client_org"]["value"]["ref"]:
        proposed["client_org"]["value"] = ext_client["value"]  # the roster's ref, the material's spelling
    for f in list(cands):
        if f in skip or f in proposed or f not in wanted:
            cands.pop(f)

    campaign_patch, written, withheld = {}, [], {}
    existing = data.get("campaign") or {}
    for fname in [f for f in CAMPAIGN_FIELDS if f in cands or f in proposed]:
        why = human_owned(data, decisions, doc, fname)
        if why:
            withheld[fname] = why
            continue
        env = {"gate": GATES[fname], "decision": did}
        if fname in proposed:
            p = proposed[fname]
            env.update(value=p["value"], origin="proposed", fact_ids=p["fact_ids"])
        else:
            c = cands[fname]
            env.update(value=c["value"], origin="extracted")
            if "item_spans" in c:
                spans = c["item_spans"]
                env["source"] = next(iter(spans.values()))
                if len({json.dumps(v, sort_keys=True) for v in spans.values()}) > 1:
                    env["item_provenance"] = {item_key(fname, it): {"origin": "extracted",
                                                                    "source": spans[item_key(fname, it)]}
                                              for it in c["value"]}
            else:
                env["source"] = c["span"]
        old = existing.get(fname) if isinstance(existing.get(fname), dict) else None
        if old and fname in LIST_FIELDS and old.get("origin") in ("extracted", "proposed"):
            env = merge_list_field(fname, old, env)
        if old and old.get("value") == env["value"] and old.get("origin") == env["origin"]:
            continue
        campaign_patch[fname] = {k: env[k] for k in
                                 ["value", "origin", "gate", "source", "fact_ids", "decision", "item_provenance"]
                                 if k in env}
        written.append(fname)

    abstained = [f for f in CAMPAIGN_FIELDS if f not in NOT_EXTRACTED and f not in written
                 and f not in withheld and f not in existing and f not in skip]
    patch = {"campaign": campaign_patch} if campaign_patch else {}
    new_mats = {m.id: {"kind": m.kind if m.kind == "prompt" else "other", "name": m.name, "sha256": m.sha,
                       "received_at": iso(), "licence": "client-confidential"}
                for m in mats if not m.known}
    if new_mats:
        patch["materials"] = new_mats
    cites = [m.id for m in mats] + [fid for p in proposed.values() for fid in p["fact_ids"]]
    rationale = (f"Read {len(mats)} material(s) with one structured-output call; filled {len(written)} field(s) "
                 f"from verified spans{' and layer pins' if proposed else ''}; abstained on {len(abstained)}"
                 + (f"; held back {len(withheld)} human-owned" if withheld else "") + ".")
    reasoning, why_notes = extract_reasoning(raw.get("grounds"), mats, facts, proposed, campaign_patch, written,
                                             abstained, withheld, notes)
    notes += why_notes
    dec = decision(doc, did, "edit", handler, action, rationale,
                   [f"campaign.{f}" for f in written] or ["campaign"], cites,
                   fields_changed=[f"campaign.{f}" for f in written],
                   material_read=[m.id for m in mats], abstained=abstained, reasoning=reasoning)
    if unread:
        dec["material_unread"] = unread
    change = {"doc": doc, "base_version": base, "data_patch": patch, "read": read_of(data, patch),
              "facts_append": [], "findings_append": [], "decisions": [dec]}
    fact_by_id = {f.get("id"): f for f in facts}
    hits = [{"id": fid, "scope": fact_by_id[fid].get("layer", "brand"), "source": fact_by_id[fid].get("origin", "")}
            for p in proposed.values() for fid in p["fact_ids"] if fid in fact_by_id]
    result = {"summary": rationale, "fields": written, "abstained": abstained, "withheld": withheld,
              "notes": notes, "materials_read": [m.id for m in mats], "materials_unread": unread,
              "materials_new": sorted(new_mats)}
    return result, change, hits


def _label(f: str) -> str:
    return f.replace("_", " ")


def extract_reasoning(raw, mats, facts, proposed, campaign_patch, written, abstained, withheld, notes):
    """The extraction's reasoning: the model's points, cite-checked against the
    materials and pins it was given; certainty from the rules — every written
    value rests on a verbatim quote or a pin, and how many the model read that
    did not verify."""
    fact_by = {f.get("id"): f for f in facts}
    mat_name = {m.id: m.name for m in mats}
    known = set(mat_name) | {fid for p in proposed.values() for fid in p["fact_ids"]}
    fallback = []
    for f in written:
        env = campaign_patch[f]
        if env.get("origin") == "proposed":
            fallback.append(rsn.point(f"{_label(f)} comes from the brand's roster pins in the layer", env["fact_ids"]))
        else:
            mid = (env.get("source") or {}).get("material_id")
            fallback.append(rsn.point(f"{_label(f)} is read verbatim from {mat_name.get(mid, 'the material')}", mid))
    if not fallback:
        fallback = [rsn.point("Nothing in the material verified against a field", [m.id for m in mats])]
    pin_levels = [fact_by.get(fid, {}).get("confidence") for p in proposed.values() for fid in p["fact_ids"]]
    if not written:
        level, basis = "low", "nothing the material says could be written to a field"
    elif notes:
        level = "medium"
        basis = (f"every written value rests on a verbatim quote or a pin, but the rules dropped {len(notes)} "
                 f"value(s) the model read (a quote not in the material, not a country code, no dated window)")
    else:
        level, basis = "high", "every written value rests on a quote found verbatim in the material, or on a pin"
    if pin_levels:
        lowest = rsn.lowest(pin_levels)
        if rsn.LEVELS.index(lowest) < rsn.LEVELS.index(level):
            level, basis = lowest, f"{basis}; the lowest pin it proposes from is {lowest} (derived)"
    rejected = ([rsn.rej(f"fill {', '.join(_label(f) for f in abstained[:6])}"
                         + (" and more" if len(abstained) > 6 else ""),
                         "nothing in the material supports them; a field is absent rather than guessed")]
                if abstained else [])
    rejected += [rsn.rej(f"write {_label(f)}", w) for f, w in withheld.items()]
    attention = []
    if notes:
        attention.append(f"The rules dropped {len(notes)} value(s) the model read: {'; '.join(notes[:3])}.")
    if withheld:
        attention.append(f"Held back {', '.join(_label(f) for f in withheld)}: a person owns them.")
    decided = (f"Filled {', '.join(_label(f) for f in written)} from the material." if written
               else "Filled no field from the material.")
    return rsn.from_model(
        raw, decided=decided, known=known, certainty_=rsn.certainty(level, basis), fallback=fallback,
        would_change_if="the client's material says otherwise, or a person confirms a different value",
        rejected=rejected, only_option="every field the material names was filled; nothing else was on offer",
        attention=" ".join(attention) or None)
