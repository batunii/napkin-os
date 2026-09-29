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
            "used": [{"id": f["id"], "version": f.get("version"), "scope": scope(f)} for f in usable],
            "skipped": skipped}
