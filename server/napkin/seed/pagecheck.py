"""Check a research source against the page itself.

A research service (anything behind napkin.research/1)
says it copied passages verbatim and read a publication date off the page.
Nothing downstream should take that on trust: the layers store an excerpt as
what the page said. So for each cited URL we fetch the page ourselves and

  - keep a passage only when it is really in the page text (whitespace and
    quote marks folded, as rules.quotes.find_quote does), stored as the page's
    own rendering of it, never the model's;
  - take the publication date from the page's metadata (article:published_time,
    JSON-LD datePublished, <time datetime>) when it has one, and say where the
    date came from; a date only the model reported is kept as 'search_api'
    basis, and none at all is 'unknown';
  - fingerprint what we read (sha256 of the extracted text).

HTML and PDF are read; anything else, a failed fetch, or a page with no
confirmed passage returns no capture, and the caller counts it as dropped.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import io
import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

import httpx

from ..rules.quotes import find_quote

UA = "Mozilla/5.0 (X11; Linux x86_64) NapkinResearchCheck/1.0"
MAX_BYTES = 8 * 1024 * 1024
MAX_EXCERPT = 1500
SKIP_TAGS = {"script", "style", "noscript", "template", "svg", "head"}
BLOCK_TAGS = {"p", "div", "br", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6", "section",
              "article", "header", "footer", "table", "ul", "ol", "blockquote", "figcaption", "dt", "dd"}
DATE_META = ("article:published_time", "og:published_time", "datepublished", "date", "dc.date",
             "dc.date.issued", "citation_publication_date", "pubdate", "publish-date", "sailthru.date")


@dataclass
class Capture:
    url: str
    title: str | None
    retrieved_at: str                  # ISO timestamp, UTC
    published_at: str | None           # YYYY-MM-DD
    published_basis: str               # page_metadata | search_api | unknown
    content_sha256: str
    excerpts: list[str] = field(default_factory=list)
    dropped: int = 0                   # passages the page did not contain


# Why a source or passage was dropped, counted per unit so the drop rate can be
# read by cause: a fetch refused, a page we cannot parse, a paraphrase.
REASONS = ("fetch_failed", "http_error", "too_large", "unsupported_type", "parse_failed", "not_in_page")


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.meta, self.jsonld, self.times = [], {}, [], []
        self.title, self._skip, self._in_title, self._in_ld = None, 0, False, False
        self._ld_buf = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            if key and a.get("content"):
                self.meta.setdefault(key, a["content"])
        elif tag == "time" and a.get("datetime"):
            self.times.append(a["datetime"])
        elif tag == "script" and "ld+json" in a.get("type", ""):
            self._in_ld = True
        if tag == "title":
            self._in_title = True
        if tag in SKIP_TAGS:
            self._skip += 1
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS and self._skip:
            self._skip -= 1
        if tag == "script" and self._in_ld:
            self.jsonld.append("".join(self._ld_buf))
            self._ld_buf, self._in_ld = [], False
        if tag == "title":
            self._in_title = False
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_ld:
            self._ld_buf.append(data)
        elif self._in_title and self.title is None:
            self.title = data.strip() or None
        elif not self._skip:
            self.parts.append(data)


def _day(value: str | None) -> str | None:
    """YYYY-MM-DD from an ISO-ish date string, or None."""
    m = re.match(r"\s*(\d{4})-(\d{2})-(\d{2})", value or "")
    if not m:
        return None
    try:
        return _dt.date(int(m[1]), int(m[2]), int(m[3])).isoformat()
    except ValueError:
        return None


def _jsonld_date(blobs: list[str]) -> str | None:
    def walk(node):
        if isinstance(node, dict):
            for k in ("datePublished", "dateCreated", "uploadDate"):
                d = _day(node.get(k)) if isinstance(node.get(k), str) else None
                if d:
                    return d
            for v in node.values():
                d = walk(v)
                if d:
                    return d
        elif isinstance(node, list):
            for v in node:
                d = walk(v)
                if d:
                    return d
        return None
    for b in blobs:
        try:
            d = walk(json.loads(b))
        except (ValueError, TypeError):
            continue
        if d:
            return d
    return None


def page_text(content: bytes, content_type: str) -> tuple[str, str | None, str | None]:
    """(visible text, title, publication date from metadata) of a page."""
    if "pdf" in content_type or content[:5] == b"%PDF-":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(content))
        text = "\n".join((p.extract_text() or "") for p in reader.pages[:200])
        info = reader.metadata or {}
        created = str(info.get("/CreationDate") or "")
        m = re.match(r"D:(\d{4})(\d{2})(\d{2})", created)
        pub = _day(f"{m[1]}-{m[2]}-{m[3]}") if m else None
        return text, (str(info.get("/Title")) if info.get("/Title") else None), pub
    html = content.decode("utf-8", errors="replace")
    p = _Text()
    p.feed(html)
    text = re.sub(r"[ \t\r\f\v]+", " ", "".join(p.parts))
    text = re.sub(r"\n\s*\n+", "\n", text)
    pub = next((_day(p.meta[k]) for k in DATE_META if _day(p.meta.get(k))), None) \
        or _jsonld_date(p.jsonld) or next((_day(t) for t in p.times if _day(t)), None)
    return text, p.title, pub


def check(url: str, quotes: list[str], model_published: str | None, client: httpx.Client):
    """(Capture or None, {reason: passages dropped}). Keeps the quotes the page really holds."""
    n = len(quotes)
    try:
        r = client.get(url, headers={"User-Agent": UA, "Accept": "text/html,application/pdf,*/*"},
                       follow_redirects=True)
    except httpx.HTTPError:
        return None, {"fetch_failed": n}
    if r.status_code != 200:
        return None, {"http_error": n}
    if len(r.content) > MAX_BYTES:
        return None, {"too_large": n}
    ctype = r.headers.get("content-type", "").lower()
    if not ("html" in ctype or "pdf" in ctype or r.content[:5] == b"%PDF-"):
        return None, {"unsupported_type": n}
    try:
        text, title, meta_pub = page_text(r.content, ctype)
    except Exception:  # a malformed PDF or page is a dropped source, not a crash
        return None, {"parse_failed": n}
    now = _dt.datetime.now(_dt.timezone.utc)
    kept, dropped = [], 0
    for q in quotes:
        span = find_quote(text, q)
        if not span:
            dropped += 1
            continue
        found = re.sub(r"\s+", " ", text[span[0]:span[1]]).strip()[:MAX_EXCERPT]
        if found and found not in kept:
            kept.append(found)
    if not kept:
        return None, {"not_in_page": dropped}
    if meta_pub and meta_pub <= now.date().isoformat():
        pub, basis = meta_pub, "page_metadata"
    elif _day(model_published) and _day(model_published) <= now.date().isoformat():
        pub, basis = _day(model_published), "search_api"
    else:
        pub, basis = None, "unknown"
    return (Capture(url=str(r.url), title=title, retrieved_at=now.isoformat(timespec="seconds"),
                    published_at=pub, published_basis=basis,
                    content_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    excerpts=kept, dropped=dropped),
            {"not_in_page": dropped} if dropped else {})
