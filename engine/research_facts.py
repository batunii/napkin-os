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
import sys

# A fact in one of these states is no longer the current truth.
NOT_CURRENT = {"superseded", "retired", "withdrawn", "rejected", "deprecated"}


def _version(f: dict) -> int:
    """A fact's version as a number (absent or unreadable: 0)."""
    try:
        return int(f.get("version") or 0)
    except (TypeError, ValueError):
        return 0


def current(facts) -> tuple:
    """(usable, skipped): the current, well-formed facts in the order given, and one
    {"id", "why"} per fact left out. A fact needs an id (text or a number) and a value;
    `status` absent is taken as current.

    2026-10-03 (EC-005, EC-071, EC-072): a research CLAN's `{"facts": [...]}` wrapper is
    unwrapped (it used to give 1 skipped, 0 used, silently); a row with a list or dict for
    its id is skipped with that reason instead of crashing the brief; of two rows with the
    same id, the higher version is used wherever it sits, and the other is skipped as an
    older version."""
    if isinstance(facts, dict):
        if isinstance(facts.get("facts"), list):
            print("[i] research facts came wrapped in {'facts': [...]}; unwrapped", file=sys.stderr)
            facts = facts["facts"]
        else:
            return [], [{"id": None, "why": "facts is a mapping, not a list of fact records"}]
    usable, skipped, at = [], [], {}
    for f in facts or []:
        if not isinstance(f, dict):
            skipped.append({"id": None, "why": "not a fact record"})
            continue
        fid = f.get("id")
        if isinstance(fid, (list, dict, set, tuple)) or isinstance(fid, bool):
            skipped.append({"id": None, "why": f"id is a {type(fid).__name__}, not text"})
            continue
        if fid in (None, "") or f.get("value") in (None, ""):
            skipped.append({"id": fid, "why": "missing id or value"})
            continue
        status = str(f.get("status") or "current").lower()
        if status in NOT_CURRENT or f.get("superseded_by"):
            skipped.append({"id": fid, "why": f"not current ({status if status in NOT_CURRENT else 'superseded'})"})
            continue
        if fid in at:
            kept = usable[at[fid]]
            if _version(f) > _version(kept):
                usable[at[fid]] = f
                skipped.append({"id": fid, "why": f"older version ({_version(kept)}) of a newer one"})
            else:
                skipped.append({"id": fid, "why": "duplicate id" if _version(f) == _version(kept)
                                else f"older version ({_version(f)}) of a newer one"})
            continue
        at[fid] = len(usable)
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


def _short(x: float) -> str:
    """A number as a writer would type it: '27', '7.3', '85.5' (no trailing zeros)."""
    return f"{x:.2f}".rstrip("0").rstrip(".")


def forms(f: dict) -> list:
    """The other honest ways to write a fact's figure, so a writer quoting it is not called a misquote:
    a proportion as a percentage (0.27 -> '27%'), a large amount in millions or billions
    (85000000 -> '85 million'). The research tool stores shares as 0-1 and money in units, and a
    planner writes '27%' and 'EUR 85 million'."""
    v, unit = f.get("value"), str(f.get("unit") or "").lower()
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return []
    out = []
    if unit in ("proportion", "share", "ratio") and 0 <= v <= 1:
        out.append(f"{_short(v * 100)}%")
    if abs(v) >= 1e9:
        out.append(f"{_short(v / 1e9)} billion")
    elif abs(v) >= 1e6:
        out.append(f"{_short(v / 1e6)} million")
    return out


# What a fact's evidence is worth (P2, 2026-10-03, EC-012): every research fact used to reach the writers
# as "VERIFIED", although 88 of 94 in one research CLAN were low confidence and single-source. A fact is
# verified when its confidence is high or a person verified it (a finding's synthesis row, or status
# verified); anything else is unverified: usable, but never stated as settled.
VERIFIED_CONFIDENCE = {"high", "verified"}


def status_of(f: dict) -> str:
    """'verified' or 'unverified' (see VERIFIED_CONFIDENCE)."""
    if str(f.get("confidence") or "").lower() in VERIFIED_CONFIDENCE or str(f.get("status") or "").lower() == "verified":
        return "verified"
    if str(f.get("method") or "").lower() == "synthesis":      # a finding a person verified (ADR 0017)
        return "verified"
    return "unverified"


def line(f: dict) -> str:
    """One fact as the writers see it: '[F:f-123 v2] brand shops: 40 shops (brand research,
    as of 2026-06; Annual report 2025)'; a share or a large amount also carries its usual form
    ('0.27 proportion (27%)'), which the figure checks then accept."""
    what = " ".join(str(x) for x in (f.get("entity"), f.get("key")) if x)
    alt = forms(f)
    value = (f"{f['value']}{(' ' + str(f['unit'])) if f.get('unit') else ''}"
             + (f" ({', '.join(alt)})" if alt else ""))
    src = next((s.get("title") or s.get("uri") for s in (f.get("sources") or []) if isinstance(s, dict)), None)
    # The market a fact describes (2026-10-03, P3 test: UK-only figures were applied to Ireland because the line
    # never said which market a fact was about).
    meta = ", ".join(x for x in (f"market {f['market']}" if f.get("market") else "", f"{scope(f)} research", status_of(f),
                                 f"as of {f['as_of']}" if f.get("as_of") else "") if x)
    return f"[{ref(f)}] {what + ': ' if what else ''}{value} ({meta}{'; ' + str(src) if src else ''})"


def record(usable: list, skipped: list) -> dict:
    """What the run was given, for meta.research_facts: every usable fact's id and version,
    and every skipped one with the reason."""
    return {"given": len(usable) + len(skipped),
            # the line too, so later checks (the grounding count) see what the writers saw
            "used": [{"id": f["id"], "version": f.get("version"), "scope": scope(f), "line": line(f),
                      "status": status_of(f)} for f in usable],
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


# Identifiers are not statistics (2026-10-02, Sai; EC-044): a research fact whose unit is a code (a phone
# number, a registration or reference number) is a name for something. A draft that writes it in full uses
# the identifier, not a figure: its digits are not checked as uncited research figures, and the fact is
# attached to the field's fact_refs automatically. Seen on Samaritans: "Save 116 123 in your phone" emptied
# the desired response.
IDENTIFIER_UNITS = {"code"}


def _identifier_patterns(facts: dict) -> dict:
    """{fact id: compiled pattern} for identifier facts with digits; the pattern accepts the value with its
    digit groups joined by a space, a hyphen or nothing ('116 123', '116-123', '116123')."""
    out = {}
    for fid, f in facts.items():
        v = str(f.get("value") or "")
        if str(f.get("unit") or "").lower() in IDENTIFIER_UNITS and re.search(r"\d", v):
            groups = re.findall(r"[A-Za-z0-9]+", v)
            if groups:
                out[fid] = re.compile(r"(?<![\w])" + r"[\s-]?".join(re.escape(g) for g in groups) + r"(?![\w])", re.I)
    return out


def _without_identifiers(text: str, ids: dict) -> tuple:
    """(text with every identifier written in full blanked out, the identifier fact ids it used)."""
    used = []
    for fid, pat in ids.items():
        if pat.search(text):
            used.append(fid)
            text = pat.sub(" ", text)
    return text, used


def citation_failures(value, facts: dict, brief_text: str) -> list:
    """Hard failures for a draft's [F:id] citations, given the run's current facts by id:
    a cited id that was not given; a figure in a citing item that is neither in the brief nor
    in a fact it cites (a misquote); a figure that exists only in the research, used without
    citing the fact. [] when all is well, or when the run has no facts."""
    if not facts:
        return []
    brief_nums = _nums(brief_text)
    # a fact's figure in every honest form (forms()) is the fact's; its date also counts, but only for an
    # item that cites it ("27% (2014) [F:x]"), so a draft saying "in 2026" is never taken for research
    fact_nums = {fid: _nums(" ".join([str(f.get("value")), str(f.get("unit") or ""), *forms(f)]))
                 for fid, f in facts.items()}
    cite_nums = {fid: ns | _nums(str(facts[fid].get("as_of") or "")) for fid, ns in fact_nums.items()}
    ids = _identifier_patterns(facts)
    out = []
    for key, text in _items(value):
        cited = [fid for fid, _v in CITE.findall(text)]
        plain, _used = _without_identifiers(CITE.sub("", text), ids)
        where = f" (item {key})" if key is not None else ""
        for fid in cited:
            if fid not in facts:
                out.append(f"{FAIL} cites F:{fid}, which was not given{where}")
        for n in sorted(_nums(plain) - brief_nums):
            if cited:
                if not any(n in cite_nums.get(fid, set()) for fid in cited):
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
    ids = _identifier_patterns(facts or {})

    def one(key, text):
        cited = {fid for fid, _v in CITE.findall(text)}
        for fid in _without_identifiers(CITE.sub("", text), ids)[1]:
            if fid not in cited:                       # an identifier written in full: its fact, attached
                f = facts.get(fid) or {}
                refs.append({"item": key, "id": fid, "version": f.get("version"), "scope": scope(f) if f else None,
                             "source_ids": [x.get("id") for x in (f.get("sources") or []) if isinstance(x, dict) and x.get("id")],
                             "auto": "identifier"})
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


# ---- C2: the territory check's rival from the brand research ---------------------------

def rivals(upstream: "dict | None", facts: list) -> list:
    """Competitor names from the brand research, in order and without repeats: the upstream
    `competitors` facet first, then current facts whose entity is 'competitor' (their value,
    or key when the value is not a name). Facts the brief contradicts are already out of
    `facts` (C1d). [] when the research names none: the territory call then picks the rival
    from the brief, as before (decision 5: research first, the Claude call as fallback)."""
    out = []
    comps = (upstream or {}).get("competitors")
    comps = comps if isinstance(comps, list) else [c for c in str(comps or "").split(",")]
    for c in comps:
        if str(c).strip() and str(c).strip() not in out:
            out.append(str(c).strip())
    for f in facts or []:
        if str(f.get("entity") or "").lower() == "competitor":
            name = str(f.get("value") if not str(f.get("value") or "").replace(".", "").isdigit() else f.get("key") or "").strip()
            if name and name not in out:
                out.append(name)
    return out


# ---- one research CLAN as run()'s upstream (ADR 0017, Sai 2026-10-03) ------------------------

def load_clan(path) -> dict:
    """The research CLAN at `path` (.clan zip, unzipped folder or a dev run's clan.json) as
    run(upstream=...) takes it: {"facts": [...], "decisions": [...], "brand": name} with whatever the CLAN
    holds. The facts file's `facts:` wrapper is unwrapped (RUNBOOK trap: unwrapped wrongly it gave 1
    skipped, 0 used); dates are turned to strings. The CLAN's category is a research-taxonomy code, not
    the engine's category list, so it is not passed (jev picks the category from the brief, EC-013)."""
    import json as _json
    import research_decisions
    parts = research_decisions.clan_parts(path)
    srcs = {s.get("id"): s for s in parts.get("sources") or [] if isinstance(s, dict) and s.get("id")}
    facts = _json.loads(_json.dumps(parts["facts"], default=str))
    for f in facts:                     # source ids -> {title, uri, publisher, published_at, tier} (EC-012)
        if isinstance(f, dict) and isinstance(f.get("sources"), list):
            f["sources"] = [({k: str(srcs[x][k]) for k in ("id", "title", "uri", "publisher", "published_at", "tier")
                              if srcs[x].get(k)} if isinstance(x, str) and x in srcs else x) for x in f["sources"]]
    up = {"facts": facts, "decisions": research_decisions.from_clan(path)}
    brand = (parts["data"].get("campaign") or {}).get("brand") if isinstance(parts["data"], dict) else None
    brand = brand.get("value") if isinstance(brand, dict) else brand
    brand = brand.get("name") if isinstance(brand, dict) else brand
    if isinstance(brand, str) and brand.strip():
        up["brand"] = brand.strip()
    return up
