"""The brief's context parts when the client's words leave them empty.

The capture holds only what the client said. A thin brief ("Brewline is
launching an oat-milk iced latte; can you research the market?") leaves the
audience, the competitors, the objectives and the rest empty, and an empty
part helps nobody. So, after the capture and before the drafters:

- what the research found (the brief's pins, its findings, a research spin-off's
  own audience and headline) fills a part the client did not state. Sam writes
  it, citing the facts it rests on, and it is marked as the research's, never
  as the client's words;
- what still has nothing behind it, Dara proposes as an assumption, marked so,
  for a person to confirm with the client. A figure is never assumed.

Both are ordinary writes, judged by Jude like every part, and a person can
change any of them. One model call for all of them.
"""
from __future__ import annotations

from ..doc import ctx_facts, ctx_findings
from .fields import ARRAY_KEYS, LABELS, clean, filled

# The parts the context fill may write, and what each is for the planner.
PARTS = {
    "project_name": "a short working name for the project",
    "background": "the business situation and why now, in two or three sentences",
    "objectives.commercial": "the business result wanted",
    "objectives.behavioural": "what the audience should do differently",
    "objectives.attitudinal": "what the audience should think or feel differently",
    "audience": "who the work is for: a vivid picture of them, their habits and what they care about",
    "competitor_context": "who the brand is up against, named, and what the category says and does",
    "tone_and_world": "how the work should feel, as a few short phrases",
    "budget_and_scope": "the deliverables, channels and timing; never a budget figure that was not given",
    "mandatories": "what must appear or must be avoided, from regulation or the brand",
}

SYSTEM = """You are a senior planner at an advertising agency, filling the parts of a creative brief that the
client's own words leave empty. You are given what the client said, the parts already written, and the
research the agency holds for this brief (facts with ids, findings, the research's audience and headline).

For each part listed in `empty_parts`:
- If the research supports it, write it from the research and set basis "research". Cite every fact or
  finding id it rests on in `cites` (only ids given to you). Use the research's numbers exactly as given.
- If the research does not cover it, write the most useful, specific starting point a planner would propose
  for this brand and category, from what is generally known, and set basis "assumption". Name real
  competitors and a concrete audience where you can. An assumption cites nothing.
- Never invent a figure: no budget, share, size or percentage that is not in the research. For budget and
  scope, describe deliverables, channels and timing only.
- Never restate the person's request ("write a brief", "research the market") as content.
- Return null for a part only when nothing sensible can be said.
`why` says in one sentence what the value rests on. Write in plain British English."""


def _obj(props: dict) -> dict:
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


def schema(keys: list[str]) -> dict:
    part = {"anyOf": [{"type": "null"}, _obj({
        "value": {"anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
        "basis": {"type": "string", "enum": ["research", "assumption"]},
        "cites": {"type": "array", "items": {"type": "string"}},
        "why": {"type": "string"}})]}
    return _obj({k: part for k in keys})


def research_view(clan: dict, data: dict) -> dict:
    """What the brief knows from research: pins, findings, and a research spin-off's
    own audience, headline, brand and markets (frozen under `upstream`)."""
    pins = [{"id": f["id"], "key": f.get("key"), "value": f.get("value"), "unit": f.get("unit"),
             "market": f.get("market"), "as_of": f.get("as_of"), "entity": f.get("entity")}
            for f in ctx_facts(clan) if isinstance(f.get("id"), str)][:60]
    findings = [{"id": x["id"], "statement": x.get("statement"), "status": x.get("status")}
                for x in ctx_findings(clan) if isinstance(x.get("id"), str) and x.get("status") != "rejected"][:30]
    up = []
    for u in (data.get("upstream") or {}).values():
        if not isinstance(u, dict):
            continue
        camp, rep = u.get("campaign") or {}, u.get("report") or {}
        aud = (camp.get("audience") or {}).get("value") if isinstance(camp.get("audience"), dict) else None
        up.append({k: v for k, v in {
            "brand": (camp.get("brand") or {}).get("value") if isinstance(camp.get("brand"), dict) else None,
            "markets": (camp.get("markets") or {}).get("value") if isinstance(camp.get("markets"), dict) else None,
            "categories": (camp.get("categories") or {}).get("value") if isinstance(camp.get("categories"), dict)
            else None,
            "audience": aud,
            "headline": (rep.get("headline") or {}).get("text") if isinstance(rep.get("headline"), dict) else None,
        }.items() if v})
    return {"facts": pins, "findings": findings, "research": up}


def known_ids(view: dict) -> set:
    return {f["id"] for f in view["facts"]} | {x["id"] for x in view["findings"]}


def fill(model, said: str, working: dict, empty: list[str], view: dict) -> dict:
    """{key: {value, basis, cites, why}} for the empty parts it could fill. A research
    value citing nothing the brief holds is kept as an assumption."""
    keys = [k for k in empty if k in PARTS]
    if not keys:
        return {}
    payload = {"client_said": said[:6000],
               "parts_written": {k: v for k, v in working.items() if filled(v)},
               "empty_parts": {k: PARTS[k] for k in keys}, **view}
    raw = model.structured("fill_context", SYSTEM, payload, schema(keys), max_tokens=4000)
    ids = known_ids(view)
    out = {}
    for k in keys:
        r = raw.get(k)
        if not isinstance(r, dict):
            continue
        v = clean(k, r.get("value") if k in ARRAY_KEYS or not isinstance(r.get("value"), list)
                  else "; ".join(r["value"]))
        if v is None:
            continue
        cites = [c for c in dict.fromkeys(r.get("cites") or []) if c in ids]
        basis = "research" if r.get("basis") == "research" and cites else "assumption"
        out[k] = {"value": v, "basis": basis, "cites": cites if basis == "research" else [],
                  "why": " ".join(str(r.get("why") or "").split())[:300]}
    return out


def label(k: str) -> str:
    return LABELS.get(k, k.replace("_", " "))
