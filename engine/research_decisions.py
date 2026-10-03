"""
research_decisions.py — what people decided on the research, as the brief's writers read it
(Shrey's side, 2026-09-30, for Sai to review; ADR 0015).

    usable, skipped = research_decisions.current(upstream.get("decisions"))
    lines = [research_decisions.line(d) for d in usable]
    # '[D:d-1] Aoife (person) rejected the footfall finding — about shops; their words: "..." (as of 2026-09-12).'

The research tool records the human side of a research document: a finding rejected or
verified, a contest resolved or left open, an edit, a verdict, the client's review. Each row
says who decided, about what, what they decided and, in their own words, why. Only a current
row is used; a superseded or malformed one is skipped and the run records why.

What the decisions may do: reach every hero writer beside the verified facts, so a writer
does not lean on a finding a person rejected or state one side of an open contest as fact.
What they never do: enter the Loop 1 capture, the golden extraction or the scorecard (they
are not the client's brief), or count as allowed figures: a figure a writer takes from a
decision line and states as fact fails the draft, as an uncited research-only figure does.
A writer may mention a decision and need not cite it; a cited [D:id] the run was not given
fails the draft.
"""
from __future__ import annotations

import re

KINDS = {"rejected_finding", "verified_finding", "resolved_contest", "open_contest", "edit", "verdict",
         "client_review"}
ROLES = {"person", "client"}
STATUSES = {"current", "superseded"}
MAX_DECISIONS = 40      # at most this many rows reach the writers (the ALLOWED FACTS list's cap)
MAX_CHARS = 300         # each text part (about, statement, reason) is clipped here, with an ellipsis
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _text(v) -> str:
    """One line of plain text: whitespace collapsed, angle brackets neutralised (a row cannot
    close the <decisions> tag), clipped at MAX_CHARS with an ellipsis."""
    s = " ".join(str(v).split()).replace("<", "‹").replace(">", "›")
    return s if len(s) <= MAX_CHARS else s[:MAX_CHARS - 1].rstrip() + "…"


def _why_malformed(d: dict) -> "str | None":
    """The first reason a row is not a well-formed decision, or None."""
    for k in ("id", "who", "about", "statement"):
        if not isinstance(d.get(k), str) or not d[k].strip():
            return f"missing or empty {k}"
    if d.get("kind") not in KINDS:
        return f"unknown kind {d.get('kind')!r}"
    if d.get("role") not in ROLES:
        return f"unknown role {d.get('role')!r}"
    if d.get("reason") is not None and not isinstance(d["reason"], str):
        return "reason is not text"
    if d.get("as_of") is not None and not (isinstance(d["as_of"], str) and _DATE.match(d["as_of"])):
        return f"as_of {d.get('as_of')!r} is not YYYY-MM-DD"
    if str(d.get("status") or "current") not in STATUSES:
        return f"unknown status {d.get('status')!r}"
    return None


def current(decisions) -> tuple:
    """(usable, skipped): the current, well-formed decisions in the order given (at most
    MAX_DECISIONS), and one {"id", "why"} per row left out. `status` absent is current."""
    usable, skipped, seen = [], [], set()
    for d in decisions or []:
        if not isinstance(d, dict):
            skipped.append({"id": None, "why": "not a decision record"})
            continue
        did = d.get("id") if isinstance(d.get("id"), str) else None
        bad = _why_malformed(d)
        if bad:
            skipped.append({"id": did, "why": bad})
            continue
        if str(d.get("status") or "current") == "superseded":
            skipped.append({"id": did, "why": "not current (superseded)"})
            continue
        if did in seen:
            skipped.append({"id": did, "why": "duplicate id"})
            continue
        if len(usable) >= MAX_DECISIONS:
            skipped.append({"id": did, "why": f"over the cap of {MAX_DECISIONS} decisions"})
            continue
        seen.add(did)
        usable.append(d)
    return usable, skipped


def line(d: dict) -> str:
    """One decision as the writers see it: '[D:<id>] <who> (<role>) <statement> — about
    <about>; their words: "<reason>" (as of <date>).' The reason and date parts are left out
    when the row has none."""
    statement = _text(d["statement"]).rstrip(".!")
    out = f"[D:{_text(d['id'])}] {_text(d['who'])} ({d['role']}) {statement} — about {_text(d['about'])}"
    if d.get("reason"):
        out += f'; their words: "{_text(d["reason"])}"'
    if d.get("as_of"):
        out += f" (as of {d['as_of']})"
    return out + "."


def record(usable: list, skipped: list) -> dict:
    """What the run was given, for meta.research_decisions: every usable row's id, kind and
    line, and every skipped one with the reason."""
    return {"given": len(usable) + len(skipped),
            "used": [{"id": d["id"], "kind": d["kind"], "line": line(d)} for d in usable],
            "skipped": skipped}


# ---- the writers' [D:id] mentions and figures, checked in code -------------------------

CITE = re.compile(r"\s*\[D:([^\]\s]+)\]")
_NUM = re.compile(r"\d[\d.,]*\d|\d")
# Every failure below starts with this, so parse_brief counts it as an invention and never
# keeps the draft (INVENTION_MARKERS).
FAIL = "research decision:"


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


# An identifier (a CLAN id like fi_43HX2GVBYFRI or d_01M3W4..., or any token mixing letters and digits) is
# not a figure: its digits were read as numbers "from what people decided" and failed honest drafts that
# said "2 in 3" (2026-10-03, ADR 0017 fix).
_ID_TOKEN = re.compile(r"\b(?:[A-Za-z]+_[A-Za-z0-9_]+|(?=[A-Za-z0-9]*[A-Za-z])(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{6,})\b")


def _figure_text(s: str) -> str:
    """`s` with identifier tokens removed, so only real figures are left for _nums."""
    return _ID_TOKEN.sub(" ", str(s or ""))


def citation_failures(value, decisions: dict, brief_text: str, facts: "dict | None" = None) -> list:
    """Hard failures for a draft, given the run's current decisions by id: a cited [D:id] the
    run was not given; a figure that is in no fact and not in the brief but is in a decision
    (its about, statement or reason: never its id or date), stated as if it were a fact.
    [] when all is well, or when the run has no decisions."""
    if not decisions:
        return []
    known = _nums(brief_text)
    for f in (facts or {}).values():
        known |= _nums(f"{f.get('value')} {f.get('unit') or ''}")
    dec_nums = {did: _nums(_figure_text(" ".join(str(d.get(k) or "") for k in ("about", "statement", "reason"))))
                for did, d in decisions.items()}
    out = []
    for key, text in _items(value):
        where = f" (item {key})" if key is not None else ""
        for did in CITE.findall(text):
            if did not in decisions:
                out.append(f"{FAIL} cites D:{did}, which was not given{where}")
        for n in sorted(_nums(CITE.sub("", text)) - known):
            src = [did for did, ns in dec_nums.items() if n in ns]
            if src:
                out.append(f"{FAIL} {n} comes from what people decided ({', '.join('D:' + x for x in src)}), "
                           f"which is not a fact{where}")
    return out


def strip(value) -> tuple:
    """(clean value, decision_refs): the [D:...] markers removed from what a reader sees, and
    one {item, id} per mention (item: list index, think/feel/do key, or None)."""
    refs = []

    def one(key, text):
        refs.extend({"item": key, "id": did} for did in CITE.findall(text))
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


# ---- reading the decisions out of a research CLAN (ADR 0017, Sai 2026-10-03) -----------------
# A research CLAN records each person's verdict on a finding on the finding itself
# (shared/findings.yaml: status verified | rejected, with a verification or rejection block) and in
# agent/decision-chain.yaml (verify_finding / reject_finding, with the reviewer's rationale). Nothing
# turned these into the rows above, so no decision ever reached the writers.

def _clan_file(path, name: str):
    """The text of `name` inside a research CLAN given as a .clan zip or an unzipped folder; None if absent."""
    import zipfile
    from pathlib import Path
    p = Path(path)
    if p.is_dir():
        f = p / name
        return f.read_text(encoding="utf-8") if f.is_file() else None
    with zipfile.ZipFile(p) as z:
        return z.read(name).decode("utf-8") if name in z.namelist() else None


def clan_parts(path) -> dict:
    """{facts, findings, chain, data} of a research CLAN in any of the forms the research tool writes: a
    .clan zip, an unzipped folder (shared/facts.yaml with its `facts:` wrapper, shared/findings.yaml,
    agent/decision-chain.yaml, shared/data.yaml) or the dev runs' single clan.json (keys facts, findings,
    decision_chain, data). Missing parts are empty."""
    import json
    from pathlib import Path
    import yaml
    p = Path(path)
    if p.suffix.lower() == ".json":
        c = json.loads(p.read_text(encoding="utf-8"))
        chain = c.get("decision_chain") or []
        return {"facts": c.get("facts") or [], "findings": c.get("findings") or [], "sources": c.get("sources") or [],
                "chain": chain.get("decisions", []) if isinstance(chain, dict) else chain, "data": c.get("data") or {}}

    def part(name, key):
        raw = _clan_file(p, name)
        v = yaml.safe_load(raw) if raw else None
        return (v.get(key, []) if isinstance(v, dict) and key else v) or ([] if key else {})
    return {"facts": part("shared/facts.yaml", "facts"), "findings": part("shared/findings.yaml", "findings"),
            "sources": part("shared/sources.yaml", "sources"),
            "chain": part("agent/decision-chain.yaml", "decisions"), "data": part("shared/data.yaml", None)}


def _date(v) -> "str | None":
    """YYYY-MM-DD from a date, datetime or ISO string; None when there is none."""
    if v is None:
        return None
    s = v.isoformat() if hasattr(v, "isoformat") else str(v)
    return s[:10] if _DATE.match(s[:10]) else None


def from_clan(path) -> list:
    """The decision rows (the shape current() reads) for every finding a person verified or rejected in
    the research CLAN at `path` (any form clan_parts reads). A proposed finding has no decision and gives no row. The reviewer is a
    user id in the CLAN, so `who` is 'a reviewer'; the reason is the rejection's own reason, else the
    rationale the decision chain recorded for that decision. The row id is the decision's id, so a
    writer's [D:id] points at the record in the CLAN."""
    parts = clan_parts(path)
    findings, chain = parts["findings"], parts["chain"]
    why = {d.get("id"): d.get("rationale") for d in chain if isinstance(d, dict) and d.get("id")}
    rows = []
    for f in findings:
        if not isinstance(f, dict) or not f.get("statement"):
            continue
        status = f.get("status")
        block = f.get("rejection") if status == "rejected" else f.get("verification") if status == "verified" else None
        if not isinstance(block, dict):
            continue
        did = str(block.get("decision") or f"d_{f.get('id')}")
        reason = block.get("reason") or why.get(block.get("decision"))
        lens = str(f.get("lens") or "research").replace("_", " ")
        rows.append({"id": did, "kind": "rejected_finding" if status == "rejected" else "verified_finding",
                     "who": "a reviewer", "role": "person", "about": f"the {lens} finding", "finding": f.get("id"),
                     "statement": ("rejected this finding: " if status == "rejected" else "verified this finding: ")
                                  + str(f["statement"]),
                     "reason": str(reason) if reason else None, "as_of": _date(block.get("at")), "status": "current"})
    return rows

