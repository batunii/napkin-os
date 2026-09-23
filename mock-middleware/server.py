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
  MOCK_MIDDLEWARE_JOB_SECONDS  wall time a long job takes to finish (2.0); start_campaign
                               spreads it over its six stages, at least one per poll
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
    "start_campaign": {"task": "start_campaign", "versions": {1: "1.0"}},
    "answer_question": {"task": "answer_question", "versions": {1: "1.0"}},
    "compose_report": {"task": "compose_report", "versions": {1: "1.0"}},
}
# Used only when the document carries no pipeline (the declared built-in map).
BUILTIN_PIPELINE = {
    "extract_ask": "extract_ask@1",
    "research_lens": "research_lens@1",
    "synthesise_findings": "synthesise_findings@1",
    "start_campaign": "start_campaign@1",
    "answer_question": "answer_question@1",
    "compose_report": "compose_report@1",
}
TASKS = set(BUILTIN_PIPELINE) | {"job_status"}
LONG_TASKS = {"research_lens", "synthesise_findings", "start_campaign"}

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
                    ref, nm = brand_ref(nm)
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


def brand_ref(name: str):
    """A brand name -> (ref, name), through the brand layer's roster when it
    knows the brand (a product of a roster brand, "Lúnasa 0.0", is that brand)."""
    name = name.strip().rstrip(".,;:")
    for ref, row in sorted(ROSTER.items(), key=lambda kv: -len(kv[1]["name"])):
        if slug(name) == slug(row["name"]) or slug(name).startswith(slug(row["name"]) + "-"):
            return ref, row["name"]
    return "brand/" + slug(name), name


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


def run_extract(doc, base, clan, inp, handler, skip=(), did=None, action="extract_ask"):
    """`skip`: fields another stage owns (start_campaign's identify writes brand,
    client_org and categories); they are neither written nor listed as abstained."""
    data = ctx_data(clan)
    facts = ctx_facts(clan)
    decisions = ctx_decisions(clan)
    mats, unread = build_materials(inp, data)
    if not mats:
        raise bad("nothing to read: input.prompt is empty and no attachment carries text")
    did = did or uid("d_", doc, base, "extract", [m.sha for m in mats])
    subject = field_value(data, "brand")
    subject_name = subject.get("name") if isinstance(subject, dict) else None
    cands, notes = extract_candidates(mats, subject_name)
    for f in skip:
        cands.pop(f, None)

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

    for f in skip:
        proposed.pop(f, None)
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
                 and f not in withheld and f not in existing and f not in skip]
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
    dec = decision(doc, did, "edit", handler, action, rationale,
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
              "materials_unread": unread, "materials_new": sorted(new_mats)}
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


def run_research(doc, base, clan, inp, handler, pairs=None):
    """`pairs`: when given, only these (lens, market) runs (start_campaign's
    research stage runs the pairs its select stage chose)."""
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
            if pairs is not None and (lens, market) not in pairs:
                continue
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
        vals = [by_market[m][lens] for m in markets if lens in by_market.get(m, {})]
        if not vals:
            continue  # not run in any market (skipped): no coverage
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
# The chat intake — start_campaign, answer_question, compose_report (§8)
# ---------------------------------------------------------------------------
# One start_campaign job runs six stages. Each stage's output is a "chunk": a
# slice of a change (patch, read-set, pins, findings, decisions) plus the agent
# message it narrates. A chunk is computed once and cached, so a repeat is
# byte-identical and carries the same decision ids (the host skips a field
# whose decisions are all in its chain, §5). A reply carries every finished
# chunk whose decisions the request's `clan.decision_chain` does not hold yet
# (§8.4): what was not delivered, or did not land, goes again.

STAGES = ["extract", "identify", "select", "research", "synthesise", "report"]

# The brand layer, stood in by a fixture roster. A row gives the brand's
# category leaves (ranked, at most two) and the client that holds it. Every
# name and brand here is invented. A roster pin the document already holds for
# the subject brand wins over this table.
ROSTER = {
    "brand/lunasa": {"name": "Lúnasa", "categories": ["drinks.cider", "drinks.no_low_alcohol"],
                     "client_org": ("org/glenmore-drinks", "Glenmore Drinks")},
    "brand/brightwater": {"name": "Brightwater 0.0", "categories": ["drinks.no_low_alcohol", "drinks.beer"],
                          "client_org": ("org/brightwater-brewing", "Brightwater Brewing")},
    "brand/kestrel-press": {"name": "Kestrel Press", "categories": ["drinks.cider"],
                            "client_org": ("org/kestrel-cider-co", "Kestrel Cider Co")},
    "brand/oakfield": {"name": "Oakfield Dairy", "categories": ["food.dairy"],
                       "client_org": ("org/oakfield-foods", "Oakfield Foods")},
    "brand/crunchwell": {"name": "Crunchwell", "categories": ["food.snacks"],
                         "client_org": ("org/oakfield-foods", "Oakfield Foods")},
    "brand/tidewater-bank": {"name": "Tidewater Bank", "categories": ["finance.banking"],
                             "client_org": ("org/tidewater-financial", "Tidewater Financial")},
    "brand/tidewater-cover": {"name": "Tidewater Cover", "categories": ["finance.insurance"],
                              "client_org": ("org/tidewater-financial", "Tidewater Financial")},
}
# The leaves the stand-in can classify into, each with the words that signal
# it. Used only to rank at most two CANDIDATES for the person to pick from when
# the brand has no roster row — never to write a category.
TAXONOMY = {
    "drinks.cider": [r"\bciders?\b"],
    "drinks.no_low_alcohol": [r"\balcohol[- ]free\b", r"\bnon[- ]alcoholic\b", r"\bno/low\b", r"\blow[- ]alcohol\b",
                              r"\b0\.0\b", r"\bzero[- ]alcohol\b"],
    "drinks.beer": [r"\bbeers?\b", r"\blagers?\b", r"\bstouts?\b", r"\bales?\b"],
    "drinks.spirits": [r"\bgin\b", r"\bwhiske?y\b", r"\bvodka\b", r"\brum\b", r"\bspirits?\b"],
    "drinks.soft_drinks": [r"\bsoft drinks?\b", r"\bsodas?\b", r"\blemonade\b", r"\bfizzy\b"],
    "drinks.mixers": [r"\bmixers?\b", r"\btonics?\b"],
    "food.dairy": [r"\bdairy\b", r"\byogh?urts?\b", r"\bcheeses?\b", r"\bmilk\b", r"\bbutter\b"],
    "food.snacks": [r"\bsnacks?\b", r"\bcrisps\b", r"\bchips\b"],
    "food.confectionery": [r"\bchocolates?\b", r"\bsweets\b", r"\bconfectionery\b"],
    "finance.banking": [r"\bbank(?:ing)?\b", r"\bcurrent accounts?\b", r"\bmortgages?\b", r"\bsavings\b"],
    "finance.insurance": [r"\binsurance\b", r"\binsurer\b", r"\bpolic(?:y|ies)\b"],
    "retail.grocery": [r"\bsupermarkets?\b", r"\bgrocer(?:y|ies)\b"],
}
# Verticals whose advertising carries category rules the clearance lens reads.
REGULATED_VERTICALS = {"drinks", "food", "finance", "health", "gambling"}
LENS_WORDS = {
    "market_structure": r"\bmarket (?:size|structure|share|landscape)\b|\blandscape\b|\bcategory growth\b",
    "brands_positioning": r"\bcompetit(?:ors?|ion|ive)\b|\bpositioning\b|\bbrand health\b|\bawareness\b|\bcomparators?\b",
    "consumer_culture": r"\baudiences?\b|\bconsumers?\b|\bculture\b|\bshoppers?\b",
    "category_codes": r"\b(?:category|visual) codes\b|\bcodes\b|\bsemiotics?\b",
    "rhythm_moments": r"\btiming\b|\bmoments?\b|\bseasonality\b|\bcalendar\b|\brhythm\b",
    "media_spend": r"\bmedia\b|\bad ?spend\b|\bshare of voice\b",
    "regulation_clearance": r"\bregulat\w*\b|\bclearance\b|\bcompliance\b|\blegal\b",
    "effectiveness_evidence": r"\beffectiveness\b|\bcase stud(?:y|ies)\b|\bcases\b",
}
LENS_TITLES = {
    "market_structure": "The market", "brands_positioning": "Brands and comparators",
    "consumer_culture": "Who it is for", "category_codes": "Category codes",
    "rhythm_moments": "Timing", "media_spend": "Media", "regulation_clearance": "Clearance",
    "effectiveness_evidence": "Effectiveness",
}
MARKET_NAMES = {"IE": "Ireland", "GB": "GB", "US": "the US", "FR": "France", "DE": "Germany", "ES": "Spain",
                "IT": "Italy", "NL": "the Netherlands", "BE": "Belgium", "PT": "Portugal", "PL": "Poland",
                "SE": "Sweden", "DK": "Denmark", "NO": "Norway", "FI": "Finland", "AT": "Austria",
                "CH": "Switzerland", "CA": "Canada", "AU": "Australia", "NZ": "New Zealand"}
FIELD_LABELS = {"brand": "Brand", "client_org": "Client", "categories": "Categories", "markets": "Markets",
                "competitor_set": "Comparators", "audience": "The researched audience",
                "in_market": "In market", "constraints": "Constraints", "campaign_type": "Campaign type"}


def market_list(codes):
    names = [MARKET_NAMES.get(c, c) for c in codes]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1] if names else "no market"


def opt_id(text: str) -> str:
    return (re.sub(r"[^a-z0-9]+", "_", slug(text)).strip("_") or "opt")[:40]


def ulid_like(prefix: str, seq: int, *seed) -> str:
    """msg_<time><seed><seq>: sorts in creation order (display order is `at`, then key)."""
    ms = int(now().timestamp() * 1000)
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    t = ""
    for _ in range(10):
        t = alphabet[ms % 32] + t
        ms //= 32
    return f"{prefix}{t}{uid('', *seed, n=4)}{seq:03d}"


def canon_sha(obj) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


# -- addresses and read-sets for staged writes ---------------------------------

def field_paths(patch: dict) -> list:
    """The fields a data_patch writes, at the granularity the host judges them:
    campaign.<f>, selection.<k>, materials.<id>, intake.messages.<id>, report."""
    out = []
    for top, sub in patch.items():
        if top == "intake" and isinstance(sub, dict) and isinstance(sub.get("messages"), dict):
            out += [f"intake.messages.{k}" for k in sub["messages"]]
        elif top in ("campaign", "selection", "materials") and isinstance(sub, dict) and sub:
            out += [f"{top}.{k}" for k in sub]
        else:
            out.append(top)
    return out


def address(path: str) -> str:
    """Patch path -> address path: map keys go in brackets (Contract 3 §2.4)."""
    for m in ("intake.messages.", "materials."):
        if path.startswith(m):
            return f"{m[:-1]}[{path[len(m):]}]"
    return path


def get_dotted(data, dotted):
    for k in dotted.split("."):
        if not isinstance(data, dict) or k not in data:
            return None
        data = data[k]
    return copy.deepcopy(data)


def read_of(data: dict, patch: dict) -> dict:
    return {p: get_dotted(data, p) for p in field_paths(patch)}


def deep_merge(a: dict, b: dict) -> dict:
    """Merge two data_patches (b after a) into one merge patch."""
    out = copy.deepcopy(a)
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and v:
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def apply_patch(target, patch):
    """RFC 7396 merge patch."""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    out = copy.deepcopy(target) if isinstance(target, dict) else {}
    for k, v in patch.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = apply_patch(out.get(k), v)
    return out


def known_ids(clan: dict) -> set:
    """Decision ids the document holds, with those a contest withheld (§4)."""
    ids = set()
    for d in ctx_decisions(clan):
        ids.add(d.get("id"))
        for h in d.get("withheld") or []:
            ids.add(h.get("id") if isinstance(h, dict) else h)
    return ids


# -- the job ---------------------------------------------------------------------

class Chunk:
    """One stage's (part of a) change, computed once."""

    def __init__(self, stage, base, patch, read, facts=(), findings=(), decisions=(), message=None):
        self.stage, self.base = stage, base
        self.patch, self.read = patch, read
        self.facts, self.findings, self.decisions = list(facts), list(findings), list(decisions)
        self.message = message  # {id, text, stage}
        self.ids = {d["id"] for d in self.decisions}


def combine(doc, chunks) -> dict | None:
    if not chunks:
        return None
    patch, read, facts, findings, decs = {}, {}, [], [], []
    for c in chunks:
        patch = deep_merge(patch, c.patch)
        for k, v in c.read.items():
            read.setdefault(k, v)  # what the EARLIEST writer of the path read
        facts += [f for f in c.facts if f["id"] not in {x["id"] for x in facts}]
        findings += [f for f in c.findings if f["id"] not in {x["id"] for x in findings}]
        decs += [d for d in c.decisions if d["id"] not in {x["id"] for x in decs}]
    return {"doc": doc, "base_version": chunks[0].base, "read": read if patch else {}, "data_patch": patch,
            "facts_append": facts, "findings_append": findings, "decisions": decs}


class CampaignJob:
    def __init__(self, jid, doc, handler, clan, inp):
        self.id, self.doc, self.handler = jid, doc, handler
        self.scope = dict(SCOPE)
        self.inp = inp            # {prompt, attachments} as extract reads them
        self.lock = threading.Lock()
        self.t0 = time.monotonic()
        self.next_due = self.t0
        self.per_stage = JOB_SECONDS / len(STAGES) if JOB_SECONDS > 0 else 0
        self.started_at = iso(_dt.datetime.now(_dt.timezone.utc))
        self.finished_at = None
        self.state = "queued"
        self.stage_idx = 0        # index of the stage being worked on
        self.question = None
        self.error = None
        self.chunks: list[Chunk] = []
        self.last_change = None
        self.seq = 0
        self.answers = {}         # field -> the value the person picked
        self.pending_text = None  # {field, text}: a free-text answer to resolve
        self.brands = None        # brands found in the material
        self.selected = None      # (pairs, skipped)
        self.hits = []
        self.W = {}; self.W_facts = []; self.W_findings = []; self.W_version = None
        self.sync(clan)

    # the job's working copy of the document: the request's clan, plus any
    # chunk of ours that has not landed there yet
    def sync(self, clan):
        known = known_ids(clan)
        W = copy.deepcopy(ctx_data(clan))
        facts = copy.deepcopy(ctx_facts(clan))
        findings = copy.deepcopy(ctx_findings(clan))
        for c in self.chunks:
            if c.ids <= known:
                continue
            for p in field_paths(c.patch):
                if get_dotted(W, p) is None:
                    W = apply_patch(W, self._sub(c.patch, p))
            facts += [f for f in c.facts if f["id"] not in {x["id"] for x in facts}]
            findings += [f for f in c.findings if f["id"] not in {x["id"] for x in findings}]
        camp = W.setdefault("campaign", {})
        for f, v in self.answers.items():
            if f not in camp:  # the person answered; the view's write has not reached us
                camp[f] = {"value": v, "origin": "confirmed", "gate": GATES[f], "decision": "d_PENDINGANSWER"}
        self.W, self.W_facts, self.W_findings, self.W_version = W, facts, findings, clan.get("version")
        self.clan = clan

    @staticmethod
    def _sub(patch, path):
        parts = path.split(".")
        node = patch
        for p in parts:
            node = node[p]
        out = node
        for p in reversed(parts):
            out = {p: out}
        return out

    def wclan(self):
        return {"id": self.doc, "version": self.W_version, "data": self.W, "facts": self.W_facts,
                "findings": self.W_findings, "decision_chain": self.clan.get("decision_chain") or {}}

    def mint_msg(self, stage, text, question=None):
        self.seq += 1
        mid = ulid_like("msg_", self.seq, self.doc, self.id, stage)
        m = {"role": "agent", "text": text, "at": iso(now()), "job_id": self.id, "stage": stage}
        if question:
            m["question"] = question
        return mid, m

    def did(self, *parts):
        return uid("d_", self.doc, self.id, *parts)

    def add_chunk(self, stage, patch, decisions, facts=(), findings=(), text=None, question=None,
                  msg_decision=None, base=None, read_from=None):
        """Record a stage's output. The message goes into the patch under
        intake.messages.<id>, and is named by `msg_decision` (or, when None, the
        first decision of the chunk)."""
        patch = copy.deepcopy(patch)
        message = None
        if text:
            mid, m = self.mint_msg(stage, text, question)
            patch.setdefault("intake", {}).setdefault("messages", {})[mid] = m
            message = {"id": mid, "text": text, "stage": stage}
            tgt = f"{self.doc}#intake.messages[{mid}]"
            owner = msg_decision or (decisions[0] if decisions else None)
            if owner is None:
                owner = decision(self.doc, self.did(stage, "narrate", self.seq), "edit", self.handler, "narrate",
                                 f"The {stage} stage's chat message.", [])
                decisions = list(decisions) + [owner]
            elif owner not in decisions:
                decisions = list(decisions) + [owner]
            owner["targets"].append(tgt)
            owner.setdefault("fields_changed", [])
            if "intake.messages" not in owner["fields_changed"]:
                owner["fields_changed"].append("intake.messages")
        # every field the patch writes is named by a decision's targets (§3)
        for p in field_paths(patch):
            a = f"{self.doc}#{address(p)}"
            if not any(a == t or t.startswith(a + "[") for d in decisions for t in d["targets"]):
                decisions[0]["targets"].append(a)
        c = Chunk(stage, base if base is not None else self.W_version, patch,
                  read_of(read_from if read_from is not None else self.W, patch), facts, findings, decisions, message)
        self.chunks.append(c)
        # the working copy moves on as if it had landed
        self.W = apply_patch(self.W, patch)
        self.W_facts += [f for f in c.facts if f["id"] not in {x["id"] for x in self.W_facts}]
        self.W_findings += [f for f in c.findings if f["id"] not in {x["id"] for x in self.W_findings}]
        return c

    def messages(self):
        return [c.message for c in self.chunks if c.message]

    # -- advancing ------------------------------------------------------------
    def advance(self, clan, only_identify=False):
        self.sync(clan)
        if self.state in ("queued", "running"):
            self.state = "running"
            ran = 0
            while self.stage_idx < len(STAGES):
                if ran and time.monotonic() < self.next_due:
                    break
                stage = STAGES[self.stage_idx]
                if only_identify and stage != "identify":
                    break
                if stage == "report":
                    earlier = set().union(*(c.ids for c in self.chunks)) if self.chunks else set()
                    if not earlier <= known_ids(clan):
                        break  # composes once the earlier stages have landed (§8.4)
                try:
                    finished = getattr(self, "stage_" + stage)()
                except TaskError as e:
                    self.fail(stage, e.etype, e.message)
                    break
                except Exception as e:  # never a silent fallback
                    self.fail(stage, "internal", f"the {stage} stage failed ({type(e).__name__})")
                    break
                ran += 1
                if not finished:  # needs_input
                    self.state = "needs_input"
                    break
                self.stage_idx += 1
                self.next_due = time.monotonic() + self.per_stage
            if self.stage_idx >= len(STAGES) and self.state == "running":
                self.state = "done"
                self.finished_at = iso(_dt.datetime.now(_dt.timezone.utc))
        known = known_ids(clan)
        pending = [c for c in self.chunks if not c.ids <= known]
        change = combine(self.doc, pending)
        if change is None and self.state == "done":
            change = self.last_change  # a done job's later polls return the same change again
        if change is not None:
            self.last_change = change
        return change

    def fail(self, stage, etype, message):
        self.state = "failed"
        self.error = {"type": etype, "message": message}
        self.finished_at = iso(_dt.datetime.now(_dt.timezone.utc))
        d = decision(self.doc, self.did(stage, "failed"), "edit", self.handler, "stage_failed",
                     f"The {stage} stage failed: {message}. What landed before it stays.", [])
        self.add_chunk(stage, {}, [d], text=f"The {stage} stage failed: {message}. What already landed stays.")

    def view(self):
        return {"id": self.id, "state": self.state,
                "progress": {"done": min(self.stage_idx, len(STAGES)), "total": len(STAGES)},
                "stage": STAGES[min(self.stage_idx, len(STAGES) - 1)],
                "question": self.question if self.state == "needs_input" else None,
                "started_at": self.started_at, "finished_at": self.finished_at, "error": self.error}

    def summary(self):
        if self.state == "needs_input":
            return f"Waiting for you: {self.question['text']}"
        if self.state == "done":
            return "Campaign ready: the report is in the document."
        if self.state == "failed":
            return f"Failed at {self.view()['stage']}: {self.error['message']}"
        return f"{self.view()['stage']}: {self.stage_idx} of {len(STAGES)} stage(s) done"

    # -- stages -----------------------------------------------------------------
    def stage_extract(self):
        did = self.did("extract")
        result, change, hits = run_extract(self.doc, self.W_version, self.wclan(), self.inp, self.handler,
                                           skip={"brand", "client_org", "categories"}, did=did, action="extract")
        self.hits += hits
        dec = change["decisions"][0]
        dec["targets"] = [t for t in dec["targets"] if t != f"{self.doc}#campaign"]
        for mid in result.get("materials_new", []):
            dec["targets"].append(f"{self.doc}#materials[{mid}]")
        filled = result["fields"]
        names = ", ".join(f.replace("_", " ") for f in filled) or "nothing"
        text = (f"Read {len(result['materials_read'])} material(s). Filled from what they say: {names}."
                + (f" Left open, because nothing supports them: {', '.join(a.replace('_', ' ') for a in result['abstained'])}."
                   if result["abstained"] else ""))
        if result["materials_unread"]:
            text += f" {len(result['materials_unread'])} attachment(s) had no readable text and ground nothing."
        self.add_chunk("extract", change["data_patch"], [dec], text=text)
        return True

    def materials(self):
        mats, _ = build_materials(self.inp, self.W)
        return mats

    def detect_brands(self):
        """Brands the material names, in order: {ref, name, span, comparator, how}."""
        if self.brands is not None:
            return self.brands
        mats = self.materials()
        found, order = {}, []
        roster_names = sorted(((r["name"], ref) for ref, r in ROSTER.items()), key=lambda x: -len(x[0]))

        def add(name, mat, s, e, how, comparator=False):
            ref, name = brand_ref(name)
            if not slug(name) or slug(name) in ("we", "the", "our", "it", "i"):
                return
            if ref not in found:
                found[ref] = {"ref": ref, "name": name, "span": mat.span(s, e), "comparator": comparator, "how": how}
                order.append(ref)
            else:
                found[ref]["comparator"] = found[ref]["comparator"] or comparator

        cues = [
            (re.compile(rf"(?P<n>{NAME})\s+(?:as|is)\s+(?:the\s+)?(?:one|brand|competitor|rival)\s+to\s+beat"), True),
            (re.compile(rf"(?i:competitors?|rivals?|competing with|compete with|up against|versus|vs\.?)"
                        rf"\s*(?:(?i:is|are|include|includes|like)\s+|:\s*)?(?P<l>{NAME}(?:\s*(?:,|and|&)\s*{NAME})*)"), True),
            (re.compile(rf"(?i:brands?)(?:\s+(?i:in this brief|named|involved|here))?\s*(?::|(?i:are|is))\s*"
                        rf"(?P<l>{NAME}(?:\s*(?:,|and|&)\s*{NAME})*)"), False),
            (re.compile(rf"(?i:(?:re)?launch(?:ing|es)?|introducing)\s+(?P<n>{NAME})"), False),
        ]
        for mat in mats:
            for para, s, e in mat.sentences:
                sent = mat.text[s:e]
                for rn, rref in roster_names:
                    for m in re.finditer(rf"(?<![\w]){re.escape(rn)}(?![\w])", sent, re.I):
                        add(rn, mat, s, e, "roster")
                for rx, comp in cues:
                    for m in rx.finditer(sent):
                        blob = m.groupdict().get("n") or m.groupdict().get("l") or ""
                        for nm in re.split(r"\s*(?:,|\band\b|&)\s*", blob):
                            if nm.strip():
                                add(nm, mat, s, e, "cue", comp)
        self.brands = [found[r] for r in order]
        return self.brands

    def subject_claim(self):
        """The subject brand when the material says clearly which is the client's."""
        mats = self.materials()
        for mat in mats:  # a Brand: label
            m = re.search(r"^[ \t]*Brand[ \t]*:[ \t]*(?P<b>[^\n]{2,60}?)[ \t]*$", mat.text, re.M)
            if m and slug(m.group("b")):
                return self._as_brand(m.group("b"), mat.span(m.start("b"), m.end("b")))
        for mat in [m for m in mats if m.kind == "prompt"]:  # named in the prompt as ours / the client's
            for rx in [rf"\b(?i:our|the)\s+client(?:['’]s brand)?\s*(?:is|,|:)?\s+(?P<b>{NAME})",
                       rf"(?P<b>{NAME})\s+is\s+(?:our|the)\s+client(?:['’]s)?\b",
                       rf"\b(?i:our|my)\s+brand\s*(?:is|,|:)?\s+(?P<b>{NAME})",
                       rf"\b(?i:for)\s+our\s+client\s+(?P<b>{NAME})"]:
                m = re.search(rx, mat.text)
                if m:
                    return self._as_brand(m.group("b"), mat.span(m.start("b"), m.end("b")))
        brands = self.detect_brands()
        if len(brands) == 1 and not brands[0]["comparator"]:
            b = brands[0]  # the only brand in the material, and not named as a comparator
            return {"ref": b["ref"], "name": b["name"], "span": b["span"]}
        return None

    def _as_brand(self, name, span):
        ref, name = brand_ref(name)
        return {"ref": ref, "name": name, "span": span}

    def category_candidates(self, texts):
        """Ranked at most two leaves by keyword hits: (leaf, span|None)."""
        score = {}
        for mat, s, e, sent in texts:
            for leaf, pats in TAXONOMY.items():
                n = sum(len(re.findall(p, sent, re.I)) for p in pats)
                if n:
                    sc = score.setdefault(leaf, [0, len(score), mat.span(s, e) if mat else None])
                    sc[0] += n
        ranked = sorted(score.items(), key=lambda kv: (-kv[1][0], kv[1][1]))[:2]
        return [(leaf, v[2]) for leaf, v in ranked]

    def ask(self, field, text, options, allow_text, intro, decisions=None, patch=None, facts=()):
        qid = uid("q_", self.doc, self.id, field, self.seq + 1, n=12)
        q = {"id": qid, "text": text, "options": options, "allow_text": allow_text,
             "address": f"{self.doc}#campaign.{field}"}
        self.question = q
        d = decision(self.doc, self.did("ask", qid), "edit", self.handler, "identify",
                     f"Asked the person ({field}): {text} Research waits; nothing is guessed.", [],
                     [o["source"]["material_id"] for o in options if o.get("source")]
                     + [f for o in options for f in o.get("fact_ids", [])])
        decs = list(decisions or []) + [d]
        self.add_chunk("identify", patch or {}, decs, facts=facts, text=f"{intro} {text}".strip(),
                       question=q, msg_decision=d)
        return False

    def stage_identify(self):
        camp = self.W.get("campaign") or {}
        mats = self.materials()
        patch, decs, facts, notes = {"campaign": {}}, [], [], []
        did = self.did("identify", len(self.chunks))
        idec = decision(self.doc, did, "edit", self.handler, "identify", "", [], [])

        def write(field, env):
            patch["campaign"][field] = dict(env, gate=GATES[field], decision=did)
            idec["targets"].append(f"{self.doc}#campaign.{field}")
            idec.setdefault("fields_changed", []).append(f"campaign.{field}")

        def flush():
            idec["rationale"] = " ".join(notes) or "Nothing settled from the material yet."
            p = patch if patch["campaign"] else {}
            ds = ([idec] if idec["targets"] else []) + decs
            return p, ds

        # 1. the subject brand ----------------------------------------------------
        brand = (camp.get("brand") or {}).get("value")
        if not brand:
            pt = self.pending_text if (self.pending_text or {}).get("field") == "brand" else None
            claim = None if pt else self.subject_claim()
            if claim:
                brand = {"ref": claim["ref"], "name": claim["name"]}
                write("brand", {"value": brand, "origin": "extracted", "source": claim["span"]})
                idec["cites"].append(claim["span"]["material_id"])
                notes.append(f"{claim['name']} is the client's brand (read from the material).")
            else:
                self.pending_text = None
                if pt:
                    opts, found = self.brand_from_text(pt["text"])
                    intro = (f"From \"{pt['text']}\":" if found else
                             f"\"{pt['text']}\" does not name a brand I can use.")
                    p, ds = flush()
                    return self.ask("brand", "Which brand is the client's?" if found else
                                    "Which brand is the client's? Type its name.", opts, True, intro, ds, p, facts)
                brands = self.detect_brands()
                p, ds = flush()
                if brands:
                    opts = [{"id": opt_id(b["name"]), "label": b["name"], "value": {"ref": b["ref"], "name": b["name"]},
                             "origin": "extracted", "source": b["span"]} for b in brands]
                    opts = list({o["id"]: o for o in opts}.values())
                    opts.append({"id": "none", "label": "None of these"})
                    names = [b["name"] for b in brands]
                    intro = (f"The material names {' and '.join(names) if len(names) < 3 else ', '.join(names)}"
                             f"{'' if len(names) > 1 else ', as a comparator'}, and does not say which is the client's."
                             + (" The other is treated as a comparator." if len(names) == 2 else
                                " The others are treated as comparators." if len(names) > 2 else "")
                             + " Research waits for your answer.")
                    return self.ask("brand", "Which brand is the client's?", opts, True, intro, ds, p, facts)
                return self.ask("brand", "Which brand is this campaign for? Type its name.", [], True,
                                "I could not find the client's brand in the prompt or the material.", ds, p, facts)
        subject_ref = brand.get("ref")

        # 2. roster row: pinned already, or looked up in the brand layer -----------
        pinned = [f for f in self.W_facts if f.get("entity") == subject_ref and str(f.get("key", "")).startswith("roster.")
                  and f.get("status", "active") == "active"]
        row = ROSTER.get(subject_ref)
        if row and not any(f["key"].startswith("roster.categories") for f in pinned):
            facts = self.roster_pins(subject_ref, row)
            pin_did = facts[0]["decision"]
            decs.append(decision(self.doc, pin_did, "pin", self.handler, "lookup",
                                 f"Deterministic lookup of {row['name']}'s roster row in the brand layer: "
                                 f"{len(facts)} row(s) pinned.", [f"facts[{f['id']}]" for f in facts],
                                 [s for f in facts for s in f["sources"]]))
            self.hits += [{"id": f["id"], "scope": "brand", "source": f["origin"]} for f in facts]
            pinned = pinned + facts
        cat_pins = sorted([f for f in pinned if f["key"] in ("roster.categories.primary", "roster.categories.secondary")
                           and re.fullmatch(r"[a-z0-9_]+\.[a-z0-9_]+", str(f.get("value")))],
                          key=lambda f: f["key"] != "roster.categories.primary")
        org_pin = next((f for f in pinned if f["key"] == "roster.client_org"
                        and re.fullmatch(r"org/[a-z0-9][a-z0-9-]*", str(f.get("value")))), None)

        # 3. categories --------------------------------------------------------------
        if not camp.get("categories"):
            if cat_pins:
                why = human_owned(self.W, ctx_decisions(self.clan), self.doc, "categories")
                if not why:
                    vals = list(dict.fromkeys(f["value"] for f in cat_pins))[:2]
                    write("categories", {"value": vals, "origin": "proposed", "fact_ids": [f["id"] for f in cat_pins]})
                    idec["cites"] += [f["id"] for f in cat_pins]
                    notes.append(f"Categories from the roster row: {', '.join(vals)} (proposed, to confirm).")
            else:
                pt = self.pending_text if (self.pending_text or {}).get("field") == "categories" else None
                self.pending_text = None
                p, ds = flush()
                if pt:
                    cands = self.category_candidates([(None, 0, 0, pt["text"])])
                    typed = [l for l in re.findall(r"\b[a-z0-9_]+\.[a-z0-9_]+\b", pt["text"]) if l in TAXONOMY]
                    leaves = list(dict.fromkeys(typed + [c[0] for c in cands]))[:2]
                    opts = self.category_options([(l, None) for l in leaves], "stated")
                    if leaves:
                        return self.ask("categories", "Which category is it?", opts, True,
                                        f"From \"{pt['text']}\":", ds, p, facts)
                    return self.ask("categories", "Which category is it? Type a leaf such as drinks.cider.", [], True,
                                    f"\"{pt['text']}\" does not match a category leaf I know.", ds, p, facts)
                texts = [(m, s, e, m.text[s:e]) for m in mats for _, s, e in m.sentences]
                cands = self.category_candidates(texts)
                if cands:
                    return self.ask("categories", "Which category is it?", self.category_options(cands, "extracted"), True,
                                    f"{brand['name']} has no roster row, so its category is not known. "
                                    "The material points at these; pick one or say what it is.", ds, p, facts)
                return self.ask("categories", "Which category is it? Type it.", [], True,
                                f"{brand['name']} has no roster row and the material does not say its category.",
                                ds, p, facts)

        # 4. markets ---------------------------------------------------------------------
        if not camp.get("markets"):
            pt = self.pending_text if (self.pending_text or {}).get("field") == "markets" else None
            self.pending_text = None
            p, ds = flush()
            if pt:
                codes = self.markets_from_text(pt["text"])
                if codes:
                    opts = [{"id": "markets", "label": market_list(codes), "value": codes, "origin": "stated"},
                            {"id": "other", "label": "Something else"}]
                    return self.ask("markets", "Which markets is it for?", opts, True, f"From \"{pt['text']}\":", ds, p, facts)
                return self.ask("markets", "Which markets is it for? Name the countries.", [], True,
                                f"\"{pt['text']}\" does not name a country I can research.", ds, p, facts)
            return self.ask("markets", "Which markets is it for? Name the countries.", [], True,
                            "Nothing names a market, and research runs once per market.", ds, p, facts)

        # 5. the client ------------------------------------------------------------------
        if not camp.get("client_org") and not human_owned(self.W, ctx_decisions(self.clan), self.doc, "client_org"):
            if org_pin:
                ext, _ = extract_candidates(mats, brand.get("name"))
                e = ext.get("client_org")
                name = e["value"]["name"] if e and e["value"]["ref"] == org_pin["value"] else (
                    row["client_org"][1] if row and row["client_org"][0] == org_pin["value"] else
                    org_pin["value"][4:].replace("-", " ").title())
                write("client_org", {"value": {"ref": org_pin["value"], "name": name}, "origin": "proposed",
                                     "fact_ids": [org_pin["id"]]})
                idec["cites"].append(org_pin["id"])
                notes.append(f"Client: {name} (from the roster row, proposed).")
            else:
                ext, _ = extract_candidates(mats, brand.get("name"))
                if "client_org" in ext:
                    write("client_org", {"value": ext["client_org"]["value"], "origin": "extracted",
                                         "source": ext["client_org"]["span"]})
                    idec["cites"].append(ext["client_org"]["span"]["material_id"])
                    notes.append(f"Client: {ext['client_org']['value']['name']} (read from the material).")

        # 6. the subject is never its own comparator: a later stage rewriting an
        #    earlier stage's field (new decision, read = what extract wrote, §3)
        cs = camp.get("competitor_set")
        if cs and cs.get("origin") in ("extracted", "proposed") and \
                any(isinstance(c, dict) and c.get("ref") == subject_ref for c in cs.get("value") or []):
            keep = [c for c in cs["value"] if c.get("ref") != subject_ref]
            if keep:
                env = {k: v for k, v in cs.items() if k not in ("decision",)}
                env["value"] = keep
                if "item_provenance" in env:
                    env["item_provenance"] = {k: v for k, v in env["item_provenance"].items() if k != subject_ref}
                    if not env["item_provenance"]:
                        env.pop("item_provenance")
                write("competitor_set", env)
            else:
                patch["campaign"]["competitor_set"] = None
                idec["targets"].append(f"{self.doc}#campaign.competitor_set")
            notes.append(f"{brand['name']} removed from the comparators: it is the client's brand.")

        idec["rationale"] = " ".join(notes) or "Subject brand, categories and markets already settled."
        p, ds = flush()
        if not ds:
            ds = [decision(self.doc, did, "edit", self.handler, "identify", idec["rationale"], [])]
        text = " ".join(notes) or f"{brand['name']}: brand, categories and markets are settled."
        self.add_chunk("identify", p, ds, facts=facts, text=text)
        self.question = None
        return True

    def category_options(self, cands, origin):
        opts = []
        for leaf, span in cands:
            o = {"id": leaf.replace(".", "_"), "label": leaf, "value": [leaf], "origin": origin}
            if origin == "extracted":
                o["source"] = span
            opts.append(o)
        if len(cands) == 2:
            o = {"id": "both", "label": f"Both: {cands[0][0]} and {cands[1][0]}",
                 "value": [cands[0][0], cands[1][0]], "origin": origin}
            if origin == "extracted":
                o["source"] = cands[0][1]
            opts.append(o)
        opts.append({"id": "other", "label": "Something else"})
        return opts

    def brand_from_text(self, text):
        """Free text -> candidate brands (origin stated: the person's words). The
        last-resort candidate is the text itself as a new brand (§8.3)."""
        t = slug(text)
        opts, refs = [], set()
        if len(t) >= 2:
            pool = [(r, v["name"]) for r, v in ROSTER.items()] + [(b["ref"], b["name"]) for b in self.detect_brands()]
            for ref, name in pool:
                s = slug(name)
                if ref not in refs and (s == t or (len(t) >= 3 and (t in s or s in t))):
                    refs.add(ref)
                    opts.append({"id": opt_id(name), "label": name, "value": {"ref": ref, "name": name},
                                 "origin": "stated"})
            if "brand/" + t not in refs and not any(slug(o["value"]["name"]) == t for o in opts):
                name = text.strip()
                opts.append({"id": opt_id(name) if opt_id(name) not in {o["id"] for o in opts} else "typed",
                             "label": f"{name} (new brand)", "value": {"ref": "brand/" + t, "name": name},
                             "origin": "stated"})
        found = bool(opts)
        opts.append({"id": "none", "label": "None of these"})
        return opts, found

    def markets_from_text(self, text):
        hits = sorted((m.start(), code) for rx, code in COUNTRIES for m in re.finditer(rx, text))
        codes = [c for _, c in hits]
        codes += [c for c in re.findall(r"\b[A-Z]{2}\b", text) if c in ISO_3166]
        return list(dict.fromkeys(codes))

    def roster_pins(self, ref, row):
        today = now().date().isoformat()
        pin_did = self.did("roster", ref)
        src = lid("src_", SCOPE["org"], "roster", ref)
        rows = [("roster.categories.primary", row["categories"][0])]
        if len(row["categories"]) > 1:
            rows.append(("roster.categories.secondary", row["categories"][1]))
        rows.append(("roster.client_org", row["client_org"][0]))
        out = []
        for key, value in rows:
            out.append({"id": uid("f_", SCOPE["org"], ref, key, 1, value), "entity": ref, "key": key, "value": value,
                        "unit": "code", "as_of": today, "retrieved_at": today, "sources": [src],
                        "confidence": derive_confidence([{"tier": "mock"}]), "licence": "client-confidential",
                        "status": "active", "version": 1, "supersedes": None,
                        "origin": origin_uri("brand", ref, key, 1), "decision": pin_did,
                        "pinned_at": iso(now()),
                        "pin_reason": f"Roster row ({key}): mock brand-layer fixture, proposed for "
                                      f"campaign.{'categories' if 'categories' in key else 'client_org'}",
                        "layer": "brand", "method": "report"})
        return out

    def stage_select(self):
        camp = self.W.get("campaign") or {}
        markets = list((camp.get("markets") or {}).get("value") or [])
        cats = list((camp.get("categories") or {}).get("value") or [])
        prompt = next((m for m in self.materials() if m.kind == "prompt"), None)
        skip = {}  # (lens, market|None) -> reason
        if prompt:
            for _, s, e in prompt.sentences:
                sent = prompt.text[s:e].strip()
                lenses = [l for l, rx in LENS_WORDS.items() if re.search(rx, sent, re.I)]
                if not lenses:
                    continue
                named = [c for c in dict.fromkeys(code for rx, code in COUNTRIES for _ in re.finditer(rx, sent))
                         if c in markets]
                if re.search(r"\b(?:skip|ignore|leave out|no need for|don['’]t need|do not need|not interested in|"
                             r"without)\b", sent, re.I):
                    for l in lenses:
                        skip[(l, None)] = f"The prompt leaves it out (\"{sent}\")."
                elif re.search(r"\bonly\b", sent, re.I) and named:
                    for l in lenses:
                        for mk in markets:
                            if mk not in named:
                                skip[(l, mk)] = f"The prompt asks for it in {market_list(named)} only (\"{sent}\")."
                elif re.search(r"\b(?:only|just)\b", sent, re.I):
                    for l in LENSES:
                        if l not in lenses:
                            skip[(l, None)] = f"The prompt asks only for {', '.join(LENS_TITLES[x].lower() for x in lenses)} (\"{sent}\")."
        if cats and not any(c.split(".")[0] in REGULATED_VERTICALS for c in cats):
            skip.setdefault(("regulation_clearance", None),
                            f"No category advertising rules for {', '.join(cats)}: the clearance lens has nothing to read.")
        pairs = [(l, m) for l in LENSES for m in markets if (l, None) not in skip and (l, m) not in skip]
        skipped = [{"lens": l, **({"market": m} if m else {}), "reason": r} for (l, m), r in
                   sorted(skip.items(), key=lambda kv: (LENSES.index(kv[0][0]), markets.index(kv[0][1]) if kv[0][1] in markets else -1))]
        self.selected = (pairs, skipped)
        did = self.did("select")
        prior = [x for x in ((self.W.get("selection") or {}).get("lenses_skipped") or [])
                 if (x.get("lens"), x.get("market")) not in {(s["lens"], s.get("market")) for s in skipped}
                 and not any(x.get("lens") == l and x.get("market") in (None, m) for l, m in pairs)]
        for s in skipped:
            s["decision"] = did
        d = decision(self.doc, did, "edit", self.handler, "select",
                     f"Selected {len(pairs)} lens x market pair(s) for this prompt; skipped {len(skipped)}"
                     + (": " + "; ".join(f"{s['lens']}{'/' + s['market'] if 'market' in s else ''}" for s in skipped)
                        if skipped else "") + ".",
                     ["selection.lenses_skipped"] + [f"selection.lenses_skipped[{s['lens']}{'/' + s['market'] if 'market' in s else ''}]"
                                                     for s in skipped],
                     [prompt.id] if prompt else [], fields_changed=["selection.lenses_skipped"])
        by_lens = {}
        for l, m in pairs:
            by_lens.setdefault(l, []).append(m)
        text = (f"Researching {len(by_lens)} lens(es) across {market_list(markets)}."
                + ("".join(f" {LENS_TITLES[s['lens']]} is skipped{' in ' + market_list([s['market']]) if 'market' in s else ''}: "
                           f"{s['reason']}" for s in skipped)))
        self.add_chunk("select", {"selection": {"lenses_skipped": prior + skipped}}, [d], text=text)
        return True

    def stage_research(self):
        pairs = self.selected[0] if self.selected else []
        if not pairs:
            d = decision(self.doc, self.did("research"), "edit", self.handler, "research",
                         "Nothing selected: no lens x market to research.", [])
            self.add_chunk("research", {}, [d], text="Nothing to research: every lens was skipped.")
            return True
        lenses = [l for l in LENSES if any(p[0] == l for p in pairs)]
        markets = list(dict.fromkeys(m for _, m in pairs))
        result, change, hits, _ = run_research(self.doc, self.W_version, self.wclan(),
                                               {"lenses": lenses, "markets": markets}, self.handler, pairs=set(pairs))
        self.hits += hits
        decs = change["decisions"]
        merge = next(d for d in decs if d["action"] == "research_merge")
        for k in change["data_patch"].get("selection", {}):
            merge["targets"].append(f"{self.doc}#selection.{k}")
        self.add_chunk("research", change["data_patch"], decs, facts=change["facts_append"],
                       text=f"Research: {result['summary']}", msg_decision=merge)
        return True

    def stage_synthesise(self):
        try:
            result, change, hits, _ = run_synthesis(self.doc, self.W_version, self.wclan(), {}, self.handler)
        except TaskError:
            d = decision(self.doc, self.did("synthesise"), "edit", self.handler, "synthesise",
                         "No pins to synthesise from; no finding is invented.", [])
            self.add_chunk("synthesise", {}, [d], text="Nothing to synthesise: research pinned no facts.")
            return True
        self.hits += hits
        n = len(change["findings_append"])
        text = (f"{n} finding(s), each derived by the agent and waiting for a person to verify or reject."
                if n else "No new findings: what the pins say is already written up.")
        self.add_chunk("synthesise", {}, change["decisions"], findings=change["findings_append"], text=text)
        return True

    def stage_report(self):
        clan = self.clan  # the request's document, now holding every earlier stage
        report, cites, hits = compose(self.doc, clan, self.handler)
        did = self.did("report")
        d = decision(self.doc, did, "edit", self.handler, "report",
                     "The report stage: structured blocks over the pins and findings the document held once the "
                     "earlier stages had landed. Every claim cites a pin or a finding.",
                     ["report"], cites, fields_changed=["report"])
        self.hits += hits
        self.add_chunk("report", {"report": report}, [d], base=clan.get("version"), read_from=ctx_data(clan),
                       text="Report ready. The short list under it is what to confirm before the brief.")
        return True


# -- the report (Contract 3 §17) ----------------------------------------------------

def compose(doc, clan, handler):
    """data.report from the document as the request holds it. Deterministic, no
    model: each claim is either a finding's own statement (citing it) or a
    sentence with no figure in it (citing the pins the view renders)."""
    data = ctx_data(clan)
    camp = data.get("campaign") or {}
    sel = data.get("selection") or {}
    pins = [f for f in ctx_facts(clan) if re.fullmatch(r"f_[0-9A-Z]{6,}", str(f.get("id", "")))]
    pin_ids = {f["id"] for f in pins}
    findings = [f for f in ctx_findings(clan) if f.get("status") in ("proposed", "verified")
                and re.fullmatch(r"fi_[0-9A-Z]{6,}", str(f.get("id", "")))]
    if not pins and not findings:
        raise bad("nothing to cite: the document holds no pin and no finding, so no claim could be sourced")
    fval = lambda f: (camp.get(f) or {}).get("value")
    brand = (fval("brand") or {}).get("name") or fval("name") or "The campaign"
    markets = list(fval("markets") or [])

    def lens_of_key(k):
        return KEY_LENS.get(str(k).split(".")[0])

    roster = [f for f in pins if str(f.get("key", "")).startswith("roster.")]
    by_lens = {l: {"pins": [], "findings": [], "gaps": [], "contests": []} for l in LENSES}
    for f in pins:
        l = lens_of_key(f.get("key"))
        if l:
            by_lens[l]["pins"].append(f)
    for fi in findings:
        l = fi.get("lens") if fi.get("lens") in LENSES else next(
            (lens_of_key(p["key"]) for p in pins if p["id"] in (fi.get("cites") or []) and lens_of_key(p["key"])), None)
        if l:
            by_lens[l]["findings"].append(fi)
    for g in sel.get("gaps") or []:
        l = g.get("lens") if g.get("lens") in LENSES else lens_of_key(str(g.get("key", "")).partition(":")[2])
        if l and re.fullmatch(r"[a-z0-9_]+", str(g.get("id", ""))):
            by_lens[l]["gaps"].append(g)
    for c in sel.get("contested") or []:
        l = lens_of_key(str(c.get("key", "")).partition(":")[2].partition("@")[0])
        if l and re.fullmatch(r"[a-z0-9_]+", str(c.get("id", ""))):
            by_lens[l]["contests"].append(c)

    def fcites(fi):
        return [fi["id"]] + [c for c in fi.get("cites") or [] if c in pin_ids]

    sections, used = [], set()
    if roster:
        ids = [f["id"] for f in roster]
        sections.append({"id": "s_identity", "title": "Brand and category", "blocks": [
            {"kind": "claim", "text": f"The categories and the client come from {brand}'s roster row in the brand layer.",
             "cites": ids}, {"kind": "pins", "fact_ids": ids}]})
        used |= set(ids)
    for l in LENSES:
        g = by_lens[l]
        if not any(g.values()):
            continue
        blocks = []
        pm = sorted({p["market"] for p in g["pins"] if p.get("market")})
        if g["findings"]:
            fi = g["findings"][0]
            blocks.append({"kind": "claim", "text": fi["statement"], "cites": fcites(fi)})
        elif g["pins"]:
            where = f" for {market_list(pm)}" if pm else ""
            blocks.append({"kind": "claim", "text": f"{LENS_TITLES[l]}: what research pinned{where}; the values are shown below.",
                           "cites": [p["id"] for p in g["pins"]]})
        if g["pins"]:
            blocks.append({"kind": "pins", "fact_ids": [p["id"] for p in g["pins"]]})
        blocks += [{"kind": "finding", "finding_id": fi["id"]} for fi in g["findings"]]
        blocks += [{"kind": "contest", "contest_id": c["id"]} for c in g["contests"]]
        blocks += [{"kind": "gap", "gap_id": x["id"]} for x in g["gaps"]]
        sec = {"id": "s_" + l, "title": LENS_TITLES[l], "lens": l, "blocks": blocks}
        mk = {x.get("market") for x in g["pins"] + g["gaps"]} - {None}
        if len(mk) == 1 and not any(not p.get("market") for p in g["pins"]):
            sec["market"] = mk.pop()
        sections.append(sec)
        for b in blocks:
            used |= set(b.get("cites", [])) | set(b.get("fact_ids", []))
            if b["kind"] == "finding":
                used.add(b["finding_id"])

    summary = [{"text": fi["statement"], "cites": fcites(fi)}
               for l in LENSES for fi in by_lens[l]["findings"][:1]][:4]
    if not summary:
        for l in LENSES:
            if by_lens[l]["pins"] and len(summary) < 4:
                summary.append({"text": f"{LENS_TITLES[l]} rests on pinned facts; nothing has been synthesised from them yet.",
                                "cites": [p["id"] for p in by_lens[l]["pins"]]})
    if not summary:
        summary = [{"text": f"Only {brand}'s roster row is pinned so far; nothing has been researched yet.",
                    "cites": [f["id"] for f in roster]}]
    head_cites = [findings[0]["id"]] if findings else [pins[0]["id"]]
    open_ct = [c for c in sel.get("contested") or [] if c.get("status") == "open"]
    headline = {"text": f"{brand}{' in ' + market_list(markets) if markets else ''}: what the research found"
                        + (", with values still contested" if open_ct else "") + ".",
                "cites": head_cites}

    # confirm: D3's four while extracted or proposed, then every other proposed field
    confirm = []
    d3 = ["brand", "categories", "markets", "competitor_set"]
    order = [f for f in d3 if (camp.get(f) or {}).get("origin") in ("extracted", "proposed")] + \
        [f for f in CAMPAIGN_FIELDS if f not in d3 and (camp.get(f) or {}).get("origin") == "proposed"]
    for f in order:
        env = camp[f]
        v = env.get("value")
        if f in ("brand", "client_org"):
            shown = v.get("name") if isinstance(v, dict) else str(v)
        elif f == "markets":
            shown = market_list(v or [])
        elif f == "competitor_set":
            shown = ", ".join(c.get("name", c.get("ref", "")) for c in v or [])
        elif f == "categories":
            shown = ", ".join(v or [])
        else:
            shown = None
        label = f"{FIELD_LABELS.get(f, f.replace('_', ' ').capitalize())}" + (f": {shown}" if shown else "")
        why = ("Read from the material; a person has not confirmed it yet." if env.get("origin") == "extracted"
               else "Proposed from pinned facts; a person has not confirmed it yet.")
        confirm.append({"address": f"{doc}#campaign.{f}", "label": label, "why": why})

    # not researched: every skipped lens x market, then any other lens x market with no run
    skipped = [s for s in sel.get("lenses_skipped") or [] if s.get("lens") in LENSES]
    nr = [{"lens": s["lens"], **({"market": s["market"]} if s.get("market") else {}), "reason": s["reason"]}
          for s in skipped]
    ran = {(r.get("lens"), r.get("market")) for r in sel.get("lenses_run") or []}
    for l in LENSES:
        for m in markets:
            if (l, m) in ran or any(s["lens"] == l and s.get("market") in (None, m) for s in skipped):
                continue
            nr.append({"lens": l, "market": m, "reason": "No research run covers it yet."})
    nr = list({json.dumps(x, sort_keys=True): x for x in nr}.values())

    bf = (data.get("projection") or {}).get("built_from") or {}
    fsha = bf.get("facts_sha256") if re.fullmatch(r"sha256:[0-9a-f]{64}", str(bf.get("facts_sha256"))) else None
    isha = bf.get("findings_sha256") if re.fullmatch(r"sha256:[0-9a-f]{64}", str(bf.get("findings_sha256"))) else None
    report = {
        "built_at": iso(now()), "handler": handler,
        "based_on": {"version": str(clan.get("version")),
                     # the host's member hashes, as read; a document with no
                     # projection yet gets a hash of the members as sent
                     "facts_sha256": fsha or canon_sha({"facts": ctx_facts(clan)}),
                     "findings_sha256": isha or canon_sha({"findings": ctx_findings(clan)})},
        "headline": headline, "summary": summary, "sections": sections,
        "confirm": confirm, "not_researched": nr,
    }
    cites = list(dict.fromkeys(list(head_cites) + [c for s in summary for c in s["cites"]] + sorted(used)))
    fact_by_id = {f["id"]: f for f in pins}
    hits = [{"id": c, "scope": fact_by_id[c].get("layer", ""), "source": fact_by_id[c].get("origin", "")}
            for c in cites if c in fact_by_id]
    return report, cites, hits


# -- dispatch for the three tasks ---------------------------------------------------

CAMPAIGN_JOBS: dict = {}


def unfinished_campaign(doc):
    with JOBS_LOCK:
        return next((j for j in CAMPAIGN_JOBS.values() if j.doc == doc and j.scope == SCOPE
                     and j.state in ("queued", "running", "needs_input")), None)


def campaign_envelope(job, change):
    return envelope("start_campaign", job.handler, job.view(),
                    {"summary": job.summary(), "messages": job.messages()}, change,
                    job.hits if job.state == "done" else [])


def start_campaign(doc, base, clan, inp, handler):
    prompt = inp.get("prompt", "")
    if not isinstance(prompt, str):
        raise bad("input.prompt must be a string")
    atts = inp.get("attachments", [])
    if not isinstance(atts, list):
        raise bad("input.attachments must be a list")
    mats = ctx_data(clan).get("materials") if isinstance(ctx_data(clan).get("materials"), dict) else {}
    for a in atts:
        if not isinstance(a, dict) or not isinstance(a.get("material_id"), str) or not a.get("sha256") \
                or not isinstance(a.get("name"), str):
            raise bad("each attachment needs a material_id, a name and a sha256")
        m = mats.get(a["material_id"])
        if not isinstance(m, dict) or norm_sha(m.get("sha256", "")) != norm_sha(a["sha256"]):
            raise bad(f"attachment {a['material_id']} is not in clan.data.materials with that sha256 "
                      "(the view indexes a file before it starts the campaign)")
    xinp = {"prompt": prompt, "attachments": [{"name": a["name"], "sha256": a["sha256"],
                                               **({"text": a["text"]} if "text" in a else {})} for a in atts]}
    build_materials(xinp, ctx_data(clan))  # validates shas
    if not (prompt.strip() or any(isinstance(a.get("text"), str) and a["text"].strip() for a in atts)):
        raise bad("nothing to read: input.prompt is empty and no attachment carries text")
    if unfinished_campaign(doc):
        raise TaskError(409, "job_state", "a start_campaign job on this document is still running or waiting "
                                          "for an answer; one composition of a document at a time")
    jid = "job_" + _digest(SCOPE, doc, base, "start_campaign", inp, next(_SEQ)).hex()[:20]
    job = CampaignJob(jid, doc, handler, clan, xinp)
    with JOBS_LOCK:
        CAMPAIGN_JOBS[jid] = job
        if len(CAMPAIGN_JOBS) > MAX_JOBS:
            for k in sorted(CAMPAIGN_JOBS, key=lambda k: CAMPAIGN_JOBS[k].t0)[: len(CAMPAIGN_JOBS) - MAX_JOBS]:
                del CAMPAIGN_JOBS[k]
    return campaign_envelope(job, None)


def campaign_job(doc, jid):
    if not isinstance(jid, str) or not jid:
        raise bad("input.job_id is required")
    with JOBS_LOCK:
        job = CAMPAIGN_JOBS.get(jid)
    if job is None or job.scope != SCOPE or job.doc != doc:
        raise TaskError(404, "unknown_job", f"no job {jid} for this document")
    return job


def poll_campaign(job, clan):
    with job.lock:
        change = job.advance(clan)
        return campaign_envelope(job, change)


def answer_question(doc, clan, inp):
    job = campaign_job(doc, inp.get("job_id"))
    with job.lock:
        if job.state != "needs_input":
            raise TaskError(409, "job_state", f"job {job.id} is {job.state}, not waiting for an answer")
        q = job.question
        if inp.get("question_id") != q["id"]:
            raise bad(f"question_id is not the job's open question ({q['id']})")
        has_opt, has_text = "option_id" in inp, "text" in inp
        if has_opt == has_text:
            raise bad("answer with exactly one of option_id and text")
        field = q["address"].partition("#campaign.")[2]
        if has_opt:
            opt = next((o for o in q["options"] if o["id"] == inp["option_id"]), None)
            if opt is None:
                raise bad(f"option {inp['option_id']!r} is not one of the question's options")
            if "value" not in opt:
                raise bad(f"option {opt['id']!r} is the escape: answer it with text")
            job.answers[field] = opt["value"]
        else:
            if not q["allow_text"]:
                raise bad("this question does not take a free-text answer")
            if not isinstance(inp["text"], str) or not inp["text"].strip():
                raise bad("text must be a non-empty string")
            job.pending_text = {"field": field, "text": inp["text"].strip()}
        job.question = None
        job.state = "running"
        change = job.advance(clan, only_identify=True)  # the answer's clan is the base from here
        return campaign_envelope(job, change)


def compose_report_task(doc, base, clan, handler):
    if unfinished_campaign(doc):
        raise TaskError(409, "job_state", "a start_campaign job on this document is unfinished; "
                                          "one composition of a document at a time")
    report, cites, hits = compose(doc, clan, handler)
    jid = "job_" + _digest(SCOPE, doc, base, "compose_report", next(_SEQ)).hex()[:20]
    did = uid("d_", doc, base, "compose_report", jid)
    mid = ulid_like("msg_", 1, doc, jid, "report")
    text = "Report refreshed from the document as it stands."
    patch = {"report": report, "intake": {"messages": {mid: {"role": "agent", "text": text, "at": iso(now()),
                                                             "job_id": jid, "stage": "report"}}}}
    d = decision(doc, did, "edit", handler, "compose_report",
                 "Refresh report: recomposed from the document as it stands; every claim cites a pin or a finding.",
                 ["report", f"intake.messages[{mid}]"], cites, fields_changed=["report", "intake.messages"])
    change = {"doc": doc, "base_version": base, "read": read_of(ctx_data(clan), patch), "data_patch": patch,
              "facts_append": [], "findings_append": [], "decisions": [d]}
    t = iso(_dt.datetime.now(_dt.timezone.utc))
    job = {"id": jid, "state": "done", "progress": {"done": 1, "total": 1}, "stage": "report", "question": None,
           "started_at": t, "finished_at": t, "error": None}
    return envelope("compose_report", handler, job,
                    {"summary": f"Report composed: {len(report['sections'])} section(s), "
                                f"{len(report['confirm'])} to confirm.",
                     "messages": [{"id": mid, "text": text, "stage": "report"}]}, change, hits)


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
        with JOBS_LOCK:
            cj = CAMPAIGN_JOBS.get(inp.get("job_id")) if isinstance(inp.get("job_id"), str) else None
        if cj is not None:
            return poll_campaign(campaign_job(doc, inp.get("job_id")), clan)
        return poll(doc, inp)
    base = clan.get("version")
    if not isinstance(base, (str, int)) or isinstance(base, bool) or base == "":
        raise bad("clan.version is required: a change records the version it read")
    handler = resolve_handler(task, clan)
    t0 = iso(_dt.datetime.now(_dt.timezone.utc))
    if task == "start_campaign":
        return start_campaign(doc, base, clan, inp, handler)
    if task == "answer_question":
        return answer_question(doc, clan, inp)
    if task == "compose_report":
        return compose_report_task(doc, base, clan, handler)
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
