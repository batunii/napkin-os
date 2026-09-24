#!/usr/bin/env python3
"""
Mock research — a stand-in for the research / source-discovery service the
Napkin middleware calls through its research port.

It answers `POST /v1/research` exactly as a real search/discovery service
will, so the middleware is pointed at the real one later by changing ONE
value: `NAPKIN_RESEARCH_URL`. Nothing in the middleware may name this server
or branch on it.

    python3 mock-research/server.py                  # http://127.0.0.1:8792/v1/research
    MOCK_RESEARCH_PORT=8793 python3 mock-research/server.py

Each request is answered by headless Claude Code with web search and fetch:

    claude -p --output-format json --tools WebSearch WebFetch \\
        --allowedTools WebSearch WebFetch --json-schema <sources schema> --model <alias>

so every source is a real, fetched URL and every excerpt is text quoted from
it. The service returns SOURCES and QUOTES only — no facts, no confidence, no
tiers. The middleware derives those (tier by its domain policy, confidence
from tier and corroboration).

Principles:
  * Validates what the model returns instead of trusting it: http(s) URLs
    only, non-empty quotes, malformed entries dropped, deduplicated by URL,
    capped at max_sources. `retrieved_at` is set here, never by the model.
  * Never a 200 that hides a failure: a CLI failure is 502, a timeout 504.
    An honest `sources: []` (nothing citable found) is a valid 200.
  * Deterministic on rerun: the validated response is cached on disk by a
    hash of the normalised request, so a repeat is free and identical
    (`?fresh=1` or MOCK_RESEARCH_NO_CACHE=1 bypasses it). The cache holds the
    query parameters and public source excerpts only — no other request data
    is written anywhere.

Environment:
  MOCK_RESEARCH_PORT         listen port (8792)
  MOCK_RESEARCH_HOST         bind address (127.0.0.1)
  MOCK_RESEARCH_MODEL        Claude Code model alias (sonnet)
  MOCK_RESEARCH_TIMEOUT      seconds one research call may take (420)
  MOCK_RESEARCH_CONCURRENCY  research subprocesses at once (4)
  MOCK_RESEARCH_DATA         cache + scratch directory
  MOCK_RESEARCH_NO_CACHE     1 = never read the cache (results are still written)
  MOCK_RESEARCH_MAX_BUDGET_USD  per-call spend cap passed to the CLI (unset = none)
  MOCK_RESEARCH_CLAUDE_BIN   the claude executable (claude)

Python 3.11+, standard library only.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit

BACKEND = "claude-code-websearch"
# Bump when the prompt or the validation changes, so old cache entries miss.
PROMPT_VERSION = "1"

DEFAULT_DATA = ("/tmp/claude-1000/-home-batunii-Documents-Code-napkin-os/"
                "8548b27c-f3fd-4056-9479-bb6b716c6517/scratchpad/mock-research")

PORT = int(os.environ.get("MOCK_RESEARCH_PORT", "8792"))
HOST = os.environ.get("MOCK_RESEARCH_HOST", "127.0.0.1")
MODEL = os.environ.get("MOCK_RESEARCH_MODEL", "sonnet")
TIMEOUT = float(os.environ.get("MOCK_RESEARCH_TIMEOUT", "420"))
CONCURRENCY = max(1, int(os.environ.get("MOCK_RESEARCH_CONCURRENCY", "4")))
DATA = Path(os.environ.get("MOCK_RESEARCH_DATA", DEFAULT_DATA))
NO_CACHE = os.environ.get("MOCK_RESEARCH_NO_CACHE", "") not in ("", "0", "false")
MAX_BUDGET = os.environ.get("MOCK_RESEARCH_MAX_BUDGET_USD", "")
CLAUDE_BIN = os.environ.get("MOCK_RESEARCH_CLAUDE_BIN", "claude")

MAX_BODY = 64 * 1024
DEFAULT_MAX_SOURCES = 8
MAX_MAX_SOURCES = 20
MAX_EXCERPTS = 5
MAX_QUOTE_CHARS = 1500

# The eight lenses of the Planner Research Taxonomy
# (app/templates/campaign-research/app/pipeline.yaml, campaign-clan.md §7),
# with what each one looks for — this steers the search, nothing else.
LENSES: dict[str, str] = {
    "market_structure": (
        "market structure: size and value of the category, volumes, growth, "
        "market shares, the main players and how concentrated the market is, "
        "channel and segment splits"),
    "brands_positioning": (
        "brands and positioning: how the brand and its competitors position "
        "themselves, their claims, price tiers, recent launches and campaigns"),
    "consumer_culture": (
        "consumer and culture: who buys the category and why, attitudes, "
        "barriers and motivations, demographics, cultural trends"),
    "category_codes": (
        "category codes: the visual, verbal and tonal conventions of the "
        "category's communication, what it looks and sounds like"),
    "rhythm_moments": (
        "rhythm and moments: seasonality, buying cycles, calendar moments, "
        "registration or sales peaks, events that move the category"),
    "media_spend": (
        "media and spend: advertising spend in the category, media mix, "
        "share of voice, channel trends"),
    "regulation_clearance": (
        "regulation and clearance: laws, advertising codes and standards, "
        "mandatory claims or disclosures, grants and incentives, what copy "
        "needs clearance"),
    "effectiveness_evidence": (
        "effectiveness evidence: published case studies, effectiveness awards "
        "and papers, measured results of campaigns in the category"),
}

REQUEST_FIELDS = {"query", "lens", "market", "entity", "category", "max_sources"}
MARKET_RE = re.compile(r"^[A-Z]{2}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# What the model must return. The service adds ids and retrieved_at itself.
SOURCES_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["sources", "queries"],
    "properties": {
        "queries": {
            "type": "array",
            "description": "every web search query you ran, verbatim",
            "items": {"type": "string"},
        },
        "sources": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["url", "publisher", "title", "excerpts"],
                "properties": {
                    "url": {"type": "string", "description": "the page you fetched"},
                    "publisher": {"type": "string",
                                  "description": "the organisation that published it"},
                    "title": {"type": "string", "description": "the page or document title"},
                    "published_at": {"type": "string",
                                     "description": "YYYY-MM-DD, only if the page states it"},
                    "excerpts": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["quote"],
                            "properties": {"quote": {
                                "type": "string",
                                "description": "text copied verbatim from the fetched page"}},
                        },
                    },
                },
            },
        },
    },
}

_sem = threading.BoundedSemaphore(CONCURRENCY)
_key_locks: dict[str, threading.Lock] = {}
_key_locks_guard = threading.Lock()


class HTTPError(Exception):
    def __init__(self, status: int, kind: str, message: str):
        super().__init__(message)
        self.status, self.kind, self.message = status, kind, message


def log(msg: str) -> None:
    print(f"[mock-research {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def today() -> str:
    return _dt.datetime.now(_dt.timezone.utc).date().isoformat()


# ---------------------------------------------------------------- input


def _clean_text(s: str) -> str:
    return " ".join(unicodedata.normalize("NFC", s).split())


def parse_request(body: bytes) -> dict:
    """Validate and normalise a request body; 400 on anything off-contract."""
    try:
        req = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise HTTPError(400, "invalid_request", f"body is not JSON: {e}")
    if not isinstance(req, dict):
        raise HTTPError(400, "invalid_request", "body must be a JSON object")
    unknown = sorted(set(req) - REQUEST_FIELDS)
    if unknown:
        raise HTTPError(400, "invalid_request", f"unknown field(s): {', '.join(unknown)}")

    query = req.get("query")
    if not isinstance(query, str) or not _clean_text(query):
        raise HTTPError(400, "invalid_request", "query must be a non-empty string")
    query = _clean_text(query)
    if len(query) > 2000:
        raise HTTPError(400, "invalid_request", "query is longer than 2000 characters")

    lens = req.get("lens")
    if lens not in LENSES:
        raise HTTPError(400, "invalid_request",
                        f"lens must be one of: {', '.join(LENSES)}")

    market = req.get("market")
    if not isinstance(market, str) or not MARKET_RE.match(market.strip().upper()):
        raise HTTPError(400, "invalid_request",
                        "market must be an ISO 3166-1 alpha-2 code, e.g. IE")
    market = market.strip().upper()

    out = {"query": query, "lens": lens, "market": market}
    for name in ("entity", "category"):
        v = req.get(name)
        if v is None:
            continue
        if not isinstance(v, str) or not v.strip() or len(v) > 200:
            raise HTTPError(400, "invalid_request",
                            f"{name} must be a non-empty string of at most 200 characters")
        out[name] = v.strip()

    ms = req.get("max_sources", DEFAULT_MAX_SOURCES)
    if isinstance(ms, bool) or not isinstance(ms, int) or not 1 <= ms <= MAX_MAX_SOURCES:
        raise HTTPError(400, "invalid_request",
                        f"max_sources must be an integer from 1 to {MAX_MAX_SOURCES}")
    out["max_sources"] = ms
    return out


def cache_key(req: dict) -> str:
    canon = {**req, "query": req["query"].lower(), "_model": MODEL, "_v": PROMPT_VERSION}
    blob = json.dumps(canon, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


# ---------------------------------------------------------------- prompt


def build_prompt(req: dict) -> str:
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
        "1. Run several WebSearch queries aimed at this lens in this market. Prefer, in order:",
        "   primary sources (regulators, official statistics offices, government bodies,",
        "   company filings, annual reports and official press releases), then industry",
        "   bodies and trade press, then reputable news. Avoid forums, SEO content farms,",
        "   aggregators that only restate others, and pages behind a paywall you cannot read.",
        "2. WebFetch every page before you cite it. Cite only pages you actually fetched and",
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


# ---------------------------------------------------------------- validation


def normalise_url(url: str) -> str | None:
    if not isinstance(url, str):
        return None
    url = url.strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    if any(c.isspace() for c in url):
        return None
    netloc = parts.netloc.lower()
    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), netloc, path, parts.query, ""))


def _valid_date(s) -> str | None:
    if not isinstance(s, str) or not DATE_RE.match(s.strip()):
        return None
    try:
        d = _dt.date.fromisoformat(s.strip())
    except ValueError:
        return None
    if d > _dt.date.fromisoformat(today()):
        return None
    return d.isoformat()


def validate_sources(raw, max_sources: int, retrieved_at: str) -> tuple[list[dict], dict]:
    """Keep only well-formed sources; returns (sources, drop counts)."""
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
        if not isinstance(title, str) or not _clean_text(title):
            drops["malformed"] += 1
            continue
        publisher = item.get("publisher")
        if not isinstance(publisher, str) or not _clean_text(publisher):
            publisher = urlsplit(url).hostname or ""
        quotes: list[str] = []
        excerpts = item.get("excerpts")
        for ex in excerpts if isinstance(excerpts, list) else []:
            q = ex.get("quote") if isinstance(ex, dict) else None
            if not isinstance(q, str):
                continue
            q = q.strip()
            if q and len(q) <= MAX_QUOTE_CHARS and q not in quotes:
                quotes.append(q)
        if url in by_url:
            drops["duplicate"] += 1
            prev = by_url[url]
            for q in quotes:
                if q not in [e["quote"] for e in prev["excerpts"]]:
                    prev["excerpts"].append({"quote": q})
            prev["excerpts"] = prev["excerpts"][:MAX_EXCERPTS]
            continue
        if not quotes:
            drops["no_quotes"] += 1
            continue
        src = {
            "id": "src_" + hashlib.sha256(url.encode()).hexdigest()[:16],
            "url": url,
            "publisher": _clean_text(publisher),
            "title": _clean_text(title),
            "retrieved_at": retrieved_at,
        }
        pub = _valid_date(item.get("published_at"))
        if pub:
            src["published_at"] = pub
        src["excerpts"] = [{"quote": q} for q in quotes[:MAX_EXCERPTS]]
        by_url[url] = src
    sources = list(by_url.values())
    if len(sources) > max_sources:
        drops["over_cap"] = len(sources) - max_sources
        sources = sources[:max_sources]
    return sources, drops


# ---------------------------------------------------------------- the CLI


def run_claude(req: dict) -> dict:
    """One fresh headless Claude Code session; returns its structured output."""
    work = DATA / "work"
    work.mkdir(parents=True, exist_ok=True)
    cmd = [
        CLAUDE_BIN, "-p",
        "--output-format", "json",
        "--model", MODEL,
        "--no-session-persistence",
        "--strict-mcp-config",
        "--permission-mode", "dontAsk",
        "--json-schema", json.dumps(SOURCES_SCHEMA, separators=(",", ":")),
    ]
    if MAX_BUDGET:
        cmd += ["--max-budget-usd", MAX_BUDGET]
    # Variadic flags last; the prompt goes on stdin so they cannot swallow it.
    cmd += ["--tools", "WebSearch", "WebFetch", "--allowedTools", "WebSearch", "WebFetch"]

    env = dict(os.environ)
    # The CLI must reach Anthropic itself, not a Messages stand-in the
    # middleware's environment may point ANTHROPIC_BASE_URL at.
    env.pop("ANTHROPIC_BASE_URL", None)

    try:
        proc = subprocess.Popen(cmd, cwd=work, env=env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True)
    except OSError as e:
        raise HTTPError(502, "upstream_error", f"could not start the research CLI: {e}")
    try:
        out, err = proc.communicate(build_prompt(req).encode(), timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()
        raise HTTPError(504, "timeout", f"research did not finish within {TIMEOUT:g}s")

    tail = err.decode("utf-8", "replace").strip()[-400:]
    try:
        env_json = json.loads(out.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        raise HTTPError(502, "upstream_error",
                        f"research CLI exited {proc.returncode} without a JSON result"
                        + (f": {tail}" if tail else ""))
    if proc.returncode != 0 or not isinstance(env_json, dict) or env_json.get("is_error"):
        detail = env_json.get("result") if isinstance(env_json, dict) else None
        raise HTTPError(502, "upstream_error",
                        f"research CLI failed (exit {proc.returncode}, "
                        f"{(env_json or {}).get('subtype') if isinstance(env_json, dict) else '?'})"
                        + (f": {str(detail)[:300]}" if detail else ""))
    structured = env_json.get("structured_output")
    if not isinstance(structured, dict):
        raise HTTPError(502, "upstream_error", "research CLI returned no structured output")
    return {"output": structured, "cost_usd": env_json.get("total_cost_usd"),
            "duration_ms": env_json.get("duration_ms")}


def _lock_for(key: str) -> threading.Lock:
    with _key_locks_guard:
        return _key_locks.setdefault(key, threading.Lock())


def research(req: dict, fresh: bool) -> dict:
    key = cache_key(req)
    path = DATA / "cache" / f"{key}.json"
    # One run per key at a time: a concurrent duplicate waits and reads the cache.
    with _lock_for(key):
        if not fresh and not NO_CACHE and path.is_file():
            try:
                cached = json.loads(path.read_text())
                log(f"cache hit {key[:12]} {req['lens']}/{req['market']}")
                return cached["response"]
            except (OSError, json.JSONDecodeError, KeyError):
                log(f"unreadable cache entry {path}, re-running")
        t0 = time.monotonic()
        with _sem:
            result = run_claude(req)
        retrieved_at = today()
        out = result["output"]
        sources, drops = validate_sources(out.get("sources"), req["max_sources"], retrieved_at)
        queries = [_clean_text(q) for q in out.get("queries") or []
                   if isinstance(q, str) and _clean_text(q)]
        response = {"sources": sources,
                    "trace": {"backend": BACKEND, "queries": queries, "model": MODEL}}
        log(f"{req['lens']}/{req['market']}: {len(sources)} sources in "
            f"{time.monotonic() - t0:.1f}s, dropped {drops}, cost {result['cost_usd']}")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        # Query parameters and public excerpts only — see the module docstring.
        tmp.write_text(json.dumps({"request": req, "created": _dt.datetime.now(
            _dt.timezone.utc).isoformat(timespec="seconds"), "dropped": drops,
            "response": response}, indent=2, ensure_ascii=False))
        tmp.replace(path)
        return response


# ---------------------------------------------------------------- HTTP


class Handler(BaseHTTPRequestHandler):
    server_version = "mock-research/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter default logging
        log(f"{self.address_string()} {fmt % args}")

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, e: HTTPError) -> None:
        self._send(e.status, {"error": {"type": e.kind, "message": e.message}})

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/healthz":
            self._send(200, {"ok": True, "backend": BACKEND, "model": MODEL,
                             "concurrency": CONCURRENCY, "timeout_s": TIMEOUT})
        elif path == "/v1/research":
            self._error(HTTPError(405, "method_not_allowed", "use POST"))
        else:
            self._error(HTTPError(404, "not_found", f"no route {path}"))

    def do_POST(self):
        parts = urlsplit(self.path)
        if parts.path != "/v1/research":
            # Drain the body so the connection stays usable.
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            return self._error(HTTPError(404, "not_found", f"no route {parts.path}"))
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                raise HTTPError(400, "invalid_request", "empty body")
            if length > MAX_BODY:
                raise HTTPError(413, "invalid_request", "body too large")
            req = parse_request(self.rfile.read(length))
            fresh = parse_qs(parts.query).get("fresh", ["0"])[0] not in ("", "0", "false")
            self._send(200, research(req, fresh))
        except HTTPError as e:
            if e.status >= 500:
                log(f"{e.status} {e.message}")
            self._error(e)
        except Exception as e:  # never a 200 that hides a failure
            log(f"internal error: {e!r}")
            self._error(HTTPError(500, "internal_error", str(e)))

    def do_PUT(self):
        self._error(HTTPError(405, "method_not_allowed", "use POST"))

    do_DELETE = do_PATCH = do_PUT


def main() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    log(f"listening on http://{HOST}:{PORT}/v1/research  model={MODEL} "
        f"concurrency={CONCURRENCY} timeout={TIMEOUT:g}s data={DATA}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
