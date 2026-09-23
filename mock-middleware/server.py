#!/usr/bin/env python3
"""
Mock middleware — a stand-in for the Napkin middleware (`napkin.middleware/1`).

It answers exactly what the real middleware (the FastAPI service in `server/`)
will answer, so the host can be pointed at the real one later by changing ONE
value: the `middleware` endpoint in workspace.yaml. Nothing in the host, the
templates or the tests may name this server or branch on it.

Contract: docs/contracts/middleware-api.md. The swap guarantee is
mock-middleware/contract_test.py, which must pass unchanged against both.

    python3 mock-middleware/server.py                 # http://127.0.0.1:8790/v1/tasks
    MOCK_MIDDLEWARE_PORT=8791 python3 mock-middleware/server.py

Principles (the same ones as the mock-llm in the build plan):
  * Transport-honest. It speaks the envelope; it does not pretend to have
    retrieval, a model or a knowledge layer. Every source it cites is a
    `mock-source://` URI at tier `mock`, and every pin says so in its reason.
  * Deterministic. No LLM, no network. Ids and values are seeded from the
    document id, the dev scope and the inputs; the same request gives the same
    change. Wall-clock timestamps are the only exception, and
    MOCK_MIDDLEWARE_CLOCK=<ISO datetime> freezes them.
  * Refuses what it cannot do faithfully: unknown task / handler / major are a
    400, an unknown job is a 404, never a fallback default.
  * Never fabricates usage: token counts are zero because no model ran.
  * Never writes a request body to disk unless MOCK_MIDDLEWARE_DUMP_DIR is set.

Environment:
  MOCK_MIDDLEWARE_PORT         listen port (8790)
  MOCK_MIDDLEWARE_HOST         bind address (127.0.0.1)
  MOCK_MIDDLEWARE_ORG          dev tenant org reported in trace.scope (org/dev-agency)
  MOCK_MIDDLEWARE_BRAND        dev tenant brand reported in trace.scope (brand/dev-brand)
  MOCK_MIDDLEWARE_TOKEN        if set, requests must carry it (Bearer or x-api-key)
  MOCK_MIDDLEWARE_JOB_SECONDS  wall time a long job takes to finish (2.0)
  MOCK_MIDDLEWARE_CLOCK        freeze content timestamps at this ISO datetime
  MOCK_MIDDLEWARE_DUMP_DIR     write each request body here (off by default)

Python 3.11+, standard library only.
"""

from __future__ import annotations

import base64
import copy
import datetime as _dt
import hashlib
import itertools
import json
import os
import re
import sys
import threading
import time
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

API = "napkin.middleware/1"
BACKEND = "mock-middleware"

PORT = int(os.environ.get("MOCK_MIDDLEWARE_PORT", "8790"))
HOST = os.environ.get("MOCK_MIDDLEWARE_HOST", "127.0.0.1")
SCOPE = {
    "org": os.environ.get("MOCK_MIDDLEWARE_ORG", "org/dev-agency"),
    "brand": os.environ.get("MOCK_MIDDLEWARE_BRAND", "brand/dev-brand"),
}
TOKEN = os.environ.get("MOCK_MIDDLEWARE_TOKEN") or None
JOB_SECONDS = float(os.environ.get("MOCK_MIDDLEWARE_JOB_SECONDS", "2.0"))
CLOCK = os.environ.get("MOCK_MIDDLEWARE_CLOCK") or None
# Request bodies carry client-confidential material (the client's own brief,
# attachment text, pinned brand facts). They are never written anywhere unless a
# developer names a directory explicitly, for a debugging session they own.
DUMP_DIR = os.environ.get("MOCK_MIDDLEWARE_DUMP_DIR") or None
MAX_BODY = 64 * 1024 * 1024

# ---------------------------------------------------------------------------
# Handler registry: name -> {major: implemented version}. The document's
# pipeline.yaml declares `handler: name@major`; that is resolved here. Anything
# not listed is a hard error (M4) — there is no "closest" handler.
# ---------------------------------------------------------------------------
REGISTRY = {
    "extract_ask": {"task": "extract_ask", "versions": {1: "1.0"}},
    "research_lens": {"task": "research_lens", "versions": {1: "1.0"}},
    "synthesise_findings": {"task": "synthesise_findings", "versions": {1: "1.0"}},
}
# Used only when the document carries no pipeline (the declared built-in map).
BUILTIN_PIPELINE = {
    "extract_ask": "extract_ask@1",
    "research_lens": "research_lens@1",
    "synthesise_findings": "synthesise_findings@1",
}
TASKS = set(BUILTIN_PIPELINE) | {"job_status"}
LONG_TASKS = {"research_lens", "synthesise_findings"}

LENSES = [
    "market_structure", "brands_positioning", "consumer_culture", "category_codes",
    "rhythm_moments", "media_spend", "regulation_clearance", "effectiveness_evidence",
]

ISO_3166 = set("""
AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN
BO BQ BR BS BT BV BW BY BZ CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ
DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD GE GF GG GH GI GL
GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM
JO JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME
MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP
NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD
SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO
TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW
""".split())

CAMPAIGN_FIELDS = [
    "id", "name", "brand", "client_org", "categories", "markets", "campaign_type",
    "problem", "objective", "audience_stated", "audience", "competitor_set",
    "success_measures", "budget_band", "in_market", "channels_mandated",
    "deliverables", "constraints", "ask_source",
]
GATES = {
    "id": "created", "name": "created", "brand": "created", "client_org": "created",
    "categories": "research", "markets": "research", "campaign_type": "none",
    "problem": "brief", "objective": "brief", "audience_stated": "brief",
    "audience": "brief", "competitor_set": "none", "success_measures": "none",
    "budget_band": "brief", "in_market": "none", "channels_mandated": "none",
    "deliverables": "none", "constraints": "none", "ask_source": "created",
}
LIST_FIELDS = {"categories", "markets", "competitor_set", "success_measures",
               "channels_mandated", "deliverables", "constraints"}
# Minted by the host when a human opens the document, or filled by research —
# never by extraction.
NOT_EXTRACTED = {"id", "ask_source", "name", "audience"}


class TaskError(Exception):
    def __init__(self, status: int, etype: str, message: str):
        super().__init__(message)
        self.status, self.etype, self.message = status, etype, message


def bad(message: str, etype: str = "invalid_input") -> TaskError:
    return TaskError(400, etype, message)


# ---------------------------------------------------------------------------
# Deterministic ids, time
# ---------------------------------------------------------------------------

def _digest(*parts) -> bytes:
    h = hashlib.sha256()
    for p in parts:
        h.update(json.dumps(p, sort_keys=True, ensure_ascii=False, default=str).encode())
        h.update(b"\x1f")
    return h.digest()


def hnum(*parts) -> int:
    return int.from_bytes(_digest(*parts)[:8], "big")


def uid(prefix: str, *parts, n: int = 10) -> str:
    """Opaque id matching ^<prefix>[0-9A-Z]{6,}$ (base32, uppercase)."""
    return prefix + base64.b32encode(_digest(*parts)).decode()[:n]


def lid(prefix: str, *parts, n: int = 8) -> str:
    """Lowercase id for src_ ids and item keys."""
    return prefix + _digest(*parts).hex()[:n]


def now() -> _dt.datetime:
    if CLOCK:
        return _dt.datetime.fromisoformat(CLOCK.replace("Z", "+00:00")).astimezone(_dt.timezone.utc)
    return _dt.datetime.now(_dt.timezone.utc)


def iso(t: _dt.datetime) -> str:
    return t.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slug(text: str) -> str:
    t = unicodedata.normalize("NFKD", text)
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")


# ---------------------------------------------------------------------------
# Reading the request's clan context
# ---------------------------------------------------------------------------

def ctx_facts(clan: dict) -> list:
    f = clan.get("facts")
    if isinstance(f, dict):
        f = f.get("facts")
    return [x for x in (f or []) if isinstance(x, dict)]


def ctx_findings(clan: dict) -> list:
    f = clan.get("findings")
    if isinstance(f, dict):
        f = f.get("findings")
    return [x for x in (f or []) if isinstance(x, dict)]


def ctx_decisions(clan: dict) -> list:
    d = clan.get("decision_chain")
    if isinstance(d, dict):
        d = d.get("decisions")
    return [x for x in (d or []) if isinstance(x, dict)]


def ctx_data(clan: dict) -> dict:
    d = clan.get("data")
    return d if isinstance(d, dict) else {}


def field_value(data: dict, name: str):
    env = (data.get("campaign") or {}).get(name)
    return env.get("value") if isinstance(env, dict) else None


def resolve_handler(task: str, clan: dict) -> str:
    """Task -> implemented handler@version. Hard errors only, never a default."""
    pipeline = clan.get("pipeline")
    if pipeline:
        if not isinstance(pipeline, dict) or not isinstance(pipeline.get("tasks"), dict):
            raise bad("clan.pipeline is present but has no tasks map")
        decl = pipeline["tasks"].get(task)
        if decl is None:
            raise bad(f"task '{task}' is not declared by this document's pipeline", "unknown_task")
        declared = decl.get("handler") if isinstance(decl, dict) else decl
    else:
        declared = BUILTIN_PIPELINE[task]
    m = re.fullmatch(r"([a-z_]+)@(\d+)(?:\.\d+)*", str(declared or ""))
    if not m:
        raise bad(f"handler '{declared}' for task '{task}' is not name@major", "unknown_handler")
    name, major = m.group(1), int(m.group(2))
    reg = REGISTRY.get(name)
    if reg is None:
        raise bad(f"no handler named '{name}' is registered", "unknown_handler")
    if reg["task"] != task:
        raise bad(f"handler '{name}' does not implement task '{task}'", "unknown_handler")
    if major not in reg["versions"]:
        raise bad(f"handler '{name}' has no major version {major} "
                  f"(registered: {sorted(reg['versions'])})", "unknown_handler")
    return f"{name}@{reg['versions'][major]}"


def trace(hits=None) -> dict:
    return {
        "scope": dict(SCOPE),
        "backend": BACKEND,
        "model": None,  # no model ran
        "hits": hits or [],
        "usage": {"input_tokens": 0, "output_tokens": 0},  # never estimated
    }


def decision(doc, did, kind, handler, action, rationale, targets, cites=(), **extra) -> dict:
    d = {
        "id": did, "kind": kind, "agent": handler, "action": action,
        "rationale": rationale, "targets": [f"{doc}#{t}" for t in targets],
        "cites": list(dict.fromkeys(cites)), "handler": handler, "backend": BACKEND,
        "timestamp": iso(now()),
    }
    d.update(extra)
    return d


# ---------------------------------------------------------------------------
# extract_ask — honest heuristic extraction from the prompt + attachment text
# ---------------------------------------------------------------------------

COUNTRIES = [
    (r"\bNorthern Ireland\b", "GB"), (r"\b(?:the\s+)?(?:UK|U\.K\.)\b", "GB"),
    (r"\bUnited Kingdom\b", "GB"), (r"\b(?:Great\s+)?Britain\b", "GB"), (r"\bGB\b", "GB"),
    (r"\bEngland\b", "GB"), (r"\bScotland\b", "GB"), (r"\bWales\b", "GB"),
    (r"(?<!Northern )\bIreland\b", "IE"), (r"\bROI\b", "IE"),
    (r"\bFrance\b", "FR"), (r"\bGermany\b", "DE"), (r"\bSpain\b", "ES"), (r"\bItaly\b", "IT"),
    (r"\b(?:the\s+)?Netherlands\b", "NL"), (r"\bBelgium\b", "BE"), (r"\bPortugal\b", "PT"),
    (r"\bPoland\b", "PL"), (r"\bSweden\b", "SE"), (r"\bDenmark\b", "DK"), (r"\bNorway\b", "NO"),
    (r"\bFinland\b", "FI"), (r"\bAustria\b", "AT"), (r"\bSwitzerland\b", "CH"),
    (r"\b(?:the\s+)?(?:US|USA|U\.S\.|United States)\b", "US"), (r"\bCanada\b", "CA"),
    (r"\bAustralia\b", "AU"), (r"\bNew Zealand\b", "NZ"),
]
MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], 1)}
MONTHS.update({k[:3]: v for k, v in list(MONTHS.items())})
MONTHS["sept"] = 9
MONTH_RE = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
RANGE_SEP = r"\s*(?:to|until|till|through|thru|–|—|-)\s*"
SEASONS = {"spring": (3, 5), "summer": (6, 8), "autumn": (9, 11), "fall": (9, 11)}
DELIVERABLES = [
    (r"(\d{2})\s*(?:[\"”″]|-?\s*(?:second|sec)s?\b)\s*(?:TVC|TV ad|TV spot|television ad)", "tvc_{0}"),
    (r"\bTVC\b", "tvc"),
    (r"\bBVOD\s+(?:cut|edit|version)s?\b", "bvod_cut"),
    (r"\bsocial\s+cut-?\s?downs?\b", "social_cutdowns"),
    (r"\b(\d{1,2})[- ]sheets?\b", "ooh_{0}sheet"),
    (r"\b(?:radio|audio)\s+(?:ads?|spots?)\b", "audio_spot"),
    (r"\bkey\s+visuals?\b", "key_visual"),
    (r"\b(?:display|digital)\s+banners?\b", "display_banners"),
    (r"\b(?:online video|OLV)\b", "olv"),
    (r"\binfluencer\b", "influencer_content"),
    (r"\b(?:point[- ]of[- ]sale|POS)\b", "in_store_pos"),
]
CHANNELS = [
    (r"\bTV\b|\btelevision\b", "tv"), (r"\bBVOD\b", "bvod"),
    (r"\bOOH\b|\boutdoor\b|out-of-home|\bbillboards?\b|\bposters?\b", "ooh"),
    (r"\bradio\b|\baudio\b|\bpodcasts?\b", "audio"), (r"\bcinema\b", "cinema"),
    (r"\bsocial\b|\bMeta\b|\bFacebook\b|\bInstagram\b", "social_meta"),
    (r"\bTikTok\b", "social_tiktok"), (r"\bYouTube\b|\bonline video\b|\bOLV\b", "olv"),
    (r"\bsearch\b|\bSEM\b", "search"), (r"\bin-store\b|\bretail media\b", "in_store"),
    (r"\bdisplay\b", "display"),
]
MANDATE_RE = re.compile(r"\b(?:must|mandatory|required|non-negotiable|essential|have to)\b", re.I)
NEED_RE = re.compile(r"\b(?:need|needs|deliver|deliverables?|require|requires|want|looking for|produce|assets?)\b", re.I)
BUDGET_RE = re.compile(r"\b(?:budget|spend|investment|working media)\b", re.I)
MONEY_RE = re.compile(
    r"(?P<cur>€|£|\$|EUR|GBP|USD)\s?(?P<num>\d[\d,]*(?:\.\d+)?)\s?(?P<mult>k|m|mn|bn|thousand|million|billion)?\b"
    r"|(?P<num2>\d[\d,]*(?:\.\d+)?)\s?(?P<mult2>k|m|mn|thousand|million)?\s?(?P<cur2>euros?|EUR|pounds?|GBP|dollars?|USD)\b",
    re.I)
# Static FX, used only when the band is the same under a ±15% move either way.
FX_TO_EUR = {"EUR": 1.0, "GBP": 1.17, "USD": 0.92}
BANDS = [(50_000, "under_50k"), (250_000, "50k_250k"), (1_000_000, "250k_1m"),
         (5_000_000, "1m_5m"), (float("inf"), "over_5m")]
NAME = r"[A-Z][\w'’&.-]*(?:[ \t]+[A-Z0-9][\w'’&.-]*)*"


class Material:
    def __init__(self, mid, text, name, kind, sha, known):
        self.id, self.text, self.name, self.kind, self.sha, self.known = mid, text, name, kind, sha, known
        # Paragraphs and sentences as spans of the ORIGINAL text, so quotes are verbatim.
        self.sentences = []  # (para_no, start, end)
        self.paragraphs = []  # (para_no, start, end)
        n = 0
        for pm in re.finditer(r"\S(?:.*?\S)?(?=\n\s*\n|\s*\Z)", text, re.S):
            n += 1
            self.paragraphs.append((n, pm.start(), pm.end()))
            # A sentence ends at . ! ? or at a line break (emails are line-shaped).
            for sm in re.finditer(r"[^\s].*?(?:[.!?](?=\s|\Z)|(?=\n)|\Z)", pm.group(0)):
                self.sentences.append((n, pm.start() + sm.start(), pm.start() + sm.end()))

    def span(self, start, end, quote=True):
        para = next((p for p, s, e in self.paragraphs if s <= start < e), 1)
        sp = {"material_id": self.id, "locator": f"¶{para}"}
        if quote:
            sp["quote"] = self.text[start:end].strip()
        return sp


def norm_sha(s: str) -> str:
    s = (s or "").strip().lower()
    return s if s.startswith("sha256:") else "sha256:" + s


def build_materials(inp: dict, data: dict):
    known = data.get("materials") if isinstance(data.get("materials"), dict) else {}
    by_sha = {norm_sha(v.get("sha256", "")): k for k, v in known.items() if isinstance(v, dict)}
    mats, unread = [], []
    prompt = inp.get("prompt")
    if prompt is not None and not isinstance(prompt, str):
        raise bad("input.prompt must be a string")
    if prompt and prompt.strip():
        sha = "sha256:" + hashlib.sha256(prompt.encode()).hexdigest()
        mid = by_sha.get(sha) or ("mat_p" + sha[7:17])
        mats.append(Material(mid, prompt, "prompt", "prompt", sha, mid in known))
    atts = inp.get("attachments", [])
    if not isinstance(atts, list):
        raise bad("input.attachments must be a list")
    for a in atts:
        if not isinstance(a, dict) or not isinstance(a.get("name"), str) or not a.get("sha256"):
            raise bad("each attachment needs a name and a sha256")
        sha = norm_sha(a["sha256"])
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", sha):
            raise bad(f"attachment '{a['name']}' has a malformed sha256")
        mid = by_sha.get(sha) or ("mat_a" + sha[7:17])
        text = a.get("text")
        if not isinstance(text, str) or not text.strip():
            unread.append(mid)  # no host-extracted text: cannot be read, so it grounds nothing
            continue
        mats.append(Material(mid, text, a["name"], "other", sha, mid in known))
    return mats, unread


def human_owned(data: dict, decisions: list, doc: str, fname: str):
    """Why extraction must not write this field, or None."""
    env = (data.get("campaign") or {}).get(fname)
    if isinstance(env, dict) and env.get("origin") in ("confirmed", "stated"):
        return f"origin {env['origin']}: the field belongs to a human"
    addr = f"{doc}#campaign.{fname}"
    verdicts = [d for d in decisions if d.get("kind") == "verdict" and d.get("polarity") == "bad"
                and addr in (d.get("targets") or [])]
    for v in verdicts:
        answered = any(d.get("kind") == "edit" and addr in (d.get("targets") or [])
                       and str(d.get("timestamp", "")) > str(v.get("timestamp", "")) for d in decisions)
        if not answered:
            return "an unanswered bad verdict stands on it"
    return None


def band_for(eur: float) -> str:
    return next(b for lim, b in BANDS if eur < lim)


def parse_amount(m) -> tuple[str, float]:
    cur = (m.group("cur") or m.group("cur2") or "").upper()
    cur = {"€": "EUR", "£": "GBP", "$": "USD", "EURO": "EUR", "EUROS": "EUR", "POUND": "GBP",
           "POUNDS": "GBP", "DOLLAR": "USD", "DOLLARS": "USD"}.get(cur, cur)
    num = float((m.group("num") or m.group("num2")).replace(",", ""))
    mult = (m.group("mult") or m.group("mult2") or "").lower()
    num *= {"k": 1e3, "thousand": 1e3, "m": 1e6, "mn": 1e6, "million": 1e6, "bn": 1e9, "billion": 1e9}.get(mult, 1)
    return cur, num


def last_day(y, m):
    return (_dt.date(y + (m == 12), m % 12 + 1, 1) - _dt.timedelta(days=1)).isoformat()


def find_window(s: str):
    """An in-market window stated with explicit dates, or None. Relative phrases
    ('next spring') are not resolved: there is no reference date to resolve them
    against, so extraction abstains."""
    m = re.search(r"(\d{4}-\d{2}-\d{2})" + RANGE_SEP + r"(\d{4}-\d{2}-\d{2})", s)
    if m:
        return {"from": m.group(1), "to": m.group(2)}, m
    m = re.search(rf"\b({MONTH_RE})(?:\s+(\d{{4}}))?{RANGE_SEP}({MONTH_RE})\s+(\d{{4}})\b", s, re.I)
    if m:
        m1, m2 = MONTHS[m.group(1).lower()[:3]], MONTHS[m.group(3).lower()[:3]]
        y2 = int(m.group(4))
        y1 = int(m.group(2)) if m.group(2) else (y2 - 1 if m1 > m2 else y2)
        return {"from": f"{y1:04d}-{m1:02d}-01", "to": last_day(y2, m2)}, m
    m = re.search(r"\bQ([1-4])(?:\s*(?:-|–|to)\s*Q([1-4]))?\s*(\d{4})\b", s)
    if m:
        q1, q2, y = int(m.group(1)), int(m.group(2) or m.group(1)), int(m.group(3))
        if q2 >= q1:
            return {"from": f"{y:04d}-{3 * q1 - 2:02d}-01", "to": last_day(y, 3 * q2)}, m
    m = re.search(r"\b(spring|summer|autumn|fall)\s+(\d{4})\b", s, re.I)
    if m:
        a, b = SEASONS[m.group(1).lower()]
        y = int(m.group(2))
        return {"from": f"{y:04d}-{a:02d}-01", "to": last_day(y, b)}, m
    m = re.search(rf"\b(?:in|during|throughout)\s+({MONTH_RE})\s+(\d{{4}})\b", s, re.I)
    if m:
        mo, y = MONTHS[m.group(1).lower()[:3]], int(m.group(2))
        return {"from": f"{y:04d}-{mo:02d}-01", "to": last_day(y, mo)}, m
    return None, None


def extract_candidates(mats: list, subject_name: str | None) -> tuple[dict, list]:
    """Field -> candidate {value, span, items?}. Only what a span supports."""
    out: dict = {}
    notes: list = []

    def sentences():
        for mat in mats:
            for para, s, e in mat.sentences:
                yield mat, s, e, mat.text[s:e]

    # markets — country names, per item provenance
    items = {}
    for mat, s, e, sent in sentences():
        hits = sorted((m.start(), code) for rx, code in COUNTRIES for m in re.finditer(rx, sent))
        for _, code in hits:  # in the order the material names them
            if code not in items:
                items[code] = mat.span(s, e)
    if items:
        out["markets"] = {"value": list(items), "item_spans": items}

    # in_market
    for mat, s, e, sent in sentences():
        if re.search(r"\b(?:deadline|due|respond|reply|pitch|submit)\b", sent, re.I):
            continue
        win, m = find_window(sent)
        if win and win["from"] <= win["to"]:
            out["in_market"] = {"value": win, "span": mat.span(s, e)}
            break
    if "in_market" not in out and any(re.search(r"\bnext\s+(?:spring|summer|autumn|winter|year|month)\b", t, re.I)
                                      for _, _, _, t in sentences()):
        notes.append("in_market: only a relative date ('next …') is given; nothing to anchor it to")

    # budget_band — a band, never the figure; the span has no quote
    bands, bspan, reasons = set(), None, []
    for mat, s, e, sent in sentences():
        if not BUDGET_RE.search(sent):
            continue
        for m in MONEY_RE.finditer(sent):
            cur, amt = parse_amount(m)
            rate = FX_TO_EUR.get(cur)
            if rate is None:
                reasons.append("budget_band: amount in an unsupported currency")
                continue
            lo, hi = band_for(amt * rate * 0.85), band_for(amt * rate * 1.15)
            if lo != hi and cur != "EUR":
                reasons.append(f"budget_band: {cur} amount sits too near a band edge to convert")
                continue
            bands.add(band_for(amt * rate))
            bspan = bspan or mat.span(s, e, quote=False)
    if len(bands) == 1:
        out["budget_band"] = {"value": bands.pop(), "span": bspan}
    elif len(bands) > 1:
        notes.append("budget_band: several amounts in different bands")
    else:
        notes.extend(reasons)

    # deliverables
    items = {}
    for mat, s, e, sent in sentences():
        if not NEED_RE.search(sent):
            continue
        found_tvc = False
        for rx, tpl in DELIVERABLES:
            for m in re.finditer(rx, sent, re.I):
                sl = tpl.format(*m.groups())
                if sl == "tvc" and found_tvc:
                    continue
                found_tvc = found_tvc or sl.startswith("tvc")
                items.setdefault(sl, mat.span(s, e))
    if items:
        out["deliverables"] = {"value": list(items), "item_spans": items}

    # channels_mandated — only in a sentence that mandates
    items = {}
    for mat, s, e, sent in sentences():
        if not MANDATE_RE.search(sent) or re.search(r"\b(?:must|may)\s+not\b|\bmust be on\b", sent, re.I):
            continue
        for rx, sl in CHANNELS:
            if re.search(rx, sent, re.I if sl not in ("tv", "social_meta") else 0):
                items.setdefault(sl, mat.span(s, e))
    if items:
        out["channels_mandated"] = {"value": list(items), "item_spans": items}

    # competitor_set
    items, names = {}, {}
    cues = [
        re.compile(rf"(?P<n>{NAME})\s+(?:as|is)\s+(?:the\s+)?(?:one|brand|competitor|rival)\s+to\s+beat"),
        re.compile(rf"(?i:competitors?|rivals?|competition|competing with|compete with|up against|versus|vs\.?)"
                   rf"\s*(?:(?i:is|are|include|includes|like)\s+|:\s*)?(?P<l>{NAME}(?:\s*(?:,|and|&)\s*{NAME})*)"),
    ]
    for mat, s, e, sent in sentences():
        for rx in cues:
            for m in rx.finditer(sent):
                blob = m.groupdict().get("n") or m.group("l")
                for nm in re.split(r"\s*(?:,|\band\b|&)\s*", blob):
                    nm = nm.strip().rstrip(".,;:")
                    if not nm or nm.lower() in ("we", "the", "our", "i"):
                        continue
                    if subject_name and slug(nm) == slug(subject_name):
                        continue
                    ref = "brand/" + slug(nm)
                    if slug(nm) and ref not in items:
                        items[ref] = mat.span(s, e)
                        names[ref] = nm
    if items:
        out["competitor_set"] = {"value": [{"ref": r, "name": names[r]} for r in items], "item_spans": items}

    # problem / objective / audience_stated — verbatim, from a labelled cue
    cue_sets = {
        "problem": [r"(?:the\s+)?(?:problem|challenge|issue)\b[^:.\n]{0,40}:\s*",
                    r"\b(?:our|the)\s+(?:problem|challenge)\s+is\s+(?:that\s+)?"],
        "objective": [r"\b(?:what we need|objective|goal|aim|the ask)\b[^:.\n]{0,40}:\s*",
                      r"\b(?:our|the)\s+(?:objective|goal|aim)\s+is\s+(?:to\s+)?"],
        "audience_stated": [r"\b(?:who it['’]?s for|target audience|audience|target)\b[^:.\n]{0,40}:\s*",
                            r"\b(?:aimed at|targeting)\s+"],
    }
    for fname, pats in cue_sets.items():
        done = False
        for mat in mats:
            for para, ps, pe in mat.paragraphs:
                ptext = mat.text[ps:pe]
                for i, pat in enumerate(pats):
                    m = re.search(pat, ptext, re.I)
                    if not m:
                        continue
                    vs = ps + m.end()
                    if i == 0:  # labelled: the rest of the paragraph
                        ve = pe
                    else:       # inline: the rest of the sentence
                        se = next((e for p, s, e in mat.sentences if s <= ps + m.start() < e), pe)
                        ve = se
                    val = mat.text[vs:ve].strip()
                    if len(val) >= 8:
                        out[fname] = {"value": val, "span": mat.span(vs, ve)}
                        done = True
                        break
                if done:
                    break
            if done:
                break

    # campaign_type — only when exactly one type is signalled
    types = {}
    for mat, s, e, sent in sentences():
        for rx, t in [(r"\blaunch(?:ing|es|ed)?\b", "launch"), (r"\bre-?brand(?:ing)?\b", "rebrand"),
                      (r"\balways[- ]on\b", "always_on"),
                      (r"\b(?:christmas|easter|halloween|festive|seasonal)\b", "seasonal")]:
            if re.search(rx, sent, re.I):
                types.setdefault(t, mat.span(s, e))
    if len(types) == 1:
        t, sp = next(iter(types.items()))
        out["campaign_type"] = {"value": t, "span": sp}
    elif len(types) > 1:
        notes.append("campaign_type: more than one type is signalled")

    # success_measures — sentences with a percentage and a goal verb
    items = {}
    for mat, s, e, sent in sentences():
        if re.search(r"\d+(?:\.\d+)?\s?%", sent) and re.search(
                r"\b(?:reach|achieve|grow|increase|target|kpi|success|measure)\b", sent, re.I) \
                and not BUDGET_RE.search(sent):
            txt = sent.strip()
            items[lid("sm_", txt, n=8)] = (txt, mat.span(s, e))
    if items:
        out["success_measures"] = {"value": [{"id": k, "text": v[0]} for k, v in items.items()],
                                   "item_spans": {k: v[1] for k, v in items.items()}}

    # constraints
    items = {}
    for mat, s, e, sent in sentences():
        if not re.search(r"\bmandator|\bmust\s+(?:not|never|be on|appear|include|carry)\b|\bmay not\b|"
                         r"\bno creative\b|\bnot permitted\b|\bcompliance\b|\bguidelines\b", sent, re.I):
            continue
        txt = sent.strip()
        if re.search(r"\blegal\b|\bregulat|\bcompliance\b|\bunder\s+\d{2}\b|\bage\b|\bASAI?\b|\bClearcast\b", txt, re.I):
            kind = "legal"
        elif re.search(r"\blogo\b|\bbadge\b|\bguidelines\b|\blivery\b|\bbrand (?:assets|identity)\b|\bfont\b", txt, re.I):
            kind = "brand"
        else:
            kind = "mandatory"
        items[lid("c_", txt, n=8)] = (kind, txt, mat.span(s, e))
    if items:
        out["constraints"] = {"value": [{"id": k, "kind": v[0], "text": v[1]} for k, v in items.items()],
                              "item_spans": {k: v[2] for k, v in items.items()}}

    # client_org — a signature line "<role>, <Org>"
    for mat in mats:
        m = re.search(r"^[ \t]*(?:[A-Z][\w ]*?(?:Manager|Director|Lead|Officer|Head of [\w ]+|CMO|CEO|VP[\w ]*))"
                      r"[ \t]*,[ \t]*(?P<org>[A-Z][^\n,]{1,60}?)[ \t]*$", mat.text, re.M)
        if m and slug(m.group("org")):
            out["client_org"] = {"value": {"ref": "org/" + slug(m.group("org")), "name": m.group("org").strip()},
                                 "span": mat.span(m.start("org"), m.end("org"))}
            break

    # brand — only from an explicit label; the subject brand never defaults
    for mat in mats:
        m = re.search(r"^[ \t]*Brand[ \t]*:[ \t]*(?P<b>[^\n]{2,60}?)[ \t]*$", mat.text, re.M)
        if m and slug(m.group("b")):
            out["brand"] = {"value": {"ref": "brand/" + slug(m.group("b")), "name": m.group("b").strip()},
                            "span": mat.span(m.start("b"), m.end("b"))}
            break
    return out, notes


def item_key(fname, item):
    return item["ref"] if fname == "competitor_set" else item["id"] if isinstance(item, dict) else item


def read_set(data: dict, patch: dict) -> dict:
    """What the job read of every field its data_patch writes, by dotted path.

    The host judges a stale base by it (middleware-api.md §3): a field that
    still holds what was read takes the patch; one a person changed meanwhile
    becomes a contest. Fields are `<top>.<key>` under an object (a campaign
    field, a selection key, a material id), `<top>` otherwise. A field that
    was absent was read as null.
    """
    out = {}
    for top, sub in patch.items():
        held = data.get(top)
        if isinstance(sub, dict) and sub:
            for key in sub:
                out[f"{top}.{key}"] = copy.deepcopy(held.get(key)) if isinstance(held, dict) else None
        else:
            out[top] = copy.deepcopy(held)
    return out


def run_extract(doc, base, clan, inp, handler):
    data = ctx_data(clan)
    facts = ctx_facts(clan)
    decisions = ctx_decisions(clan)
    mats, unread = build_materials(inp, data)
    if not mats:
        raise bad("nothing to read: input.prompt is empty and no attachment carries text")
    did = uid("d_", doc, base, "extract", [m.sha for m in mats])
    subject = field_value(data, "brand")
    subject_name = subject.get("name") if isinstance(subject, dict) else None
    cands, notes = extract_candidates(mats, subject_name)

    # Step 1 of the contract: deterministic layer lookups -> proposed, citing pins.
    subject_ref = subject.get("ref") if isinstance(subject, dict) else None
    roster = [f for f in facts if str(f.get("key", "")).startswith("roster.")
              and (subject_ref is None or f.get("entity") == subject_ref)
              and f.get("status", "active") == "active"]
    proposed = {}
    cats = [f for f in roster if f.get("key") in ("roster.categories.primary", "roster.categories.secondary")]
    cats.sort(key=lambda f: f["key"] != "roster.categories.primary")
    cat_vals = [f["value"] for f in cats if isinstance(f.get("value"), str)
                and re.fullmatch(r"[a-z0-9_]+\.[a-z0-9_]+", f["value"])][:2]  # at most two
    if cat_vals:
        proposed["categories"] = {"value": list(dict.fromkeys(cat_vals)), "fact_ids": [f["id"] for f in cats]}
    co = next((f for f in roster if f.get("key") == "roster.client_org"
               and re.fullmatch(r"org/[a-z0-9][a-z0-9-]*", str(f.get("value")))), None)
    if co:
        ext = cands.get("client_org")
        name = ext["value"]["name"] if ext and ext["value"]["ref"] == co["value"] else \
            co["value"][4:].replace("-", " ").title()
        proposed["client_org"] = {"value": {"ref": co["value"], "name": name}, "fact_ids": [co["id"]]}
        cands.pop("client_org", None)

    campaign_patch, written, withheld = {}, [], {}
    existing = data.get("campaign") or {}
    for fname in [f for f in CAMPAIGN_FIELDS if f in cands or f in proposed]:
        why = human_owned(data, decisions, doc, fname)
        if why:
            withheld[fname] = why
            continue
        env = {"gate": GATES[fname], "decision": did}
        if fname in proposed:
            p = proposed[fname]
            env.update(value=p["value"], origin="proposed", fact_ids=p["fact_ids"])
        else:
            c = cands[fname]
            env.update(value=c["value"], origin="extracted")
            if "item_spans" in c:
                spans = c["item_spans"]
                uniq = {json.dumps(v, sort_keys=True) for v in spans.values()}
                env["source"] = next(iter(spans.values()))
                if len(uniq) > 1:
                    env["item_provenance"] = {
                        item_key(fname, it): {"origin": "extracted", "source": spans[item_key(fname, it)]}
                        for it in c["value"]}
            else:
                env["source"] = c["span"]
        old = existing.get(fname) if isinstance(existing.get(fname), dict) else None
        if old and fname in LIST_FIELDS and old.get("origin") in ("extracted", "proposed"):
            env = merge_list_field(fname, old, env)
        if old and old.get("value") == env["value"] and old.get("origin") == env["origin"]:
            continue  # nothing new; don't churn the decision pointer
        campaign_patch[fname] = {k: env[k] for k in
                                 ["value", "origin", "gate", "source", "fact_ids", "decision", "item_provenance"]
                                 if k in env}
        written.append(fname)

    abstained = [f for f in CAMPAIGN_FIELDS if f not in NOT_EXTRACTED and f not in written
                 and f not in withheld and f not in existing]
    patch = {"campaign": campaign_patch} if campaign_patch else {}
    new_mats = {m.id: {"kind": m.kind, "name": m.name, "sha256": m.sha, "received_at": iso(now()),
                       # Unindexed material is classed at the strictest licence until a human says otherwise.
                       "licence": "client-confidential"}
                for m in mats if not m.known}
    if new_mats:
        patch["materials"] = new_mats
    cites = [m.id for m in mats] + [fid for p in proposed.values() for fid in p["fact_ids"]]
    rationale = (f"Read {len(mats)} material(s); filled {len(written)} field(s) from spans"
                 f"{' and layer pins' if proposed else ''}; abstained on {len(abstained)}"
                 + (f"; held back {len(withheld)} human-owned" if withheld else "") + ".")
    dec = decision(doc, did, "edit", handler, "extract_ask", rationale,
                   [f"campaign.{f}" for f in written] or ["campaign"], cites,
                   fields_changed=[f"campaign.{f}" for f in written],
                   material_read=[m.id for m in mats], abstained=abstained)
    if unread:
        dec["material_unread"] = unread
    change = {"doc": doc, "base_version": base, "data_patch": patch, "read": read_set(data, patch),
              "facts_append": [], "findings_append": [], "decisions": [dec]}
    fact_by_id = {f.get("id"): f for f in facts}
    hits = [{"id": fid, "scope": fact_by_id[fid].get("layer", "brand"), "source": fact_by_id[fid].get("origin", "")}
            for p in proposed.values() for fid in p["fact_ids"] if fid in fact_by_id]
    result = {"summary": rationale, "fields": written, "abstained": abstained,
              "withheld": withheld, "notes": notes, "materials_read": [m.id for m in mats],
              "materials_unread": unread}
    return result, change, hits


def merge_list_field(fname, old, new):
    """Union a re-extraction with an earlier extracted/proposed list, keeping per-item provenance."""
    def prov(env, key):
        ip = (env.get("item_provenance") or {}).get(key)
        if ip:
            return ip
        p = {"origin": env["origin"]}
        if env["origin"] == "extracted" and env.get("source"):
            p["source"] = env["source"]
        if env["origin"] == "proposed" and env.get("fact_ids"):
            p["fact_ids"] = env["fact_ids"]
        return p
    items, provs = [], {}
    for env in (old, new):
        for it in env.get("value") or []:
            k = item_key(fname, it)
            if k not in provs:
                items.append(it)
                provs[k] = prov(env, k)
    origins = {p["origin"] for p in provs.values()}
    origin = "proposed" if "proposed" in origins else "extracted"
    out = {"value": items, "origin": origin, "gate": new["gate"], "decision": new["decision"]}
    fact_ids = list(dict.fromkeys(f for p in provs.values() for f in p.get("fact_ids", [])))
    if origin == "proposed":
        out["fact_ids"] = fact_ids
    else:
        out["source"] = next(p["source"] for p in provs.values() if "source" in p)
    if len(origins) > 1 or len({json.dumps(p, sort_keys=True) for p in provs.values()}) > 1:
        out["item_provenance"] = provs
    return out


# ---------------------------------------------------------------------------
# research_lens — facts per lens x market from a fixture bank + generator
# ---------------------------------------------------------------------------
# Each lens asks for a small set of fact keys. `who`: category | subject |
# competitor. `per_market`: whether the key's identity includes the market.
LENS_SPECS = {
    "market_structure": [("category", "market.value_growth_yoy", "proportion", "report", True),
                         ("category", "market.size_eur", "eur", "report", True)],
    "brands_positioning": [("subject", "awareness.prompted", "proportion", "measurement", True),
                           ("competitor", "launch.date", "date", "observation", False)],
    "consumer_culture": [("category", "consumer.moderating_share", "proportion", "measurement", True)],
    "category_codes": [("category", "codes.dominant_colour", "text", "observation", False)],
    "rhythm_moments": [("category", "rhythm.peak_months", "text", "report", True)],
    "media_spend": [("category", "media.tv_share_of_spend", "proportion", "report", True)],
    "regulation_clearance": [("category", "regulation.min_age_depicted", "years", "report", True)],
    "effectiveness_evidence": [("category", "effectiveness.published_cases", "count", "report", True)],
}
KEY_LENS = {"market": "market_structure", "awareness": "brands_positioning", "launch": "brands_positioning",
            "positioning": "brands_positioning", "product": "brands_positioning",
            "consumer": "consumer_culture", "codes": "category_codes", "rhythm": "rhythm_moments",
            "media": "media_spend", "regulation": "regulation_clearance",
            "effectiveness": "effectiveness_evidence"}
# The small fixture bank: values for known category leaves, keyed (key, market);
# market "*" is a market-independent value. Anything not here is generated.
FIXTURES = {
    "drinks.cider": {
        ("market.value_growth_yoy", "IE"): 0.03, ("market.value_growth_yoy", "GB"): 0.01,
        ("regulation.min_age_depicted", "IE"): 25, ("regulation.min_age_depicted", "GB"): 25,
        ("rhythm.peak_months", "IE"): "05-08", ("rhythm.peak_months", "GB"): "06-08",
        ("codes.dominant_colour", "*"): "amber",
    },
    "drinks.no_low_alcohol": {
        ("market.value_growth_yoy", "IE"): 0.21, ("market.value_growth_yoy", "GB"): 0.11,
        ("consumer.moderating_share", "IE"): 0.34, ("consumer.moderating_share", "GB"): 0.29,
        ("regulation.min_age_depicted", "IE"): 25, ("regulation.min_age_depicted", "GB"): 25,
        ("codes.dominant_colour", "*"): "white",
    },
}
TIER_BASE = {"primary": 1, "secondary": 0, "mock": 0}  # mock is ranked with secondary
CONF = ["low", "medium", "high"]
LICENCE_RANK = ["open", "licensed-internal", "client-confidential"]


def derive_confidence(sources: list) -> str:
    """Confidence from tier + corroboration, never asserted:
    start at the best source's tier (primary=medium, secondary/mock=low), then
    one step up per independent corroborating source (2 sources +1, 3+ +2),
    capped at high."""
    if not sources:
        return "low"
    base = max(TIER_BASE[s["tier"]] for s in sources)
    return CONF[min(2, base + min(2, len(sources) - 1))]


def gen_value(unit, cat, key, market, entity):
    fx = FIXTURES.get(cat, {})
    if (key, market or "*") in fx:
        return fx[(key, market or "*")]
    h = hnum(SCOPE["org"], entity, key, market)
    if unit == "proportion":
        return round(0.04 + (h % 560) / 1000, 2)
    if unit == "eur":
        return (h % 490 + 10) * 1_000_000
    if unit == "date":
        return (_dt.date(2024, 1, 1) + _dt.timedelta(days=h % 900)).isoformat()
    if unit == "years":
        return [18, 21, 25][h % 3]
    if unit == "count":
        return h % 7
    if key == "rhythm.peak_months":
        return ["05-08", "06-08", "11-12", "03-05", "09-10"][h % 5]
    return ["amber", "green", "gold", "black", "white", "red"][h % 6]


def n_sources(entity, key, market):
    return [0, 1, 1, 1, 2, 2, 2, 3][hnum(SCOPE["org"], "n", entity, key, market) % 8]


def make_sources(lens, entity, key, market, method):
    out = []
    for i in range(n_sources(entity, key, market)):
        h = hnum(SCOPE["org"], "lic", entity, key, market, i)
        lic = {"measurement": "licensed-internal", "observation": "open"}.get(
            method, ["open", "licensed-internal"][h % 2])
        out.append({"id": lid("src_", SCOPE["org"], entity, key, market, i),
                    "uri": f"mock-source://{lens}/{market}/{entity}/{key}/{i + 1}",
                    "tier": "mock", "licence": lic,
                    "title": f"Mock {lens.replace('_', ' ')} source {i + 1} ({market})"})
    return out


def origin_uri(layer, entity, key, version):
    typ, _, rest = entity.partition("/")
    path = rest if typ == layer else entity
    return f"fact://{layer}/{path}/{key}@{version}"


def run_research(doc, base, clan, inp, handler):
    data = ctx_data(clan)
    lenses = inp.get("lenses", LENSES)
    if not isinstance(lenses, list) or not lenses or any(l not in LENSES for l in lenses):
        raise bad(f"input.lenses must be a non-empty list of lens ids from {LENSES}")
    lenses = [l for l in LENSES if l in lenses]
    markets = inp.get("markets")
    if markets is None:
        markets = field_value(data, "markets")
        if not markets:
            raise bad("campaign.markets is absent and input.markets not given: research cannot run (gate research)")
    if not isinstance(markets, list) or not markets or any(m not in ISO_3166 for m in markets):
        raise bad("markets must be ISO 3166-1 alpha-2 codes (the UK is GB)")
    markets = list(dict.fromkeys(markets))
    cats = field_value(data, "categories")
    if not cats or not isinstance(cats, list):
        raise bad("campaign.categories is absent: research cannot run (gate research)")
    cats = cats[:2]
    subject = field_value(data, "brand")
    subject_ref = subject.get("ref") if isinstance(subject, dict) else None
    comps = [c for c in (field_value(data, "competitor_set") or []) if isinstance(c, dict) and c.get("ref")]

    t_now = now()
    today = t_now.date()
    existing = ctx_facts(clan)
    excluded = {e.get("fact_id") for e in ((data.get("selection") or {}).get("excluded") or [])}
    pinned = {(f.get("entity"), f.get("key"), f.get("market")): f for f in existing}
    sel = data.get("selection") or {}

    # --- one run per lens x market ---------------------------------------
    runs, cands, gaps, sources_seen = [], {}, [], {}
    for lens in lenses:
        for market in markets:
            found, gap_ids = [], []
            for who, key, unit, method, per_market in LENS_SPECS[lens]:
                if who == "category":
                    targets = [("category/" + c, "category", c) for c in cats]
                elif who == "subject":
                    targets = [(subject_ref, "brand", cats[0])] if subject_ref else []
                else:
                    targets = [(c["ref"], "category", cats[0]) for c in comps]
                for entity, layer, cat in targets:
                    srcs = make_sources(lens, entity, key, market, method)
                    ident = (entity, key, market if per_market else None)
                    if not srcs:
                        gid = lid("gap_", doc, entity, key, market)
                        gaps.append({"id": gid, "key": f"{entity}:{key}", "lens": lens, "market": market,
                                     "searched": f"{key.replace('.', ' ').replace('_', ' ')} for {entity} in {market}",
                                     "sources_tried": [f"mock-source://{lens}/{market}/search"]})
                        gap_ids.append(gid)
                        continue
                    # Contest rule: the FIRST comparator's launch date is read differently
                    # by each market's run (each market saw its own launch).
                    value = gen_value(unit, cat, key, market if per_market else None, entity)
                    if who == "competitor" and comps and entity == comps[0]["ref"] and len(markets) > 1:
                        d0 = _dt.date.fromisoformat(value)
                        value = (d0 + _dt.timedelta(days=markets.index(market) * (hnum(entity, market) % 200 + 30))).isoformat()
                    for s in srcs:
                        sources_seen[s["id"]] = (s, layer)
                    cands.setdefault(ident, []).append(
                        {"value": value, "unit": unit, "method": method, "layer": layer, "lens": lens,
                         "market": market, "sources": srcs})
                    found.append(len(srcs))
            cov = "empty" if not found else "filled" if all(n >= 2 for n in found) else "thin"
            runs.append({"lens": lens, "market": market, "coverage": cov, "gaps": gap_ids,
                         "found": len(found)})

    # --- merge by entity + key + market ------------------------------------
    merge_did = uid("d_", doc, base, "research-merge", lenses, markets)
    facts_append, contests, contest_decs = [], [], []
    open_keys = {c.get("key") for c in (sel.get("contested") or []) if c.get("status") == "open"}
    for ident, cs in cands.items():
        entity, key, market = ident
        values = {json.dumps(c["value"]) for c in cs}
        c0 = cs[0]
        version = 1
        ckey = f"{entity}:{key}" + (f"@{market}" if market else "")
        prior = pinned.get(ident)
        if len(values) == 1:
            srcs = list({s["id"]: s for c in cs for s in c["sources"]}.values())  # union = corroboration
            fid = uid("f_", SCOPE["org"], entity, key, market, version, c0["value"])
            if prior is not None:
                if prior.get("value") == c0["value"] or prior.get("id") == fid:
                    continue  # already pinned
                if ckey in open_keys:
                    continue
                cid = lid("ct_", doc, ckey)
                cdid = uid("d_", doc, base, "contest", ckey)
                contests.append({"id": cid, "key": ckey, "status": "open", "opened_by": cdid, "values": [
                    {"value": prior["value"], "unit": prior.get("unit"), "fact_id": prior["id"], "from": "pinned",
                     "sources": prior.get("sources", [])},
                    {"value": c0["value"], "unit": c0["unit"], "fact_id": fid, "from": f"{c0['lens']}/{c0['market']}",
                     "sources": [s["id"] for s in srcs]}]})
                contest_decs.append(decision(doc, cdid, "contest", handler, "open_contest",
                                             f"Research reads {ckey} differently from the pinned value; nothing is picked.",
                                             [f"selection.contested[{cid}]"], [prior["id"], fid]))
                continue
            lic = max((s["licence"] for s in srcs), key=LICENCE_RANK.index)
            as_of = (today - _dt.timedelta(days=hnum(entity, key, market, "asof") % 200 + 10)).isoformat()
            f = {"id": fid, "entity": entity, "key": key, "value": c0["value"], "unit": c0["unit"],
                 "as_of": as_of, "retrieved_at": today.isoformat(), "sources": [s["id"] for s in srcs],
                 "confidence": derive_confidence(srcs), "licence": lic, "status": "active",
                 "version": version, "supersedes": None,
                 "origin": origin_uri(c0["layer"], entity, key, version), "decision": merge_did,
                 "pinned_at": iso(t_now),
                 "pin_reason": f"research_lens {c0['lens']}/{'+'.join(sorted({c['market'] for c in cs}))}: "
                               f"mock-source fixture, {len(srcs)} source(s)",
                 "layer": c0["layer"], "method": c0["method"]}
            if market:
                f["market"] = market
            if fid in excluded:
                continue
            facts_append.append(f)
        else:
            if ckey in open_keys:
                continue
            cid = lid("ct_", doc, ckey)
            cdid = uid("d_", doc, base, "contest", ckey)
            vals = []
            for c in cs:
                vals.append({"value": c["value"], "unit": c["unit"],
                             "fact_id": uid("f_", SCOPE["org"], entity, key, c["market"], version, c["value"]),
                             "from": f"{c['lens']}/{c['market']}", "sources": [s["id"] for s in c["sources"]]})
            contests.append({"id": cid, "key": ckey, "status": "open", "opened_by": cdid, "values": vals})
            contest_decs.append(decision(
                doc, cdid, "contest", handler, "open_contest",
                f"{len(vals)} market runs disagree on {entity} {key}; the layer row is contested and nothing is picked.",
                [f"selection.contested[{cid}]"], [v["fact_id"] for v in vals] + [s for v in vals for s in v["sources"]]))
    for v in contests:
        for x in v["values"]:
            if x.get("unit") is None:
                x.pop("unit")

    # --- selection -----------------------------------------------------------
    run_decs, lenses_run = [], []
    by_market = {}
    for r in runs:
        rdid = uid("d_", doc, base, "run", r["lens"], r["market"])
        lenses_run.append({"lens": r["lens"], "market": r["market"], "ran_at": iso(t_now),
                           "handler": handler, "decision": rdid})
        by_market.setdefault(r["market"], {})[r["lens"]] = r["coverage"]
        run_srcs = [s for s, _ in sources_seen.values() if f"/{r['lens']}/{r['market']}/" in s["uri"].replace("mock-source://", "/")]
        run_decs.append(decision(
            doc, rdid, "edit", handler, "research_run",
            f"{r['lens']}/{r['market']}: {r['found']} fact(s) found, coverage {r['coverage']}, {len(r['gaps'])} gap(s).",
            [f"selection.lenses_run[{r['lens']}/{r['market']}]"] + [f"selection.gaps[{g}]" for g in r["gaps"]],
            [s["id"] for s in run_srcs], fields_changed=["selection.lenses_run", "selection.coverage_by_market"]
            + (["selection.gaps"] if r["gaps"] else [])))
    coverage = {}
    for lens in lenses:
        vals = [by_market[m][lens] for m in markets]
        coverage[lens] = "filled" if all(v == "filled" for v in vals) else \
            "empty" if all(v == "empty" for v in vals) else "thin"

    run_keys = {(l["lens"], l["market"]) for l in lenses_run}
    all_runs = [x for x in (sel.get("lenses_run") or []) if (x.get("lens"), x.get("market")) not in run_keys] + lenses_run
    old_gaps = [g for g in (sel.get("gaps") or []) if g.get("id") not in {x["id"] for x in gaps}]
    old_ct = [c for c in (sel.get("contested") or []) if c.get("id") not in {x["id"] for x in contests}]
    # data_patch is a JSON merge patch: arrays are replaced whole, so each array
    # carries what was there plus this run's entries; objects merge key-wise.
    sel_patch = {"lenses_run": all_runs, "coverage": coverage, "coverage_by_market": by_market,
                 "gaps": old_gaps + gaps}
    if contests:
        sel_patch["contested"] = old_ct + contests
    merge_dec = decision(
        doc, merge_did, "pin", handler, "research_merge",
        f"Merged {len(runs)} lens x market run(s) by entity + key + market: {len(facts_append)} pin(s), "
        f"{len(contests)} contest(s), {len(gaps)} gap(s). Confidence is derived from source tier and corroboration.",
        [f"facts[{f['id']}]" for f in facts_append] + ["selection.coverage"],
        [f["id"] for f in facts_append] + [s for f in facts_append for s in f["sources"]],
        fields_changed=["selection.coverage"])
    change = {"doc": doc, "base_version": base, "data_patch": {"selection": sel_patch},
              "read": read_set(ctx_data(clan), {"selection": sel_patch}),
              "facts_append": facts_append, "findings_append": [],
              "decisions": run_decs + [merge_dec] + contest_decs}
    hits = [{"id": s["id"], "scope": layer, "source": s["uri"]} for s, layer in sources_seen.values()]
    result = {"summary": f"{len(runs)} lens x market run(s): {len(facts_append)} fact(s) pinned, "
                         f"{len(contests)} contest(s) open, {len(gaps)} gap(s).",
              "coverage": coverage,
              "sources": {s["id"]: {k: s[k] for k in ("uri", "tier", "licence", "title")}
                          for s, _ in sources_seen.values()}}
    return result, change, hits, len(runs)


# ---------------------------------------------------------------------------
# synthesise_findings — findings over the pins, confidence derived
# ---------------------------------------------------------------------------

def finding_confidence(cited: list) -> str:
    """Contract 3 §6.1: the lowest confidence among the cited pins, one level
    lower if it cites a single pin or any stale pin (floor low)."""
    lvl = min(CONF.index(f.get("confidence", "low")) for f in cited)
    if len(cited) == 1 or any(f.get("stale") for f in cited):
        lvl -= 1
    return CONF[max(0, lvl)]


def fact_lens(f):
    return KEY_LENS.get(str(f.get("key", "")).split(".")[0])


def run_synthesis(doc, base, clan, inp, handler):
    lenses = inp.get("lenses", LENSES)
    if not isinstance(lenses, list) or not lenses or any(l not in LENSES for l in lenses):
        raise bad(f"input.lenses must be a non-empty list of lens ids from {LENSES}")
    data = ctx_data(clan)
    excluded = {e.get("fact_id") for e in ((data.get("selection") or {}).get("excluded") or [])}
    facts = [f for f in ctx_facts(clan) if re.fullmatch(r"f_[0-9A-Z]{6,}", str(f.get("id", "")))
             and f.get("id") not in excluded and f.get("confidence") in CONF]
    if not facts:
        raise bad("no pinned facts in clan.facts to synthesise from (a finding must cite pins)")
    existing = {tuple(sorted(x.get("cites") or [])) for x in ctx_findings(clan)
                if x.get("status") in ("proposed", "verified")}
    groups = {}
    for f in sorted(facts, key=lambda f: f["id"]):
        l = fact_lens(f)
        if l in lenses:
            groups.setdefault(l, []).append(f)
    findings, decs = [], []
    t = iso(now())
    for lens in [l for l in LENSES if l in groups]:
        fs = groups[lens]
        # Prefer a key read in two markets: a cross-market comparison.
        by_key = {}
        for f in fs:
            by_key.setdefault((f["entity"], f["key"]), []).append(f)
        pair = next((v for v in by_key.values()
                     if len(v) >= 2 and all(isinstance(x["value"], (int, float)) and not isinstance(x["value"], bool)
                                            for x in v) and len({x.get("market") for x in v}) == len(v)), None)
        if pair:
            cited = sorted(pair, key=lambda x: -x["value"])[:3]
            hi, lo = cited[0], cited[-1]
            k = hi["key"].replace(".", " ").replace("_", " ")
            if hi["value"] == lo["value"]:
                stmt = f"On {k} for {hi['entity']}, {' and '.join(x['market'] for x in cited)} read the same; the markets can share one plan here."
            else:
                stmt = f"On {k} for {hi['entity']}, {hi['market']} reads higher than {lo['market']}; the two markets may need different plans here."
        else:
            cited = fs[:3]
            keys = ", ".join(sorted({f["key"] for f in cited}))
            stmt = f"The {lens.replace('_', ' ')} picture rests on {len(cited)} pin(s) ({keys}); read together before briefing."
        cites = [f["id"] for f in cited]
        if tuple(sorted(cites)) in existing:
            continue
        fid = uid("fi_", doc, lens, cites)
        did = uid("d_", doc, base, "finding", fid)
        mk = sorted({f["market"] for f in cited if f.get("market")})
        fi = {"id": fid, "statement": stmt, "cites": cites, "method": "synthesis", "status": "proposed",
              "derived_by": handler, "confidence": finding_confidence(cited), "derived_at": t,
              "decision": did, "lens": lens}
        if mk:
            fi["markets"] = mk
        findings.append(fi)
        decs.append(decision(doc, did, "finding", handler, "synthesise_finding",
                             f"Derived from {len(cites)} pin(s); confidence {fi['confidence']} is the lowest cited, "
                             f"stepped down for a single or stale citation. Proposed until a human verifies it.",
                             [f"findings[{fid}]"], cites))
    change = {"doc": doc, "base_version": base, "data_patch": {}, "read": {}, "facts_append": [],
              "findings_append": findings, "decisions": decs}
    hits = [{"id": c, "scope": next((f.get("layer", "") for f in facts if f["id"] == c), ""),
             "source": next((f.get("origin", "") for f in facts if f["id"] == c), "")}
            for fi in findings for c in fi["cites"]]
    result = {"summary": f"{len(findings)} finding(s) proposed from {len(facts)} pin(s).",
              "findings": [f["id"] for f in findings]}
    return result, change, hits, max(1, len(findings))


# ---------------------------------------------------------------------------
# Jobs (in memory)
# ---------------------------------------------------------------------------
JOBS: dict = {}
JOBS_LOCK = threading.Lock()
_SEQ = itertools.count(1)
MAX_JOBS = 500


def submit(task, handler, doc, base, result, change, hits, total, inp):
    jid = "job_" + _digest(SCOPE, doc, base, task, inp, next(_SEQ)).hex()[:20]
    job = {"id": jid, "task": task, "handler": handler, "doc": doc, "base": base, "scope": dict(SCOPE),
           "t0": time.monotonic(), "started_at": iso(_dt.datetime.now(_dt.timezone.utc)),
           "finished_at": None, "polls": 0, "total": total, "result": result, "change": change,
           "hits": hits}
    with JOBS_LOCK:
        JOBS[jid] = job
        if len(JOBS) > MAX_JOBS:
            for k in sorted(JOBS, key=lambda k: JOBS[k]["t0"])[: len(JOBS) - MAX_JOBS]:
                del JOBS[k]
    return job


def job_view(job, state, done):
    return {"id": job["id"], "state": state, "progress": {"done": done, "total": job["total"]},
            "started_at": job["started_at"], "finished_at": job["finished_at"], "error": None}


def poll(doc, inp):
    jid = inp.get("job_id")
    if not isinstance(jid, str) or not jid:
        raise bad("input.job_id is required")
    with JOBS_LOCK:
        job = JOBS.get(jid)
        # Jobs are scoped to the tenant (from auth) and to the document.
        if job is None or job["scope"] != SCOPE or job["doc"] != doc:
            raise TaskError(404, "unknown_job", f"no job {jid} for this document")
        job["polls"] += 1
        if JOB_SECONDS <= 0:
            done = job["total"]
        else:
            by_time = int((time.monotonic() - job["t0"]) * job["total"] / JOB_SECONDS)
            done = min(job["total"], max(job["polls"], by_time))
        if done >= job["total"] and job["finished_at"] is None:
            job["finished_at"] = iso(_dt.datetime.now(_dt.timezone.utc))
        finished = job["finished_at"] is not None
    state = "done" if finished else "running"
    body = envelope(job["task"], job["handler"], job_view(job, state, job["total"] if finished else done),
                    job["result"] if finished else
                    {"summary": f"{done} of {job['total']} run(s) done"},
                    job["change"] if finished else None, job["hits"] if finished else [])
    # Stale base: the change still carries the version the job READ. The host
    # compares it with the document it holds and decides; the middleware does
    # not hold the document, so it cannot know which version is current.
    return body


def envelope(task, handler, job, result, change, hits):
    return {"api": API, "task": task, "handler": handler, "job": job, "result": result,
            "change": change, "trace": trace(hits)}


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def _has_scope(obj):
    return isinstance(obj, dict) and any(k in obj for k in ("scope", "tenant", "org", "org_id", "tenant_id"))


def handle(body: dict) -> dict:
    if not isinstance(body, dict):
        raise bad("the body must be a JSON object")
    if body.get("request_kind") != "middleware":
        raise bad("request_kind must be 'middleware'")
    payload = body.get("payload")
    if not isinstance(payload, dict):
        raise bad("payload must be an object")
    inp = payload.get("input", {})
    if not isinstance(inp, dict):
        raise bad("payload.input must be an object")
    clan = body.get("clan")
    # M3: scope comes from auth. A request that tries to name it is refused, so
    # a caller can never widen what it reads.
    if _has_scope(body) or _has_scope(payload) or _has_scope(inp) or _has_scope(clan):
        raise bad("scope is derived from auth and must not appear in the request (M3)")
    task = payload.get("task")
    if not isinstance(task, str) or not task:
        raise bad("payload.task is required")
    if task not in TASKS:
        raise bad(f"unknown task '{task}'", "unknown_task")
    if not isinstance(clan, dict) or not isinstance(clan.get("id"), str) or not clan["id"]:
        raise bad("clan.id is required: a change is computed for one document")
    doc = clan["id"]
    if task == "job_status":
        return poll(doc, inp)
    base = clan.get("version")
    if not isinstance(base, (str, int)) or isinstance(base, bool) or base == "":
        raise bad("clan.version is required: a change records the version it read")
    handler = resolve_handler(task, clan)
    t0 = iso(_dt.datetime.now(_dt.timezone.utc))
    if task == "extract_ask":
        result, change, hits = run_extract(doc, base, clan, inp, handler)
        job = {"id": "job_" + _digest(SCOPE, doc, base, task, inp, next(_SEQ)).hex()[:20], "state": "done",
               "progress": {"done": 1, "total": 1}, "started_at": t0,
               "finished_at": iso(_dt.datetime.now(_dt.timezone.utc)), "error": None}
        return envelope(task, handler, job, result, change, hits)
    if task == "research_lens":
        result, change, hits, total = run_research(doc, base, clan, inp, handler)
    else:
        result, change, hits, total = run_synthesis(doc, base, clan, inp, handler)
    job = submit(task, handler, doc, base, result, change, hits, total, inp)
    return envelope(task, handler, job_view(job, "queued", 0),
                    {"summary": f"queued: {total} run(s)"}, None, [])


def log(msg):
    print(f"[mock-middleware] {msg}", file=sys.stderr, flush=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "napkin-middleware"
    sys_version = ""

    def log_message(self, fmt, *args):  # request lines only; bodies are never logged
        pass

    def _send(self, status, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _err(self, status, etype, message):
        self._send(status, {"error": {"type": etype, "message": message}})

    def do_GET(self):
        if self.path.split("?")[0] == "/healthz":
            return self._send(200, {"ok": True, "api": API})
        self._err(404, "not_found", "no such route")

    def do_POST(self):
        if self.path.split("?")[0] != "/v1/tasks":
            return self._err(404, "not_found", "no such route")
        if TOKEN:
            auth = self.headers.get("Authorization", "")
            given = auth[7:] if auth.lower().startswith("bearer ") else self.headers.get("x-api-key", "")
            if given != TOKEN:
                return self._err(401, "unauthenticated", "missing or wrong credentials")
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._err(400, "invalid_input", "bad Content-Length")
        if n <= 0 or n > MAX_BODY:
            return self._err(400, "invalid_input", "empty or oversized body")
        raw = self.rfile.read(n)
        if DUMP_DIR:
            # Opt-in only: bodies carry client-confidential material.
            p = Path(DUMP_DIR)
            p.mkdir(parents=True, exist_ok=True)
            (p / f"{time.time_ns()}.json").write_bytes(raw)
        task = "?"
        try:
            body = json.loads(raw)
            task = (body.get("payload") or {}).get("task", "?") if isinstance(body, dict) else "?"
            out = handle(body)
            log(f"200 {task} {out['handler']} job={out['job']['state']}")
            self._send(200, out)
        except json.JSONDecodeError:
            self._err(400, "invalid_input", "the body is not JSON")
        except TaskError as e:
            log(f"{e.status} {task} {e.etype}")
            self._err(e.status, e.etype, e.message)
        except Exception as e:  # never a 200 with a fallback
            log(f"500 {task} {type(e).__name__}")
            self._err(500, "internal", "the middleware failed to handle this task")


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    log(f"{API} on http://{HOST}:{PORT}/v1/tasks  scope={SCOPE['org']} {SCOPE['brand']}  "
        f"job_seconds={JOB_SECONDS}  auth={'token' if TOKEN else 'open (dev)'}"
        + (f"  DUMPING BODIES to {DUMP_DIR}" if DUMP_DIR else ""))
    log("usage is reported as zero: no model runs here")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
