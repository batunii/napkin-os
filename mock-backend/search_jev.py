"""MOCK_RESEARCH_BACKEND=search-jev: the agent only searches; code reads the pages and jev picks the passages.

One research unit (one lens in one market):
  1. a `claude -p` agent with WebSearch only (no WebFetch) returns up to MAX_CANDIDATES page URLs it found;
  2. code fetches every page with plain HTTP and extracts its text (trafilatura plus the page's visible lines,
     pypdf for PDFs), no model;
  3. the text is cut into overlapping windows of ~500 characters, and jev (TypeSafe) scores each with one yes/no
     question against the unit's research question: does this passage state a figure, date, rule or finding
     for this market?
  4. the best windows across all the unit's pages, up to UNIT_CHARS characters, become the excerpts; every one is
     verbatim page text by construction.
A unit where no page could be read falls back to the ordinary agent, so a blocked site never empties a unit.
The jev key comes from TYPESAFE_API_KEY, engine/.env or the repo-root .env and is never logged.
Measured offline on the IBM runs (runs/metrics/jev-passages): plain fetch read 76 of 77 pages, jev kept 93% of the
fetchable pinned quotes at 6,000 characters a page, and extraction kept 137 facts against 82.
"""

from __future__ import annotations

import concurrent.futures as cf
import html as _html
import io
import os
import re
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from common import ClaudeCall, ClaudeFailure, PeripheralError, dotenv_value, log, record_external, run_claude

VERSION = "3"                     # bump when selection changes, so cached answers miss
MAX_CANDIDATES = int(os.environ.get("MOCK_JEV_CANDIDATES", "12"))
# Each CLI web search also runs a Haiku helper (~$0.023 a search, measured 2026-10-01), so searches are capped.
MAX_SEARCHES = int(os.environ.get("MOCK_JEV_SEARCHES", "2"))
UNIT_CHARS = int(os.environ.get("MOCK_JEV_UNIT_CHARS", "4000"))
WIN, STEP = 500, 250              # a quote under 250 characters always sits whole inside one window
MAX_PAGE_CHARS = 150_000
MAX_QUOTE_CHARS, MAX_EXCERPTS = 1500, 5
JEV_USD_PER_TOKEN = 0.042 / 1e6   # docs.typesafe.ai pricing: input tokens; output is free
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
BOT = re.compile(r"think that you are a bot|are you a robot|captcha|access denied|enable javascript", re.I)
MARKETS = {"IE": "Ireland", "GB": "United Kingdom", "DE": "Germany"}
QUESTION = ("Does PASSAGE state a figure, date, rule or concrete finding that answers the research question for "
            "this market?")
CRITERIA = {"true": ("PASSAGE states a specific number, share, amount, date, named rule or regulation, or a concrete "
                     "finding that answers the research question for the market, or for the brand or category in "
                     "that market."),
            "false": ("PASSAGE is navigation, a menu, a cookie notice, marketing copy with no fact, about another "
                      "market or topic, or too vague to cite as evidence.")}

# The qualitative lenses ask about how brands present themselves, not about numbers: with the figure question above,
# category_codes came back empty in every market of an IBM run (jev ranked survey percentages over creative
# descriptions, and the search had found pages about AI-generated ads and ad rules instead).
LENS_QUESTION = {
    "category_codes": (
        "Does PASSAGE describe how brands in this category present themselves in their communication?",
        {"true": ("PASSAGE names or describes recurring visual, verbal or tonal conventions of the category's "
                  "advertising or branding: typical imagery, colours, language, taglines, claims, tone, campaign "
                  "themes, or a code that is worn out or emerging, concretely enough to cite."),
         "false": ("PASSAGE is about something else: advertising rules or regulation, survey figures about "
                   "advertising in general, product features, navigation, or marketing copy that only sells.")}),
    "brands_positioning": (
        "Does PASSAGE state how the brand or a named competitor positions itself or what it claims?",
        {"true": ("PASSAGE states a brand's positioning, promise, claim, tagline, price tier, target audience, "
                  "launch or campaign, or how it differs from a named competitor, concretely enough to cite."),
         "false": "PASSAGE is navigation, generic copy with no positioning, or about an unrelated brand or topic."}),
}
SEARCH_HINT = {
    "category_codes": ("Look for analyses and examples of how brands in this category advertise and brand themselves: "
                       "campaign reviews, creative or brand-language trend pieces, agency commentary. Not advertising "
                       "regulation, and not articles about AI-generated advertising."),
    "brands_positioning": ("Look for the brand's and its named competitors' own positioning: campaign launches, "
                           "brand platforms, taglines, press releases and reviews of their campaigns."),
}

CANDIDATES_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["candidates", "queries"],
    "properties": {
        "queries": {"type": "array", "items": {"type": "string"}},
        "candidates": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["url", "title", "publisher"],
            "properties": {"url": {"type": "string"}, "title": {"type": "string"}, "publisher": {"type": "string"}}}},
    },
}


def candidates_prompt(req: dict, lens_text: str, today: str) -> str:
    lines = [
        "You are the search step of a research pipeline. Find web pages that are likely to state citable facts",
        "for ONE research lens in ONE market. You do not read the pages: another system fetches them.",
        "",
        f"Today's date: {today}",
        f"Research question: {req['query']}",
        f"Lens: {req['lens']} — {lens_text}",
        f"Market: {req['market']} (ISO 3166-1 alpha-2). Pages must bear on this market.",
    ]
    if "category" in req:
        lines.append(f"Category (leaf code): {req['category']}")
    if "entity" in req:
        lines.append(f"Entity in focus: {req['entity']}")
    lines += [
        "",
        "How to work:",
        f"1. Run at most {MAX_SEARCHES} WebSearch queries aimed at this lens in this market"
        + (", the last one in the market's language when it is not English." if MAX_SEARCHES > 1 else "."),
        "   Prefer primary sources (regulators, official statistics, government, company filings and press",
        "   releases), then industry bodies and trade press, then reputable news."
        + (f" {SEARCH_HINT[req['lens']]}" if req["lens"] in SEARCH_HINT else ""),
        f"2. Return up to {MAX_CANDIDATES} distinct page URLs taken from the search results, best first: pages whose",
        "   title or snippet suggests figures, dates, named rules or concrete findings for this lens and market.",
        "   Never invent a URL, and skip paywalled pages, forums and aggregators that only restate others.",
        "3. publisher is the organisation behind the page; title as shown in the results.",
        "4. List in `queries` every search query you ran, verbatim.",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------ reading pages


def visible_lines(html: str) -> list[str]:
    """The page's visible text, one block per line (scripts, styles, nav, header and footer removed)."""
    h = re.sub(r"(?is)<(script|style|noscript|svg|nav|header|footer)[^>]*>.*?</\1>", " ", html)
    h = re.sub(r"(?i)<(br|/p|/li|/h[1-6]|/div|/tr|/td|/th|/section|/article|/blockquote)[^>]*>", "\n", h)
    t = _html.unescape(re.sub(r"<[^>]+>", " ", h))
    return [re.sub(r"\s+", " ", ln).strip() for ln in t.split("\n") if len(ln.split()) >= 4]


def page_text(body: bytes, ctype: str, url: str) -> tuple[str, str | None]:
    """(text, published date or None). trafilatura's main text plus the visible lines it left out (it drops some
    lists, cards and infographic text); pypdf for a PDF."""
    if "pdf" in ctype or urlsplit(url).path.lower().endswith(".pdf") or body[:5] == b"%PDF-":
        from pypdf import PdfReader
        return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(body)).pages[:80]), None
    import trafilatura
    html = body.decode("utf-8", "replace")
    main = trafilatura.extract(html, favor_recall=True, include_tables=True, include_comments=False) or ""
    seen, extra = re.sub(r"\s+", " ", main.lower()), []
    for ln in visible_lines(html):
        if ln.lower() not in seen:
            extra.append(ln)
            seen += " " + ln.lower()
    meta = trafilatura.extract_metadata(html)
    date = getattr(meta, "date", None) if meta else None
    return main + "\n" + "\n".join(extra), date if isinstance(date, str) and re.match(r"\d{4}-\d{2}-\d{2}$", date) else None


def fetch(url: str) -> dict:
    """One page: {"text", "date"} when it can be read, else {"failed": reason}: "http_error" (a 4xx/5xx answer),
    "network" (no answer, a timeout, TLS), "bot_wall", "thin_text" (under 500 characters) or "error" (it could not
    be parsed). A page that cannot be read is skipped, never fatal; the reasons are counted in the reader line."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en,de;q=0.8"})
        with urllib.request.urlopen(req, timeout=25) as r:
            ctype, body = r.headers.get("Content-Type", ""), r.read(20_000_000)
        text, date = page_text(body, ctype, url)
    except urllib.error.HTTPError as e:
        log(f"search-jev: could not read {urlsplit(url).hostname}: HTTP {e.code}")
        return {"failed": "http_error"}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log(f"search-jev: could not read {urlsplit(url).hostname}: {type(e).__name__}")
        return {"failed": "network"}
    except Exception as e:
        log(f"search-jev: could not read {urlsplit(url).hostname}: {type(e).__name__}")
        return {"failed": "error"}
    text = re.sub(r"[ \t]+", " ", text)[:MAX_PAGE_CHARS]
    if BOT.search(text[:2000]):
        return {"failed": "bot_wall"}
    if len(text) < 500:
        return {"failed": "thin_text"}
    return {"text": text, "date": date}


def windows(text: str) -> list[tuple[int, int, str]]:
    out, i, n = [], 0, len(text)
    while i < n:
        j = min(n, i + WIN)
        if j < n:
            k = text.rfind(" ", i + WIN - 80, j)
            j = k if k > i else j
        out.append((i, j, text[i:j].strip()))
        if j >= n:
            break
        nxt = i + STEP
        k = text.find(" ", nxt)
        i = k + 1 if 0 < k < nxt + 60 else nxt
    return [w for w in out if len(w[2]) >= 40]


# ------------------------------------------------------------------ jev


_client = None


def jev_client():
    global _client
    if _client is None:
        key = dotenv_value("TYPESAFE_API_KEY")
        if not key:
            raise PeripheralError(503, "not_configured", "MOCK_RESEARCH_BACKEND=search-jev needs TYPESAFE_API_KEY "
                                                         "(environment, engine/.env or the repo-root .env)")
        import typesafe_sdk
        _client = typesafe_sdk.TypeSafeClient(api_key=key, model="jev-latest")
    return _client


def jev_state(req: dict, lens_text: str) -> dict:
    return {"research_question": req["query"], "lens": lens_text, "market": MARKETS.get(req["market"], req["market"]),
            "category": req.get("category", ""), "entity": req.get("entity", "")}


def score_passages(state: dict, passages: list[str], lens: str = "") -> tuple[list[float], int]:
    """(one probability per passage, input tokens billed). 50 questions per request, sent concurrently. The
    qualitative lenses get their own question (LENS_QUESTION)."""
    client, scores, tokens = jev_client(), [0.0] * len(passages), 0
    question, criteria = LENS_QUESTION.get(lens, (QUESTION, CRITERIA))

    def batch(b):
        qs = {f"p{n:04d}": {"type": "noul", "instructions": {"question": question, "passage": passages[n]},
                            "criteria": criteria} for n in range(b, min(b + 50, len(passages)))}
        for attempt in range(3):
            try:
                return qs, client.system_one(state=state, questions=qs, model="jev-latest")
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(2 * (attempt + 1))

    with cf.ThreadPoolExecutor(4) as ex:
        for qs, resp in ex.map(batch, range(0, len(passages), 50)):
            tokens += resp.usage.input_tokens or 0
            for name in qs:
                scores[int(name[1:])] = resp.nouls[name].noul
    return scores, tokens


# ------------------------------------------------------------------ selection


def merge(spans):
    out = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def chunks(text: str, a: int, b: int) -> list[str]:
    """A selected span as quotes of at most MAX_QUOTE_CHARS, cut at whitespace."""
    out, s = [], text[a:b].strip()
    while s:
        if len(s) <= MAX_QUOTE_CHARS:
            out.append(s)
            break
        k = s.rfind(" ", 0, MAX_QUOTE_CHARS)
        k = k if k > MAX_QUOTE_CHARS // 2 else MAX_QUOTE_CHARS
        out.append(s[:k].strip())
        s = s[k:].strip()
    return out


def select(pages: list[dict], unit_chars: int) -> list[tuple[dict, list[tuple[int, int]], float]]:
    """The best windows across all pages until unit_chars characters: [(page, merged spans, best score)], best
    page first."""
    cand = [(sc, n, a, b) for n, p in enumerate(pages) for (a, b, _), sc in zip(p["windows"], p["scores"])]
    spans: dict[int, list] = {}
    for sc, n, a, b in sorted(cand, key=lambda c: -c[0]):
        spans.setdefault(n, []).append((a, b))
        spans = {k: merge(v) for k, v in spans.items()}
        if sum(e - s for v in spans.values() for s, e in v) >= unit_chars:
            break
    out = [(pages[n], v, max(pages[n]["scores"])) for n, v in spans.items()]
    return sorted(out, key=lambda x: -x[2])


# ------------------------------------------------------------------ the unit


def run(research, req: dict, lens_text: str, today: str) -> dict:
    """One unit: search, read, score, select. Returns the backend's {"output", "cost_usd"}."""
    cfg = research.cfg
    call = ClaudeCall(alias=cfg.research_model, prompt=candidates_prompt(req, lens_text, today),
                      json_schema=CANDIDATES_SCHEMA, tools=["WebSearch"], permission_mode="dontAsk", trace_tools=True)
    if not research.slots.acquire(cfg.queue_timeout_for("research")):
        raise PeripheralError(503, "overloaded", f"all {research.slots.n} claude slots busy (MOCK_CONCURRENCY)")
    try:
        env = run_claude(cfg, call, cfg.timeout["research"], cfg.data / "work")
    except ClaudeFailure as f:
        raise PeripheralError(504 if f.kind == "timeout" else 502, "timeout" if f.kind == "timeout" else
                              "upstream_failed", f.message) from None
    finally:
        research.slots.release()
    out = env.get("structured_output") if not (env.get("_exit") or env.get("is_error")) else None
    if not isinstance(out, dict):
        raise PeripheralError(502, "upstream_failed", "the search agent returned no structured output")
    agent_cost = env.get("total_cost_usd") or 0.0
    seen, cands = set(), []
    for c in out.get("candidates") or []:
        url = c.get("url") if isinstance(c, dict) else None
        if isinstance(url, str) and url.startswith(("http://", "https://")) and url not in seen:
            seen.add(url)
            cands.append(c)
    cands = cands[:MAX_CANDIDATES]

    t0 = time.monotonic()
    with cf.ThreadPoolExecutor(8) as ex:
        got = list(ex.map(lambda c: fetch(c["url"]), cands))
    pages, failed = [], {}
    for c, g in zip(cands, got):
        ws = windows(g["text"]) if "text" in g else []
        if ws:
            pages.append({**c, **g, "windows": ws})
        else:
            why = g.get("failed") or "thin_text"
            failed[why] = failed.get(why, 0) + 1
    log(f"search-jev {req['lens']}/{req['market']}: {len(cands)} candidates, {len(pages)} readable")
    reader = {"candidates": len(cands), "read": len(pages), "failed": failed, "fallback": not pages,
              "passages": 0, "kept_chars": 0, "sources": 0, "jev_tokens": 0}
    if not pages:  # nothing readable: the ordinary agent does the unit
        record_external(cfg, "research", req, time.monotonic() - t0, True, 0.0, 0, "reader", extra={"reader": reader})
        fb = research._run_agent(req)
        return {"output": fb["output"], "cost_usd": agent_cost + (fb["cost_usd"] or 0.0)}

    flat = [w[2] for p in pages for w in p["windows"]]
    try:
        scores, tokens = score_passages(jev_state(req, lens_text), flat, req["lens"])
    except PeripheralError:
        raise
    except Exception as e:
        record_external(cfg, "research", req, time.monotonic() - t0, False, None, 0, "jev", type(e).__name__,
                        extra={"reader": dict(reader, passages=len(flat))})
        raise PeripheralError(502, "upstream_failed", f"jev scoring failed ({type(e).__name__})") from None
    k = 0
    for p in pages:
        p["scores"] = scores[k:k + len(p["windows"])]
        k += len(p["windows"])
    jev_cost = tokens * JEV_USD_PER_TOKEN

    sources = []
    for p, spans, _best in select(pages, UNIT_CHARS):
        # the page's highest-scoring spans first, so the excerpt cap keeps the best
        ranked = sorted(spans, key=lambda s: -max((sc for (a, b, _), sc in zip(p["windows"], p["scores"])
                                                   if a >= s[0] and b <= s[1]), default=0.0))
        quotes = [q for s in ranked for q in chunks(p["text"], s[0], s[1])][:MAX_EXCERPTS]
        item = {"url": p["url"], "title": p.get("title") or urlsplit(p["url"]).hostname or "",
                "publisher": p.get("publisher") or urlsplit(p["url"]).hostname or "",
                "excerpts": [{"quote": q} for q in quotes]}
        if p.get("date"):
            item["published_at"] = p["date"]
        sources.append(item)
    reader.update(passages=len(flat), kept_chars=sum(len(e["quote"]) for x in sources for e in x["excerpts"]),
                  sources=len(sources), jev_tokens=tokens)
    record_external(cfg, "research", req, time.monotonic() - t0, True, jev_cost, 0, "jev", extra={"reader": reader})
    return {"output": {"sources": sources, "queries": out.get("queries") or []}, "cost_usd": agent_cost + jev_cost}
