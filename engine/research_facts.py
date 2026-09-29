"""
research_facts.py — verified facts from the knowledge layer, as the brief's writers read them
(C1a, Sai 2026-09-28/29).

    usable, skipped = research_facts.current(upstream.get("facts"))
    lines = [research_facts.line(f) for f in usable]      # '[F:f-123 v2] brand shops: 40 shops (...)'

The facts come from the verified-facts databases (foundation-spec.clan storage_model: fact
rows with id, version, status, supersedes, entity, key, value, unit, as_of and sources). They
are one source among others, re-validated or replaced by new versions, with a human deciding.
So only a current fact is used; one that has been superseded, retired or withdrawn is skipped
and the run records why. The brief records which fact versions it was given, so a fact that
is replaced later makes the brief visibly stale.

What the facts may do (Sai): back the insight and SMP as well as the RTB and desired
response. What they never do: enter the Loop 1 capture or the golden extraction, which record
the client's own brief. Sources are shown by the campaign CLAN's fields, not written into the
prose, so a line carries the fact's id and version for the writers to cite.
"""
from __future__ import annotations

import re

# A fact in one of these states is no longer the current truth.
NOT_CURRENT = {"superseded", "retired", "withdrawn", "rejected", "deprecated"}


def current(facts) -> tuple:
    """(usable, skipped): the current, well-formed facts in the order given, and one
    {"id", "why"} per fact left out. A fact needs an id and a value; `status` absent is
    taken as current."""
    usable, skipped, seen = [], [], set()
    for f in facts or []:
        if not isinstance(f, dict):
            skipped.append({"id": None, "why": "not a fact record"})
            continue
        fid = f.get("id")
        if not fid or f.get("value") in (None, ""):
            skipped.append({"id": fid, "why": "missing id or value"})
            continue
        status = str(f.get("status") or "current").lower()
        if status in NOT_CURRENT or f.get("superseded_by"):
            skipped.append({"id": fid, "why": f"not current ({status if status in NOT_CURRENT else 'superseded'})"})
            continue
        if fid in seen:
            skipped.append({"id": fid, "why": "duplicate id"})
            continue
        seen.add(fid)
        usable.append(f)
    return usable, skipped


def scope(f: dict) -> str:
    """'brand' or 'category': a fact row with brand_id null is category-level
    (foundation-spec); an explicit `scope` wins."""
    if f.get("scope"):
        return str(f["scope"])
    if "brand_id" in f:
        return "category" if f["brand_id"] is None else "brand"
    return "brand" if str(f.get("entity") or "").lower() == "brand" else "research"


def ref(f: dict) -> str:
    """The citation a writer uses: 'F:<id> v<version>'."""
    v = f.get("version")
    return f"F:{f['id']}" + (f" v{v}" if v not in (None, "") else "")


def line(f: dict) -> str:
    """One fact as the writers see it: '[F:f-123 v2] brand shops: 40 shops (brand research,
    as of 2026-06; Annual report 2025)'."""
    what = " ".join(str(x) for x in (f.get("entity"), f.get("key")) if x)
    value = f"{f['value']}{(' ' + str(f['unit'])) if f.get('unit') else ''}"
    src = next((s.get("title") or s.get("uri") for s in (f.get("sources") or []) if isinstance(s, dict)), None)
    meta = ", ".join(x for x in (f"{scope(f)} research", f"as of {f['as_of']}" if f.get("as_of") else "") if x)
    return f"[{ref(f)}] {what + ': ' if what else ''}{value} ({meta}{'; ' + str(src) if src else ''})"


def record(usable: list, skipped: list) -> dict:
    """What the run was given, for meta.research_facts: every usable fact's id and version,
    and every skipped one with the reason."""
    return {"given": len(usable) + len(skipped),
            # the line too, so later checks (the grounding count) see what the writers saw
            "used": [{"id": f["id"], "version": f.get("version"), "scope": scope(f), "line": line(f)}
                     for f in usable],
            "skipped": skipped}


# ---- C1b: the writers' citations, checked in code and moved out of the prose -------------

CITE = re.compile(r"\s*\[F:([^\]\s]+)(?:\s+v(\d+))?\]")
_NUM = re.compile(r"\d[\d.,]*\d|\d")
# Every failure below starts with this, so parse_brief counts it as an invention and never
# keeps the draft (INVENTION_MARKERS).
FAIL = "fact citation:"


def _nums(text: str) -> set:
    """The figures in `text`, separators dropped ('1,200' -> '1200')."""
    return {m.replace(",", "").rstrip(".") for m in _NUM.findall(str(text or ""))}


def _items(value) -> list:
    """(item key, text) for a field value: list index, dict key, or None for a string."""
    if isinstance(value, list):
        return [(i, str(v)) for i, v in enumerate(value)]
    if isinstance(value, dict):
        return [(k, str(v)) for k, v in value.items()]
    return [(None, str(value or ""))]


def citation_failures(value, facts: dict, brief_text: str) -> list:
    """Hard failures for a draft's [F:id] citations, given the run's current facts by id:
    a cited id that was not given; a figure in a citing item that is neither in the brief nor
    in a fact it cites (a misquote); a figure that exists only in the research, used without
    citing the fact. [] when all is well, or when the run has no facts."""
    if not facts:
        return []
    brief_nums = _nums(brief_text)
    fact_nums = {fid: _nums(f"{f.get('value')} {f.get('unit') or ''}") for fid, f in facts.items()}
    out = []
    for key, text in _items(value):
        cited = [fid for fid, _v in CITE.findall(text)]
        plain = CITE.sub("", text)
        where = f" (item {key})" if key is not None else ""
        for fid in cited:
            if fid not in facts:
                out.append(f"{FAIL} cites F:{fid}, which was not given{where}")
        for n in sorted(_nums(plain) - brief_nums):
            if cited:
                if not any(n in fact_nums.get(fid, set()) for fid in cited):
                    out.append(f"{FAIL} {n} is not in the fact it cites ({', '.join('F:' + c for c in cited)}){where}")
            else:
                src = [fid for fid, ns in fact_nums.items() if n in ns]
                if src:
                    out.append(f"{FAIL} {n} comes from the research ({', '.join('F:' + x for x in src)}) "
                               f"but is not cited{where}")
    return out


def strip(value, facts: dict) -> tuple:
    """(clean value, fact_refs): the [F:...] markers removed from what a reader sees, and one
    ref per citation {item, id, version, scope, source_ids}, item being the list index or
    think/feel/do key (None for a one-line field). The campaign CLAN shows the sources from
    fact_refs, not from the prose."""
    refs = []

    def one(key, text):
        for fid, v in CITE.findall(text):
            f = facts.get(fid) or {}
            refs.append({"item": key, "id": fid,
                         "version": int(v) if v else f.get("version"),
                         "scope": scope(f) if f else None,
                         "source_ids": [x.get("id") for x in (f.get("sources") or []) if isinstance(x, dict) and x.get("id")]})
        return CITE.sub("", text).strip()

    if isinstance(value, list):
        clean = [one(i, str(v)) for i, v in enumerate(value)]
    elif isinstance(value, dict):
        clean = {k: one(k, str(v)) for k, v in value.items()}
    elif isinstance(value, str):
        clean = one(None, value)
    else:
        clean = value
    return clean, refs


# ---- C1d: the brief and a fact disagree ------------------------------------------------

CONFLICT_P = 0.9     # the pipeline's usual jev confidence line (DISPUTE_P, FIGURE_FAIL_P)


def split_conflicts(brief_text: str, facts: list) -> tuple:
    """(agreed, contested): the facts the client brief does not contradict, and one
    {"id", "version", "line", "p"} per fact it does (jev at p >= CONFLICT_P). Sai: the engine
    picks no winner; two sources that disagree are recorded for CLAN's merge report and a
    person settles it. Until then a contested fact is not given to the writers as usable (a
    writer may only ask about it as TO CONFIRM). When jev cannot answer, every fact is kept
    as agreed and nothing is marked contested."""
    if not facts:
        return [], []
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent / "rag"))
    try:
        import jev_checks
    except Exception:            # noqa: BLE001 — retrieval package unavailable: no conflict check
        return list(facts), []
    ps = jev_checks.fact_conflicts(brief_text, [line(f) for f in facts])
    if ps is None:
        return list(facts), []
    agreed, contested = [], []
    for f, p in zip(facts, ps):
        if isinstance(p, (int, float)) and p >= CONFLICT_P:
            contested.append({"id": f["id"], "version": f.get("version"), "line": line(f), "p": round(float(p), 2)})
        else:
            agreed.append(f)
    return agreed, contested


def conflict_question(c: dict) -> dict:
    """The open question a contested fact raises."""
    return {"question": f"The client brief conflicts with verified research ({c['line']}). Which is current?",
            "why_it_matters": "the brief and the research disagree; the brief does not state either as fact "
                              "until a person settles it",
            "priority": "high", "fact_id": c["id"]}
