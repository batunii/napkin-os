"""
gap_filler.py: finds facts for the brief's gaps, stored facts first, then the web (ADR 0019).

Sai, 2026-10-02/03: "have another agent search and fill them up, Shrey's flow had that"; "stored facts
first then web, right now there are no stored facts but it will be cached." A gap is a field that came
out as the agency's proposal (the client never gave it) or reasons to believe that no given fact could
prove (shipped as a flagged draft on 6 of 7 briefs, 2026-10-03).

  needs(...)          what is missing, as {field, lens, query}: one per gap, on the research tool's lenses
  stored(...)         tier 1: facts already held (the run's research plus the local fact store) that jev
                      says answer the need
  search(...)         tier 2: one call to the research tool's port (napkin.research/1, POST /v1/research at
                      BRIEF_RESEARCH_URL): web sources with verbatim quotes, turned into fact rows
  fill(...)           both tiers for every need, the web only where the store had nothing, in parallel;
                      every fact the web finds is written to the store for the next brief
  infer_market(...)   the ISO country the brief is about, from the research facts or the brief's text

Facts it adds are ordinary research fact rows (ADR 0014): {id: g_..., entity, key gap.<field>, value: the
quote, unit text, as_of, sources [{title, uri}], confidence low, method web}. The writers cite them as
[F:id] like any other fact; the figure and citation checks treat them the same way. The web tier is off
when BRIEF_RESEARCH_URL is unset (the run says so); the store is BRIEF_FACT_STORE (default
~/.cache/napkin/facts).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# The lenses are the research tool's (mock-backend/research_port.py LENSES); a need names one.
NEED = {
    "reasons_to_believe": ("brands_positioning", "evidence that this is true of {brand}: {smp}"),
    "competitor_context": ("brands_positioning", "the main competitors and alternatives to {brand} in {category}"),
    "budget_scope": ("media_spend", "typical advertising spend and media costs for {category} campaigns"),
    "tone_world_assets": ("category_codes", "{brand}'s brand codes, tone of voice and distinctive assets"),
    "audience": ("consumer_culture", "who buys {category} and why: attitudes, barriers, motivations"),
    "mandatories": ("regulation_clearance", "advertising rules and required statements for {category}"),
    "background": ("market_structure", "the market for {category}: size, growth, main players"),
}
MAX_STORED = 8          # facts per need from the store
MAX_SOURCES = 4         # web sources per need (the port's default is 6)
SEARCH_WORKERS = 4
RELEVANT_P = 0.6        # jev p(the fact helps with the need) to keep a stored fact

# Countries named in briefs so far and their neighbours; the research port wants an ISO 3166-1 alpha-2 code.
COUNTRIES = {"ireland": "IE", "irish": "IE", "united kingdom": "GB", "britain": "GB", "british": "GB",
             "england": "GB", "scotland": "GB", "wales": "GB", "romania": "RO", "romanian": "RO",
             "moldova": "MD", "germany": "DE", "german": "DE", "france": "FR", "french": "FR",
             "spain": "ES", "italy": "IT", "netherlands": "NL", "poland": "PL", "united states": "US",
             "usa": "US", "american": "US", "austria": "AT", "hungary": "HU", "bulgaria": "BG"}


def infer_market(brief_text: str, facts=None) -> "str | None":
    """The country the brief is about: the commonest `market` among the research facts, else the country
    named most often in the brief's text, else None (the web tier then does not run)."""
    counts: dict = {}
    for f in facts or []:
        m = str((f or {}).get("market") or "").upper()
        if re.fullmatch(r"[A-Z]{2}", m):
            counts[m] = counts.get(m, 0) + 1
    if counts:
        return max(counts, key=counts.get)
    text = str(brief_text or "").lower()
    for name, code in COUNTRIES.items():
        n = len(re.findall(rf"(?<![a-z]){re.escape(name)}(?![a-z])", text))
        if n:
            counts[code] = counts.get(code, 0) + n
    return max(counts, key=counts.get) if counts else None


def needs(fields: dict, brand: str, category: str) -> list:
    """One need per gap: a field marked as a proposal, or reasons to believe kept as a flagged draft or
    left empty. [{field, lens, query}]; the query names the brand, the category and, for the reasons to
    believe, the proposition they must prove."""
    smp = (fields.get("smp") or {}).get("value") if isinstance(fields.get("smp"), dict) else None
    out = []
    for fid, (lens, q) in NEED.items():
        e = fields.get(fid) if isinstance(fields.get(fid), dict) else {}
        gap = bool(e.get("proposed")) or e.get("value") in (None, "", [], {})
        if fid == "reasons_to_believe":
            gap = gap or (e.get("review") or {}).get("status") == "failed_checks"
            if not smp:
                continue
        if gap:
            out.append({"field": fid, "lens": lens,
                        "query": q.format(brand=brand or "the brand", category=category or "the category",
                                          smp=str(smp or "")[:200])[:470]})
    return out


def _store_dir() -> Path:
    d = Path(os.environ.get("BRIEF_FACT_STORE") or Path.home() / ".cache" / "napkin" / "facts")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(s or "").lower()).strip("-") or "unknown"


def remember(facts: list, brand: str, market: "str | None") -> None:
    """Add facts to the store under brand/market, one JSON line each, skipping ids already there. With no
    brand nothing is stored: facts filed under 'unknown' would be read back for any brand in the market."""
    if not facts or not _slug(brand) or _slug(brand) == "unknown":
        return
    f = _store_dir() / f"{_slug(brand)}__{(market or 'xx').lower()}.jsonl"
    have = {json.loads(line).get("id") for line in f.read_text().splitlines() if line.strip()} if f.is_file() else set()
    with f.open("a", encoding="utf-8") as out:
        for row in facts:
            if row.get("id") not in have:
                have.add(row.get("id"))                 # a repeat inside this batch is skipped too
                out.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def recall(brand: str, market: "str | None") -> list:
    """Every fact the store holds for brand/market (and brand with no market)."""
    rows = []
    for name in {f"{_slug(brand)}__{(market or 'xx').lower()}.jsonl", f"{_slug(brand)}__xx.jsonl"}:
        f = _store_dir() / name
        if f.is_file():
            rows += [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
    return rows


def stored(need: dict, held: list) -> list:
    """Tier 1: the held facts jev says help with the need (p >= RELEVANT_P), at most MAX_STORED. With no
    jev answer, none (the web tier then runs): an unchecked fact is not passed off as an answer."""
    if not held:
        return []
    try:
        import jev_checks
        ps = jev_checks.facts_relevant(need["query"], [_line(f) for f in held])
    except Exception as e:  # noqa: BLE001 - no relevance check: the store is skipped, not trusted
        print(f"[i] gap-filler: store check unavailable ({type(e).__name__})", file=sys.stderr)
        return []
    if not ps:
        return []
    ranked = sorted((p, i) for i, p in enumerate(ps) if isinstance(p, (int, float)) and p >= RELEVANT_P)
    return [held[i] for _p, i in reversed(ranked)][:MAX_STORED]


def _line(f: dict) -> str:
    return f"{f.get('key', '')}: {f.get('value', '')}"[:600]


def search(need: dict, brand: str, market: str) -> list:
    """Tier 2: one call to the research port; every verbatim quote becomes a fact row. [] when the port is
    not set (BRIEF_RESEARCH_URL) or fails; the caller records why."""
    base = os.environ.get("BRIEF_RESEARCH_URL", "").strip().rstrip("/")
    if not base or not market:
        return []
    body = {"query": need["query"], "lens": need["lens"], "market": market, "max_sources": MAX_SOURCES}
    if brand:
        body["entity"] = f"brand/{_slug(brand)}"
    req = urllib.request.Request(base + "/v1/research", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=float(os.environ.get("BRIEF_RESEARCH_TIMEOUT", "300"))) as r:
        reply = json.loads(r.read())
    rows = []
    for src in reply.get("sources") or []:
        for ex in src.get("excerpts") or []:
            quote = str((ex or {}).get("quote") or "").strip()
            if not quote:
                continue
            fid = "g_" + hashlib.sha256((src.get("url", "") + quote).encode()).hexdigest()[:12].upper()
            rows.append({"id": fid, "version": 1, "status": "active", "entity": f"brand/{_slug(brand)}",
                         "key": f"gap.{need['field']}", "value": quote[:600], "unit": "text", "market": market,
                         "as_of": src.get("published_at") or src.get("retrieved_at"), "confidence": "low",
                         "method": "web", "sources": [{"title": src.get("title"), "uri": src.get("url")}]})
    return rows


def fill(gaps: list, brand: str, market: "str | None", held: list) -> dict:
    """Both tiers for every need: the store first; the web only for needs the store could not answer, in
    parallel. -> {"facts": [new or recalled rows], "by_field": {field: {"stored": n, "web": n, "why": ...}}}.
    Web facts are written to the store."""
    pool = list(held or []) + [f for f in recall(brand, market) if f.get("id") not in {h.get("id") for h in held or []}]
    report, found, ask_web = {}, {}, []
    for n in gaps:
        got = stored(n, pool)
        report[n["field"]] = {"stored": len(got), "web": 0}
        found[n["field"]] = got
        if not got:
            ask_web.append(n)
    if ask_web and not os.environ.get("BRIEF_RESEARCH_URL"):
        for n in ask_web:
            report[n["field"]]["why"] = "web search off (BRIEF_RESEARCH_URL unset)"
        ask_web = []
    elif ask_web and not market:
        for n in ask_web:
            report[n["field"]]["why"] = "market unknown"
        ask_web = []

    def one(n):
        try:
            return n, search(n, brand, market), None
        except Exception as e:  # noqa: BLE001 - a failed search is a gap that stays, recorded
            return n, [], f"{type(e).__name__}: {str(e)[:120]}"
    with ThreadPoolExecutor(max_workers=SEARCH_WORKERS) as ex:
        for n, rows, err in ex.map(one, ask_web):
            found[n["field"]] = rows
            report[n["field"]]["web"] = len(rows)
            if err:
                report[n["field"]]["why"] = err
            remember(rows, brand, market)
    facts, seen = [], {h.get("id") for h in held or []}
    for rows in found.values():
        for r in rows:
            if r.get("id") not in seen:
                seen.add(r.get("id"))
                facts.append(r)
    return {"facts": facts, "by_field": report, "market": market}
