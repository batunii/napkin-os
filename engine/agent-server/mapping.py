"""Map a briefing-engine brief_object onto the Brief Maker app's field schema.

Pure functions, no I/O, no engine imports — unit-testable in isolation.

Contract notes (dictated by the app frontend, app/templates/brief-maker/index.html):
- The reply is a flat JSON object of schema.json top-level keys plus the meta keys
  `rationale` and `context` (the frontend strips those before patchData).
- Empty values are OMITTED, never emitted: a second Generate must not clobber
  user-filled boxes with blanks, and omitted keys keep no-op writes out of the
  artifact's decision chain.
- Regeneration replies use the app's literal dotted field keys
  ("objectives.commercial", "desired_response.think"): the frontend patches the
  value it finds at res.data[<dotted key>] verbatim, and a nested object would
  render as "[object Object]".
"""

from __future__ import annotations

# The app's regenerable fields and the JSON type each must be returned as.
# Dotted keys are literal — see module docstring.
FIELD_TYPES: dict[str, str] = {
    "project_name": "string",
    "client": "string",
    "background": "string",
    "objectives.commercial": "string",
    "objectives.behavioural": "string",
    "objectives.attitudinal": "string",
    "audience": "string",
    "competitor_context": "string",
    "insight": "string",
    "single_minded_proposition": "string",
    "reasons_to_believe": "array",
    "desired_response.think": "string",
    "desired_response.feel": "string",
    "desired_response.do": "string",
    "tone_and_world": "array",
    "budget_and_scope": "string",
    "mandatories": "array",
    "open_questions": "array",
}


def _fv(node):
    """Unwrap an engine field node: {value, status/confidence/...} → value.

    Lists of wrapped nodes unwrap element-wise. Anything else passes through.
    """
    if isinstance(node, dict) and "value" in node:
        return node["value"]
    if isinstance(node, list):
        return [_fv(x) for x in node]
    return node


def _first(*vals):
    """First non-empty value ('' / [] / {} / None all count as empty)."""
    for v in vals:
        if v not in (None, "", [], {}):
            return v
    return None


def _to_list(v) -> list[str]:
    """Coerce an engine value to the app's array-of-strings shape.

    Strings split on strong separators (newline, ';', '·') first; a comma split
    is the last resort and only kept when it actually yields multiple items.
    """
    if v in (None, "", [], {}):
        return []
    if isinstance(v, list):
        return [s for s in (str(_fv(x)).strip() for x in v) if s]
    text = str(v).strip()
    for sep in ("\n", ";", "·"):
        if sep in text:
            return [p.strip(" -•") for p in text.split(sep) if p.strip(" -•")]
    if ", " in text:
        parts = [p.strip() for p in text.split(", ") if p.strip()]
        if len(parts) > 1:
            return parts
    return [text]


def _format_question(q) -> str:
    if not isinstance(q, dict):
        return str(q)
    text = str(q.get("question", "")).strip()
    prio = q.get("priority")
    why = q.get("why_it_matters")
    if prio:
        text = f"[{prio}] {text}"
    if why:
        text = f"{text} — {why}"
    return text


def map_brief(brief: dict, clan_data: dict | None = None) -> dict:
    """brief_object → Brief Maker fields. Empty values are omitted."""
    clan_data = clan_data or {}
    meta = brief.get("meta") or {}
    l1f = (brief.get("loop1_capture") or {}).get("fields") or {}
    l2 = brief.get("loop2_brief") or {}
    gf = (brief.get("loop2_golden") or {}).get("fields") or {}

    def g(key):
        return _fv(gf.get(key))

    def l2v(key):
        return _fv(l2.get(key))

    def l1v(key):
        return _fv(l1f.get(key))

    out: dict = {}

    def put(key, value):
        if value not in (None, "", [], {}):
            out[key] = value

    put("project_name", _first(clan_data.get("project_name"), meta.get("project")))
    put("client", _first(clan_data.get("client"), meta.get("client")))
    put("background", _first(g("background"), l2v("problem"),
                             l1v("background_context"), l1v("business_problem")))

    objectives = {}
    gobj = g("objectives")
    if isinstance(gobj, dict):
        for k in ("commercial", "behavioural", "attitudinal"):
            if gobj.get(k):
                objectives[k] = str(gobj[k])
    else:
        # Group Loop-1 objective items by their objective_type tag; untyped
        # items (heuristic mode has no tag) land in commercial.
        items = l1f.get("objective")
        if isinstance(items, dict):
            items = [items]
        for item in items or []:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("objective_type", "commercial")).lower()
            if kind not in ("commercial", "behavioural", "attitudinal"):
                kind = "commercial"
            val = str(_fv(item) or "").strip()
            if val:
                objectives[kind] = (objectives[kind] + "; " + val) if kind in objectives else val
        if not objectives and l2v("objective"):
            objectives["commercial"] = str(l2v("objective"))
    put("objectives", objectives)

    put("audience", _first(g("audience"), l2v("audience"), l1v("target_audience")))
    put("competitor_context", _first(g("competitor_context"), l1v("competitors_market")))

    # Strategy fields follow the engine's fill-vs-flag rule: golden-brief value
    # or nothing — a Loop-2 client fact is not an insight or an SMP.
    put("insight", g("insight"))
    put("single_minded_proposition", g("smp"))
    put("reasons_to_believe", _to_list(_first(g("reasons_to_believe"), l1v("proof_points"))))

    dr = g("desired_response")
    if isinstance(dr, dict):
        dr = {k: str(v) for k, v in dr.items() if k in ("think", "feel", "do") and v}
        put("desired_response", dr)

    put("tone_and_world", _to_list(_first(g("tone_world_assets"), l1v("tone_and_brand"))))
    put("budget_and_scope", _first(g("budget_scope"), l2v("scope"), l1v("budget")))
    put("mandatories", _to_list(_first(g("mandatories"), l1v("mandatories"))))
    put("open_questions", [s for s in (_format_question(q) for q in l2.get("open_questions") or []) if s])

    put("rationale", build_rationale(brief))
    put("context", build_context(brief))
    return out


def build_rationale(brief: dict) -> str:
    """One- to two-sentence patch rationale; must state heuristic mode plainly."""
    meta = brief.get("meta") or {}
    mode = str(meta.get("extraction_mode", "unknown"))
    ledger = (brief.get("loop1_capture") or {}).get("no_loss_ledger") or {}
    coverage = ledger.get("coverage_pct")
    gf = (brief.get("loop2_golden") or {}).get("fields") or {}
    filled = sum(1 for v in gf.values() if _fv(v) not in (None, "", [], {}))

    if mode.startswith("heuristic"):
        return ("Heuristic extraction (no API keys): captured facts only — "
                "strategy fields (insight/SMP/RTBs) need LLM keys + RAG to fill.")
    bits = [f"Extracted via {mode}"]
    if coverage is not None:
        bits.append(f"no-loss ledger coverage {coverage}%")
    if filled:
        bits.append(f"golden-brief fill {filled}/{len(gf)} fields")
    return "; ".join(bits) + "."


def build_context(brief: dict, research_summary: str | None = None) -> str:
    """Short markdown for the app's context panel: scorecard verdict, ledger
    coverage, RAG citations, and (when present) the research dossier summary."""
    lines: list[str] = []

    sc = brief.get("betterbriefs_scorecard") or {}
    if sc.get("summary"):
        lines.append(f"**Brief quality:** {sc['summary']}")
    sm = (sc.get("single_mindedness") or {})
    if sm.get("verdict") == "multiple":
        lines.append(f"**⚠ Multiple briefs detected** — consider splitting "
                     f"({len(sm.get('split_into') or [])} candidate briefs).")

    ledger = (brief.get("loop1_capture") or {}).get("no_loss_ledger") or {}
    if ledger.get("coverage_pct") is not None:
        lines.append(f"**Capture coverage:** {ledger['coverage_pct']}% of source segments "
                     f"mapped ({ledger.get('mapped_segments')}/{ledger.get('total_segments')}).")

    l37 = brief.get("loops3_7") or {}
    sources = l37.get("sources_used") or []
    if l37.get("enabled") and sources:
        cited = "\n".join(f"- {s}" for s in sources[:8])
        more = f"\n- …and {len(sources) - 8} more" if len(sources) > 8 else ""
        lines.append(f"**Strategy grounded in precedent:**\n{cited}{more}")

    if research_summary:
        lines.append(f"**Research:**\n{research_summary}")

    return "\n\n".join(lines)
