"""The research port — `napkin.research/1` (contract 5, §2).

  POST /v1/research   web sources + verbatim quotes, found by one fresh
                      `claude -p` with WebSearch and WebFetch

Returns SOURCES and QUOTES only: no facts, no confidence, no tiers (the
middleware derives those). The service validates what the model returns
instead of trusting it (http(s) URLs, non-empty quotes, dedupe by URL, the
cap), sets `retrieved_at` itself, and never answers 200 to a failure.

Cache (<MOCK_DATA>/cache/research/): by sha256 of the normalised request plus
the model and a prompt version. What is on disk is the query parameters (a
public-web question, never material text) and public excerpts. `?fresh=1` or
MOCK_NO_CACHE=1 skips reading it; a failure is never cached.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import os
import re
import time
import unicodedata
from urllib.parse import urlsplit, urlunsplit

from common import (ClaudeCall, ClaudeFailure, Config, DiskCache, PeripheralError, Request, Response, Slots,
                    canon, log, parse_json_object, record_cache_hit, run_claude, sha256_hex)

API = "napkin.research/1"
BACKEND = "claude-code-websearch"
# Bump when the prompt or the validation changes, so old cache entries miss.
PROMPT_VERSION = "2"

DEFAULT_MAX_SOURCES = 8
MAX_MAX_SOURCES = 20
MAX_EXCERPTS = 5
MAX_QUOTE_CHARS = 1500
MAX_QUERY_CHARS = 500

# The eight lenses (campaign-clan.md §7), with what each looks for. This steers
# the search, nothing else.
LENSES: dict[str, str] = {
    "market_structure": ("market structure: size and value of the category, volumes, growth, market shares, "
                         "the main players and how concentrated the market is, channel and segment splits"),
    "brands_positioning": ("brands and positioning: how the brand and its competitors position themselves, "
                           "their claims, price tiers, recent launches and campaigns"),
    "consumer_culture": ("consumer and culture: who buys the category and why, attitudes, barriers and "
                         "motivations, demographics, cultural trends"),
    "category_codes": ("category codes: the visual, verbal and tonal conventions of the category's "
                       "communication, what it looks and sounds like"),
    "rhythm_moments": ("rhythm and moments: seasonality, buying cycles, calendar moments, registration or "
                       "sales peaks, events that move the category"),
    "media_spend": ("media and spend: advertising spend in the category, media mix, share of voice, "
                    "channel trends"),
    "regulation_clearance": ("regulation and clearance: laws, advertising codes and standards, mandatory claims "
                             "or disclosures, grants and incentives, what copy needs clearance"),
    "effectiveness_evidence": ("effectiveness evidence: published case studies, effectiveness awards and "
                               "papers, measured results of campaigns in the category"),
}

REQUEST_FIELDS = {"query", "lens", "market", "entity", "category", "max_sources"}
MARKET_RE = re.compile(r"^[A-Z]{2}$")
ENTITY_RE = re.compile(r"^(brand|org|category)/[a-z0-9][a-z0-9._-]*$")
LEAF_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# What the model must return. The service adds ids and retrieved_at itself.
SOURCES_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["sources", "queries"],
    "properties": {
        "queries": {"type": "array", "description": "every web search query you ran, verbatim",
                    "items": {"type": "string"}},
        "sources": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["url", "publisher", "title", "excerpts"],
            "properties": {
                "url": {"type": "string", "description": "the page you fetched"},
                "publisher": {"type": "string", "description": "the organisation that published it"},
                "title": {"type": "string", "description": "the page or document title"},
                "published_at": {"type": "string", "description": "YYYY-MM-DD, only if the page states it"},
                "excerpts": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False, "required": ["quote"],
                    "properties": {"quote": {"type": "string",
                                             "description": "text copied verbatim from the fetched page"}}}},
            }}},
    },
}


def bad(message: str) -> PeripheralError:
    return PeripheralError(400, "invalid_input", message)


def today() -> str:
    return _dt.datetime.now(_dt.timezone.utc).date().isoformat()


def _clean(s: str) -> str:
    return " ".join(unicodedata.normalize("NFC", s).split())


# ------------------------------------------------------------------ input


def parse_request(raw: bytes) -> dict:
    """Validate and normalise; 400 invalid_input on anything off-contract (§2.1)."""
    req = parse_json_object(raw)
    unknown = sorted(set(req) - REQUEST_FIELDS)
    if unknown:
        raise bad(f"unknown field(s): {', '.join(unknown)}")
    query = req.get("query")
    if not isinstance(query, str) or not _clean(query):
        raise bad("query must be a non-empty string")
    query = _clean(query)
    if len(query) > MAX_QUERY_CHARS:
        raise bad(f"query is longer than {MAX_QUERY_CHARS} characters")
    lens = req.get("lens")
    if lens not in LENSES:
        raise bad(f"lens must be one of: {', '.join(LENSES)}")
    market = req.get("market")
    if not isinstance(market, str) or not MARKET_RE.match(market.strip().upper()):
        raise bad("market must be an ISO 3166-1 alpha-2 code, e.g. IE")
    market = market.strip().upper()
    if market == "UK":
        raise bad("market UK is not ISO 3166-1 alpha-2: the United Kingdom is GB")
    out = {"query": query, "lens": lens, "market": market}
    if req.get("entity") is not None:
        if not isinstance(req["entity"], str) or not ENTITY_RE.match(req["entity"]):
            raise bad("entity must be an entity ref like brand/<slug>, org/<slug> or category/<slug>")
        out["entity"] = req["entity"]
    if req.get("category") is not None:
        if not isinstance(req["category"], str) or not LEAF_RE.match(req["category"]):
            raise bad("category must be a leaf code <vertical>.<leaf>")
        out["category"] = req["category"]
    ms = req.get("max_sources", DEFAULT_MAX_SOURCES)
    if isinstance(ms, bool) or not isinstance(ms, int) or not 1 <= ms <= MAX_MAX_SOURCES:
        raise bad(f"max_sources must be an integer from 1 to {MAX_MAX_SOURCES}")
    out["max_sources"] = ms
    return out


def cache_key(req: dict, model: str) -> str:
    if model == "search-jev":  # searched by the agent, read by code, picked by jev
        import search_jev
        return sha256_hex(canon({**req, "query": req["query"].lower(), "_backend": "search-jev", "_sj": search_jev.VERSION,
                                  "_n": search_jev.MAX_CANDIDATES, "_u": search_jev.UNIT_CHARS,
                                  "_s": search_jev.MAX_SEARCHES}))
    return sha256_hex(canon({**req, "query": req["query"].lower(), "_model": model, "_v": PROMPT_VERSION,
                              **({"_p": prompt_variant()} if prompt_variant() else {})}))


# ------------------------------------------------------------------ prompt


# MOCK_RESEARCH_PROMPT picks an experimental wording for the "how to work" steps, to measure how much of a
# unit's cost follows the pages read. Empty (the default) is the wording the service has always used.
VARIANTS = {
    "capped": {"search": "1. Run at most 2 WebSearch queries aimed at this lens in this market. Prefer, in order:",
               "fetch": "2. WebFetch at most 3 pages in total, and WebFetch every page before you cite it. Cite only pages you actually fetched and"},
    "primary": {"search": "1. Run WebSearch queries aimed at this lens in this market, starting with primary sources. Stop searching and\n"
                          "   fetching as soon as you have read 3 good pages from primary or industry bodies. Prefer, in order:",
                "fetch": "2. WebFetch every page before you cite it. Cite only pages you actually fetched and"},
}


def prompt_variant() -> str:
    v = os.environ.get("MOCK_RESEARCH_PROMPT", "").strip().lower()
    if v and v not in VARIANTS:
        raise SystemExit(f"MOCK_RESEARCH_PROMPT must be one of {sorted(VARIANTS)} or empty, not {v!r}")
    return v


def build_prompt(req: dict) -> str:
    v = prompt_variant()

    def _V(part: str, default: str) -> str:
        return VARIANTS[v][part] if v else default

    lines = [
        "You are the source-discovery step of a research pipeline. Find current, citable",
        "web sources for ONE research lens in ONE market and return verbatim quotes from them.",
        "You do not analyse, summarise or conclude: another system extracts facts from your quotes.",
        "",
        f"Today's date: {today()}",
        f"Research question: {req['query']}",
        f"Lens: {req['lens']} — {LENSES[req['lens']]}",
        f"Market: {req['market']} (ISO 3166-1 alpha-2). Sources must bear on this market.",
    ]
    if "entity" in req:
        lines.append(f"Entity in focus: {req['entity']}")
    if "category" in req:
        lines.append(f"Category (leaf code): {req['category']}")
    lines += [
        f"Return at most {req['max_sources']} sources.",
        "",
        "How to work:",
        _V("search", "1. Run several WebSearch queries aimed at this lens in this market. Prefer, in order:"),
        "   primary sources (regulators, official statistics offices, government bodies,",
        "   company filings, annual reports and official press releases), then industry",
        "   bodies and trade press, then reputable news. Avoid forums, SEO content farms,",
        "   aggregators that only restate others, and pages behind a paywall you cannot read.",
        _V("fetch", "2. WebFetch every page before you cite it. Cite only pages you actually fetched and"),
        "   read; never cite a URL from search results alone, and never invent a URL.",
        "3. From each fetched page, copy 1 to 3 short passages (a sentence or a short paragraph,",
        "   under 400 characters each) that bear directly on the question: figures, dates,",
        "   named rules, stated positions. Copy them EXACTLY as written on the page — no",
        "   paraphrase, no ellipses that join separate passages, no added words.",
        "4. Prefer the most recent data. Set published_at (YYYY-MM-DD) only when the page",
        "   states its publication or update date; otherwise leave it out.",
        "5. publisher is the organisation behind the page (e.g. 'Central Statistics Office'),",
        "   title is the page or document title as shown.",
        "6. List in `queries` every search query you ran, verbatim.",
        "",
        "If you find nothing citable, return an empty sources list — that is a valid answer.",
        "Do not pad the list with weak or off-market sources.",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------ validation


def normalise_url(url) -> str | None:
    """http(s), a host, no whitespace; lower-cased scheme and host, fragment
    dropped, trailing slash trimmed."""
    if not isinstance(url, str):
        return None
    url = url.strip()
    if not url or any(c.isspace() for c in url):
        return None
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not host:
        return None
    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, parts.query, ""))


def valid_date(s) -> str | None:
    """YYYY-MM-DD, a real date, not in the future; else None (published_at absent)."""
    if not isinstance(s, str) or not DATE_RE.match(s.strip()):
        return None
    try:
        d = _dt.date.fromisoformat(s.strip())
    except ValueError:
        return None
    if d > _dt.date.fromisoformat(today()):
        return None
    return d.isoformat()


def validate_sources(raw, max_sources: int, retrieved_at: str) -> tuple[list, dict]:
    drops = {"malformed": 0, "bad_url": 0, "no_quotes": 0, "duplicate": 0, "over_cap": 0}
    if not isinstance(raw, list):
        return [], drops
    by_url: dict[str, dict] = {}
    for item in raw:
        if not isinstance(item, dict):
            drops["malformed"] += 1
            continue
        url = normalise_url(item.get("url"))
        if url is None:
            drops["bad_url"] += 1
            continue
        title = item.get("title")
        if not isinstance(title, str) or not _clean(title):
            drops["malformed"] += 1
            continue
        publisher = item.get("publisher")
        if not isinstance(publisher, str) or not _clean(publisher):
            publisher = urlsplit(url).hostname or ""
        quotes: list[str] = []
        excerpts = item.get("excerpts")
        for ex in excerpts if isinstance(excerpts, list) else []:
            q = ex.get("quote") if isinstance(ex, dict) else None
            if isinstance(q, str):
                q = q.strip()
                if q and len(q) <= MAX_QUOTE_CHARS and q not in quotes:
                    quotes.append(q)
        if url in by_url:
            drops["duplicate"] += 1
            prev = by_url[url]
            have = [e["quote"] for e in prev["excerpts"]]
            prev["excerpts"] = (prev["excerpts"] + [{"quote": q} for q in quotes if q not in have])[:MAX_EXCERPTS]
            continue
        if not quotes:
            drops["no_quotes"] += 1
            continue
        src = {"id": "src_" + hashlib.sha256(url.encode()).hexdigest()[:16], "url": url,
               "publisher": _clean(publisher), "title": _clean(title), "retrieved_at": retrieved_at}
        pub = valid_date(item.get("published_at"))
        if pub:
            src["published_at"] = pub
        src["excerpts"] = [{"quote": q} for q in quotes[:MAX_EXCERPTS]]
        by_url[url] = src
    sources = list(by_url.values())
    if len(sources) > max_sources:
        drops["over_cap"] = len(sources) - max_sources
        sources = sources[:max_sources]
    return sources, drops


# ------------------------------------------------------------------ the port


class Research:
    def __init__(self, cfg: Config, slots: Slots):
        self.cfg, self.slots = cfg, slots
        self.cache = DiskCache(cfg.cache_root, "research")

    def health(self) -> dict:
        return {"api": API, "backend": BACKEND, "model": self.cfg.research_model}

    def _run(self, req: dict) -> dict:
        if self.cfg.research_backend == "search-jev":
            import search_jev
            return search_jev.run(self, req, LENSES[req["lens"]], today())
        return self._run_agent(req)

    def _run_agent(self, req: dict) -> dict:
        """The default: one claude -p agent that searches, reads and quotes."""
        cfg = self.cfg
        call = ClaudeCall(alias=cfg.research_model, prompt=build_prompt(req), json_schema=SOURCES_SCHEMA,
                          tools=["WebSearch", "WebFetch"], permission_mode="dontAsk", trace_tools=True)
        if not self.slots.acquire(cfg.queue_timeout_for("research")):
            raise PeripheralError(503, "overloaded", f"all {self.slots.n} claude slots busy (MOCK_CONCURRENCY)")
        try:
            env = run_claude(cfg, call, cfg.timeout["research"], cfg.data / "work")
        except ClaudeFailure as f:
            if f.kind == "timeout":
                raise PeripheralError(504, "timeout", f"research did not finish within "
                                                      f"{cfg.timeout['research']:g}s") from None
            raise PeripheralError(502, "upstream_failed", f.message) from None
        finally:
            self.slots.release()
        if env.get("_exit") or env.get("is_error") or str(env.get("subtype", "success")) != "success":
            raise PeripheralError(502, "upstream_failed",
                                  f"the research CLI failed (exit {env.get('_exit')}, {env.get('subtype')})")
        out = env.get("structured_output")
        if not isinstance(out, dict):
            raise PeripheralError(502, "upstream_failed", "the research CLI returned no structured output")
        return {"output": out, "cost_usd": env.get("total_cost_usd")}

    def research(self, req: dict, fresh: bool) -> dict:
        b = self.cfg.research_backend
        key = cache_key(req, b if b == "search-jev" else self.cfg.research_model)
        with self.cache.lock(key):  # one run per key; a concurrent duplicate reads the cache
            if not fresh and not self.cfg.no_cache:
                hit = self.cache.get(key)
                if hit and isinstance(hit.get("response"), dict):
                    log(f"research cache hit {key[:12]} {req['lens']}/{req['market']}")
                    record_cache_hit(self.cfg, req)
                    return hit["response"]
            t0 = time.monotonic()
            result = self._run(req)
            out = result["output"]
            sources, drops = validate_sources(out.get("sources"), req["max_sources"], today())
            queries = [_clean(q) for q in out.get("queries") or [] if isinstance(q, str) and _clean(q)]
            response = {"sources": sources,
                        "trace": {"backend": "search-jev" if b == "search-jev" else BACKEND, "queries": queries,
                                  "model": self.cfg.research_model}}
            log(f"research {req['lens']}/{req['market']}: {len(sources)} sources in "
                f"{time.monotonic() - t0:.1f}s, dropped {drops}, cost {result['cost_usd']}")
            # The query parameters (a public-web question) and public excerpts only. An empty answer is
            # never cached: it would repeat on every later run with the same request.
            if sources:
                self.cache.put(key, {"request": req, "created": _dt.datetime.now(_dt.timezone.utc)
                                 .isoformat(timespec="seconds"), "dropped": drops, "response": response})
            return response

    def handle(self, req: Request) -> Response:
        try:
            if req.path.rstrip("/") != "/v1/research":
                raise PeripheralError(404, "not_found", f"no route {req.path}")
            if req.method != "POST":
                raise PeripheralError(405, "method_not_allowed", "use POST")
            body = parse_request(req.body)
            fresh = (req.q("fresh") or "0") not in ("", "0", "false")
            resp = self.research(body, fresh)
            return Response(200, resp, note=f"lens={body['lens']} market={body['market']} "
                                            f"sources={len(resp['sources'])}")
        except PeripheralError as e:
            return e.response()
