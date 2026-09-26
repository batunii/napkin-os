#!/usr/bin/env python3
"""
Briefing tool — MVP (Loops 1 & 2)
=================================

Take a client brief in any format (Word / PDF / text / scraps) and produce:
  * Loop 1 — Parse/Ingest: a faithful structured capture + win-rules, with a
    no-loss ledger proving nothing was dropped. NO research, NO RAG.
  * Loop 2 — First-round brief: shape the capture into an agency brief
    (problem, objective, audience, scope) + the open questions to ask first.

Both loops are written to plain output files (JSON + a markdown one-pager). No
database, no persistent memory in the MVP.

Pipeline:  ingest -> segment -> extract -> [Loop 1] -> review
           -> [Loop 2 shaping] -> review -> render

Usage:
    python parse_brief.py samples/messy_brief_sample.txt \
        --client "Northwind Motors" --project "Moving People"
    python parse_brief.py /path/to/vw_brief.pdf
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import functools
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.request
import urllib.error
from pathlib import Path

def _load_env_file(path: Path) -> None:
    """Load KEY=VALUE lines from .env into os.environ (without overriding existing).
    Dependency-free fallback — python-dotenv isn't installed in the `claws` env, and
    without this a mis-sourced shell (`. .env` vs `. ./.env`) silently drops all API
    keys and the whole pipeline falls back to heuristic mode. Never let that be silent."""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
    except FileNotFoundError:
        pass


try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    _load_env_file(Path(__file__).resolve().parent / ".env")  # stdlib fallback

PARSER_VERSION = "0.3.0"
PROMPT_VERSION = "loop12-v2-betterbriefs"
# A browser-like UA so Cloudflare-fronted APIs (Groq, Cerebras) don't 1010-block us.
_HTTP_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

# ---------------------------------------------------------------------------
# LLM call ledger — instrumentation only, zero behavioural effect. Reset per
# run(); snapshot lands in brief_object.json → meta.llm_stats so optimisation
# work is measured, not eyeballed. prompt/completion tokens come from provider
# `usage` when present; chars are always counted as the fallback ruler.
# ---------------------------------------------------------------------------
_LLM_STATS = {}
_STATS_LOCK = __import__("threading").Lock()   # loops 3–7 retrieval/rerank run in threads
# A run's ledger travels with the thread (and, via _scoped, with the threads it starts), so
# two briefs in one process — or a name-derivation call next to a running brief on the
# agent-server — never write into each other's numbers (audit 2026-09-24, critic-G11).
_STATS_TL = threading.local()

_EMPTY_STATS = {"calls": 0, "http_attempts": 0, "input_chars": 0, "output_chars": 0,
                "prompt_tokens": 0, "completion_tokens": 0, "cache_read_tokens": 0,
                "cache_creation_tokens": 0, "retries": 0, "rate_limited": 0, "truncations": 0,
                "refusals": 0, "by_provider": {}, "answered_by": {}}


def _ledger() -> dict:
    """The ledger this thread writes to: the run-scoped one set by _stats_scope (and carried
    into worker threads by _scoped), else the module-global _LLM_STATS."""
    led = getattr(_STATS_TL, "stats", None)
    if led is not None:
        return led
    if not _LLM_STATS:
        _stats_reset()
    return _LLM_STATS


class _stats_scope:
    """`with _stats_scope() as ledger:` gives the calling thread a fresh, private ledger for
    the block; nested calls in this thread (and threads started through _scoped) record into
    it. run() uses it so meta.llm_stats is exactly that brief's calls."""

    def __enter__(self):
        """Install a fresh ledger for this thread; return it."""
        self._prev = getattr(_STATS_TL, "stats", None)
        self.ledger = {**_EMPTY_STATS, "by_provider": {}, "answered_by": {}}
        _STATS_TL.stats = self.ledger
        return self.ledger

    def __exit__(self, *exc):
        """Restore whatever ledger the thread had before."""
        _STATS_TL.stats = self._prev
        return False


def _scoped(fn):
    """Wrap `fn` so that, run on another thread (a pool submit/map), it records into the
    ledger of the thread that called _scoped. Every pool inside a run uses it."""
    led = getattr(_STATS_TL, "stats", None)

    def wrapped(*a, **k):
        """Run fn with the parent thread's ledger installed."""
        _STATS_TL.stats = led
        return fn(*a, **k)
    return wrapped


def _stats_reset():
    """Zero the LLM call ledger (_LLM_STATS) under the stats lock, by_provider
    included. Direct calls from tests and scripts land here; run() records into its own
    scoped ledger (see _stats_scope)."""
    with _STATS_LOCK:
        _LLM_STATS.clear()
        _LLM_STATS.update({**_EMPTY_STATS, "by_provider": {}, "answered_by": {}})


def _stats_call(provider_label: str, in_chars: int):
    """Count one outbound request to a link in the LLM call ledger: bumps `calls`,
    adds `in_chars` (system + user prompt length) to `input_chars` and tallies the request
    under by_provider[provider_label]. Instrumentation only."""
    led = _ledger()
    with _STATS_LOCK:
        led["calls"] += 1
        led["input_chars"] += in_chars
        bp = led["by_provider"]
        bp[provider_label] = bp.get(provider_label, 0) + 1


def _stats_usage(usage: dict | None, out_chars: int):
    """Add one reply to the LLM call ledger: `out_chars` to `output_chars`, and the
    provider's usage when a usage dict is given: prompt_tokens (uncached input),
    completion_tokens, and the separately priced cache_read_tokens / cache_creation_tokens
    (missing or None counts as 0). Instrumentation only."""
    led = _ledger()
    with _STATS_LOCK:
        led["output_chars"] += out_chars
        if usage:
            for k in ("prompt_tokens", "completion_tokens", "cache_read_tokens", "cache_creation_tokens"):
                led[k] = led.get(k, 0) + int(usage.get(k) or 0)


def _stats_answered(provider_label: str):
    """Record that `provider_label` is the link whose reply was USED (parsed and accepted),
    as opposed to merely attempted (by_provider). meta.extraction_mode and the synthesis
    label are read from this, so a brief is labelled with the model that wrote it."""
    led = _ledger()
    with _STATS_LOCK:
        ab = led.setdefault("answered_by", {})
        ab[provider_label] = ab.get(provider_label, 0) + 1


def _stats_bump(key: str):
    """Add one to a counter in the ledger (truncations, refusals, ...)."""
    led = _ledger()
    with _STATS_LOCK:
        led[key] = led.get(key, 0) + 1


def _stats_snapshot() -> dict:
    """A deep copy of the current ledger. The old shallow copy shared by_provider with the
    live ledger, so the critic call made after run() changed a finished brief's numbers."""
    import copy
    with _STATS_LOCK:
        return copy.deepcopy(_ledger())


HERE = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# 1. INGEST
# ---------------------------------------------------------------------------

_VISION_PROMPT = (
    "Transcribe this document image into clean, faithful text for an advertising-brief pipeline.\n"
    "Rules: (1) Capture ALL text verbatim — headings, body, bullets, labels, captions, table cells, "
    "prices, names, figures. Lose nothing. (2) Preserve reading order and structure (use markdown "
    "headings / bullets / tables to mirror the layout). (3) For a meaningful non-text visual (a chart, "
    "an org diagram, a product photo with a caption), add a short bracketed note of what it shows. "
    "(4) Do NOT summarise, interpret, or invent — transcribe only. Output only the transcription."
)

# image input → faithful text, via a NIM vision model (no-loss capture stays intact)
_IMAGE_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp"}


def _vision_transcribe(image_bytes: bytes, mime: str, label: str = "image") -> str:
    """Transcribe one image to text with a vision model (default NIM nemotron-nano-vl).
    Returns '' and warns on failure rather than crashing the run."""
    # Vision endpoint is independent of the main model: point it at NIM (default) or a
    # local Ollama (keyless) via BRIEF_VISION_BASE/MODEL — e.g. gemma3 for image->text.
    base = os.environ.get("BRIEF_VISION_BASE", PROVIDERS["nim"][0])
    model = os.environ.get("BRIEF_VISION_MODEL", "nvidia/llama-3.1-nemotron-nano-vl-8b-v1")
    key = os.environ.get("BRIEF_VISION_API_KEY") or os.environ.get("NVIDIA_API_KEY")
    is_local = "localhost" in base or "127.0.0.1" in base   # e.g. Ollama — no key needed
    if not key and not is_local:
        print(f"[i] {label}: image ingest needs a vision key (NVIDIA_API_KEY / BRIEF_VISION_API_KEY) "
              "or a local endpoint (BRIEF_VISION_BASE=http://localhost:11434/v1) — skipping.",
              file=sys.stderr)
        return ""
    headers = {"Content-Type": "application/json", "User-Agent": _HTTP_UA}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    payload = {
        "model": model, "temperature": 0.0,
        "max_tokens": int(os.environ.get("BRIEF_MAX_TOKENS", "4000")),
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": _VISION_PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(image_bytes).decode()}"}},
        ]}],
    }
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions", data=json.dumps(payload).encode(),
        headers=headers, method="POST")
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                content = json.loads(r.read())["choices"][0]["message"].get("content") or ""
                return re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:200]
            if e.code in (429, 500, 502, 503) and attempt < 2:
                import time
                time.sleep(5 * (attempt + 1)); continue
            print(f"[i] {label}: vision model HTTP {e.code}: {detail}", file=sys.stderr)
            return ""
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt < 2:
                import time
                time.sleep(3 * (attempt + 1)); continue
            print(f"[i] {label}: vision model unreachable: {e}", file=sys.stderr)
            return ""
    return ""


def _pdf_vision_transcribe(path: Path, max_pages: int = 20) -> str:
    """Render an image-only / slide-deck PDF page by page and transcribe each."""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        print(f"[i] {path.name}: looks image-only but PyMuPDF isn't installed "
              "(pip install pymupdf) — can't transcribe.", file=sys.stderr)
        return ""
    doc = fitz.open(str(path))
    n = min(len(doc), max_pages)
    if len(doc) > max_pages:
        print(f"[i] {path.name}: image-PDF — transcribing first {max_pages} of {len(doc)} pages.",
              file=sys.stderr)
    parts = []
    for i in range(n):
        png = doc[i].get_pixmap(dpi=150).tobytes("png")
        t = _vision_transcribe(png, "image/png", f"{path.name} p{i + 1}")
        if t:
            parts.append(f"--- page {i + 1} ---\n{t}")
    return "\n\n".join(parts)


def _strip_html(html: str) -> str:
    """Crude HTML→text: drop tags, unescape the common entities. Good enough for
    an email body when no text/plain part exists."""
    import html as _h
    text = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return _h.unescape(text)


def ingest_email_text(raw: str) -> str:
    """A copy-pasted email is just text. Keep it verbatim — Loop 1 is no-loss —
    but normalise CRLF so the segmenter sees clean lines."""
    return raw.replace("\r\n", "\n").replace("\r", "\n")


def ingest(path: Path) -> tuple[str, str]:
    """Return (raw_text, mime) from .txt/.md, .docx, .pdf, or .eml."""
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md", ".text"}:
        return path.read_text(encoding="utf-8", errors="replace"), "text/plain"
    if suffix == ".eml":
        import email
        from email import policy
        msg = email.message_from_bytes(path.read_bytes(), policy=policy.default)
        body = msg.get_body(preferencelist=("plain", "html"))
        content = body.get_content() if body else (msg.get_content() or "")
        if body is not None and body.get_content_type() == "text/html":
            content = _strip_html(content)
        hdr = [f"{k}: {msg[k]}" for k in ("Subject", "From", "Date") if msg[k]]
        text = ("\n".join(hdr) + "\n\n" + content) if hdr else content
        return ingest_email_text(text), "message/rfc822"
    if suffix == ".docx":
        try:
            import docx
        except ImportError:
            sys.exit("Need python-docx for .docx:  pip install python-docx")
        d = docx.Document(str(path))
        parts = [p.text for p in d.paragraphs]
        for table in d.tables:
            for row in table.rows:
                parts.append(" | ".join(c.text for c in row.cells))
        return "\n".join(parts), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if suffix == ".pdf":
        try:
            import pdfplumber
        except ImportError:
            sys.exit("Need pdfplumber for .pdf:  pip install pdfplumber")
        out = []
        with pdfplumber.open(str(path)) as pdf:
            for page in pdf.pages:
                out.append(page.extract_text() or "")
        text = "\n".join(out)
        # Image-only / slide-deck PDFs carry little or no text layer — fall back to
        # rendering each page and transcribing it with the vision model.
        if len(text.strip()) < max(200, 40 * max(1, len(out))):
            print(f"[i] {path.name}: thin text layer — transcribing pages with the vision model.",
                  file=sys.stderr)
            text = _pdf_vision_transcribe(path) or text
        return text, "application/pdf"
    if suffix in _IMAGE_MIME:
        mime = _IMAGE_MIME[suffix]
        return _vision_transcribe(path.read_bytes(), mime, path.name), mime
    sys.exit(f"Unsupported file type: {suffix}. Use .txt, .md, .docx, .pdf, .eml, "
             "or an image (.png/.jpg/.jpeg/.webp) — or paste with --text / '-' for stdin.")


# ---------------------------------------------------------------------------
# 2. SEGMENT
# ---------------------------------------------------------------------------

def segment(text: str) -> list[str]:
    """Coalesce soft-wrapped lines into blocks, then sentence-split. Each
    segment becomes a row in the no-loss ledger."""
    blocks: list[str] = []
    buf: list[str] = []

    def flush():
        """Join the buffered lines into one block, append it to `blocks` and clear the
        buffer; a no-op when the buffer is empty."""
        if buf:
            blocks.append(" ".join(buf).strip())
            buf.clear()

    for raw in text.splitlines():
        stripped = raw.strip()
        is_bullet = bool(re.match(r"^\s*[-*•]\s+", raw))
        is_label = bool(re.match(r"^[A-Za-z /]{3,30}\s*[:=]\s+\S", stripped))
        if not stripped:
            flush(); continue
        if is_bullet or is_label:
            flush()
        buf.append(stripped.lstrip("-*• \t"))
    flush()

    segs: list[str] = []
    for block in blocks:
        for piece in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'])", block):
            piece = piece.strip()
            if len(piece) >= 4:
                segs.append(piece)
    return segs


# ---------------------------------------------------------------------------
# 3a. EXTRACT (heuristic) — Loop 1 faithful capture, no API needed
# ---------------------------------------------------------------------------

LABEL_MAP = {
    "background": "background_context", "context": "background_context",
    "problem": "business_problem", "challenge": "business_problem",
    "objective": "objective", "goal": "objective",
    "audience": "target_audience", "target": "target_audience",
    "budget": "budget", "timeline": "timeline", "timing": "timeline",
    "deadline": "timeline", "deliverable": "deliverables",
    "mandator": "mandatories", "must": "mandatories",
    "kpi": "success_metrics", "success": "success_metrics", "metric": "success_metrics",
    "competitor": "competitors_market", "tone": "tone_and_brand",
    "brand guideline": "tone_and_brand",
}

LIST_FIELDS = {"deliverables", "mandatories", "timeline", "success_metrics",
               "decision_makers", "constraints", "objective", "proof_points",
               "evaluation_criteria"}

KEYWORD_CUES = {
    "business_problem": ["stalled", "perception", "problem", "struggl", "down vs"],
    "target_audience":  ["audience", "switcher", "family", "suburban", "demographic"],
    "budget":           ["£", "$", "€", "budget", "working media"],
    "timeline":         ["week of", "deadline", "pitch presentations", "by july"],
    "mandatories":      ["mandatory", "must ", "can't", "cannot", "asa", "logo",
                         "ci/", "ci ", "claim", "naming", "lockup"],
    "success_metrics":  ["kpi", "consideration", "success =", "test drive", "ipa-style"],
    "decision_makers":  ["cmo", "brand director", "the one to win", "decision-maker"],
    "competitors_market": ["tesla", "polestar", "kia", "hyundai", "competitor", "own \""],
    "tone_and_brand":   ["slogan", "heritage", "unmistakably", "tone of voice"],
}


def _cap(value, status="fact", quote=None, conf=0.6):
    """Build one Captured entry {value, status, source_quote, confidence} as the heuristic
    extractor records it (status 'fact', confidence 0.6 unless given)."""
    return {"value": value, "status": status, "source_quote": quote, "confidence": conf}


def extract_heuristic(segments):
    """Keyless Loop 1 capture: map each segment to at most one field by rule.
    A `Label: text` segment whose label contains a LABEL_MAP key goes to that field; any
    other segment goes to the first KEYWORD_CUES field with a matching cue (case-insensitive
    substring). The whole segment is stored as both value and source_quote. A LIST_FIELDS
    field collects every match; a single-value field keeps only its first match, and later
    matches are not stored but still count as used.
    Returns (fields, used): field id -> Captured entry (a list for list fields), and the set
    of segment indexes that were mapped, which build_ledger takes as the ledger's mapping."""
    fields, used = {}, set()

    def add(field, idx, seg):
        """Record `seg` under `field` (appended for a list field, first match
        wins for a single-value field) and mark segment `idx` as used either way."""
        if field in LIST_FIELDS:
            fields.setdefault(field, []).append(_cap(seg, quote=seg))
        elif field not in fields:
            fields[field] = _cap(seg, quote=seg)
        used.add(idx)

    for idx, seg in enumerate(segments):
        low = seg.lower()
        m = re.match(r"^([A-Za-z /]{3,30}?)\s*[:=]\s*(.+)$", seg)
        if m:
            label = m.group(1).strip().lower()
            for key, field in LABEL_MAP.items():
                if key in label:
                    add(field, idx, seg); break
            else:
                pass
            if idx in used:
                continue
        for field, cues in KEYWORD_CUES.items():
            if any(c in low for c in cues):
                add(field, idx, seg); break
    return fields, used


# ---------------------------------------------------------------------------
# 3b. EXTRACT (llm)
# ---------------------------------------------------------------------------

EXTRACTION_SYSTEM = """You are Loop 1 of an ad-agency briefing system, the
Client Brief Parser. Convert a messy client brief into a faithful structured
capture as JSON.

"fields" MUST use ONLY these exact keys (do not invent new field names):
  background_context  - the situation/context behind the brief
  business_problem    - the core problem/challenge to solve (ALWAYS capture this if stated)
  objective           - what the work must achieve   [array, a handful max]
  target_audience     - who we are talking to
  deliverables        - what we must produce        [array]
  mandatories         - non-negotiables: legal, brand, naming, claims  [array]
  budget              - money
  timeline            - dates/deadlines              [array]
  success_metrics     - KPIs / how success is measured  [array]
  key_message         - the ONE single-minded message the client wants to land
  proof_points        - evidence/claims supporting the key message  [array]
  evaluation_criteria - how the client says ideas/work will be judged  [array]
  strategic_angle     - any strategic direction/approach the client suggests
  anti_target         - who the brand is explicitly NOT for / NOT targeting
  competitors_market  - competitors and market context
  tone_and_brand      - tone of voice, brand heritage, style
  decision_makers     - who decides / who to win     [array]
  constraints         - other limits                 [array]
If something does not fit a key, attach it to the CLOSEST key. Never create
keys like "Pitch Details" or "Key Themes". Themes/landmines belong in how_to_win.

Each value is an object: {"value", "status", "source_quote", "confidence"}.
Array fields are lists of such objects.
- status: "fact" (stated), "assumption" (inferred), "gap" (not provided -> value null).
- objective items also carry "objective_type": "commercial" | "behavioural" |
  "attitudinal" (BetterBriefs: the three types should coexist and link —
  attitude shift -> behaviour change -> commercial outcome).
- EVERY fact MUST include a verbatim source_quote copied word-for-word from the brief.
- LOSE NOTHING: every concrete sentence in the brief must be reflected in some
  field's value or source_quote.

how_to_win holds ONLY what the brief reveals (stated_evaluation_criteria,
unstated_needs, likely_landmines, winning_themes, proof_required) as
{"point","evidence"} items. Do NOT invent strategy.

open_questions = what the brief FAILS to answer. Do NOT ask about anything the
brief already states (e.g. if the problem/timeline/decision-maker is given, do
not ask for it).

Return ONLY JSON with keys: fields, how_to_win, open_questions.
Output raw JSON only — no markdown fences, no commentary before or after."""


# BetterBriefs scorecard — judges the CLIENT brief against the BetterBriefs
# rubric (reference/betterbriefs/). Presence is checked elsewhere; this judges
# quality: the dominant failure mode is present-but-VAGUE (78% of marketers
# think their briefs are clear; 5% of agencies agree).
SCORECARD_SYSTEM = """You are a brief-quality judge applying the BetterBriefs
rubric (the global study on briefing) to a CLIENT brief. Judge ONLY what the
brief text says — quote evidence verbatim, do not invent.

Score exactly these dimensions, each as
{"dimension", "verdict": "pass"|"vague"|"missing", "evidence", "fix"}:
  objectives_quality   - a handful at most, benchmarked + time-stamped, the
                         commercial/behavioural/attitudinal chain linked, not
                         wishful, clear hierarchy (not a shopping list)
  audience_vividness   - a vivid picture (demographics + psychographics +
                         needs); FLAG demographic cliches ("millennials",
                         "everyone", bare age ranges) as vague; states who it
                         is NOT for
  single_minded_message - ONE key message, supported by relevant proof points
  evaluation_criteria  - how the work will be judged is stated
  budget_interlock     - budget, objectives and audience are mutually
                         feasible (flag mass-market ambitions on small money)
  strategic_clarity    - a clear strategic angle/choice, including what NOT
                         to do; strategy is not left for the agency to guess
  language             - simple, jargon-free, succinct; no category-speak

Also detect single-mindedness of the WHOLE brief: one brief = one strategy.
If it bundles mutually exclusive strategies or multiple separate jobs
(e.g. several exercises/events/streams), say how to split it.

Return ONLY raw JSON (no fences, no commentary):
{"dimensions": [ ...exactly the 7 above... ],
 "single_mindedness": {"verdict": "single"|"multiple",
                       "split_into": ["one line per separate brief", ...]},
 "summary": "one sentence on overall brief quality"}"""


# The zone-3 strategic fields are GENERATED, not extracted (the guided-generative
# fill). Order follows the schema dependency graph: insight feeds smp, smp feeds
# reasons_to_believe, and desired_response leans on smp + objectives.
GEN_ZONE3_ORDER = ["insight", "smp", "reasons_to_believe", "desired_response"]
# "Do" must be an observable behaviour; these are the schema's own bad_example verbs.
FORBIDDEN_DO_VERBS = ("engage with", "explore the", "interact with", "connect with the brand")


def _field_by_id(schema, fid):
    """Return the schema field definition whose id is `fid`, or None when the schema
    has no such field."""
    for f in schema.get("fields", []):
        if f.get("id") == fid:
            return f
    return None


# The schema's code (auto) rubric checks, said in words to the generator. golden_critic
# scores a brief with these exact checks; before 2026-09-23 the generator was never told
# them, and its gate did not run them, so SMPs of two sentences won their tournaments
# and then failed the critic.
_HARD_RULE_TEXT = {
    "single_sentence": "ONE sentence only.",
    "max_items": "at most {max_items} items.",
    "reveals_why": "state the motivation explicitly ('because …' / 'which means …').",
    "all_three": "fill think, feel AND do.",
}


def _gen_field_system(field, n: int = 1) -> str:
    """Build a generation system prompt for ONE golden field straight from the schema
    — no hard-coded sentence template. The schema's good_example carries the shape;
    the bad_example is a hard negative. This is what frees the insight from Mad-Libs.
    n>1 switches to TOURNAMENT mode: one call returns n distinct drafts (the heavy
    context is sent once instead of n times)."""
    mw = field.get("max_words")
    lim = f"Hard limit: {mw} words.\n" if mw else ""
    lim += "".join(f"Hard rule: {_HARD_RULE_TEXT[c['id']].format(**field)}\n"
                   for c in field.get("rubric") or []
                   if c.get("method") == "auto" and c["id"] in _HARD_RULE_TEXT)
    own = ""
    if field.get("id") in ("insight", "smp"):
        own = ("\nOWNABLE TENSION: claim territory the named competitor does NOT own. If every rival "
               "in the category would nod at your line, it is a category truth and a FAIL — use the "
               "competitor_context to find the white space. Reconcile the WHOLE stated audience (if "
               "it is split, name the tension that unites the segments; a truly split audience may "
               "need two briefs). Do not simply restate the brand's standing line unless the brief "
               "asks for continuity; the proposition is this campaign's choice.\n")
    t = field.get("type")
    if t == "tfd":
        out_shape = ('{"think": "...", "feel": "...", '
                     '"do": "<a concrete, observable behaviour — NOT \'engage\'/\'explore\'>"}')
    elif t == "list":
        out_shape = '["...", "...", "..."]'
    else:
        out_shape = '"<text>"'
    return (
        f"You are a senior strategy planner writing the '{field['label']}' field of a brief "
        f"for THIS specific brand. Write it now — do not extract it, derive it.\n\n"
        f"WHAT THIS FIELD IS: {field.get('prompt','')}\n{lim}\n"
        f"STYLE REFERENCES (DIFFERENT brands, different shapes — copy the depth ONLY, never the "
        f"words, brand, topic or construction of any one of them; and NEVER mention them in your rationale):\n"
        f"{_good_examples_block(field)}\n\n"
        f"BAD — never produce anything like this:\n"
        f"  {field.get('bad_example','')}  ({field.get('bad_reason','')})\n"
        f"{own}\n"
        "Reason from the brief context and the real award-winning PRECEDENTS provided below. The "
        "precedents are for SHAPE and DEPTH only — do NOT borrow their words, brands or themes. Be "
        "specific to this brand: a line that could be pasted onto a different brief is a failure. Do "
        "NOT output a generic fill-in-the-blank sentence. You are synthesising strategy (source "
        "'inferred'), never inventing client facts.\n\n"
        + (f'Return ONLY raw JSON, no fences: {{"value": {out_shape}, '
           '"confidence": 0.0-1.0, "rationale": "one line on THIS brand\'s tension and which AWARD '
           'PRECEDENT shaped it — never mention the style reference"}'
           if n <= 1 else
           f"You will write {n} GENUINELY DISTINCT drafts of this field — different strategic "
           f"ideas, not rewordings of one idea. Make them compete.\n"
           f'Return ONLY raw JSON, no fences: {{"candidates": [{n} objects, each '
           f'{{"value": {out_shape}, "confidence": 0.0-1.0, "rationale": "one line — never '
           f'mention the style reference"}}]}}')
    )


def _good_examples_block(field) -> str:
    """The field's good examples as indented lines: `good_examples` when the schema lists
    several (the SMP shows three sourced propositions of different shapes, so a writer
    cannot copy one construction — R1 §4.3, §5.4), else the single `good_example`."""
    exs = field.get("good_examples") or ([field["good_example"]] if field.get("good_example") else [])
    return "\n".join(f"  - {e}" for e in exs) or "  (none)"


def _coerce_candidates(o) -> list:
    """Accept every plausible shape a batched tournament call can come back in:
    {"candidates":[...]}, a bare list, or a single {"value": ...} draft."""
    if isinstance(o, dict) and isinstance(o.get("candidates"), list):
        return [c for c in o["candidates"] if isinstance(c, dict) and c.get("value")]
    if isinstance(o, list):
        return [c for c in o if isinstance(c, dict) and c.get("value")]
    if isinstance(o, dict) and o.get("value"):
        return [o]
    return []


def _build_golden_system():
    """Build a per-field extraction system prompt from golden_brief.schema.json."""
    schema_path = HERE / "golden-brief" / "golden_brief.schema.json"
    if not schema_path.exists():
        return None
    schema = json.loads(schema_path.read_text())
    lines = [
        "You are a senior strategic planner filling a Golden Brief from a raw client brief.",
        "Extract ONLY what the brief contains. Mark provenance accurately.",
        "",
        "PROVENANCE:",
        "  client_stated — directly from brief text; include verbatim source_quote",
        "  inferred      — reasonably implied but not stated",
        "  missing       — not in brief at all; value MUST be null",
        "",
        "CONFIDENCE: 0.9+ verbatim, 0.6–0.8 inferred, 0.0 missing.",
        "",
        "FIELD SPECIFICATIONS (fill all 11 content fields):",
    ]
    for f in schema.get("fields", []):
        t = f.get("type", "text")
        if t == "objectives":
            shape = '{"commercial": "str", "behavioural": "str", "attitudinal": "str"}'
        elif t == "list":
            shape = '["item1", "item2", ...]'
        elif t == "tfd":
            shape = '{"think": "str", "feel": "str", "do": "str"}'
        else:
            shape = "string or null"
        lines += [
            f"\n## {f['id']}  ({f['label']})",
            f"Instruction: {f['prompt']}",
            f"Good: {f['good_example']}",
            f"Bad: {f['bad_example']} — Why bad: {f['bad_reason']}",
            f"Max words: {f.get('max_words', 'no limit')}  Value shape: {shape}",
        ]
    lines += [
        "",
        "NOTE: insight, smp, reasons_to_believe, desired_response are zone-3 STRATEGY",
        "fields. Mark them client_stated ONLY if the brief explicitly articulates that",
        "strategic element in its own words. A market fact, background, objective, or",
        "audience description is NOT an insight or a proposition — if the brief merely",
        "describes the situation, mark these fields MISSING (value null). Never repackage",
        "a background/market statement as the insight or SMP. Do NOT invent strategy.",
        "",
        'Return ONLY raw JSON, no fences:',
        '{"fields": {"<id>": {"value": <value>, "source": "client_stated"|"inferred"|"missing",',
        '  "confidence": 0.0-1.0, "source_quote": "verbatim" | null}, ...}}',
    ]
    return "\n".join(lines)


def extract_golden_brief(raw_text: str) -> "dict | None":
    """Per-field Golden Brief extraction using schema prompts + good/bad examples.
    Returns a dict with 'fields' key, or None on failure.

    One chain walk with a shape check (`accept`): a reply that parses but carries no
    `fields` is rejected like unparseable output, so the next link gets its turn. Before
    2026-09-25 this looped three times on link 1 with no accept, and a wrong-shaped reply
    never reached link 2 (audit F7)."""
    system = _build_golden_system()
    if not system:
        return None
    user = f"CLIENT BRIEF:\n\"\"\"\n{_clip_brief(raw_text)}\n\"\"\""
    return _json_call(user, system=system, retries=1, max_tokens=MAXTOK_EXTRACT,
                      accept=lambda o: isinstance(o, dict) and isinstance(o.get("fields"), dict)
                      and bool(o["fields"]))


def _fv(field) -> str:
    """Safely extract .value from a golden field that may be a dict or plain string."""
    if isinstance(field, dict):
        return str(field.get("value") or "")
    return str(field or "")


def _word_count(v) -> int:
    """Count whitespace-separated words in a value, recursing into dict values and list
    items so a structured field is counted as a whole. Any other value is counted via
    str(), so None counts as one word."""
    if isinstance(v, dict):
        return sum(_word_count(x) for x in v.values())
    if isinstance(v, list):
        return sum(_word_count(x) for x in v)
    return len(str(v).split())


def _text_overlap(a: str, b: str) -> float:
    """Fraction of a's (3+ char) words that also appear in b. Used to detect a
    strategy field that just parrots a client fact."""
    wa = {w for w in re.findall(r"[a-z]{3,}", a.lower())}
    if not wa:
        return 0.0
    wb = {w for w in re.findall(r"[a-z]{3,}", b.lower())}
    return len(wa & wb) / len(wa)


_BRAND_BOILERPLATE_MARKERS = ("vision", "mission", "purpose", "brand promise",
                              "brand value", "campaign claim", "brand claim", "tagline")


def _brand_boilerplate(text: str) -> str:
    """Collect the brand's own vision / mission / claim / tagline lines from the brief
    (incl. attachments). Used to reject an SMP that just echoes the masterbrand line —
    an SMP must be a campaign CHOICE, not a restatement of the standing brand vision."""
    lines = []
    for ln in (text or "").splitlines():
        low = ln.lower()
        if any(m in low for m in _BRAND_BOILERPLATE_MARKERS):
            lines.append(ln.strip())
    return " ".join(lines)


def _rubric_hard(field, value, brand_lines: str = "") -> list:
    """The rubric's code tests (no model call): the schema's auto checks exactly as
    golden_critic runs them (word limit, one sentence, item cap, a stated 'why', think/feel/
    do all filled), a 'do' that is not an observable behaviour, an SMP that echoes the
    masterbrand line. Any failure here is final."""
    from golden_critic import AUTO, FAIL
    blob = (json.dumps(value).lower() if not isinstance(value, str) else value.lower())
    hard = []
    ids = [c["id"] for c in field.get("rubric") or [] if c.get("method") == "auto" and c["id"] in AUTO]
    if field.get("max_words") and "within_limit" not in ids:
        ids.append("within_limit")                   # a word limit applies whether or not listed
    for cid in ids:
        status, note = AUTO[cid](field, value)
        if status == FAIL:
            hard.append(f"{cid}: {note}")
    if field.get("type") == "tfd" and any(v in blob for v in FORBIDDEN_DO_VERBS):
        hard.append("'do' is not an observable behaviour (engage/explore/interact)")
    return hard


def _rubric_flags(field, value, brand_lines: str = "") -> list:
    """Notes shown to a human, never scored (R1 2026-09-24 §3 'FLAG'): an SMP that echoes
    the brand's standing vision / claim / tagline. Until 2026-09-26 this was a hard fail;
    no source makes it one, and BBH's own Levi's and Forte Posthouse briefs restate the
    standing line on purpose."""
    flags = []
    if field.get("id") == "smp" and brand_lines and isinstance(value, str) \
            and _text_overlap(value, brand_lines) >= 0.5:
        flags.append("echoes the brand's standing vision/claim/tagline — confirm this campaign wants continuity")
    return flags


def _pass_rule(hard: list, soft: list, n_llm: int) -> bool:
    """The ONE pass rule for every generated field. A code (hard) failure is final. A field
    with three or more llm tests (insight, SMP) tolerates one failed llm test, so a single
    subjective verdict cannot sink it; a field with fewer tolerates none. Before 2026-09-25
    every field tolerated one soft failure, and the reasons to believe (one llm test:
    supports_smp) and desired response (one: ladders) could therefore never fail their judge
    (audit F1/G1). Sai's decision 2026-09-25: keep the tolerance for insight and SMP."""
    allowed = 1 if n_llm >= 3 else 0
    return not hard and len(soft) <= allowed


UNJUDGED = "unjudged"   # prefix of the failure a candidate carries when the judge gave no verdict


def _verdict(res, tid: str) -> "bool | None":
    """One strict verdict from a judge reply: True or False, or None when the judge did not
    answer this test in a form we can read. Accepts {"pass": bool}, {"verdict": "pass"|"fail"},
    a bare bool, and the strings true/false/pass/fail in any case. Everything else — a
    missing key, an empty object, a reason with no verdict — is None, and None never counts
    as a pass (audit F5: 7 of 8 malformed reply shapes used to pass every candidate)."""
    v = res.get(tid) if isinstance(res, dict) else None
    if isinstance(v, dict):
        v = v.get("pass") if "pass" in v else v.get("verdict")
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "pass", "passed", "yes"):
            return True
        if s in ("false", "fail", "failed", "no"):
            return False
    return None


def _why(res, tid: str) -> str:
    """The judge's reason for test `tid`, or '' when it gave none."""
    v = res.get(tid) if isinstance(res, dict) else None
    return str(v.get("why") or v.get("reason") or "") if isinstance(v, dict) else ""


def _conf(x) -> "float | None":
    """A model-reported confidence as a float in [0, 1], or None when it is missing or not a
    number in range (a bool, 'high', 1.7, ...). None counts as BELOW the floor: before
    2026-09-25 a missing or 0.0 confidence coerced to the floor and passed, while 'high'
    crashed the whole run (audit F6)."""
    if isinstance(x, bool) or x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if 0.0 <= f <= 1.0 else None


def _name_candidates(why: str, candidates: list) -> str:
    """Replace 'candidate 2' / '[2]' / 'draft 2' in a judge's note with the first six words
    of that draft, so review text reads as prose instead of an index into a list the reader
    never sees (audit H11). Both 0- and 1-based references are read as 0-based."""
    def name(i: int) -> str:
        """The first six words of candidate i, quoted; the reference unchanged if out of range."""
        if 0 <= i < len(candidates):
            words = _golden_text(candidates[i].get("value")).split()
            return '"' + " ".join(words[:6]) + ('…' if len(words) > 6 else '') + '"'
        return None
    def sub(m):
        """Swap one reference for the draft's opening words."""
        n = name(int(m.group("a") or m.group("b")))
        return n if n is not None else m.group(0)
    return re.sub(r"\b(?:candidate|draft|option)\s*#?\s*(?P<a>\d+)\b|\[(?P<b>\d+)\]",
                  sub, why or "", flags=re.I)


# A parenthesised aside about a sentence number goes whole; a bare 'sentence 12' or
# 'sentences 2-3' loses only the reference itself; '[5]' goes.
_MARKER_RE = re.compile(r"\(\s*sentences?\s+\d+[^)\n]{0,80}\)|\bsentences?\s+\d+(?:\s*[-–,]\s*\d+)*|\[\d+\]", re.I)


def _scrub_markers(text):
    """Strip the pipeline's internal references — '(sentence 35 says TBC)', 'sentences 2-3',
    '[5]' — from text that reaches a client or a reviewer (audit H11). Lists and dicts are
    scrubbed per item; other values pass through unchanged."""
    if isinstance(text, str):
        out = _MARKER_RE.sub("", text)
        out = re.sub(r"\s+([?.,;:!])", r"\1", out)     # 'impact ?' -> 'impact?'
        return re.sub(r"\s{2,}", " ", out).strip()
    if isinstance(text, list):
        return [_scrub_markers(x) for x in text]
    if isinstance(text, dict):
        return {k: _scrub_markers(v) for k, v in text.items()}
    return text


def _norm_quote(s: str) -> str:
    """Lower-case, punctuation-free, single-spaced text for verbatim-quote matching."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())).strip()


def _quote_in_brief(quote, brief_text: str) -> bool:
    """True when every fragment of `quote` (split on '...' / '…') appears verbatim in the
    brief, punctuation and case ignored. A missing or empty quote is False. This is the code
    check behind 'client-stated': the extractor's label alone proved unreliable (audit F4b:
    4 of 13 briefs shipped extractor prose as the client's words)."""
    if not isinstance(quote, str) or not quote.strip() or not brief_text:
        return False
    hay = _norm_quote(brief_text)
    frags = [f for f in (_norm_quote(p) for p in re.split(r"\.{3}|…", quote)) if f]
    return bool(frags) and all(f in hay for f in frags)


MAXTOK_BATCH_JUDGE = 3000   # one verdict per candidate x test (reasons on failures only) + ranking.
                            # 1500 could truncate on 6 SMP drafts x 5 tests; a ceiling, not a cost.


def _judge_and_gate(field, candidates, brand_lines: str = "", ctx: str = "", territory=None) -> list:
    """Rank the candidates AND run every llm rubric test (and, for the SMP, the two territory
    tests) on every candidate in ONE judge call. Returns [(candidate, passed, failures)]
    best-first. The pass rule is _pass_rule; a line off the brand's territory fails.

    This is the only gate: every generated field, hero or not, one draft or six, goes
    through it (audit C1 deleted the per-candidate _rubric_gate, the ranking-only judge and
    the separate territory gate — three fail-open paths).

    Strict verdicts (audit F5/J2): every candidate must carry a readable verdict for every
    test, or it is UNJUDGED: not ok, with a failure starting with 'unjudged'. Keys numbered
    from 1 are realigned (and logged); any other key set that does not cover the candidates
    leaves them unjudged. A judge that is down leaves every candidate unjudged, order kept,
    so the field becomes missing with an open question instead of an unchecked line."""
    llm_tests = [r for r in (field.get("rubric") or []) if r.get("method") == "llm"]
    many = len(candidates) > 1
    judge = None
    if llm_tests or territory or many:
        tests = "\n".join(f'- {r["id"]}: {r["test"]}' for r in llm_tests)
        if territory:
            tests += (f"\n- own_territory: does the line live on what the BRAND should own "
                      f"({territory['own']}) rather than on {territory['rival']}'s ground "
                      f"({territory['avoid']}) — counting synonyms and rephrasings?"
                      f"\n- brand_only: is this a line ONLY this brand can credibly say? "
                      f"(fail if {territory['rival']}'s own campaign could run it verbatim "
                      f"without changing its meaning)")
        listing = "\n".join(f"[{i}] {json.dumps(c.get('value'))}" for i, c in enumerate(candidates))
        # The ranking guidance depends on the field's shape. A hero LINE (insight, SMP) is
        # judged on purity and single-mindedness; a list field (reasons to believe) or a
        # think/feel/do set IS several items by design, and must not be failed for being one
        # (live check 2026-09-25: the hero wording failed a four-item RTB "for being a list").
        shape = field.get("type")
        if shape == "list":
            framing = ("The value is a LIST of items by design — judge the set as a whole on each test "
                       "and never fail it for having several items. Rank by how well the set supports the "
                       "proposition with specific, credible, non-invented reasons; penalise vague or "
                       "generic items.")
        elif shape == "tfd":
            framing = ("The value is a think / feel / do set by design. Judge it on each test as a set; "
                       "rank by how clearly the three ladder up to the objectives and how observable the "
                       "'do' is.")
        else:
            # R1 2026-09-24 §5.2: no "never an 'and'" (unsourced; Levi's and Corona contradict
            # it), and copy is named by its devices rather than as "restated taglines".
            framing = ("Then rank the candidates: reward one strategic choice stated plainly, a real "
                       "human tension specific to THIS brand, and a reason for this audience to care; "
                       "penalise category truths any rival could claim, lines that try to say two things, "
                       "and lines written as copy (a pun or double meaning, hype standing in for a thought, "
                       "the brand's sign-off line). Short or headline-able is not a fault.")
        judge = _json_call(
            (f"UPSTREAM CONTEXT (use this to judge derivation/ownability — do NOT re-test it):\n{ctx}\n\n"
             if ctx else "")
            + f"FIELD: {field['label']}\nGOOD shapes (different brands, do not copy):\n{_good_examples_block(field)}\n"
            f"BAD: {field.get('bad_example','')} ({field.get('bad_reason','')})\n"
            + ("PROPOSITION vs COPY, for the not-a-tagline test:\n"
               + "\n".join(f"  - {c}" for c in field["contrast_examples"]) + "\n"
               if field.get("contrast_examples") else "")
            + f"\nTESTS (judge EVERY candidate on each):\n{tests}\n\nCANDIDATES:\n{listing}",
            accept=lambda o: isinstance(o, dict) and isinstance(o.get("results"), dict),
            system=("You are a strategy director judging candidate '" + field["label"] + "' values for a "
                    "creative brief — fair but rigorous. Judge each candidate on each test on its own "
                    "merits, using the upstream context where given (do not fail derivation merely because "
                    "the context wasn't repeated in the value). " + framing
                    + " Candidate indexes start at 0. Return ONLY raw JSON: "
                    '{"results": {"<candidate index>": {"<test_id>": {"pass": true|false, "why": "short, '
                    'ONLY when pass is false"}}}, "ranking": [candidate indexes, best first], '
                    '"why": "one line on the winner"}'),
            retries=1, max_tokens=MAXTOK_BATCH_JUDGE, whole=True)
    judge = judge if isinstance(judge, dict) else {}
    n = len(candidates)
    results = judge.get("results") if isinstance(judge.get("results"), dict) else {}
    results = {str(k): v for k, v in results.items()}
    shift = 0
    if n and set(results) == {str(i) for i in range(1, n + 1)}:
        shift = 1                                    # the model numbered from 1: realign
        print(f"[i] judge for '{field.get('id')}' numbered candidates from 1; realigned.",
              file=sys.stderr)
    order = [int(i) - shift for i in (judge.get("ranking") or [])
             if (isinstance(i, int) and not isinstance(i, bool)) or (isinstance(i, str) and i.isdigit())]
    order = [i for i in order if 0 <= i < n]
    order = list(dict.fromkeys(order)) + [i for i in range(n) if i not in order]
    judged_tests = [r["id"] for r in llm_tests] + (["own_territory", "brand_only"] if territory else [])
    out = []
    for rank, i in enumerate(order):
        c = candidates[i]
        if rank == 0 and many and isinstance(judge.get("why"), str):
            c = {**c, "_judge_why": _name_candidates(judge["why"], candidates)}
        flags = _rubric_flags(field, c["value"], brand_lines)
        if flags:
            c = {**c, "_flags": flags}
        res = results.get(str(i + shift))
        res = res if isinstance(res, dict) else {}
        hard = _rubric_hard(field, c["value"], brand_lines)
        verdicts = {tid: _verdict(res, tid) for tid in judged_tests}
        missing = [tid for tid, v in verdicts.items() if v is None]
        if missing:
            reason = "judge unavailable" if not judge else f"no verdict for {', '.join(missing)}"
            out.append((c, False, hard + [f"{UNJUDGED}: {reason}"]))
            continue
        # An llm test the schema marks tolerance:hard (the SMP's single_minded and
        # derives_from, R1 §5.5) fails the draft outright; the rest count against the
        # field's tolerance. The territory tests are soft too since 2026-09-26 (D6:
        # differentiation is contested; no source makes "a rival could run it" fatal).
        soft = []
        for r in llm_tests:
            if verdicts[r["id"]] is False:
                note = f'{r["id"]}: {_why(res, r["id"]) or "failed"}'
                (hard if r.get("tolerance") == "hard" else soft).append(note)
        if territory and (verdicts["own_territory"] is False or verdicts["brand_only"] is False):
            why = _why(res, "own_territory") or _why(res, "brand_only")
            soft.append(f"walks onto the competitor's ground: {why}")
        ok = _pass_rule(hard, soft, len(llm_tests))
        out.append((c, ok, hard + soft))
    return out


def _refine_field(field, value, note: str = ""):
    """One sharpening pass on the chosen hero value — purer, more single-minded, more
    ownable. Returns a candidate dict, or None on failure (caller keeps the original)."""
    system = _gen_field_system(field) + (
        "\n\nREFINE MODE: you are given a strong draft. Make it PURER and more single-minded — "
        "one idea only, sharper, more ownable. Keep what already works; never add a second idea. "
        "If it is already optimal, return it unchanged.")
    user = (f"DRAFT '{field['label']}' to sharpen:\n{json.dumps(value)}\n"
            + (f"\nDirector's note: {note}\n" if note else "")
            + "Return the improved value in the same JSON shape.")
    raw = _json_call(user, system=system, max_tokens=MAXTOK_GEN)
    return raw if isinstance(raw, dict) and raw.get("value") else None


# Distinct proposition TYPES seeded one-per-draft so the SMP tournament gets a real spread
# instead of N identical draws. The types are the ones the sources name (R1 2026-09-24
# §5.2): Ogilvy's DO brief ("a killer fact, a promise, a straight message, or just a plain
# and simple big idea") and Weichselbaum p.305 (a product point, a practical benefit, an
# emotional benefit). The previous seeds (what the audience settles for; how they are
# judged by others) were unsourced and pushed every draft toward social-judgement lines.
SMP_ANGLE_SEEDS = (
    "build it on a killer fact: one true, specific thing about this brand or product that changes the audience's view",
    "build it on a promise: the practical benefit this audience gets that rivals do not deliver as well",
    "build it on an emotional benefit: how this audience feels, or is freed from feeling, because of this brand",
    "build it on a plain, simple big idea: the single thought that reframes the category for this audience",
)


def _smp_territory(brief_text: str, competitor_ctx: str) -> "dict | None":
    """Map the SMP's ownable white space in one call. Returns {own, avoid, rival}:
    `own`  — the territory THIS brand should claim (its white space, per the brief);
    `avoid`— the emotional/territorial ground the named competitor ALREADY owns;
    `rival`— the competitor's name (for the 'could they run this line?' kill-test).
    Returns None when there is nothing to map or the call fails on every link: the SMP then
    skips the territory tests and the brief asks which competitor the proposition must beat.
    Before 2026-09-25 a failure substituted a placeholder rival ('the named competitor'),
    so the territory tests judged a line against nobody (audit J12)."""
    if not (brief_text or competitor_ctx):
        return None
    obj = _json_call(
        f"BRIEF:\n\"\"\"\n{_clip_brief(brief_text or '')}\n\"\"\"\n\nCOMPETITOR CONTEXT: {competitor_ctx}",
        system=("You map strategic white space for a single-minded proposition. From the brief and the "
                "competitor context identify three things: the named competitor; the emotional/territorial "
                "ground that competitor ALREADY OWNS (so we steer away from it — give the concept plus its "
                "common synonyms); and the adjacent white space THIS brand should claim instead (its real, "
                "ownable edge as the brief itself describes it). Be concrete and short. Return ONLY raw "
                'JSON: {"rival": "competitor name", "avoid": "the concept they own + synonyms", '
                '"own": "the white space this brand should claim"}'),
        retries=1, max_tokens=MAXTOK_GEN,
        accept=lambda o: isinstance(o, dict) and bool(o.get("own")) and bool(o.get("avoid"))
        and bool(o.get("rival")))
    if isinstance(obj, dict) and obj.get("own") and obj.get("avoid") and obj.get("rival"):
        return {"own": str(obj["own"]), "avoid": str(obj["avoid"]), "rival": str(obj["rival"])}
    return None


FULLTEXT_IPA_CHARS = 1200     # BRIEF_FULLTEXT arm: per IPA precedent (5 max)
FULLTEXT_METHOD_CHARS = 800   # BRIEF_FULLTEXT arm: per playbook method (3 max)


def _precedent_blocks(loops: dict, key: str):
    """Pull a loop's retrieved evidence into (ipa_block, method_block, evidence_ids):
    IPA effectiveness cases (shape/depth exemplars) and playbook/framework snippets.
    Each hero field reads ITS OWN loop — insight←loop4 (tension), smp←loop5 (the
    proposition playbook, incl. the single-minded-proposition rulebook)."""
    loop = (loops or {}).get(key) or {}
    ipa_ex, methods, ev_ids = [], [], []
    full = os.environ.get("BRIEF_FULLTEXT", "").lower() in ("1", "true", "yes")
    for e in (loop.get("evidence") or []):
        # BRIEF_FULLTEXT=1 is the A/B arm Sai approved: the generator reads the evidence
        # span (`text`, capped) instead of the 280-char display snippet clipped again below.
        snip = ((e.get("text") if full else None) or e.get("snippet") or "").strip()
        if not snip:
            continue
        src = e.get("source") or e.get("framework") or e.get("citation") or ""
        if (e.get("category") or "") == "ipa_effectiveness_case":
            ipa_ex.append(f"- {snip[:FULLTEXT_IPA_CHARS if full else 220]}")
            if src:
                ev_ids.append(src)
        else:
            methods.append(f"- {snip[:FULLTEXT_METHOD_CHARS if full else 160]}")
    return ("\n".join(ipa_ex[:5]) or "(no IPA precedent retrieved)",
            "\n".join(methods[:3]) or "(no playbook evidence)", ev_ids)


def fill_derivable_fields(golden_fields: dict, loop37_result: dict, schema: dict, brief_text: str = ""):
    """Guided-generative fill of the zone-3 strategy fields (insight → smp →
    reasons_to_believe → desired_response), schema-driven via each field's
    depends_on and rubric. A field is generated ONLY if its extracted source is
    missing/inferred — a client_stated value is never overwritten (the no-invent
    invariant), but 'client_stated' must first be PROVED: the value needs a verbatim
    source_quote from the brief that it matches, else it is the extractor's own writing
    and is generated and gated like any other (audit F4b). A genuine client line that
    breaks a code rule is kept as written and raises an open question.
    Each generated value is rubric-gated by _judge_and_gate (the one gate); a failure
    downgrades the field to 'missing' and surfaces an open question. A crash in one field
    becomes that field's 'missing' plus an open question, never a failed run (audit F6).
    Mutates golden_fields in place so a later field can read a freshly generated upstream
    one (smp reads insight). Returns (fills, open_questions)."""
    if not resolve_provider():
        return {}, []
    floor = float(schema.get("confidence_floor") or 0.6)

    # Precedent pools: each hero field draws on ITS OWN loop's retrieval. The insight reads
    # Loop 4 (human tension); the SMP reads Loop 5 (the proposition playbook — incl. the
    # single-minded-proposition rulebook). SMP falls back to Loop 4 if Loop 5 was empty.
    loops = loop37_result.get("loops") or {}
    insight_ipa, insight_methods, insight_ev = _precedent_blocks(loops, "loop4_insight")
    smp_ipa, smp_methods, smp_ev = _precedent_blocks(loops, "loop5_proposition")
    if smp_ipa.startswith("(no") and smp_methods.startswith("(no"):
        smp_ipa, smp_methods, smp_ev = insight_ipa, insight_methods, insight_ev

    def val(fid):
        """Return golden field `fid` as text (via _fv), or '' when it is absent.
        Reads golden_fields live, so it sees values generated earlier in this fill."""
        f = golden_fields.get(fid)
        return _fv(f) if f else ""

    # Guard: a client_stated strategy field is kept only when the client really said it.
    #   1. Its source_quote must be verbatim in the brief and the value must match the
    #      quote (>= 60% of its words), else it is the extractor's paraphrase: generation
    #      owns it (audit F4b: 4 of 13 briefs shipped ungated extractor text this way).
    #   2. A value that substantially echoes the client's fact fields is a background or
    #      market statement, not a strategy statement: downgraded, nothing lost (the text
    #      still lives in its real fact field).
    #   3. A masterbrand vision/tagline is not a campaign proposition: downgraded.
    #   4. A genuine client line that breaks a code rule is kept as written (never rewrite
    #      the client) and an open question is raised.
    fact_blob = " ".join(val(f) for f in
                         ("background", "audience", "objectives", "competitor_context"))
    brand_blob = _brand_boilerplate(brief_text)
    guard_qs = []
    for fid in GEN_ZONE3_ORDER:
        cur = golden_fields.get(fid) or {}
        if not (isinstance(cur, dict) and cur.get("source") == "client_stated"):
            continue
        v = _golden_text(cur.get("value"))
        quote = str(cur.get("source_quote") or "")
        field = _field_by_id(schema, fid) or {"id": fid, "label": fid}
        if not _quote_in_brief(quote, brief_text):
            reason = "labelled client_stated without a verbatim source quote from the brief"
        elif _text_overlap(v, quote) < 0.6:
            reason = (f"extractor paraphrase, not client wording (matches {_text_overlap(v, quote):.0%} "
                      f"of its own quote)")
        elif _text_overlap(v, fact_blob) >= 0.6:
            reason = "extracted value echoed a client fact, not a distinct strategy statement"
        elif fid in ("smp", "insight") and brand_blob and _text_overlap(v, brand_blob) >= 0.5:
            reason = "echoed the masterbrand vision/claim, not a campaign-specific proposition"
        else:
            hard = _rubric_hard(field, cur.get("value"), brand_blob)
            if hard:
                guard_qs.append({"question": f"The brief's own {field['label'].lower()} breaks our rules "
                                             f"({'; '.join(hard)}) — agree a version that keeps them.",
                                 "why_it_matters": "client wording is kept as written, never rewritten",
                                 "priority": "high" if field.get("hero") else "medium", "blocks_field": fid})
            continue
        print(f"[i] {fid}: client_stated label rejected — {reason}; generating it instead.",
              file=sys.stderr)
        golden_fields[fid] = {"value": None, "source": "missing", "reason": reason,
                              "rejected_attempt": cur.get("value")}

    from concurrent.futures import ThreadPoolExecutor
    parallel = os.environ.get("BRIEF_PARALLEL", "1").lower() not in ("0", "false", "no")

    def gate_one(field, value, ctx, territory=None) -> bool:
        """Does one value clear its field's rubric (and, given a territory, the SMP's
        territory tests)? One judge call through the one gate."""
        return _judge_and_gate(field, [{"value": value}], brand_blob, ctx, territory)[0][1]

    def _one(fid):
        """Generate, judge, gate and sharpen ONE field. Returns (entry or None, open questions).
        Writes golden_fields[fid] so a later field can read it."""
        qs = []
        field = _field_by_id(schema, fid)
        if not field:
            return None, qs
        cur = golden_fields.get(fid) or {}
        if isinstance(cur, dict) and cur.get("source") == "client_stated":
            return None, qs  # never overwrite a client fact

        deps = list(field.get("depends_on", []))
        # The sharpest captured thinking (the white space vs the competitor) must reach
        # the insight/SMP generator, not sit in a fact field it never reads.
        if fid in ("insight", "smp") and "competitor_context" not in deps:
            deps.append("competitor_context")
        ctx = "\n".join(f"{d}: {val(d)}" for d in deps if val(d))
        ctx = (ctx + f"\nbackground: {val('background')}").strip()
        use_ipa = fid in ("insight", "smp")
        if fid == "smp":
            f_ipa, f_methods, f_ev = smp_ipa, smp_methods, smp_ev
            rules_label = "PROPOSITION RULEBOOK (apply these rules — how a single-minded proposition is written)"
        else:
            f_ipa, f_methods, f_ev = insight_ipa, insight_methods, insight_ev
            rules_label = "PLANNING FRAMEWORKS"
        user = (
            "BRIEF CONTEXT:\n" + ctx + "\n\n"
            + (f"AWARD-WINNING PRECEDENT (shape & depth only — do not copy):\n{f_ipa}\n\n"
               f"{rules_label}:\n{f_methods}\n\n" if use_ipa else "")
            + f"Write the '{field['label']}' for THIS brand now."
        )
        system = _gen_field_system(field)

        # Hero fields (insight, smp) run a TOURNAMENT: generate N candidates, rank them by
        # purity/ownability, pick the best that clears the rubric, then one sharpen pass.
        # Non-hero fields generate once. N is tunable (a stronger model needs fewer).
        n_cand = int(os.environ.get("BRIEF_HERO_CANDIDATES", "4")) if fid in ("insight", "smp") else 1
        # The SMP is the brief's hardest field to own — map its white space once, generate
        # a wider, angle-seeded spread, and gate every candidate on territory (below).
        territory = None
        if fid == "smp":
            n_cand = max(n_cand, int(os.environ.get("BRIEF_SMP_CANDIDATES", "6")))
            territory = f_terr.result() if f_terr else _smp_territory(brief_text, val("competitor_context"))
            if territory is None:
                # No mapped competitor: the territory tests are skipped, and the brief says so
                # instead of testing the line against a placeholder rival (audit J12).
                qs.append({"question": "Which competitor must the proposition beat?",
                           "why_it_matters": "the SMP's ownable-territory tests could not run: no "
                                             "competitor could be mapped from the brief",
                           "priority": "medium", "blocks_field": "smp"})
        # SMP territory block — built once, reused by the batched call and the fallback.
        terr_block = ""
        if fid == "smp" and territory:
            terr_block = (
                f"\n\nOWNABLE TERRITORY — CLAIM THIS: {territory['own']}.\n"
                f"DO NOT walk onto {territory['rival']}'s ground ({territory['avoid']}); a "
                f"proposition {territory['rival']} could also run is a FAIL — find the white space.\n"
                f"This is a single-minded PROPOSITION — the ONE thing to make the audience believe — "
                f"and it MUST visibly derive from the insight above (the reader should see the line "
                f"through to the insight). Write the strategic proposition itself, NOT written as "
                f"copy: no puns, slogans or sign-off lines. Short is fine.")

        candidates = []
        if n_cand > 1:
            # BATCHED tournament: ONE call returns all N drafts, so the heavy context
            # (brief ctx + precedent + rulebook) is sent once instead of N times (~80%
            # input cut on the heaviest phase) — and the model can differentiate its own
            # drafts, which gives a wider spread than N independent samples. whole=True:
            # a reply cut off mid-list is retried, never read as one draft (audit F8).
            u = user + terr_block
            if fid == "smp" and territory:
                seeds = "\n".join(
                    f"  draft {i+1} — pull the idea this way (do not name the angle): "
                    f"{SMP_ANGLE_SEEDS[i % len(SMP_ANGLE_SEEDS)]}" for i in range(n_cand))
                u += f"\nANGLES — one per draft:\n{seeds}"
            batched = _json_call(u, system=_gen_field_system(field, n=n_cand),
                                 max_tokens=MAXTOK_GEN * 2, whole=True,
                                 accept=lambda o: bool(_coerce_candidates(o)))
            candidates = _coerce_candidates(batched)[:n_cand]
        if not candidates:
            # Single-draft path: non-hero fields, or fallback when the batched call
            # exhausted the chain (never fail the tournament over one bad response).
            for i in range(max(1, n_cand)):
                u = user + terr_block
                if fid == "smp" and territory:
                    seed = SMP_ANGLE_SEEDS[i % len(SMP_ANGLE_SEEDS)]
                    u += f"\nANGLE FOR THIS DRAFT (pull the idea this way, do not name the angle): {seed}"
                raw = _json_call(u, system=system, max_tokens=MAXTOK_GEN)
                if isinstance(raw, dict) and raw.get("value"):
                    candidates.append(raw)
        if not candidates:
            golden_fields[fid] = {"value": None, "source": "missing",
                                  "reason": "generation produced no output"}
            qs.append({"question": f"Agree the {field['label'].lower()} — none could be derived.",
                            "priority": "high" if field.get("hero") else "medium", "blocks_field": fid})
            return None, qs

        # One call ranks every draft and runs every test on it. The best-ranked draft that
        # passes wins; if none passes, the best-ranked one carries its failures.
        chosen, chosen_fail, chosen_notes = None, None, []
        judged = _judge_and_gate(field, candidates, brand_blob, ctx, territory)
        candidates = [c for c, _ok, _f in judged]
        for c, ok, fails in judged:
            if ok:
                chosen, chosen_fail, chosen_notes = c, [], list(fails)   # tolerated soft failures
                break
            if chosen is None:
                chosen, chosen_fail = c, fails
        unjudged = any(f.startswith(UNJUDGED) for f in chosen_fail or [])
        n_llm = sum(1 for r in field.get("rubric") or [] if r.get("method") == "llm")
        # Code-rule repair: the best draft broke ONLY the schema's code rules (two sentences,
        # over the word limit, too many items, no stated 'why') — one rewrite that fixes
        # exactly those, re-checked, before the field is given up as missing. Not attempted
        # when the judge gave no verdict: the re-check could not pass either.
        if chosen_fail and not unjudged:
            hard = _rubric_hard(field, chosen["value"], brand_blob)
            soft = [f for f in chosen_fail if f not in hard]
            if hard and _pass_rule([], soft, n_llm):     # fixing the code rules would pass
                resc = _refine_field(field, chosen["value"],
                                     note="Fix exactly this and keep everything else: " + "; ".join(hard))
                if resc and gate_one(field, resc["value"], ctx, territory if fid == "smp" else None):
                    chosen = {**resc, "_judge_why": chosen.get("_judge_why", "")}
                    chosen_fail = []
        # SMP territory rescue: if no candidate could both pass the rubric AND hold the white
        # space, push the best draft off the competitor's ground once before giving up.
        if fid == "smp" and territory and chosen_fail and not unjudged:
            note = (f"This proposition walks onto {territory['rival']}'s ground ({territory['avoid']}). "
                    f"Rewrite it to claim the brand's own white space: {territory['own']}. Keep the same "
                    f"underlying insight, ONE idea only, within the word limit — a line "
                    f"{territory['rival']} could not credibly run. Make it a strategic PROPOSITION that "
                    f"derives from the insight, not written as copy (no puns, slogans or sign-off lines).")
            resc = _refine_field(field, chosen["value"], note=note)
            if resc:
                if gate_one(field, resc["value"], ctx, territory):
                    chosen = {**resc, "_judge_why": chosen.get("_judge_why", "")}
                    chosen_fail = []
        conf = _conf(chosen.get("confidence"))

        if chosen_fail or conf is None or conf < floor:
            why = ("; ".join(chosen_fail) if chosen_fail
                   else "no confidence reported" if conf is None
                   else f"confidence {conf:.2f} < floor {floor}")
            golden_fields[fid] = {
                "value": None, "source": "missing", "reason": why,
                "rejected_attempt": chosen.get("value"),
            }
            if unjudged:
                golden_fields[fid]["unjudged"] = True
            qs.append({"question": f"Agree the {field['label'].lower()}"
                                   + (" — the quality judge was unavailable, so the draft was not checked."
                                      if unjudged else "."),
                       "why_it_matters": why,
                       "priority": "high" if field.get("hero") else "medium", "blocks_field": fid})
            return None, qs

        # Sharpen the winning hero line once; keep the refinement only if it still clears the gate.
        if fid in ("insight", "smp"):
            refined = _refine_field(field, chosen["value"], chosen.get("_judge_why", ""))
            if refined:
                # gate_one includes the territory tests for the SMP: never let the sharpen
                # pass drift it back onto the competitor's ground.
                rconf = _conf(refined.get("confidence"))
                if rconf is not None and rconf >= floor \
                        and gate_one(field, refined["value"], ctx, territory if fid == "smp" else None):
                    refined["_judge_why"] = chosen.get("_judge_why", "")
                    chosen, conf = refined, rconf

        entry = {"value": chosen["value"], "source": "inferred",
                 "method": f"gen:{fid}", "confidence": round(conf, 2)}
        if chosen.get("rationale"):
            entry["rationale"] = chosen["rationale"]
        if chosen.get("_judge_why"):
            entry["judge_note"] = chosen["_judge_why"]
        # What the gate tolerated and what it flags, kept so a judge's reasons can be
        # audited later (R1 §5.2: without stored reasons "reads like a tagline" cannot be
        # checked against itself).
        if chosen_notes:
            entry["gate_notes"] = chosen_notes
        flags = _rubric_flags(field, chosen["value"], brand_blob)   # recomputed: the sharpen pass may have replaced the draft
        if flags:
            entry["flags"] = flags
        if use_ipa and f_ev:
            entry["evidence_ids"] = f_ev[:5]
        alts = [c["value"] for c in candidates if c.get("value") != chosen["value"]]
        if alts:
            entry["alternatives"] = alts[:3]
        golden_fields[fid] = entry  # downstream deps see the generated value
        return entry, qs

    def _one_safe(fid):
        """_one with a net: an exception in one field (a bad reply shape, a bug) makes THAT
        field missing with an open question, and the other fields carry on. Before
        2026-09-25 it propagated out of run() and the app silently re-ran the brief with no
        strategy at all (audit F6/CC13)."""
        try:
            return _one(fid)
        except Exception as e:
            import traceback
            print(f"[!] generation of '{fid}' failed ({e.__class__.__name__}: {e}); the field is "
                  f"left open.", file=sys.stderr)
            traceback.print_exc()
            label = (_field_by_id(schema, fid) or {}).get("label", fid)
            golden_fields[fid] = {"value": None, "source": "missing",
                                  "reason": f"generation error: {e.__class__.__name__}: {e}"}
            return None, [{"question": f"Agree the {label.lower()} — it could not be generated.",
                           "why_it_matters": f"generation error: {e.__class__.__name__}",
                           "priority": "high", "blocks_field": fid}]

    # Waves from depends_on: a field waits only for the generated fields it reads. Measured
    # order insight -> smp -> {reasons_to_believe, desired_response}: the last two both read
    # the smp and not each other, so they run together. The SMP's territory map needs only the
    # brief, so it starts at once, alongside the insight.
    waves: dict[str, int] = {}
    for fid in GEN_ZONE3_ORDER:
        deps = (_field_by_id(schema, fid) or {}).get("depends_on", [])
        waves[fid] = 1 + max((waves[d] for d in deps if d in waves), default=-1)
    results = {}
    smp_cur = golden_fields.get("smp") or {}
    smp_generated = not (isinstance(smp_cur, dict) and smp_cur.get("source") == "client_stated")
    with ThreadPoolExecutor(max_workers=4) if parallel else _Inline() as ex:
        f_terr = (ex.submit(_scoped(_smp_territory), brief_text, val("competitor_context"))
                  if smp_generated and _field_by_id(schema, "smp") else None)
        for w in sorted(set(waves.values())):
            wave = [f for f in GEN_ZONE3_ORDER if waves[f] == w]
            for fid, fut in [(f, ex.submit(_scoped(_one_safe), f)) for f in wave]:
                results[fid] = fut.result()
    fills, open_qs = {}, list(guard_qs)
    for fid in GEN_ZONE3_ORDER:                     # canonical order, whatever finished first
        entry, qs = results.get(fid) or (None, [])
        if entry:
            fills[fid] = entry
        open_qs += qs
    return fills, open_qs


# provider -> (default base_url, api-key env var, default model)
# nim = NVIDIA NIM (Nemotron) via build.nvidia.com — free for prototyping on Inception.
PROVIDERS = {
    "nim":    ("https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY",
               "nvidia/llama-3.1-nemotron-70b-instruct"),
    # Free-tier, OpenAI-compatible alternatives — faster/steadier than NIM for this
    # workload. Get a free key, set BRIEF_PROVIDER + the key env var, done.
    "groq":     ("https://api.groq.com/openai/v1", "GROQ_API_KEY", "llama-3.3-70b-versatile"),
    "cerebras": ("https://api.cerebras.ai/v1", "CEREBRAS_API_KEY", "llama-3.3-70b"),
    "gemini":   ("https://generativelanguage.googleapis.com/v1beta/openai",
                 "GEMINI_API_KEY", "gemini-2.0-flash"),
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY", "gpt-4o"),
    "ollama": ("http://localhost:11434/v1", None, "llama3.1"),
}


def resolve_provider() -> str:
    """Explicit BRIEF_PROVIDER wins; otherwise auto-detect from whichever key is set."""
    p = os.environ.get("BRIEF_PROVIDER", "").lower().strip()
    if p:
        return p
    for env, prov in (("GROQ_API_KEY", "groq"), ("CEREBRAS_API_KEY", "cerebras"),
                      ("GEMINI_API_KEY", "gemini"), ("NVIDIA_API_KEY", "nim"),
                      ("ANTHROPIC_API_KEY", "anthropic"), ("OPENAI_API_KEY", "openai")):
        if os.environ.get(env):
            return prov
    return ""


def model_for(provider: str) -> str:
    """Return the model id for `provider`: BRIEF_MODEL when set, 'claude-opus-4-6' for
    anthropic, else the provider's default in PROVIDERS ('?' for an unknown provider).
    Used for the run's extraction_mode label and as the default model of the Anthropic path,
    the loop synthesis and a pinned provider's lead link."""
    if os.environ.get("BRIEF_MODEL"):
        return os.environ["BRIEF_MODEL"]
    if provider == "anthropic":
        return "claude-opus-4-6"
    return PROVIDERS.get(provider, (None, None, "?"))[2]


# One consistent clip for every prompt that embeds the brief. Judge/strategy calls
# (scorecard, golden extract, territory) don't need the tail of a long deck; extraction
# gets a much more generous window because Loop 1 is the no-loss capture (the project's
# one hard rule) — starving it would drop segments from the ledger's LLM mapping.
CLIP_JUDGE = int(os.environ.get("BRIEF_CLIP_CHARS", "6500"))
CLIP_EXTRACT = int(os.environ.get("BRIEF_CLIP_EXTRACT_CHARS", "12000"))


def _clip_brief(text: str, limit: int = CLIP_JUDGE) -> str:
    """Clip at a sentence/line boundary near the limit so the model never sees a
    mid-word truncation."""
    if not text or len(text) <= limit:
        return text
    cut = text[:limit]
    for m in (cut.rfind(". "), cut.rfind(".\n"), cut.rfind("\n")):
        if m > limit * 0.85:
            return cut[:m + 1]
    return cut


def _user_msg(text, schema):
    """Build the user message for extract_llm: the brief-object schema as indented JSON,
    cut to its first 2,500 characters, then the client brief clipped to CLIP_EXTRACT at a
    sentence or line boundary."""
    return (f"SCHEMA KEYS:\n{json.dumps(schema, indent=2)[:2500]}\n\n"
            f"CLIENT BRIEF:\n\"\"\"\n{_clip_brief(text, CLIP_EXTRACT)}\n\"\"\"")


class _RateLimited(RuntimeError):
    """A link returned HTTP 429. Raised fast (no backoff) so the chain fails over and the
    link is put on a short cooldown — see _LINK_COOLDOWN."""


class _Truncated(RuntimeError):
    """The reply hit the output cap (stop_reason max_tokens / finish_reason length). Raised
    instead of returning the partial text, so _json_call retries once with more room and
    never parses half a reply — a tournament cut off mid-list used to become one draft
    (audit F8)."""


class _NoEvidence(RuntimeError):
    """Retrieval ran but returned no evidence at all: loops_3_7 falls back to the digests
    with that reason recorded instead of reporting an enabled, empty RAG run (audit RAG-2)."""


class NoClaudeAvailable(RuntimeError):
    """The chain is Claude-only and no route to Claude exists (no API key for transport
    api, no `claude` CLI for transport cli). Raised by run() before any call, so a brief is
    never quietly written by another model (Sai, 2026-09-25)."""


class _Refused(RuntimeError):
    """The model declined to answer (stop_reason refusal). Logged loudly and counted; the
    chain moves on to its next link, which by default is another Claude model or nothing
    (audit BW3: a refusal used to be handed silently to a non-Claude link)."""


# Right-sized output budgets per call class. max_tokens counts against free-tier
# TPM budgets (Groq bills the CAP, not actual output), so a global 4000 was ~60%
# waste — judges return ~100-token verdicts. BRIEF_MAX_TOKENS overrides everything.
MAXTOK_JUDGE = 500       # rubric gates / judges / territory gate / rerank: tiny JSON verdicts
MAXTOK_GEN = 1200        # candidate generation / refine: one field's worth of text
MAXTOK_SYNTH_ONE = 700    # one loop's synthesis paragraph (was 2500 for all five in one call)
MAXTOK_EXTRACT = 8000    # extraction / scorecard / golden: one big JSON. Was 2500: measured 2026-09-23 on a
                         # real 4,156-char client brief, Loop-1 extraction hit stop_reason=max_tokens
                         # at 2,500 output tokens, the JSON was cut off, and every chain link failed the same way.
                         # A ceiling, not a cost: billing is per token actually produced.


# Per-request timeout for the OpenAI-compatible links (NIM, Groq, Cerebras, …). Was 300 s,
# retried 3x: on 2026-09-24 a fallback gpt-oss link timed out three times and held one
# brief for 941 s. 90 s still covers the slowest normal reply measured (~40 s).
LINK_TIMEOUT_S = float(os.environ.get("BRIEF_LINK_TIMEOUT", "90"))


def _chat_openai_compatible(base_url, key, model, user, provider_label="llm",
                            timeout=None, system=None, max_tokens=None, json_mode=False, schema=None):
    """One code path for NVIDIA NIM, OpenAI, and Ollama — all OpenAI-compatible."""
    timeout = timeout or LINK_TIMEOUT_S
    # NOTE: do NOT prepend a "detailed thinking off" system message for
    # Nemotron — on NIM a second system message displaces the real one and
    # the model ignores the JSON instruction entirely (verified 2026-06-10).
    messages = [{"role": "system", "content": system or EXTRACTION_SYSTEM},
                {"role": "user", "content": user}]
    payload = {
        "model": model, "temperature": 0.2,
        "max_tokens": int(os.environ.get("BRIEF_MAX_TOKENS") or max_tokens or MAXTOK_EXTRACT),
        "messages": messages,
    }
    # Structured-output mode: cuts the unclean-JSON retries that multiply calls through
    # the chain. A 400 drops the flag and retries the same link (see the handler below),
    # so naming a provider that turns out not to accept it costs one round trip rather
    # than a chain hop — which is why `nim` is included despite being the backstop.
    if schema and json_mode:
        # A schema is a hard constraint, not a hint: the model cannot return prose.
        payload["response_format"] = {"type": "json_schema",
                                      "json_schema": {"name": "extraction", "strict": True,
                                                      "schema": schema}}
    elif json_mode and provider_label.split(":", 1)[0] in ("groq", "cerebras", "openai", "nim"):
        payload["response_format"] = {"type": "json_object"}
    # Disable thinking only for reasoning models — non-reasoning models (e.g.
    # llama-3.3-70b) don't support this flag and may error on it.
    if "reasoning" in model or "thinking" in model:
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    body = json.dumps(payload).encode()
    # Groq/Cerebras sit behind Cloudflare, which blocks Python's default UA (error 1010).
    headers = {"Content-Type": "application/json", "User-Agent": _HTTP_UA}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(base_url.rstrip("/") + "/chat/completions",
                                 data=body, headers=headers, method="POST")
    _stats_call(provider_label, len(system or EXTRACTION_SYSTEM) + len(user))
    # Retry transient timeouts/network blips — these (not API errors) are what was
    # silently dropping briefs to heuristic mode on NIM.
    for attempt in range(3):
        led = _ledger()
        with _STATS_LOCK:
            led["http_attempts"] += 1
            if attempt:
                led["retries"] += 1
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read())
                choice = data["choices"][0]
                msg = choice["message"]
                content = msg.get("content") or ""
                # Reasoning models (e.g. nemotron-*-reasoning) emit <think>…</think>
                # before the answer. Strip it so _loads_lenient finds clean JSON.
                content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
                _stats_usage(data.get("usage"), len(content))
                if choice.get("finish_reason") == "length":
                    _stats_bump("truncations")
                    raise _Truncated(f"{provider_label}: reply hit the output cap")
                return content
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            # 429 = rate-limited: fail FAST so the chain fails over to a free sibling link
            # immediately, instead of burning ~15s backing off a provider that's saturated.
            if e.code == 429:
                _stats_bump("rate_limited")
                raise _RateLimited(f"HTTP 429 from {provider_label}") from None
            # A 400 right after adding response_format = this model rejects structured
            # output — drop the flag and retry the same link (don't burn a chain hop).
            if e.code == 400 and "response_format" in payload and attempt < 2:
                payload.pop("response_format", None)
                body = json.dumps(payload).encode()
                req = urllib.request.Request(base_url.rstrip("/") + "/chat/completions",
                                             data=body, headers=headers, method="POST")
                continue
            # Transient server errors: one short backoff, then hand off to the chain.
            if e.code in (500, 502, 503) and attempt < 2:
                import time
                print(f"[i] {provider_label} HTTP {e.code} (attempt {attempt+1}/3), backing off…",
                      file=sys.stderr)
                time.sleep(2 * (attempt + 1))
                continue
            # Name the failure class. A dead link and an unentitled one print the same
            # "link failed" today, and they need opposite responses: one is a config fix
            # you own, the other is a vendor conversation. Both went unnoticed for four
            # weeks behind a working lead link because the log never said which.
            if e.code == 410:
                raise RuntimeError(
                    f"HTTP 410 from {provider_label}: model RETIRED by the provider — "
                    f"replace it in the chain. {detail}") from None
            if e.code == 404 and "not found for account" in detail.lower():
                raise RuntimeError(
                    f"HTTP 404 from {provider_label}: model exists in the catalog but is "
                    f"NOT ENTITLED to this account — a vendor/tier question, not a config "
                    f"one. {detail}") from None
            raise RuntimeError(f"HTTP {e.code} from {provider_label}: {detail}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt < 2:
                import time
                print(f"[i] {provider_label} timeout/blip (attempt {attempt+1}/3), retrying…",
                      file=sys.stderr)
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(f"{provider_label} unreachable after 3 attempts: {e}") from None


def check_chain(verbose=True) -> dict:
    """Call every link in the CONFIGURED chain once and report which are alive.

    `list_models` already existed and answers a different question — what the catalog
    lists — which is not the same thing and was not enough: both dead links were a
    catalog lookup away from looking fine, and one of them IS still in the catalog. The
    only honest test of a fallback chain is to call it.

    Nothing exercises the backstop in normal operation, because it is only reached when
    the lead fails. So it rots silently and the first time you find out is the day the
    lead is down — the one day the fallback had to work. Wire this into --check and CI."""
    rows, alive = [], 0
    for provider, m in _model_chain(None):
        label = f"{provider}:{m}"
        try:
            out = _call_link(provider, m, 'Return {"ok": true} and nothing else.',
                             system="Return only raw JSON.", max_tokens=60, json_mode=True)
            clean = isinstance(out, str) and _loads_lenient(
                re.sub(r"^```(?:json)?|```$", "", (out or "").strip(), flags=re.MULTILINE)
            ) is not None
            rows.append({"link": label, "ok": True, "clean_json": clean})
            alive += 1
            if verbose:
                print(f"  {'OK  ' if clean else 'WARN'} {label}"
                      f"{'' if clean else '  — reachable but did not return clean JSON'}",
                      file=sys.stderr)
        except Exception as e:
            rows.append({"link": label, "ok": False, "error": str(e)[:200]})
            if verbose:
                print(f"  DEAD {label}\n       {str(e)[:180]}", file=sys.stderr)
    if verbose:
        print(f"[i] chain: {alive}/{len(rows)} links alive", file=sys.stderr)
        if alive <= 1:
            print("[!] no working fallback — if the lead link fails, every call returns "
                  "None and the run drops to heuristic mode.", file=sys.stderr)
    return {"alive": alive, "total": len(rows), "links": rows}


def list_models(provider="nim"):
    """Connectivity check: list available models (esp. Nemotron). For `--check`."""
    default_base, key_env, _ = PROVIDERS[provider]
    base_url = os.environ.get("BRIEF_BASE_URL", default_base)
    key = os.environ.get(key_env) if key_env else "ollama"
    req = urllib.request.Request(base_url.rstrip("/") + "/models",
                                 headers={"Authorization": f"Bearer {key}", "User-Agent": _HTTP_UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        ids = [m["id"] for m in json.loads(r.read()).get("data", [])]
    return ids


# Models that think before answering by default (measured 2026-09-24: Opus 5.5 spent ~900
# tokens thinking on a 220-token paragraph, even at effort low; Sonnet 5 ~150). Thinking
# counts against max_tokens, so the pipeline's tight per-call ceilings (700 for a synthesis
# paragraph) cut the answer off mid-JSON. They get this much extra room — a ceiling, billed
# only when used. Opus 4.6 does not think by default and gets none.
THINKING_HEADROOM = int(os.environ.get("BRIEF_THINKING_HEADROOM", "2500"))
_THINKING_MODELS = ("claude-opus-5", "claude-sonnet-5", "claude-fable", "claude-mythos")


def _thinking_headroom(model) -> int:
    """Extra output tokens for a model that thinks by default; 0 for one that does not."""
    return THINKING_HEADROOM if str(model or model_for("anthropic")).startswith(_THINKING_MODELS) else 0


def _chat_anthropic(user, system=None, max_tokens=None, schema=None, model=None):
    """The Anthropic link. `schema` turns on structured outputs — the API constrains the
    response to that JSON Schema rather than the prompt merely asking for JSON.

    Why it matters here: this is the FIRST link in the live chain, and until now it was
    the only link that could not be asked for JSON at all — `json_mode` was not even a
    parameter, so `_json_call` set it and this function ignored it. Every JSON guarantee
    rested on the words 'Return ONLY raw JSON' in a prompt plus _loads_lenient cleaning up
    afterwards. That holds while there is something to extract and fails when there is
    not: a model with no signal to report explains itself in prose instead, which is the
    no-signal case that returned prose five times out of five."""
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    _stats_call(f"anthropic:{model or model_for('anthropic')}", len(system or EXTRACTION_SYSTEM) + len(user))
    kw = {}
    if schema:
        kw["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
    # max_tokens was hardcoded at 4000, which ignored every caller's ceiling — a judge
    # call asking for 500 was allocated 4000.
    # `model` is the chain link's model. Before 2026-09-24 this always sent
    # model_for("anthropic"), so an explicit model= (the Sonnet judges) silently ran on Opus.
    msg = client.messages.create(model=model or model_for("anthropic"),
                                 max_tokens=int(max_tokens or MAXTOK_EXTRACT) + _thinking_headroom(model),
                                 system=system or EXTRACTION_SYSTEM,
                                 messages=[{"role": "user", "content": user}], **kw)
    # Take the first TEXT block rather than content[0]: a model configured with thinking
    # returns a thinking block first, and indexing blindly would read the wrong one.
    text = next((b.text for b in msg.content if getattr(b, "type", None) == "text"), "")
    u = getattr(msg, "usage", None)
    _stats_usage({"prompt_tokens": getattr(u, "input_tokens", 0) or 0,
                  "completion_tokens": getattr(u, "output_tokens", 0) or 0,
                  "cache_read_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
                  "cache_creation_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0}
                 if u else None, len(text))
    label = f"anthropic:{model or model_for('anthropic')}"
    # stop_reason decides whether the text may be read at all. Before 2026-09-25 it was
    # only logged when the text was empty, so a reply cut off at max_tokens went to the
    # lenient parser and a refusal (empty text) fell through to the next link unremarked.
    stop = getattr(msg, "stop_reason", None)
    if stop == "max_tokens":
        _stats_bump("truncations")
        raise _Truncated(f"{label}: reply hit the output cap ({int(max_tokens or MAXTOK_EXTRACT)} tokens)")
    if stop == "refusal":
        _stats_bump("refusals")
        raise _Refused(f"{label}: the model declined ({getattr(msg, 'stop_details', None) or 'no details'})")
    if not text:
        print(f"[i] {label} returned no text (stop_reason={stop or '?'}); next link…", file=sys.stderr)
    return text


# Default model chain, best→most-reliable. Every call walks this until one link
# succeeds, so a slow/rate-limited/JSON-flaky provider never fails the run: the strong,
# no-rate-limit models lead; NIM's clean-JSON llama is the backstop that's almost never
# empty. Links whose API key is absent are dropped. Override with BRIEF_MODEL_CHAIN.
_DEFAULT_CHAIN = [
    ("cerebras", "gpt-oss-120b"),                  # fast, no rate limit, strong generation
    ("cerebras", "zai-glm-4.7"),                   # 2nd Cerebras model (different failure mode)
    ("groq",     "openai/gpt-oss-120b"),           # same strong model, different host
    ("groq",     "llama-3.3-70b-versatile"),       # fast, clean JSON
    # NIM backstop, re-verified 2026-09-22 by probing every link on this account.
    # The two that used to sit here were both dead and had been for weeks, silently,
    # because the chain only reaches them when the lead fails:
    #   meta/llama-3.3-70b-instruct          410 Gone — EOL 2026-08-26, gone from the catalog
    #   nvidia/llama-3.1-nemotron-70b-instr  404 — IN the catalog, not entitled to this account
    # Those are different problems with the same log line, which is why neither was noticed.
    # Catalog presence does not imply entitlement: check with a real call, not `list_models`.
    ("nim",      "nvidia/nemotron-3-super-120b-a12b"),   # probed clean JSON
    ("nim",      "openai/gpt-oss-20b"),                  # probed clean JSON, different family
    # Rejected after probing: nemotron-3.5-lightning-30b-a3b leaks its reasoning preamble
    # (the enable_thinking guard keys off "reasoning"/"thinking" in the NAME and this has
    # neither), and nemotron-3-ultra-550b-a55b returned a corrupted key: {"ok{": true}.
]
_KEY_ENV = {"cerebras": "CEREBRAS_API_KEY", "groq": "GROQ_API_KEY", "nim": "NVIDIA_API_KEY",
            "gemini": "GEMINI_API_KEY", "openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
            "ollama": None}
_CHAIN_LOGGED = False
# Circuit-breaker: a link that 429s is skipped for this many seconds so subsequent calls
# settle onto a free link instead of re-failing the saturated lead on every single call.
_LINK_COOLDOWN = {}          # "provider:model" -> monotonic deadline
_COOLDOWN_SECS = float(os.environ.get("BRIEF_LINK_COOLDOWN", "45"))


def _allow_nonclaude() -> bool:
    """True when BRIEF_ALLOW_NONCLAUDE is set (1/true/yes): non-Claude links may stay in a
    chain led by Claude. Off by default since 2026-09-25."""
    return os.environ.get("BRIEF_ALLOW_NONCLAUDE", "").strip().lower() in ("1", "true", "yes")


def _cooldown(provider, model):
    """Put the provider:model link on cooldown after an HTTP 429: store a monotonic
    deadline _COOLDOWN_SECS from now (BRIEF_LINK_COOLDOWN, default 45 s) in _LINK_COOLDOWN.
    _model_chain drops links that are still cooling, unless every link is."""
    import time
    _LINK_COOLDOWN[f"{provider}:{model}"] = time.monotonic() + _COOLDOWN_SECS


def _provider_for_model(m: str) -> "str | None":
    """Best-guess host for an explicitly-pinned model id (a wrong guess self-heals — the
    chain falls through to the next link if the pinned link errors)."""
    if m.startswith("claude-"):
        return "anthropic"
    if m.startswith(("nvidia/", "meta/")):
        return "nim"
    if m in ("gpt-oss-120b", "zai-glm-4.7"):
        return "cerebras"
    if m.startswith(("openai/", "meta-llama/", "qwen/")) or m.endswith("-versatile"):
        return "groq"
    return None


def _model_chain(model=None) -> list:
    """Ordered [(provider, model)] to try, best first. BRIEF_MODEL_CHAIN overrides the
    default; an explicit BRIEF_PROVIDER (and model=) is honoured as the FIRST link, with
    the rest kept as fallback so a pinned provider still never fails the run."""
    global _CHAIN_LOGGED
    env_chain = os.environ.get("BRIEF_MODEL_CHAIN", "").strip()
    if env_chain:
        chain = [tuple(t.strip().split(":", 1)) for t in env_chain.split(",")
                 if ":" in t]
    else:
        chain = [(p, m) for (p, m) in _DEFAULT_CHAIN
                 if p == "ollama" or os.environ.get(_KEY_ENV.get(p) or "")]
    # Honour an explicit pin (BRIEF_PROVIDER / model=) as the lead link, keep fallback.
    pin_p = os.environ.get("BRIEF_PROVIDER", "").lower().strip()
    pin_m = model or os.environ.get("BRIEF_MODEL", "")
    if pin_p:
        # With no explicit model, the pinned provider's model comes from the chain itself
        # (BRIEF_MODEL_CHAIN), not model_for()'s default. Before 2026-09-24 a pin replaced
        # "anthropic:claude-opus-5-5" in the chain with the default claude-opus-4-6.
        pin_m = pin_m or next((m for p, m in chain if p == pin_p), "")
        host = _provider_for_model(model) if model else None     # e.g. gpt-oss under an anthropic pin
        lead = (host or pin_p, pin_m or model_for(pin_p))
        chain = [lead] + [l for l in chain if l != lead]
    elif model:
        prov = _provider_for_model(model) or (chain[0][0] if chain else resolve_provider())
        lead = (prov, model)
        chain = [lead] + [l for l in chain if l != lead]
    # Claude-only by default. With the lead link on Claude, a non-Claude fallback link
    # meant that an API key with no credit produced a brief written by a NIM model (whose
    # terms exclude production use) and labelled as Claude (audit N2/C6). BRIEF_ALLOW_NONCLAUDE=1
    # keeps the old behaviour for someone who wants the backstop. Sai, 2026-09-25: Claude
    # only; testing runs on the Claude Code login.
    if chain and chain[0][0] == "anthropic" and not _allow_nonclaude():
        chain = [l for l in chain if l[0] == "anthropic"]
    if not _CHAIN_LOGGED and chain:
        print("[i] model chain: " + " → ".join(f"{p}:{m}" for p, m in chain)
              + ("" if _allow_nonclaude() or chain[0][0] != "anthropic"
                 else "  (Claude only; BRIEF_ALLOW_NONCLAUDE=1 to allow other links)"), file=sys.stderr)
        _CHAIN_LOGGED = True
    # Drop links that are mid-cooldown (recently 429'd); if every link is cooling, keep the
    # full chain rather than deadlock — something is better than heuristic.
    import time
    now = time.monotonic()
    live = [l for l in chain if _LINK_COOLDOWN.get(f"{l[0]}:{l[1]}", 0.0) <= now]
    return live or chain


# BRIEF_CLAUDE_TRANSPORT=cli sends every `anthropic:` link through the Claude Code CLI
# (`claude -p`, the logged-in Claude Code account) instead of the API key — the route
# serve.py already uses for the mock agent. Chains, model pins, labels and pricing are
# unchanged, so a run is comparable with an API run; only the transport differs. Parity
# with the API path: no tools, one turn, no session file, no user/project settings or MCP
# servers; thinking off for models that do not think by default on the API (Opus 4.6), and
# the API's default effort for those that do (Opus 5.5 medium, others high); the caller's
# max_tokens (plus the same thinking headroom) as the output cap.
CLI_TIMEOUT_S = float(os.environ.get("BRIEF_CLI_TIMEOUT", "240"))
_CLI_DEFAULT_EFFORT = {"claude-opus-5-5": "medium"}


# Set once `auto` has switched this process to the CLI (credit/auth failure on the API).
# The switch expires after CLI_FALLBACK_TTL_S so a server re-checks the API instead of
# staying on the slower CLI until restart after one auth hiccup (audit critic-G11).
_CLI_FALLBACK = {"on": False, "since": 0.0}
_CLI_FALLBACK_LOCK = threading.Lock()
CLI_FALLBACK_TTL_S = float(os.environ.get("BRIEF_CLI_FALLBACK_TTL", "600"))


def _switch_to_cli(reason: str) -> None:
    """Move every later `anthropic:` call in this process to the CLI (transport `auto`)
    for CLI_FALLBACK_TTL_S seconds, announcing it once on stderr. Idempotent and
    thread-safe: parallel calls that fail at the same moment switch once and print once."""
    import time
    with _CLI_FALLBACK_LOCK:
        if _cli_fallback_active():
            return
        _CLI_FALLBACK["on"] = True
        _CLI_FALLBACK["since"] = time.monotonic()
    print(f"[!] BRIEF_CLAUDE_TRANSPORT=auto: the API key cannot be used ({reason}); "
          f"this process continues on the Claude Code CLI for {int(CLI_FALLBACK_TTL_S)} s, "
          f"then re-tries the API.", file=sys.stderr)


def _cli_fallback_active() -> bool:
    """True while an `auto` switch to the CLI is in force (set and younger than the TTL)."""
    import time
    if not _CLI_FALLBACK.get("on"):
        return False
    if time.monotonic() - float(_CLI_FALLBACK.get("since") or 0.0) > CLI_FALLBACK_TTL_S:
        _CLI_FALLBACK["on"] = False
        return False
    return True


def _claude_transport() -> str:
    """The transport for `anthropic:` links, from BRIEF_CLAUDE_TRANSPORT:
      api   the API key (the default; unchanged behaviour),
      cli   the Claude Code CLI (`claude -p`, the logged-in account),
      auto  the API, switching this process to the CLI on a credit or auth failure, or
            straight away when no ANTHROPIC_API_KEY is set but `claude` is installed.
    Anything else reads as 'api', so a typo never silently moves traffic."""
    t = os.environ.get("BRIEF_CLAUDE_TRANSPORT", "").strip().lower()
    return t if t in ("api", "cli", "auto") else "api"


def _api_account_failure(exc: Exception) -> bool:
    """True when an API error means the KEY cannot be used (no credit, bad or missing key),
    as opposed to a transient or request error that the next chain link should handle."""
    name, msg = exc.__class__.__name__, str(exc)
    return (name in ("AuthenticationError", "PermissionDeniedError")
            or (name == "BadRequestError" and "credit balance" in msg.lower())
            or isinstance(exc, KeyError) and "ANTHROPIC_API_KEY" in msg)


def transport_used() -> str:
    """What `anthropic:` links actually ran on in this process: 'api', 'cli', or
    'api→cli' once `auto` has switched. Written into run outputs so API and Claude Code
    runs are never compared as if they were the same thing."""
    t = _claude_transport()
    if t == "auto":
        return "api→cli" if _cli_fallback_active() else "api"
    return t


def _chat_claude_cli(user, system=None, max_tokens=None, schema=None, model=None):
    """The Anthropic link over the Claude Code CLI: one `claude -p` call, prompt on stdin.

    Returns the reply text (for a `schema` call, the CLI's validated structured output
    re-serialised as JSON, so _json_call parses it the same way). Raises _RateLimited when
    the account's usage limit is hit and RuntimeError on any other failure, so the chain
    advances exactly as it does for an API error. Usage is recorded under the same
    `anthropic:<model>` label as the API path, with the CLI's own token counts."""
    import shutil
    import subprocess
    model = model or model_for("anthropic")
    if not shutil.which("claude"):
        raise RuntimeError("claude CLI not on PATH (BRIEF_CLAUDE_TRANSPORT=cli)")
    _stats_call(f"anthropic:{model}", len(system or EXTRACTION_SYSTEM) + len(user))
    cmd = ["claude", "-p", "--model", model, "--system-prompt", system or EXTRACTION_SYSTEM,
           "--tools", "", "--max-turns", "1", "--output-format", "json",
           "--no-session-persistence", "--setting-sources", "", "--strict-mcp-config"]
    env = {**os.environ,
           "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(int(max_tokens or MAXTOK_EXTRACT) + _thinking_headroom(model))}
    if _thinking_headroom(model):
        cmd += ["--effort", os.environ.get("BRIEF_CLI_EFFORT") or _CLI_DEFAULT_EFFORT.get(model, "high")]
    else:
        env["MAX_THINKING_TOKENS"] = "0"          # the API path does not think on these models
    if schema:
        cmd += ["--json-schema", json.dumps(schema)]
    # The CLI must not inherit an API key: with one set it bills the key (which may have no
    # credit) instead of the logged-in account this transport exists to use.
    env.pop("ANTHROPIC_API_KEY", None)
    try:
        proc = subprocess.run(cmd, input=user, capture_output=True, text=True,
                              timeout=CLI_TIMEOUT_S, env=env, cwd=os.path.expanduser("~"))
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"claude CLI timed out after {CLI_TIMEOUT_S:.0f}s") from e
    try:
        env_out = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        env_out = {}
    if proc.returncode != 0 or env_out.get("is_error") or env_out.get("subtype") not in (None, "success"):
        detail = str(env_out.get("result") or proc.stderr or proc.stdout or "")[:300]
        if re.search(r"usage limit|rate limit|429|overloaded", detail, re.I):
            raise _RateLimited(f"claude CLI: {detail}")
        raise RuntimeError(f"claude CLI failed (exit {proc.returncode}): {detail}")
    so = env_out.get("structured_output")
    text = json.dumps(so, ensure_ascii=False) if (schema and so is not None) else str(env_out.get("result") or "")
    u = env_out.get("usage") or {}
    # Cache reads and writes are recorded apart from the uncached input: they are priced
    # differently (reads at 0.1x), and folding them into prompt_tokens overstated CLI runs
    # by ~35-40% (audit BW13/CC9).
    _stats_usage({"prompt_tokens": int(u.get("input_tokens") or 0),
                  "completion_tokens": int(u.get("output_tokens") or 0),
                  "cache_read_tokens": int(u.get("cache_read_input_tokens") or 0),
                  "cache_creation_tokens": int(u.get("cache_creation_input_tokens") or 0)}, len(text))
    stop = env_out.get("stop_reason")
    if stop == "max_tokens" or re.search(r"exceeded .*output token", text[:200], re.I):
        _stats_bump("truncations")
        raise _Truncated(f"claude-cli:{model}: reply hit the output cap")
    if stop == "refusal":
        _stats_bump("refusals")
        raise _Refused(f"claude-cli:{model}: the model declined")
    if not text:
        print(f"[i] claude-cli:{model} returned no text (stop_reason={stop or '?'}); next link…",
              file=sys.stderr)
    return text


def _call_link(provider: str, model: str, user, system=None, max_tokens=None,
               json_mode=False, schema=None) -> "str | None":
    """Call exactly ONE (provider, model) link. Raises on failure so the chain advances.
    An `anthropic` link goes over the API or the Claude Code CLI per BRIEF_CLAUDE_TRANSPORT
    (see _claude_transport and _chat_claude_cli)."""
    if provider == "anthropic":
        kw = dict(system=system, max_tokens=max_tokens, schema=schema if json_mode else None, model=model)
        t = _claude_transport()
        if t == "cli" or (t == "auto" and _cli_fallback_active()):
            return _chat_claude_cli(user, **kw)
        if t == "auto" and not os.environ.get("ANTHROPIC_API_KEY"):
            import shutil
            if shutil.which("claude"):
                _switch_to_cli("no ANTHROPIC_API_KEY set")
                return _chat_claude_cli(user, **kw)
        try:
            return _chat_anthropic(user, **kw)
        except Exception as e:
            if t == "auto" and _api_account_failure(e):
                _switch_to_cli(f"{e.__class__.__name__}: {str(e)[:120]}")
                return _chat_claude_cli(user, **kw)
            raise
    cfg = PROVIDERS.get(provider)
    if not cfg:
        raise RuntimeError(f"unknown provider '{provider}'")
    default_base, key_env, _ = cfg
    key = os.environ.get(key_env) if key_env else "ollama"
    if key_env and not key:
        raise RuntimeError(f"{key_env} not set")
    return _chat_openai_compatible(default_base, key, model, user,
                                   provider_label=f"{provider}:{model}", system=system,
                                   max_tokens=max_tokens, json_mode=json_mode, schema=schema)


def _stats_logical():
    """One LOGICAL call (one _chat/_json_call) may cost several link attempts when the
    chain fails over — 'calls' counts attempts, this counts intent."""
    _stats_bump("logical_calls")


def _chat(user, system=None, model=None, max_tokens=None):
    """Provider-agnostic chat. Walks the model chain (see _model_chain) and returns the
    first link's raw text, or None if every link failed (callers fall back to heuristic)."""
    _stats_logical()
    for provider, m in _model_chain(model):
        try:
            raw = _call_link(provider, m, user, system=system, max_tokens=max_tokens)
            if raw:
                _note_answer(provider, m)
                return raw
        except _RateLimited:                     # saturated — cool it down so later calls skip it
            _cooldown(provider, m)
            print(f"[i] link {provider}:{m} rate-limited; cooling {int(_COOLDOWN_SECS)}s, next link…",
                  file=sys.stderr)
        except Exception as e:                   # network/auth/HTTP — try the next link
            print(f"[i] link {provider}:{m} failed ({e.__class__.__name__}); next link…",
                  file=sys.stderr)
    return None


_NONCLAUDE_WARNED = threading.local()


def _note_answer(provider: str, m: str):
    """Record the link whose reply is being used, and say so loudly the first time a
    non-Claude link answers under a Claude lead (once per thread-ledger, i.e. per run)."""
    label = f"{provider}:{m}"
    _stats_answered(label)
    if provider != "anthropic" and resolve_provider() == "anthropic":
        led = _ledger()
        if not led.get("nonclaude_warned"):
            led["nonclaude_warned"] = True
            print(f"[!] a non-Claude link answered ({label}) — this brief is not a Claude brief; "
                  f"meta.fallback_links records it.", file=sys.stderr)


def _json_call(user, system=None, retries=1, model=None, accept=None, max_tokens=None,
               schema=None, parse=None, whole=False, only_model=False, info=None):
    """Chat call that must return JSON, with provider juggling: each chain link gets up to
    `retries`+1 tries; unparseable output (or one rejected by `accept`) advances to the next
    link. Returns the first usable object, or None if the whole chain is exhausted.
    `parse` replaces the JSON reader for a non-JSON reply format (the TOON calls pass
    _loads_toon) and turns the providers' JSON mode off; it returns None on failure.
    `whole=True` reads only a complete top-level object (no inner-span salvage): for
    tournaments, judges and the critic, where a partial reply must never pass as a whole one.
    `only_model=True` walks the pinned `model` alone (over api or cli), never a fallback
    link: the critic and the eval judges must answer on their own model or not at all.
    `info`, when a dict, receives {"link": "<provider:model>"} of the link that answered.
    A reply cut off at the output cap is retried once with 1.5x the cap on the same link,
    then the next link; a refusal is logged and counted, then the next link."""
    _stats_logical()
    if only_model and model:
        chain = [(_provider_for_model(model) or "anthropic", model)]
    else:
        chain = _model_chain(model)
    reader = parse or (functools.partial(_loads_lenient, whole=True) if whole else _loads_lenient)
    for provider, m in chain:
        cap, grew = max_tokens, False
        attempt = 0
        while attempt <= retries:
            try:
                raw = _call_link(provider, m, user, system=system, max_tokens=cap,
                                 json_mode=parse is None, schema=schema)
            except _RateLimited:
                _cooldown(provider, m)
                print(f"[i] link {provider}:{m} rate-limited; cooling {int(_COOLDOWN_SECS)}s, next link…",
                      file=sys.stderr)
                break
            except _Truncated as e:
                if not grew:
                    cap, grew = int((cap or MAXTOK_EXTRACT) * 1.5), True
                    print(f"[i] {e}; retrying once with {cap} tokens.", file=sys.stderr)
                    continue                     # same link, more room, not counted as a retry
                print(f"[!] {e} again at {cap} tokens; next link…", file=sys.stderr)
                break
            except _Refused as e:
                print(f"[!] {e}; next link…", file=sys.stderr)
                break
            except Exception as e:
                print(f"[i] link {provider}:{m} failed ({e.__class__.__name__}); next link…",
                      file=sys.stderr)
                break                            # this link is down — go to the next one
            if not raw:
                break
            raw = re.sub(r"^```(?:json|toon)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
            obj = reader(raw)
            if obj is not None and (accept is None or accept(obj)):
                _note_answer(provider, m)
                if isinstance(info, dict):
                    info["link"] = f"{provider}:{m}"
                return obj
            attempt += 1
            if attempt <= retries:
                print(f"[i] {provider}:{m} unclean/unusable JSON; retrying once.", file=sys.stderr)
        # link exhausted → fall through to the next provider in the chain
    print("[i] no provider in the chain returned usable JSON.", file=sys.stderr)
    return None


def extract_llm(text, schema):
    """Provider-agnostic extractor. Walks the model chain (BRIEF_MODEL_CHAIN); a link that
    returns valid-but-empty JSON is skipped so extraction lands on a clean-JSON model.
    Returns a dict, or None to fall back to heuristic."""
    obj = _json_call(_user_msg(text, schema), max_tokens=MAXTOK_EXTRACT,
                     accept=lambda o: isinstance(o, dict) and isinstance(o.get("fields"), dict)
                     and bool(o["fields"]))
    out = _normalize_llm(obj) if obj is not None else None
    # Shape guard: the lenient parser can fish an inner {...} out of truncated
    # output — an "extraction" with no fields must not count as a success.
    if not (isinstance(out, dict) and isinstance(out.get("fields"), dict)
            and out["fields"]):
        if obj is not None:
            print("[i] LLM JSON had no usable 'fields'.", file=sys.stderr)
        print("[i] Falling back to heuristic extraction.", file=sys.stderr)
        return None
    return out


# ---------------------------------------------------------------------------
# 3c. EXTRACT (LLM, TOON + sentence citations) — the default Loop 1 capture
# ---------------------------------------------------------------------------
# Measured 2026-09-23 on a real client brief: the JSON capture above wrote 5,256 output
# tokens (78 s, 30% of the brief's cost) for a 1,200-token brief — 43% of it verbatim
# source_quotes copying the brief back out, the rest JSON keys repeated on every list item,
# plus how_to_win in the same call. So the default capture (Sai, 2026-09-23):
#   * numbers the brief's sentences (the same segment() rows the no-loss ledger counts) and
#     asks for `src: 4 7` instead of a quote — code attaches the verbatim text, so quotes
#     are exact by construction instead of "copied word-for-word" by the model;
#   * writes TOON, the format CLAN uses: list fields are one header + one row per item;
#   * moves how_to_win to its own call, run alongside (run() starts both at once).
# The output shape is unchanged (fields with value/status/source_quote/confidence, plus
# `source_refs`), so everything downstream reads it as before. BRIEF_CAPTURE=json restores
# the one-call JSON capture; a TOON reply that fails to parse falls back to it too.

CAPTURE_ARRAY_FIELDS = ("objective", "deliverables", "mandatories", "timeline", "success_metrics",
                        "proof_points", "evaluation_criteria", "decision_makers", "constraints")
HOW_TO_WIN_KEYS = ("stated_evaluation_criteria", "unstated_needs", "likely_landmines",
                   "winning_themes", "proof_required")

_CAPTURE_KEYS = EXTRACTION_SYSTEM[EXTRACTION_SYSTEM.index('"fields" MUST'):
                                  EXTRACTION_SYSTEM.index("Each value is an object")]

CAPTURE_TOON_SYSTEM = ("""You are Loop 1 of an ad-agency briefing system, the Client Brief
Parser. Convert a messy client brief into a faithful structured capture.

""" + _CAPTURE_KEYS + """
The brief is given as numbered sentences [1]..[N]. NEVER copy brief text as a quote: cite
sentence numbers in `src`, space-separated (src: 4 7). Code attaches the verbatim text.
- status: fact (stated) | assumption (inferred) | gap (not provided: value null, no src).
- value: a concise capture on ONE line — no line breaks, never the | character.
- objective rows also carry objective_type: commercial | behavioural | attitudinal
  (BetterBriefs: the three should coexist and link — attitude -> behaviour -> commercial).
- LOSE NOTHING: every concrete sentence number should appear in some field's src.
- Every one of the 18 keys appears under fields (a gap if absent).
- open_questions = what the brief FAILS to answer; never ask for something it states.

Reply in TOON (not JSON), 2-space indentation, nothing before or after:
fields:
  business_problem:
    value: Under-30s see the bank as the one their parents use
    status: fact
    src: 4 5
    confidence: 0.9
  budget:
    value: null
    status: gap
  objective[2|]{value|status|objective_type|src|confidence}:
    Grow app sign-ups among under-30s|fact|commercial|6|0.9
    Get lapsed users opening the app weekly|assumption|behavioural|7 8|0.6
  mandatories[1|]{value|status|src|confidence}:
    Every asset carries the regulator disclaimer|fact|11|0.95
open_questions[2]:
  - What is the media budget?
  - Who signs off the creative?
Every array field (""" + ", ".join(CAPTURE_ARRAY_FIELDS) + """) is a table like
objective or mandatories; every other field is a block like business_problem.""")

HOW_TO_WIN_TOON_SYSTEM = """You are Loop 1's how-to-win reader for an ad-agency pitch. From the
numbered client brief, list ONLY what the brief itself reveals about winning this work — do
NOT invent strategy. Five tables, AT MOST 5 rows each (the most important first); each row
is one point on ONE line, under 20 words (never the | character), and the numbers of the
sentences that evidence it:
  stated_evaluation_criteria  how the client says the work will be judged
  unstated_needs              what they need but do not say outright
  likely_landmines            what would lose the pitch
  winning_themes              what a winning answer is built around
  proof_required              what we will have to prove
Reply in TOON (not JSON), nothing before or after, e.g.:
stated_evaluation_criteria[2|]{point|src}:
  Must prove the app is simpler than the challenger banks|12
  Shows the parents' bank can feel modern|3 9
unstated_needs[0|]{point|src}:
Several sentence numbers go in ONE src cell separated by spaces (3 9), never as extra |
cells. An empty table is its header line with no rows below it, as unstated_needs above."""


def _numbered(segs: list[str], limit: int) -> str:
    """The brief as `[i] sentence` lines — the numbering `src` cites — clipped at `limit` chars."""
    out, n = [], 0
    for i, s in enumerate(segs, 1):
        line = f"[{i}] {s}"
        if n + len(line) > limit and out:
            break
        out.append(line); n += len(line) + 1
    return "\n".join(out)


def _loads_toon(raw: str):
    """TOON reply -> dict, or None when unreadable (so _json_call moves on)."""
    import toon_lite
    try:
        return toon_lite.decode(raw)
    except toon_lite.ToonError:
        return None


def _refs(src, n_segs: int) -> list[int]:
    """Sentence numbers from a `src` cell (`4 7`, `4,7`, 4, None), kept to 1..n_segs."""
    # Whole numbers only: a short table row can shift `0.9` (a confidence) into src.
    nums = [int(x) for x in re.findall(r"(?<![\d.])\d+(?![\d.])", str(src if src is not None else ""))]
    return list(dict.fromkeys(i for i in nums if 1 <= i <= n_segs))


def _quote(refs: list[int], segs: list[str]) -> "str | None":
    """The verbatim sentences `refs` cite, joined — the source_quote, attached by code."""
    return " ".join(segs[i - 1] for i in refs) or None


_GLUED_REFS = re.compile(r"\s*\|\s*\d+(\s*\|\s*\d+)*\s*$")


def _unglue(text, src):
    """Guard for a TOON row whose extra sentence numbers were glued onto the text cell
    ('Resolve the tension|11|31'): return (text without them, src with them appended).
    toon_lite now puts such cells into src itself; this catches any that still slip past
    (audit H6: 38 of 73 how-to-win rows on Opus 4.6 carried a glued '|n')."""
    if not isinstance(text, str):
        return text, src
    m = _GLUED_REFS.search(text)
    if not m:
        return text, src
    nums = re.findall(r"\d+", m.group(0))
    return text[:m.start()].rstrip(), " ".join([str(src)] * (src is not None) + nums)


def _capture_item(it: dict, segs: list[str]) -> dict:
    """One captured value in the pipeline's shape: value/status/source_quote/confidence,
    plus source_refs (the cited sentence numbers) and objective_type where given."""
    v, src = _unglue(it.get("value"), it.get("src"))
    refs = _refs(src, len(segs))
    v = None if v is None else str(v)            # TOON reads `50000` as a number; fields are text
    out = {"value": v, "status": it.get("status") or ("gap" if v is None else "fact"),
           "source_quote": _quote(refs, segs), "confidence": it.get("confidence"), "source_refs": refs}
    if it.get("objective_type") in ("commercial", "behavioural", "attitudinal"):
        out["objective_type"] = it["objective_type"]
    return out


def capture_toon(segs: list[str]) -> "dict | None":
    """Loop 1 capture as TOON with sentence citations. Returns the same
    {fields, how_to_win: {}, open_questions} shape as extract_llm, or None on failure
    (run() then falls back to extract_llm's JSON capture)."""
    if not segs:
        return None
    obj = _json_call(f"CLIENT BRIEF (numbered sentences):\n{_numbered(segs, CLIP_EXTRACT)}",
                     system=CAPTURE_TOON_SYSTEM, max_tokens=MAXTOK_EXTRACT, parse=_loads_toon,
                     accept=lambda o: isinstance(o.get("fields"), dict) and bool(o["fields"]))
    if not obj:
        return None
    fields = {}
    for k, v in obj["fields"].items():
        if isinstance(v, list):
            fields[k] = [_capture_item(it if isinstance(it, dict) else {"value": it}, segs)
                         for it in v if it is not None]
        elif isinstance(v, dict):
            fields[k] = _capture_item(v, segs)
        elif v is not None:                          # inline `key: text`
            fields[k] = _capture_item({"value": v}, segs)
    if not fields:
        return None                                  # nothing usable: run() falls back to JSON
    qs = obj.get("open_questions") or []
    return _normalize_llm({"fields": fields, "how_to_win": {},
                           "open_questions": [q for q in qs if isinstance(q, (str, dict))]})


def how_to_win_toon(segs: list[str]) -> dict:
    """how_to_win in its own call (TOON, sentence citations), shaped as before:
    {key: [{"point", "evidence"}]} with evidence the verbatim cited sentences.
    Returns {} on failure — how_to_win is advisory, never a reason to fail the run."""
    if not segs:
        return {}
    obj = _json_call(f"CLIENT BRIEF (numbered sentences):\n{_numbered(segs, CLIP_EXTRACT)}",
                     system=HOW_TO_WIN_TOON_SYSTEM, max_tokens=MAXTOK_EXTRACT, parse=_loads_toon,
                     accept=lambda o: any(k in o for k in HOW_TO_WIN_KEYS))
    out = {}
    for k in HOW_TO_WIN_KEYS:
        rows = [_unglue(r.get("point"), r.get("src")) for r in ((obj or {}).get(k) or [])
                if isinstance(r, dict) and r.get("point")]
        out[k] = [{"point": point, "evidence": _quote(_refs(src, len(segs)), segs),
                   "source_refs": _refs(src, len(segs))} for point, src in rows if point]
    return out


def _loads_lenient(raw, whole=False):
    """Reasoning models sometimes wrap the JSON in prose. Try a clean parse,
    then every balanced {...} span (largest first), each with a trailing-comma
    repair pass. Returns dict/list or None.
    `whole=True` stops after the clean parse: no inner span is salvaged, so a reply cut
    off part-way (a truncated tournament, judge or critic) reads as unusable rather than
    as the one complete object inside it (audit F8/G4)."""
    def _try(s):
        """Parse `s` as JSON, then once more with trailing commas before } or ]
        removed. Returns the parsed value, or None when both attempts fail."""
        for candidate in (s, re.sub(r",\s*([}\]])", r"\1", s)):
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
        return None

    obj = _try(raw)
    if obj is not None:
        return obj
    if whole:
        return None
    spans = []
    start = raw.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(raw)):
            ch = raw[i]
            if in_str:
                if esc:        esc = False
                elif ch == "\\": esc = True
                elif ch == '"': in_str = False
            elif ch == '"':    in_str = True
            elif ch == "{":    depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    spans.append(raw[start:i + 1])
                    break
        start = raw.find("{", start + 1)
    for span in sorted(set(spans), key=len, reverse=True):
        obj = _try(span)
        if isinstance(obj, dict):
            return obj
    return None


# LLM field name (free-form) -> the canonical key the rest of the pipeline keys on.
# The model invents synonyms; without this, Loop 2 shaping and the renderer (which
# only walk canonical keys) silently drop genuinely-extracted content.
FIELD_ALIASES = {
    "audience": "target_audience", "target": "target_audience",
    "mandatory_requirements": "mandatories", "mandatory": "mandatories",
    "must_haves": "mandatories", "requirements": "mandatories",
    "competitors": "competitors_market", "competition": "competitors_market",
    "competitive_landscape": "competitors_market",
    "problem": "business_problem", "challenge": "business_problem",
    "business_challenge": "business_problem",
    "background": "background_context", "context": "background_context",
    "goal": "objective", "goals": "objective", "objectives": "objective",
    "kpis": "success_metrics", "metrics": "success_metrics", "success": "success_metrics",
    "timing": "timeline", "deadline": "timeline", "key_dates": "timeline",
    "deliverable": "deliverables",
    "tone": "tone_and_brand", "brand": "tone_and_brand",
    "brand_guidelines": "tone_and_brand", "tone_of_voice": "tone_and_brand",
    "message": "key_message", "key_messages": "key_message",
    "proposition": "key_message", "single_minded_proposition": "key_message",
    "proof": "proof_points", "proofs": "proof_points", "rtbs": "proof_points",
    "reasons_to_believe": "proof_points", "support": "proof_points",
    "evaluation": "evaluation_criteria", "judging_criteria": "evaluation_criteria",
    "assessment_criteria": "evaluation_criteria",
    "strategy": "strategic_angle", "strategic_direction": "strategic_angle",
    "anti_audience": "anti_target", "non_target": "anti_target",
}


def _canonicalize_fields(fields):
    """Rename known LLM synonyms to canonical keys; merge if both exist."""
    if not isinstance(fields, dict):
        return fields
    out = {}
    for k, v in fields.items():
        ck = FIELD_ALIASES.get(k, k)
        if ck in out:                                # merge collisions into a list
            cur = out[ck]
            out[ck] = (cur if isinstance(cur, list) else [cur]) + \
                      (v if isinstance(v, list) else [v])
        else:
            out[ck] = v
    return out


def _coerce_open_qs(qs):
    """LLMs sometimes return open_questions as bare strings — wrap into the
    {question, ...} dict shape the ledger and renderer consume."""
    out = []
    for q in (qs if isinstance(qs, list) else []):
        if isinstance(q, str) and q.strip():
            out.append({"question": q.strip()})
        elif isinstance(q, dict):
            out.append(q)
    return out


def _normalize_llm(d):
    """Tolerate two output shapes. The prompt asks for the flat
    {fields, how_to_win, open_questions}, but models fed the full schema often
    mirror it instead (fields under loop1_capture, open_questions under
    loop2_brief). Unwrap either into the flat shape the pipeline consumes, and
    canonicalize field keys either way."""
    if not isinstance(d, dict):
        return d
    if "fields" in d:                                # already flat
        d["fields"] = _canonicalize_fields(d.get("fields"))
        d["open_questions"] = _coerce_open_qs(d.get("open_questions"))
        return d
    l1 = d.get("loop1_capture", {}) if isinstance(d.get("loop1_capture"), dict) else {}
    l2 = d.get("loop2_brief", {}) if isinstance(d.get("loop2_brief"), dict) else {}
    fields = l1.get("fields") or d.get("capture") or {}
    how_to_win = l1.get("how_to_win") or d.get("how_to_win") or {}
    open_qs = (l2.get("open_questions") or d.get("open_questions")
               or l1.get("open_questions") or [])
    return {"fields": _canonicalize_fields(fields),
            "how_to_win": how_to_win, "open_questions": _coerce_open_qs(open_qs)}


# ---------------------------------------------------------------------------
# 4. NO-LOSS LEDGER
# ---------------------------------------------------------------------------

def _norm(s):
    """Normalise text for ledger matching: lower-case it and drop every character that is
    not a-z, 0-9 or a space. A list or tuple (LLMs sometimes return one) is joined with
    spaces first; any other value goes through str()."""
    if isinstance(s, (list, tuple)):  # LLMs sometimes return a list of values
        s = " ".join(str(x) for x in s)
    return re.sub(r"[^a-z0-9 ]", "", str(s).lower())


def build_ledger(segments, used_idx, fields, source_name, how_to_win=None, open_qs=None):
    """Build the Loop 1 no-loss ledger: which brief segments the capture accounts for.
    When `used_idx` is given (the heuristic path) it is taken as the mapped set as is. When
    it is None (an LLM capture), quotes are collected from the captured fields (source_quote
    and value), the how_to_win items (evidence and point) and the open-question texts, all
    normalised with _norm. A segment is mapped when its normalised text contains, or is
    contained in, one quote, or when it has at least 4 distinct words and 70% or more of
    them appear in a single quote.
    Returns {total_segments, mapped_segments, coverage_pct (one decimal; 0.0 with no
    segments), unmapped: [{segment, source_ref}]}, source_ref being `source_name`."""
    if used_idx is None:
        used_idx = set()
        quotes = []
        # 1) verbatim source_quotes (and values) from the captured fields
        for v in fields.values():
            for it in (v if isinstance(v, list) else [v]):
                if isinstance(it, dict):
                    for k in ("source_quote", "value"):
                        if it.get(k):
                            quotes.append(_norm(it[k]))
        # 2) evidence quotes used in how_to_win count as "captured" too
        for items in (how_to_win or {}).values():
            for it in (items or []):
                if isinstance(it, dict):
                    for k in ("evidence", "point"):
                        if it.get(k):
                            quotes.append(_norm(it[k]))
        # 3) text the open questions were derived from
        for q in (open_qs or []):
            if q.get("question"):
                quotes.append(_norm(q["question"]))
        quotes = [q for q in quotes if q]
        # A segment counts as mapped on a substring hit, or when most of its
        # content words appear in one quote — verbatim-only matching capped
        # coverage ~50% even on good extractions (quotes get lightly rephrased).
        quote_words = [(q, set(q.split())) for q in quotes]
        for i, seg in enumerate(segments):
            ns = _norm(seg)
            sw = set(ns.split())
            for q, qw in quote_words:
                if ns and (ns in q or q in ns):
                    used_idx.add(i); break
                if len(sw) >= 4 and len(sw & qw) / len(sw) >= 0.7:
                    used_idx.add(i); break
    unmapped = [{"segment": s, "source_ref": source_name}
                for i, s in enumerate(segments) if i not in used_idx]
    total = len(segments); mapped = total - len(unmapped)
    return {"total_segments": total, "mapped_segments": mapped,
            "coverage_pct": round(100 * mapped / total, 1) if total else 0.0,
            "unmapped": unmapped}


# ---------------------------------------------------------------------------
# 5. SELF-CRITIQUE (the REVIEW step of each loop)
# ---------------------------------------------------------------------------

# core field -> (fallback fields that also satisfy it, why it matters, priority)
# why_it_matters carries the BetterBriefs evidence — these questions go back to
# the client, and the stats are the ammunition for asking them.
CORE_FIELDS = {
    "business_problem": ([], "We can't position the work without the real problem.", "blocker"),
    "objective": (["success_metrics"], "Objectives are the most critical yet most "
                  "poorly defined element of a brief — 61% of marketers and 71% of "
                  "agencies rank them #1 (BetterBriefs).", "blocker"),
    "target_audience": ([], "65% of agencies can't picture the target from the briefs "
                        "they get; if we can't picture them, neither can creatives "
                        "(BetterBriefs).", "blocker"),
    "key_message": ([], "A good brief lands ONE single-minded message backed by proof "
                    "points — not a shopping list (BetterBriefs).", "important"),
    "evaluation_criteria": (["success_metrics"], "Only 30% of clients define how work "
                            "will be judged, and 88% of agencies are unclear on it — "
                            "agreeing criteria upfront prevents subjective rounds of "
                            "rework (BetterBriefs).", "important"),
    "budget": ([], "Budget, objectives and audience must interlock — scope and "
               "ambition depend on the money (BetterBriefs).", "important"),
    "timeline": ([], "Drives feasibility and the pitch date.", "important"),
    "success_metrics": (["objective"], "Objectives need benchmarks and a time stamp; "
                        "Loop 7 (IPA QA) can't score without KPIs.", "important"),
    "mandatories": ([], "Missing mandatories = legal/brand risk downstream.", "important"),
    "decision_makers": ([], "We win the room by knowing who decides — 62% of marketers "
                        "vs 43% of agencies say the right people sign off "
                        "(BetterBriefs).", "important"),
}

# When the client gave no evaluation criteria, ask with Orlando Wood's three
# tests rather than a bare "what are the criteria?".
EVAL_CRITERIA_QUESTION = (
    "How will the work be evaluated? Can we agree criteria that (1) connect to "
    "real-world business outcomes, (2) give oxygen to the creative idea rather "
    "than reduce its impact, and (3) indicate whether the work builds mental "
    "availability or fame?")


def review_loop1(ledger, fields):
    """Loop 1 self-review: flag ledger coverage below 85% and any captured values whose
    status is 'assumption'. Returns {passed, flags}; passed only when there are no flags."""
    flags = []
    if ledger["coverage_pct"] < 85:
        flags.append(f"Coverage {ledger['coverage_pct']}% < 85% — "
                     f"{len(ledger['unmapped'])} segments need a home (re-pass).")
    assumptions = sum(1 for v in fields.values()
                      for it in (v if isinstance(v, list) else [v])
                      if isinstance(it, dict) and it.get("status") == "assumption")
    if assumptions:
        flags.append(f"{assumptions} value(s) are assumptions — verify with client.")
    return {"passed": not flags, "flags": flags}


def review_loop2(loop2):
    """Loop 2 self-review: flag each of problem, objective and audience that is absent or
    has an empty value. Returns {passed, flags}; passed only when there are no flags."""
    missing = [k for k in ("problem", "objective", "audience")
               if not loop2.get(k) or not loop2[k].get("value")]
    flags = [f"Agency brief missing: {m}" for m in missing]
    return {"passed": not flags, "flags": flags}


# ---------------------------------------------------------------------------
# 5b. BETTERBRIEFS SCORECARD — quality of the client brief, not just presence
# ---------------------------------------------------------------------------

SCORECARD_DIMENSIONS = ("objectives_quality", "audience_vividness",
                        "single_minded_message", "evaluation_criteria",
                        "budget_interlock", "strategic_clarity", "language")

# Demographic clichés = "as sure a sign as any that you have not got a strategy".
AUDIENCE_CLICHES = re.compile(
    r"\b(millennials?|gen\s*[zxy]|boomers?|everyone|general (?:public|population)"
    r"|adults?\s*\d{2}\s*[-–]\s*\d{2}|all (?:adults|consumers))\b", re.I)


def _dim(dimension, verdict, evidence, fix=""):
    """Build one scorecard dimension row {dimension, verdict, evidence, fix}."""
    return {"dimension": dimension, "verdict": verdict,
            "evidence": evidence, "fix": fix}


def scorecard_heuristic(fields):
    """No-API scorecard: presence + cheap quality cues only. The LLM judge is
    the real test; this keeps the pipeline keyless-safe."""
    def items(key):
        """Return the captured entries for `key` that have a value, as a list (a
        single entry is wrapped); non-dict items are skipped."""
        v = fields.get(key)
        return [x for x in (v if isinstance(v, list) else [v])
                if isinstance(x, dict) and x.get("value")]

    dims = []
    objs = items("objective")
    if not objs and not items("success_metrics"):
        dims.append(_dim("objectives_quality", "missing", "no objective captured",
                         "Ask for a handful of benchmarked, time-stamped objectives."))
    elif len(objs) > 5:
        dims.append(_dim("objectives_quality", "vague",
                         f"{len(objs)} objectives captured — more than a handful",
                         "Adding objectives dramatically reduces the odds any works."))
    else:
        txt = " ".join(str(o.get("value")) for o in objs + items("success_metrics"))
        timed = bool(re.search(r"\b(20\d\d|q[1-4]|by \w+|\d+\s*(%|pts?|points))", txt, re.I))
        dims.append(_dim("objectives_quality", "pass" if timed else "vague",
                         "benchmark/time-stamp found" if timed
                         else "no benchmark or time stamp detected",
                         "" if timed else "Objectives need benchmarks and a time stamp."))

    aud = _val(fields, "target_audience")
    if not aud:
        dims.append(_dim("audience_vividness", "missing", "no audience captured",
                         "Ask for a vivid picture: demographics + psychographics + needs."))
    elif AUDIENCE_CLICHES.search(aud) or len(aud) < 40:
        dims.append(_dim("audience_vividness", "vague", aud[:120],
                         "Demographic clichés signal no strategy — push for a portrait."))
    else:
        dims.append(_dim("audience_vividness", "pass", aud[:120]))

    dims.append(_dim("single_minded_message",
                     "pass" if _val(fields, "key_message") else "missing",
                     _val(fields, "key_message") or "no key message captured",
                     "" if _val(fields, "key_message")
                     else "A good brief lands ONE message with proof points."))
    dims.append(_dim("evaluation_criteria",
                     "pass" if items("evaluation_criteria") else "missing",
                     "; ".join(str(x.get("value")) for x in items("evaluation_criteria"))[:120]
                     or "no criteria captured", ""))
    dims.append(_dim("budget_interlock",
                     "pass" if _val(fields, "budget") else "missing",
                     _val(fields, "budget") or "no budget captured",
                     "" if _val(fields, "budget")
                     else "Budget, objectives and audience must interlock."))
    dims.append(_dim("strategic_clarity",
                     "pass" if _val(fields, "strategic_angle") else "vague",
                     _val(fields, "strategic_angle") or "no strategic angle captured",
                     "" if _val(fields, "strategic_angle")
                     else "Don't leave strategy for the creative process to discover."))
    dims.append(_dim("language", "pass", "not assessed in heuristic mode"))
    return {"dimensions": dims,
            "single_mindedness": {"verdict": "single", "split_into": []},
            "summary": "Heuristic scorecard — run with an LLM key for the real judge.",
            "mode": "heuristic"}


def score_betterbriefs(text, fields=None):
    """LLM judge against the BetterBriefs rubric; heuristic fallback (from `fields`, which
    may be None when the call is made before the capture lands: run() then rebuilds the
    heuristic scorecard from the capture). Verdicts and dimension names are read in any
    case, one row per dimension (the first wins), and evidence the judge quotes must be in
    the brief, else the row is marked and downgraded to 'vague' (audit J11: 'Pass' used to
    read as vague and 'Multiple' as single, hiding the split-this-brief warning)."""
    obj = _json_call(f"CLIENT BRIEF:\n\"\"\"\n{_clip_brief(text)}\n\"\"\"", system=SCORECARD_SYSTEM,
                     max_tokens=MAXTOK_EXTRACT)
    if not isinstance(obj, dict) or not isinstance(obj.get("dimensions"), list):
        return scorecard_heuristic(fields or {})
    hay = _norm_quote(text)
    dims, scored = [], set()
    for d in obj["dimensions"]:
        if not isinstance(d, dict):
            continue
        name = str(d.get("dimension") or "").strip().lower()
        if name not in SCORECARD_DIMENSIONS or name in scored:
            continue
        scored.add(name)
        verdict = str(d.get("verdict") or "").strip().lower()
        verdict = verdict if verdict in ("pass", "vague", "missing") else "vague"
        evidence = str(d.get("evidence") or "")
        if evidence.strip() and _norm_quote(evidence) not in hay:
            evidence += " (evidence not found in brief)"
            if verdict == "pass":
                verdict = "vague"
        dims.append(_dim(name, verdict, evidence, str(d.get("fix") or "")))
    for missing in SCORECARD_DIMENSIONS:          # judge skipped one — make it visible
        if missing not in scored:
            dims.append(_dim(missing, "vague", "not scored by judge"))
    sm = obj.get("single_mindedness") or {}
    if not isinstance(sm, dict):
        sm = {}
    multiple = str(sm.get("verdict") or "").strip().lower() == "multiple"
    split = sm.get("split_into")
    return {"dimensions": dims,
            "single_mindedness": {
                "verdict": "multiple" if multiple else "single",
                "split_into": [str(s) for s in split] if (multiple and isinstance(split, list)) else []},
            "summary": str(obj.get("summary") or ""), "mode": "llm"}


# ---------------------------------------------------------------------------
# 6. LOOP 2 — shape the capture into a first-round agency brief
# ---------------------------------------------------------------------------

def _val(fields, key):
    """Return a captured field's value for shaping. A list field joins its non-empty values
    (Captured dicts or plain strings) with '; '; a dict field returns its 'value' unchanged.
    Returns None for an absent or empty field, and also for a field stored as a bare
    string, which is not a Captured entry."""
    v = fields.get(key)
    if isinstance(v, list):
        parts = []
        for x in v:
            if isinstance(x, dict) and x.get("value"):
                parts.append(str(x["value"]))
            elif isinstance(x, str) and x.strip():
                parts.append(x.strip())
        return "; ".join(parts) or None
    return v.get("value") if isinstance(v, dict) else None


def shape_loop2(fields, llm_open_qs):
    """Map Loop-1 capture into an agency-brief shape + open questions.
    Deterministic so it runs without an API; LLM open-questions used if present."""
    def slot(text):
        """Wrap a value as a Loop 2 slot: status 'fact' when the value is truthy, else
        'gap'."""
        return {"value": text, "status": "fact" if text else "gap"}

    scope_bits = [b for b in (_val(fields, "deliverables"), _val(fields, "budget"),
                              _val(fields, "timeline")) if b]
    loop2 = {
        "problem": slot(_val(fields, "business_problem")),
        "objective": slot(_val(fields, "objective") or _val(fields, "success_metrics")),
        "audience": slot(_val(fields, "target_audience")),
        # BetterBriefs slots: the single-minded message (+ proof), how the work
        # will be judged, and the strategic sacrifice (who/what we're NOT for).
        "key_message": slot(_val(fields, "key_message")),
        "evaluation_criteria": slot(_val(fields, "evaluation_criteria")),
        "not_doing": slot(_val(fields, "anti_target")),
        "scope": slot(" · ".join(scope_bits) if scope_bits else None),
    }

    # open questions = genuine gaps in the core fields (a field counts as present
    # if it OR one of its fallbacks was captured), plus any the LLM surfaced — with the
    # capture's internal sentence references scrubbed out (audit H11).
    open_qs = [({**q, "question": _scrub_markers(q.get("question"))} if isinstance(q, dict)
                else _scrub_markers(q)) for q in (llm_open_qs or [])]
    for key, (fallbacks, why, priority) in CORE_FIELDS.items():
        if _val(fields, key) or any(_val(fields, fb) for fb in fallbacks):
            continue
        question = (EVAL_CRITERIA_QUESTION if key == "evaluation_criteria"
                    else f"What is the {key.replace('_', ' ')}?")
        open_qs.append({
            "question": question, "why_it_matters": why, "priority": priority,
        })
    rank = {"blocker": 0, "important": 1, "nice_to_have": 2}

    def _q_rank(q):
        """Sort key for open questions: blocker 0, important 1, nice_to_have 2, and 3 for
        anything else (an unknown or absent priority, or a bare-string question)."""
        pr = q.get("priority") if isinstance(q, dict) else None
        return rank.get(pr, 3) if isinstance(pr, str) else 3

    open_qs.sort(key=_q_rank)
    loop2["open_questions"] = open_qs
    return loop2


# ---------------------------------------------------------------------------
# 7. RENDER (Brain markdown mirror)
# ---------------------------------------------------------------------------

FIELD_TITLES = {
    "background_context": "Background / context", "business_problem": "Business problem",
    "objective": "Objective", "target_audience": "Target audience",
    "key_message": "Key message (single-minded)", "proof_points": "Proof points",
    "evaluation_criteria": "Evaluation criteria", "strategic_angle": "Strategic angle",
    "anti_target": "Not for / not targeting",
    "deliverables": "Deliverables", "mandatories": "Mandatories (non-negotiable)",
    "budget": "Budget", "timeline": "Timeline & key dates",
    "success_metrics": "Success metrics / KPIs", "competitors_market": "Competitors & market",
    "tone_and_brand": "Tone & brand", "decision_makers": "Decision-makers",
    "constraints": "Constraints",
}
VERDICT_ICON = {"pass": "✅", "vague": "⚠️", "missing": "❌"}
STATUS_TAG = {"fact": "", "assumption": " _(assumption)_", "gap": " _(gap)_"}


def _fmt(c):
    """Render one Captured entry for review.md: its value followed by its STATUS_TAG
    (nothing for a fact or an unknown status), or '_not stated_' when the entry is empty or
    its value is None or ''."""
    if not c or c.get("value") in (None, ""):
        return "_not stated_"
    return f"{c['value']}{STATUS_TAG.get(c.get('status', 'fact'), '')}"


# ---------------------------------------------------------------------------
# 6b. LOOPS 3–7 — RAG-grounded strategy
#     The ONLY place retrieval happens. Built from the Loop-2 brief, never from
#     Loop-1 capture, and the rag retriever is imported lazily so the Loop-1
#     path never even touches `rag`. Behind a flag; degrades to a stub if the
#     index hasn't been built.
# ---------------------------------------------------------------------------

# Each loop maps a stage of the strategic process to a query built from the
# brief gist. Retrieval grounds it in the planner playbooks + IPA evidence.
# The five per-field queries live in rag/mix_queries.py, shared with rag_io.handle's mix
# path, so the middleware retrieves exactly as the brief generator does.
if str(HERE / "rag") not in sys.path:
    sys.path.insert(0, str(HERE / "rag"))
from mix_queries import LOOP37_SPECS  # noqa: E402


def _rerank_hits(query: str, hits: list, k: int) -> list:
    """Second-stage rerank: dense retrieval gives recall (vector-similar), this gives
    precision (actually-relevant). The hosted NVIDIA nv-rerankqa NIM isn't provisioned on
    this key, so we rerank in-house via the model chain — keeps retrieval self-hosted and
    provenance-preserving. Best-effort: falls back to the cosine order on any failure.
    Disable with BRIEF_RERANK=0."""
    if os.environ.get("BRIEF_RERANK", "1") == "0" or len(hits) <= k:
        return hits[:k]
    ordered = _chain_order(query, hits)
    if ordered is not None:
        return ordered[:k]
    def _snippet(t):
        """Clip passage text to 200 characters for the rerank listing. The pattern is a
        raw string with a doubled backslash, so it matches a literal backslash followed by one
        or more 's' characters, not whitespace: in practice newlines and runs of spaces are kept."""
        return re.sub(r'\s+', ' ', t)[:200]
    listing = "\n".join(f"[{i}] {h.get('citation','')}: {_snippet(h.get('text',''))}"
                        for i, h in enumerate(hits))
    obj = _json_call(
        f"QUERY: {query}\n\nPASSAGES:\n{listing}",
        system=("You are a retrieval reranker for a strategy brief. Rank the passages by how directly "
                "each one helps answer the QUERY — most useful first. Demote passages that merely share "
                "words but miss the intent. Return ONLY raw JSON: {\"ranking\": [passage indexes, best first]}"),
        retries=1, max_tokens=MAXTOK_JUDGE)
    if isinstance(obj, dict) and isinstance(obj.get("ranking"), list):
        order = [i for i in obj["ranking"] if isinstance(i, int) and 0 <= i < len(hits)]
        order += [i for i in range(len(hits)) if i not in order]   # keep any the judge dropped
        return [hits[i] for i in order][:k]
    return hits[:k]


def _chain_order(query: str, hits: list) -> list | None:
    """Order `hits` by the RAG validation chain (RAG_VALIDATOR) when one is configured:
    one cross-encoder call instead of an LLM rerank call (~0.5s against ~2-12s). Ordering
    ONLY — nothing is dropped, because a live brief showed a QA reranker rejects precedent
    it should keep (engine/rag/docs/adr/0003, Live finding). Judged hits by calibrated
    score, else raw output; unjudged ones after, in fused order.

    Returns None — use the LLM rerank — when no chain is configured, nobody answered, or
    the chain cannot be built. parse_brief's rule is that RAG never crashes a run, so a
    misconfigured RAG_VALIDATOR is reported on stderr here rather than raised.
    Note: with `local` leading the chain, the five loops' concurrent calls queue on one
    worker and may time out; the chain then falls through or returns None (LLM rerank)."""
    try:
        _load_retriever()
        import brief_context                              # rag/ is on sys.path now
        from judge_base import Passage, Query
        chain = brief_context.default_chain()
    except Exception as e:
        print(f"[!] validation chain unavailable ({e.__class__.__name__}: {e}); "
              f"using the LLM rerank", file=sys.stderr)
        return None
    if chain.empty:
        return None
    res = chain.judge(Query(text=query), [
        Passage(str(i), ((h.get("header") or "") + "\n" + (h.get("text") or "")).strip())
        for i, h in enumerate(hits)])
    if res.verdicts is None:
        return None
    aligned = res.aligned()
    def key(i):
        """Judged first by score (else raw), then unjudged in fused order."""
        v = aligned[i]
        if v is None:
            return (1, 0.0, i)
        return (0, -(v.score if v.score is not None else (v.raw or 0.0)), i)
    return [hits[i] for i in sorted(range(len(hits)), key=key)]


@functools.lru_cache(maxsize=1)
def _case_packs() -> tuple:
    """Case packs from the corpus dirs (or packs.lock), discovered once per process —
    never a hardcoded list, so adding or removing a pack needs no code change."""
    try:
        from packs import discover_packs
        return tuple(p for p in discover_packs() if p.kind == "case")
    except Exception:
        return ()


def _dedupe_by_source(hits: list) -> list:
    """First (best-ranked) hit per source, order kept."""
    seen, out = set(), []
    for h in hits:
        if h.get("source") not in seen:
            seen.add(h.get("source")); out.append(h)
    return out


def _load_retriever():
    """Import the Loops 3–7 retriever lazily. Adds briefing/rag/ to sys.path and
    imports the top-level `retrieve` module — whose own `import rag` then resolves
    to rag/rag.py (not the rag/ dir as a package). Keeps the Loop-1 path rag-free."""
    rag_dir = HERE / "rag"
    if str(rag_dir) not in sys.path:
        sys.path.insert(0, str(rag_dir))
    import retrieve                                # noqa: E402  (rag/retrieve.py)
    return retrieve


def _retrieval_scopes(fields, brand: str | None = None) -> list[str]:
    """The scopes Loops 3–7 may retrieve from, derived the SAME way the brief's own
    retrieval entry point derives them.

    Both paths call brief_context.scopes_for(), rather than this file growing its own
    idea of what a scope is. Two derivations of a confidentiality boundary drift, and
    the one that drifts is the one nobody is looking at — which, until now, was this
    one: Loops 3–7 passed no scope at all.

    Loop-1 fields are capsules ({"value": ...}) or lists of them, and scopes_for()
    expects flat strings, so they are flattened through _val() first. Falls back to
    the house corpus on any error: a scope that cannot be derived must narrow, never
    widen.

    `brand` is the authorised brand for this run. `fields` cannot supply it — these are
    extracted from the uploaded brief, so letting them name the brand would let the
    attachment pick its own scope."""
    try:
        import brief_context                        # rag/ is on sys.path via _load_retriever
        pairs = {k: _val(fields, k) for k in (fields or {})}
        pairs = {k: v for k, v in pairs.items() if v}
        _q, _kw, filters, _notes = brief_context.plan(pairs)
        notes: list[str] = []
        scopes = brief_context.scopes_for(pairs, filters, brand=brand, notes=notes)
        for n in notes:
            print(f"[i] {n}", file=sys.stderr)
        return scopes
    except Exception:
        return ["global"]


def _capsule_text(v) -> str:
    """Plain text from a Loop-2 capsule / Loop-1 field (dict | list | str)."""
    if isinstance(v, dict):
        return str(v.get("value") or "")
    if isinstance(v, list):
        return " · ".join(t for t in (_capsule_text(x) for x in v) if t)
    return str(v or "")


def _brief_gist(loop2, fields) -> dict:
    """Build the gist the Loops 3–7 queries are written from: problem, objective, audience
    and key_message. Each is the Loop 2 slot's text, falling back to the Loop 1 capture
    (business_problem; objective, then success_metrics; target_audience; key_message), and
    '' when neither has it. Returns a dict of four strings."""
    g = {k: _capsule_text(loop2.get(k))
         for k in ("problem", "objective", "audience", "key_message")}
    g["problem"] = g["problem"] or _val(fields, "business_problem") or ""
    g["objective"] = g["objective"] or _val(fields, "objective") or _val(fields, "success_metrics") or ""
    g["audience"] = g["audience"] or _val(fields, "target_audience") or ""
    g["key_message"] = g["key_message"] or _val(fields, "key_message") or ""
    return g


def _classify_intent(gist, fields) -> str:
    """Lightweight brief-intent label. The full Loop-0 classifier is backlog #2;
    here it just biases the read and is surfaced in the output."""
    blob = (" ".join(gist.values()) + " "
            + " ".join(_capsule_text(fields.get(k)) for k in fields)).lower()
    table = [("tender", ("tender", "rfp", "itt", "procurement", "pitch document")),
             ("media", ("media plan", "media buying", "channel mix", "reach and frequency", "grp")),
             ("btl-event", ("activation", "experiential", " btl", "sampling", "event")),
             ("retail", ("shopper", "in-store", "point of sale", "retail media", "trade")),
             ("creative-campaign", ("campaign", "creative", "advert", "tvc", "film", "launch"))]
    for label, kws in table:
        if any(kw in blob for kw in kws):
            return label
    return "general-strategy"


def _synthesize_loops37(gist, intent, loops) -> str:
    """Ground a short paragraph per loop in the retrieved evidence, citing
    `source › section`. One call per loop, run concurrently: the loops are independent,
    and one call writing all five serially was 41.8s of a 56.9s brief (73%). Same
    instructions, model and output shape as before; about 4 extra calls of small input.
    Falls back per loop to an evidence-only summary when a call returns no paragraph.
    Mutates loops."""
    synth_model = os.environ.get("BRIEF_SYNTH_MODEL") or None
    answered = {}

    def one(item):
        """Write one loop's paragraph; returns (key, paragraph or None)."""
        key, d = item
        info = {}
        ev = "\n".join(f"  - ({e['citation']}) {e['snippet']}" for e in d["evidence"]) \
             or "  (no evidence retrieved)"
        user = (
            "You are an advertising planning director. Using ONLY the retrieved evidence "
            "below, write one grounded, specific paragraph that applies the frameworks to "
            "THIS brief. Cite the playbooks you use inline as (source › section), copied "
            "exactly. Never invent frameworks or statistics.\n\n"
            f"BRIEF GIST: problem={gist['problem']!r}; objective={gist['objective']!r}; "
            f"audience={gist['audience']!r}; key_message={gist['key_message']!r}; intent={intent}.\n\n"
            f"### {key} — {d['title']}\nRETRIEVED EVIDENCE:\n{ev}\n\n"
            'Return JSON only: {"paragraph": "..."}')
        obj = _json_call(user, system="You are a precise strategy planner. Output JSON only.",
                         model=synth_model, max_tokens=MAXTOK_SYNTH_ONE, info=info,
                         schema={"type": "object", "properties": {"paragraph": {"type": "string"}},
                                 "required": ["paragraph"], "additionalProperties": False})
        para = obj.get("paragraph") if isinstance(obj, dict) else None
        if info.get("link"):
            answered[info["link"]] = answered.get(info["link"], 0) + 1
        return key, (para.strip() if isinstance(para, str) and para.strip() else None)

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=max(1, len(loops))) as ex:
        paras = dict(ex.map(_scoped(one), list(loops.items())))
    wrote = False
    for key, d in loops.items():
        if paras.get(key):
            d["synthesis"] = paras[key]; wrote = True
        elif d["evidence"]:                           # evidence-only fallback, per loop
            tops = "; ".join(f"{e['framework']} ({e['citation']})" for e in d["evidence"][:3])
            d["synthesis"] = f"Apply, in order of fit: {tops}."
        else:
            d["synthesis"] = "No playbook evidence retrieved for this loop."
    if wrote:
        # The label names the link that ANSWERED, not the configured default: every Opus
        # 5.5 run used to read 'llm:claude-opus-4-6' here (audit CC14/F12).
        used = max(answered, key=answered.get) if answered else (synth_model or model_for(resolve_provider()))
        return f"llm:{used.split(':', 1)[-1]}"
    return "evidence-only"


def _loops37_from_digests(loop2, fields, synthesize=True) -> dict | None:
    """Digest mode: no vector store, but pack digests (packs_dist/<id>/digest.md,
    written offline by scripts/distil_pack.py) exist. Ground Loops 3–7 on those —
    static per pack rather than query-matched, but the synthesis, citations and
    strategy fill all run. This is the app's default grounding path."""
    digest_dir = HERE / "packs_dist"
    digests = sorted(digest_dir.glob("*/digest.md")) if digest_dir.is_dir() else []
    entries = []
    for d in digests:
        text = d.read_text(errors="replace").strip()
        if text:
            entries.append({
                "citation": f"{d.parent.name} digest",
                "framework": f"{d.parent.name} digest",
                "category": None,
                "score": 0.0,
                "snippet": re.sub(r"\s+", " ", text)[:600],
            })
    if not entries:
        return None
    gist = _brief_gist(loop2, fields)
    intent = _classify_intent(gist, fields)
    loops = {key: {"title": title, "query": "(digest mode — no retrieval)",
                   "evidence": list(entries)}
             for key, title, _q in LOOP37_SPECS}
    synthesis_mode = _synthesize_loops37(gist, intent, loops) if synthesize else "deferred"
    return {
        "enabled": True,
        "index": "digests:packs_dist",
        "intent": intent,
        "k": 0,
        "gist": gist,
        "loops": loops,
        "sources_used": sorted({e["citation"] for e in entries}),
        "synthesis_mode": synthesis_mode,
    }


# The evidence a gate reads and the evidence a human skims are different things. The
# 280-char clip below is a DISPLAY concern; a judgement backend asked "does this passage
# support that claim?" needs the passage, not its first sentence. So each entry carries
# both: `snippet` for reading, `text` for checking.
# Bounded rather than unbounded, because the corpus contains chunks that are not
# retrievable units at all — 91-pestle-steep-analysis.md has a single 238,675-char
# "OUTPUT TEMPLATE" section, and three of the four chunks over 20k chars come from it.
# 6,000 chars keeps the p90 of every level whole (child 785, chunk 2,507, parent 4,790)
# and stops one malformed chunk from swallowing a prompt.
EVIDENCE_MAX_CHARS = 6000


def loops_3_7(loop2, fields, k=5, index_dir=None, synthesize=True) -> dict:
    """Loops 3–7: classify intent → build queries from the Loop-2 brief → retrieve
    top-k playbooks + effectiveness evidence → ground a short strategy with
    citations. Retrieval-only; degrades to a disabled stub if the index is absent.
    synthesize=False skips the five synthesis calls (synthesis_mode "deferred") so run()
    can make them in parallel with the hero fields."""
    try:
        retriever = _load_retriever()
    except Exception as e:                            # never crash the run over RAG
        return {"enabled": False,
                "reason": f"retriever import failed: {e.__class__.__name__}: {e}"}
    if not retriever.index_available(index_dir):
        # LOUD on purpose. On 2026-09-24 the store was unreachable for a whole comparison
        # run and every brief quietly used the pack digests instead: no retrieval, no
        # validator, and nothing in the output said so. The warning goes to stderr, the
        # reason into the result, so a run on digests is never mistaken for a RAG run.
        label = retriever.index_label(index_dir)
        print(f"[!] RAG store unavailable ({label}); Loops 3-7 fall back to the pack digests — "
              "no retrieval and no validation for this brief.", file=sys.stderr)
        digest_loops = _loops37_from_digests(loop2, fields, synthesize=synthesize)
        if digest_loops:
            digest_loops["fallback"] = {"to": "digests", "reason": f"RAG store unavailable: {label}"}
            return digest_loops
        return {"enabled": False,
                "reason": "no retrieval store (rag/index absent, no Qdrant) and no pack "
                          "digests (packs_dist/) — Loops 3–7 skipped."}

    def _digests(reason: str) -> dict:
        """Fall back to the pack digests with the reason recorded and announced. LOUD on
        purpose: on 2026-09-24 a whole comparison run quietly used digests and nothing in
        the output said so."""
        print(f"[!] {reason}; Loops 3-7 fall back to the pack digests — no retrieval and no "
              "validation for this brief.", file=sys.stderr)
        try:
            digest_loops = _loops37_from_digests(loop2, fields, synthesize=synthesize)
        except Exception as e:
            digest_loops = None
            reason += f"; digests failed too: {e.__class__.__name__}: {e}"
        if digest_loops:
            digest_loops["fallback"] = {"to": "digests", "reason": reason}
            return digest_loops
        return {"enabled": False, "reason": f"{reason} — Loops 3–7 skipped."}

    def _grounded() -> dict:
        """The retrieval stage proper; raises on any failure, caught below."""
        gist = _brief_gist(loop2, fields)
        intent = _classify_intent(gist, fields)
        scopes = _retrieval_scopes(fields)

        # Case packs, discovered from the corpus dirs (or packs.lock at runtime) —
        # never a hardcoded list, so adding/removing a pack needs no code change
        # and a pack with no corpus simply cannot exist (the old `effie` bug).
        # Case packs are only read by the loops path (_one_loop); discovered lazily and once
        # per process (_case_packs), so the mix path never pays the directory scan.

        def _one_loop(spec):
            """Retrieval + rerank + precedent pull for ONE loop — fully independent given
            the gist, so the five loops run concurrently (network-bound: Qdrant + NIM
            embeddings + optional rerank). ~5× wall-clock cut on this stage."""
            key, title, qfn = spec
            q = re.sub(r"\s+", " ", qfn(gist)).strip()
            seen, evidence = set(), []
            # Over-retrieve for recall, then LLM-rerank down to k for precision.
            # 1.5× is enough headroom — 3× ranked 15 passages to keep 5 (dead tokens).
            pool = retriever.retrieve(q, k=max(int(k * 1.5), 8), index_dir=index_dir,
                                      scopes=scopes)
            # One section per source BEFORE the cut to k, not after: several sections of one
            # playbook used to fill the top k and then collapse to one — loop5_proposition
            # returned 1 evidence item where its siblings returned 5-8.
            pool = _dedupe_by_source(pool)
            for h in _rerank_hits(q, pool, k):
                if h["source"] in seen:
                    continue
                seen.add(h["source"])
                evidence.append({
                    "citation": h["citation"],
                    "framework": h.get("framework") or h["source"],
                    "category": h.get("category"),
                    "score": h["score"],
                    # Which confidentiality scope this passage came from. Written per hit and
                    # carried into the CLAN file, because the index is mutable — chunks get
                    # retagged and superseded — so a scope not recorded at retrieval time
                    # cannot be recovered afterwards from anything.
                    "scope": str((h.get("metadata") or {}).get("scope") or "global"),
                    "snippet": re.sub(r"\s+", " ", ((h.get("header") + " — ") if h.get("header") else "") + h["text"])[:280],
                    "text": (h.get("text") or "")[:EVIDENCE_MAX_CHARS],
                })
            # Pull award-winning PRECEDENT cases from every case pack whose `loops`
            # gate includes this loop (default: insight + substantiation). Which packs
            # exist, their tag, and their per-pack k all come from the pack itself.
            eligible = [p for p in _case_packs() if p.eligible(key)]
            if eligible:
                case_q = (f"award-winning precedent insight {gist['audience']} {gist['problem']}"
                          if key == "loop4_insight"
                          else f"award-winning effectiveness results proof {gist['objective']}"
                          if key == "loop6_substantiation" else q)
                for pack in eligible:
                    for h in retriever.retrieve(case_q, k=pack.k, index_dir=index_dir,
                                                where={"source": pack.tag, "level": "parent"},
                                                scopes=scopes):
                        if h["source"] not in seen:
                            seen.add(h["source"])
                            evidence.append({
                                "citation": h["citation"],
                                "framework": h.get("framework") or h["source"],
                                "category": h.get("category"),
                                "score": h["score"],
                                "scope": str((h.get("metadata") or {}).get("scope") or "global"),
                                "snippet": re.sub(r"\s+", " ", ((h.get("header") + " — ") if h.get("header") else "") + h["text"])[:280],
                                "text": (h.get("text") or "")[:EVIDENCE_MAX_CHARS],
                            })
            return key, {"title": title, "query": q, "evidence": evidence}

        # RAG_PATH: `mix` (default, Sai's decision 2026-09-23) runs each loop's query through
        # brief_context.build_multi() — the per-field queries that make this path good, with
        # brief_context's scope, admission, budgets, thin-bucket widening and validation.
        # `loops` is the previous path (per-loop retrieve + rerank + case packs), kept one
        # environment variable away until Shrey's finished-brief test confirms the choice.
        # Blind-judged on 6 real briefs: B 23, MIX 20, A 14 (B and MIX within judge noise).
        rag_path = (os.environ.get("RAG_PATH") or "mix").strip().lower()
        loops, retrieval_trace = None, None
        if rag_path == "mix":
            # A mix failure goes to the digests, NOT to the loops path: the loops path would hit
            # the same failing store again, five requests at up to 240 s each (audit RAG-2/C3).
            loops, retrieval_trace = _loops_via_mix(gist, fields, index_dir)
        if loops is None:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=len(LOOP37_SPECS)) as pool_ex:
                results = dict(pool_ex.map(_one_loop, LOOP37_SPECS))
            # Preserve the canonical loop order regardless of completion order.
            loops = {key: results[key] for key, _t, _q in LOOP37_SPECS}
        citations_all = [e["citation"] for d in loops.values() for e in d["evidence"]]
        if not citations_all:
            raise _NoEvidence("retrieval returned no evidence")

        # Run-level scope record, alongside the per-hit one on every evidence entry. Two
        # different questions: `scopes` is what this run was AUTHORISED for, `scopes_served`
        # is what it actually used. A served scope that is not in the authorised set is the
        # signal that something upstream is wrong — and without both written down, neither
        # question has an answer after the fact.
        served: dict[str, int] = {}
        for d in loops.values():
            for e in d["evidence"]:
                served[e["scope"]] = served.get(e["scope"], 0) + 1

        # synthesize=False: run() writes the five paragraphs itself, alongside the hero fields,
        # which read the evidence and never the synthesis ("deferred" until then).
        synthesis_mode = _synthesize_loops37(gist, intent, loops) if synthesize else "deferred"
        return {
            "enabled": True,
            "index": (retriever.index_label(index_dir) if hasattr(retriever, "index_label")
                      else str(index_dir or getattr(retriever, "DEFAULT_INDEX", HERE / "rag" / "index"))),
            "store": os.environ.get("RAG_STORE", "local").lower().strip() or "local",
            "intent": intent,
            "scopes": list(scopes),
            "scopes_served": dict(sorted(served.items())),
            "k": k,
            "gist": gist,
            "loops": loops,
            "sources_used": sorted(set(citations_all)),
            "synthesis_mode": synthesis_mode,
            "rag_path": rag_path,
            "retrieval_trace": retrieval_trace,
            # Loops whose evidence no validator judged (see _loops_via_mix); [] when every
            # loop was validated or validation is off.
            "validation_degraded": list((retrieval_trace or {}).get("validation_degraded") or []),
        }

    # RAG never crashes a run (audit N1/RAG-2): a store that dies after the availability
    # check, a validator that raises, an empty result — all become the digest fallback with
    # a recorded reason, never an exception into run() and never a brief with no strategy.
    try:
        return _grounded()
    except _NoEvidence as e:
        return _digests(str(e))
    except Exception as e:
        return _digests(f"retrieval failed mid-run: {e.__class__.__name__}: {e}")


def _loops_via_mix(gist, fields, index_dir=None) -> tuple[dict, dict]:
    """Loops 3-7 evidence from the mix path: each LOOP37_SPECS query through
    brief_context.build_multi() in one pass. Returns (loops, trace) in exactly the shape
    the loops path produces — citation "doc › section", framework, category (doc_kind,
    which _precedent_blocks keys IPA precedent on), score, scope, snippet, text — so
    fill_derivable_fields, the synthesis and review.md read it unchanged. Scope comes
    from the same scopes_for() as _retrieval_scopes (no brand authorised here, as there)."""
    _load_retriever()
    import brief_context
    pairs = {k: _val(fields, k) for k in (fields or {})}
    pairs = {k: v for k, v in pairs.items() if v}
    pairs.update({k: v for k, v in (("problem", gist["problem"]), ("objective", gist["objective"]),
                                    ("audience", gist["audience"])) if v and k not in pairs})
    from mix_queries import queries_for
    queries = queries_for(gist)
    # The validator sees the brief, not only a 500-character templated query: the gist
    # plus the captured background and competitor context, clipped (audit JL-5: with the
    # brief as context jev's p>=0.5 count went from 36 to 70 of 960 replayed passages).
    # Brief text already goes to the validator vendor under production-use terms (jev).
    context = "\n".join(f"{k}: {v}" for k, v in (
        ("problem", gist.get("problem")), ("objective", gist.get("objective")),
        ("audience", gist.get("audience")), ("key_message", gist.get("key_message")),
        ("background", _val(fields, "background_context")),
        ("competitors", _val(fields, "competitors_market"))) if v)[:3000]
    # jev on for every brief (Sai, 2026-09-26): brief_chain() defaults to jev when
    # RAG_VALIDATOR is unset. A backend that cannot be built here is said loudly and the
    # brief runs unvalidated, which loops3_7.validation_degraded then reports.
    unconfigured = None
    try:
        chain = brief_context.brief_chain()
    except Exception as e:      # noqa: BLE001 — BackendNotConfigured or a bad RAG_VALIDATOR
        unconfigured = f"{e.__class__.__name__}: {e}"
        print(f"[!] RAG validator not available ({unconfigured}); this brief runs UNVALIDATED.",
              file=sys.stderr)
        import judge
        chain = judge.Chain([], requested="unconfigured")
    mc = brief_context.build_multi(pairs, queries, index_dir=index_dir, context=context, chain=chain)
    loops = {}
    for key, title, _q in LOOP37_SPECS:
        ev = []
        for h in mc.fields.get(key, []):
            md = h.metadata or {}
            head = (h.header + " — ") if h.header else ""
            # doc_id, not Hit.source: Hit.source is the corpus name ("ipa"), and a citation
            # must name the document ("ipa_0003 › Insight") as the loops path's file names do.
            ev.append({"citation": f"{h.doc_id} › {h.section}", "cite": h.cite,
                       "framework": md.get("framework_name") or h.title or h.source,
                       "category": md.get("doc_kind") or md.get("category"),
                       "score": h.score, "scope": str(md.get("scope") or "global"),
                       "snippet": re.sub(r"\s+", " ", head + h.text)[:280],
                       "text": (h.text or "")[:EVIDENCE_MAX_CHARS]})
        loops[key] = {"title": title, "query": queries[key], "evidence": ev}
    # Which validator judged each loop's evidence, written where the brief and the eval
    # rows can see it. Before 2026-09-25 this sat only inside retrieval_trace.validation:
    # a smoke run had 4 of 5 loops with no validator verdict and nothing said so (audit
    # JL-2/RAG-11). loops3_7.validation_degraded lists the loops left unvalidated.
    trace = mc.trace if isinstance(mc.trace, dict) else {}
    val = trace.get("validation")
    degraded = []
    if unconfigured:
        degraded = [key for key, _t, _q in LOOP37_SPECS]
        for key in degraded:
            loops[key]["validated_by"] = None
            loops[key]["validation_fell_back"] = True
        trace = {**trace, "validator_unconfigured": unconfigured}
    elif isinstance(val, dict) and isinstance(val.get("per_field"), dict):
        for key, _t, _q in LOOP37_SPECS:
            pf = val["per_field"].get(key)
            pf = pf if isinstance(pf, dict) else {}
            used = pf.get("backend_used")
            loops[key]["validated_by"] = used
            loops[key]["validation_fell_back"] = bool(pf.get("fell_back")) or not used
            if not used:
                degraded.append(key)
        if degraded:
            print(f"[!] retrieval validation missing for {len(degraded)} of {len(LOOP37_SPECS)} loops "
                  f"({', '.join(degraded)}): their evidence is unvalidated.", file=sys.stderr)
    # The brief path retrieves UNFILTERED: the capture's field names match none of
    # plan()'s filter/keyword keys, so no category filter, no brand keywords and no
    # widening apply here (audit RAG-9/JL-8; a real category filter is an A/B first).
    notes = list(trace.get("notes") or [])
    if not trace.get("filters") and not trace.get("keywords"):
        notes.append("brief path: no category filter or brand keywords (capture fields carry none)")
    trace = {**trace, "validation_degraded": degraded, "notes": notes,
             "validator_context_chars": len(context)}
    return loops, trace


def render_loops37(L, brief):
    """Append the Loops 3–7 markdown section. No-op when the stage didn't run
    (flag off), so Loop-1/Loop-2 output stays byte-for-byte identical."""
    s = brief.get("loops3_7")
    if not s:
        return
    L.append("## Loops 3–7 · RAG-grounded strategy  ")
    if not s.get("enabled"):
        L.append(f"_skipped — {s.get('reason', '')}_\n")
        return
    # Provenance from the run itself, not a constant: which path retrieved and which tier
    # embedded (a hardcoded model name here misreported every brief and hid fallbacks).
    embed = (s.get("retrieval_trace") or {}).get("embed") or "hosted nemotron-3-embed-1b"
    L.append(f"_intent: {s['intent']} · retrieval: {s.get('rag_path') or 'loops'} · embed: {embed} "
             f"· synthesis: {s['synthesis_mode']}_\n")
    for d in s["loops"].values():
        L.append(f"### {d['title']}\n")
        if d.get("synthesis"):
            L.append(f"{d['synthesis']}\n")
        if d["evidence"]:
            L.append("_Grounded in:_")
            for e in d["evidence"]:
                cat = f" · {e['category']}" if e.get("category") else ""
                L.append(f"- **{e['framework']}** ({e['citation']}{cat}) — {e['snippet']}")
            L.append("")
        else:
            L.append("_No playbook evidence retrieved for this loop._\n")
    if s.get("sources_used"):
        L.append(f"_Sources cited: {len(s['sources_used'])} playbook sections._\n")


def render_client_brief(brief) -> str:
    """The DELIVERABLE — only the final brief. Assembles the Golden Brief (facts +
    generated strategy) into a clean one-pager: no loop labels, no provenance tags,
    no ledgers, no scorecard, no 'Grounded in' citations. All of that machinery lives
    in review.md. This is what a creative director actually reads."""
    m = brief["meta"]
    gf = (brief.get("loop2_golden") or {}).get("fields", {}) or {}
    l2 = brief.get("loop2_brief", {}) or {}
    title = m.get("project") or m.get("client") or "Client brief"

    def gv(fid):                      # golden value, else loop-2 fallback for the FACTS only
        """Return golden field `fid`'s value. When it is empty, only background,
        objectives and audience fall back to the Loop 2 slot (problem, objective, audience);
        every other field, the generated strategy fields included, returns its empty value so
        the section renders as to be agreed."""
        f = gf.get(fid)
        v = f.get("value") if isinstance(f, dict) else f
        if v:
            return v
        # Strategy fields (insight, smp, reasons_to_believe, desired_response) must NEVER
        # fall back to a loop-2 value: if generation didn't clear the rubric the field is a
        # real gap (and carries an open question). Falling back would re-show the masterbrand
        # line while also flagging "agree the SMP" — the contradiction. Facts may fall back.
        fb = l2.get({"background": "problem", "objectives": "objective",
                     "audience": "audience"}.get(fid, ""))
        return fb.get("value") if isinstance(fb, dict) else fb   # loop-2 fields are {value,status}

    L = [f"# {title} — Brief", ""]
    TBD = "_To be agreed — see open questions._"

    def text_section(heading, value):
        """Append a '## heading' section: the value as text (internal sentence markers
        scrubbed), or the to-be-agreed placeholder when it is empty, then a blank line."""
        L.append(f"## {heading}")
        L.append(_scrub_markers(str(value)) if value else TBD)
        L.append("")

    text_section("Background", gv("background"))

    obj = gv("objectives")
    L.append("## Objectives")
    if isinstance(obj, dict):
        for k, lab in (("commercial", "Commercial"), ("behavioural", "Behavioural"),
                       ("attitudinal", "Attitudinal")):
            if obj.get(k):
                L.append(f"- **{lab}:** {obj[k]}")
    elif obj:
        L.append(str(obj))
    else:
        L.append(TBD)
    L.append("")

    text_section("Audience", gv("audience"))
    text_section("Competitor context", gv("competitor_context"))
    text_section("The insight", gv("insight"))
    text_section("Single-minded proposition", gv("smp"))

    rtb = gv("reasons_to_believe")
    L.append("## Reasons to believe")
    if isinstance(rtb, list) and rtb:
        L += [f"- {_scrub_markers(r if isinstance(r, str) else (r.get('value') if isinstance(r, dict) else r))}"
              for r in rtb]
    elif rtb:
        L.append(str(rtb))
    else:
        L.append(TBD)
    L.append("")

    dr = gv("desired_response")
    L.append("## Desired response")
    if isinstance(dr, dict):
        for k, lab in (("think", "Think"), ("feel", "Feel"), ("do", "Do")):
            if dr.get(k):
                L.append(f"- **{lab}:** {dr[k]}")
    elif dr:
        L.append(str(dr))
    else:
        L.append(TBD)
    L.append("")

    text_section("Tone & world", gv("tone_world_assets"))
    text_section("Budget & scope", gv("budget_scope"))
    text_section("Mandatories", gv("mandatories"))

    oqs = l2.get("open_questions") or []
    if oqs:
        L.append("## Open questions to resolve before research")
        seen = set()
        for q in oqs:
            txt = _scrub_markers(q if isinstance(q, str) else (q.get("question") or q.get("value") or ""))
            key = re.sub(r"[^a-z0-9]+", " ", txt.lower()).strip()   # dedupe near-identical questions
            if not key or key in seen:
                continue
            seen.add(key)
            pr = "" if isinstance(q, str) else (f"**[{q.get('priority')}]** " if q.get("priority") else "")
            L.append(f"- {pr}{txt}")
        L.append("")
    return "\n".join(L).strip() + "\n"


def render_markdown(brief):
    """Render review.md, the team-facing record of a run (not the client deliverable):
    the Loop 1 capture (FIELD_TITLES order, then any extra fields the LLM returned), the
    win-rules, the Loop 1 self-review with the no-loss ledger and its unmapped segments, the
    BetterBriefs scorecard when present, the Loop 2 slots, open questions and self-review,
    then the Loops 3–7 narrative and the generated-field RAG provenance when those ran.
    Returns the markdown text."""
    m, l1, l2 = brief["meta"], brief["loop1_capture"], brief["loop2_brief"]
    led = l1["no_loss_ledger"]
    title = m.get("project") or m.get("client") or "Client brief"
    L = [f"# Brief — {title}",
         f"_briefing tool v{m['parser_version']} · {m['parsed_at']} · "
         f"mode: {m['extraction_mode']}_\n"]

    L.append("## Loop 1 · Faithful capture  \n_IPA: background + objectives · no RAG_\n")
    f = l1["fields"]
    # Canonical fields first (ordered), then any extra LLM fields — never drop content.
    extra = [k for k in f if k not in FIELD_TITLES and k not in ("client", "project")]
    for key, tit in list(FIELD_TITLES.items()) + [(k, k.replace("_", " ").title()) for k in extra]:
        if key not in f:
            continue
        v = f[key]
        if isinstance(v, list):
            if v:
                L.append(f"**{tit}**\n")
                L += [f"- {_fmt(it)}" for it in v]; L.append("")
        else:
            L.append(f"**{tit}** — {_fmt(v)}\n")

    L.append("### Win-rules (what the brief reveals)\n")
    htw = l1["how_to_win"]
    titles = {"stated_evaluation_criteria": "How they'll judge us",
              "unstated_needs": "Unstated needs", "likely_landmines": "Landmines",
              "winning_themes": "Recurring themes", "proof_required": "Proof expected"}
    if any(htw.get(k) for k in titles):
        for k, t in titles.items():
            if htw.get(k):
                L.append(f"**{t}**\n")
                for it in htw[k]:
                    if not isinstance(it, dict):
                        L.append(f"- {it}"); continue
                    # LLM uses value/source_quote; heuristic uses point/evidence.
                    point = it.get("point") or it.get("value") or it.get("text") or ""
                    src = it.get("evidence") or it.get("source_quote")
                    ev = f"  \n  ↳ _{src}_" if src else ""
                    L.append(f"- {point}{ev}")
                L.append("")
    else:
        L.append("_Run with an LLM key for the full win-rules read._\n")

    r1 = l1["review"]
    L.append(f"### Loop 1 self-review — {'✅ pass' if r1['passed'] else '⚠️ needs a pass'}\n")
    L += [f"- {x}" for x in r1["flags"]] or ["- clean"]
    L.append(f"\n**No-loss ledger:** {led['coverage_pct']}% "
             f"({led['mapped_segments']}/{led['total_segments']} mapped)\n")
    if led["unmapped"]:
        L.append("_Review queue (nothing dropped silently):_\n")
        L += [f"- {u['segment']}" for u in led["unmapped"]]; L.append("")

    sc = brief.get("betterbriefs_scorecard")
    if sc:
        L.append("### BetterBriefs scorecard — quality of the client brief  \n"
                 f"_rubric: reference/betterbriefs · judge: {sc.get('mode')}_\n")
        L.append("| Dimension | Verdict | Evidence / fix |")
        L.append("|---|---|---|")
        for d in sc["dimensions"]:
            note = d["evidence"] + (f" → _{d['fix']}_" if d.get("fix") else "")
            L.append(f"| {d['dimension'].replace('_', ' ')} "
                     f"| {VERDICT_ICON.get(d['verdict'], '')} {d['verdict']} "
                     f"| {note.replace('|', '/').replace(chr(10), ' ')} |")
        L.append("")
        sm = sc.get("single_mindedness") or {}
        if sm.get("verdict") == "multiple":
            L.append("**⚠️ Not single-minded — one brief = one strategy. Split into:**\n")
            L += [f"- {s}" for s in sm.get("split_into", [])]; L.append("")
        if sc.get("summary"):
            L.append(f"_{sc['summary']}_\n")

    L.append("## Loop 2 · First-round agency brief  \n_IPA: objective + role_\n")
    for k, t in (("problem", "Problem"), ("objective", "Objective"),
                 ("audience", "Audience"), ("key_message", "Key message"),
                 ("evaluation_criteria", "Evaluation criteria"),
                 ("not_doing", "Not doing"), ("scope", "Scope")):
        if k in l2:                                  # old brief_objects lack new slots
            L.append(f"**{t}** — {_fmt(l2[k])}\n")
    L.append("### Open questions (ask before research)\n")
    if l2["open_questions"]:
        for q in l2["open_questions"]:
            if isinstance(q, str):                # LLM returns bare strings
                L.append(f"- {q}"); continue
            pr = f"**[{q.get('priority','')}]** " if q.get("priority") else ""
            why = q.get("why_it_matters") or q.get("why") or ""
            text = q.get("question") or q.get("value") or ""
            L.append(f"- {pr}{text}" + (f"  \n  _why: {why}_" if why else ""))
    else:
        L.append("_None._")
    r2 = l2["review"]
    L.append(f"\n### Loop 2 self-review — {'✅ pass' if r2['passed'] else '⚠️ gaps'}\n")
    L += [f"- {x}" for x in r2["flags"]] or ["- clean"]
    render_loops37(L, brief)                          # no-op unless Loops 3–7 ran
    render_golden_provenance(L, brief)                # per-field RAG citations (review-only)
    return "\n".join(L)


def render_golden_provenance(L, brief):
    """Surface, in review.md only, which RAG sources grounded each generated strategy
    field — the `evidence_ids` we stamp in fill_derivable_fields. Kept OUT of the
    client deliverable by design (a footnoted 'grounded in <case>' reads as harmful);
    this is where the provenance lives for a planner to audit or defend a route."""
    gf = (brief.get("loop2_golden") or {}).get("fields", {}) or {}
    gen = [(fid, f) for fid, f in gf.items()
           if isinstance(f, dict) and f.get("source") == "inferred" and f.get("method", "").startswith("gen:")]
    miss = [(fid, f) for fid, f in gf.items()
            if isinstance(f, dict) and f.get("source") == "missing" and f.get("reason")]
    prov = (brief.get("loop2_golden") or {}).get("provenance") or {}
    if not gen and not miss and not prov:
        return
    L.append("\n## Generated strategy — RAG provenance  \n"
             "_Review only; never rendered in the client brief._\n")
    if prov:
        # Which lines are the client's, which are our reading, which we wrote (Sai,
        # 2026-09-26: shown here and carried in the brief object, not on the client page).
        L.append("**Provenance of every field**\n")
        for fid, p in prov.items():
            conf = f" (confidence {p['confidence']:.2f})" if isinstance(p.get("confidence"), (int, float)) else ""
            L.append(f"- {fid.replace('_', ' ')}: {p['mark']}{conf}")
        L.append("")
    for fid, f in gen:
        label = fid.replace("_", " ")
        conf = f.get("confidence")
        cites = ", ".join(f.get("evidence_ids") or []) or "(no IPA cases cited — playbook-grounded)"
        L.append(f"**{label}** — _{f.get('method')}_, confidence {conf}")
        L.append(f"  \n  ↳ grounded in: {cites}")
        if f.get("rationale"):
            L.append(f"  \n  ↳ rationale: _{f['rationale']}_")
        if f.get("judge_note"):
            L.append(f"  \n  ↳ tournament: _{f['judge_note']}_")
        if f.get("alternatives"):
            L.append(f"  \n  ↳ runner-up: {json.dumps(f['alternatives'])[:200]}")
        L.append("")
    for fid, f in miss:
        L.append(f"**{fid.replace('_', ' ')}** — _missing_: {f.get('reason')}")
        L.append("")


# ---------------------------------------------------------------------------
# RICH OUTPUT (docx / pdf via pandoc)
# ---------------------------------------------------------------------------

# pdflatex/xelatex have no colour-emoji glyphs; map the few we emit to ASCII
# so the PDF renders cleanly. (docx keeps the originals — Word has the fonts.)
_PDF_GLYPHS = {"✅": "[PASS]", "⚠️": "[!]", "⚠": "[!]", "❌": "[FAIL]", "✗": "[FAIL]",
               "🟢": "[+]", "🟡": "[~]", "🔴": "[-]", "↳": ">", "•": "-", "→": "->"}


def _find_xelatex() -> str | None:
    """Return the path of the xelatex binary: from PATH, else the MacTeX default
    /Library/TeX/texbin/xelatex when it exists, else None."""
    return shutil.which("xelatex") or next(
        (p for p in ("/Library/TeX/texbin/xelatex",) if Path(p).exists()), None)


def write_rich_formats(md_text: str, md_file: Path, formats: list[str]) -> list[str]:
    """Emit docx/pdf alongside the markdown one-pager, via pandoc. Returns the
    formats actually written. Degrades with a clear message, never raises."""
    want = [f.strip().lower() for f in formats if f.strip() and f.strip().lower() != "md"]
    if not want:
        return []
    if not shutil.which("pandoc"):
        print("  ⚠️ pandoc not found — skipping docx/pdf  (brew install pandoc)")
        return []
    done = []
    if "docx" in want:
        out = md_file.with_suffix(".docx")
        r = subprocess.run(["pandoc", str(md_file), "-o", str(out)],
                           capture_output=True, text=True)
        if r.returncode == 0:
            done.append("docx")
        else:
            print(f"  ⚠️ docx failed: {r.stderr.strip()[:200]}")
    if "pdf" in want:
        xelatex = _find_xelatex()
        if not xelatex:
            print("  ⚠️ no xelatex engine — skipping pdf  (install BasicTeX/MacTeX)")
        else:
            clean = md_text
            for k, v in _PDF_GLYPHS.items():
                clean = clean.replace(k, v)
            # Stray backslashes (e.g. Windows paths like \ACME leaking from a brief)
            # are undefined LaTeX control sequences and abort the PDF. They're path
            # noise in a deliverable anyway — neutralise to forward slashes.
            clean = clean.replace("\\", "/")
            tmp = md_file.with_name(".brief_pdf_src.md")
            tmp.write_text(clean, encoding="utf-8")
            env = dict(os.environ)
            env["PATH"] = str(Path(xelatex).parent) + os.pathsep + env.get("PATH", "")
            out = md_file.with_suffix(".pdf")
            r = subprocess.run(
                ["pandoc", str(tmp), "-o", str(out),
                 "--pdf-engine=xelatex", "-V", "geometry:margin=2cm"],
                capture_output=True, text=True, env=env)
            tmp.unlink(missing_ok=True)
            if r.returncode == 0:
                done.append("pdf")
            else:
                print(f"  ⚠️ pdf failed: {r.stderr.strip()[:300]}")
    return done


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

# Golden-brief field -> the capture key the retrieval reads it as (queries and filters).
_GOLDEN_AS_CAPTURE = {"background": "background_context", "objectives": "objective",
                      "audience": "target_audience", "competitor_context": "competitors_market",
                      "tone_world_assets": "tone_and_brand", "mandatories": "mandatories",
                      "budget_scope": "budget"}


def _golden_text(v) -> str:
    """A golden value (text, list or objectives/tfd dict) as one line of text."""
    if isinstance(v, dict):
        return "; ".join(str(x) for x in v.values() if x)
    if isinstance(v, list):
        return "; ".join(str(x) for x in v if x)
    return str(v or "")


def _retrieval_fields_from_golden(gb: dict) -> dict:
    """The golden extraction as the capture-shaped fields loops_3_7 retrieves from, so
    retrieval can start when the golden extraction lands (~21 s) instead of waiting for
    the capture (~37 s). A client-stated SMP stands in for the key message. Measured
    2026-09-24 on 3 briefs: ~30% of evidence items change, a blind judge scored the two
    evidence sets 21 vs 20 (golden better on 2 of 3)."""
    g = (gb or {}).get("fields") or {}
    out = {cap: {"value": _golden_text(e.get("value")), "status": "fact"}
           for gid, cap in _GOLDEN_AS_CAPTURE.items()
           if isinstance(e := g.get(gid), dict) and e.get("source") != "missing" and _golden_text(e.get("value"))}
    smp = g.get("smp") if isinstance(g.get("smp"), dict) else {}
    if smp.get("source") == "client_stated" and _golden_text(smp.get("value")):
        out["key_message"] = {"value": _golden_text(smp["value"]), "status": "fact"}
    if "background_context" in out:
        out["business_problem"] = out["background_context"]
    return out


PROVENANCE_MARKS = {"client_stated": "client-stated", "generated": "proposed (written by the tool)",
                    "inferred": "our assumption, to confirm", "missing": "missing"}


def _mark_provenance(out: dict) -> None:
    """Label every golden field with where its value came from, for the reviewer and the
    lineage — NOT for the client page (Sai, 2026-09-26: the marks go to review.md and the
    brief object so they can ride into the CLAN context / lineage when the RAG module joins
    the rest of the system; the client brief stays clean). Writes
    loop2_golden.provenance = {field: {kind, mark, confidence}} where kind is client_stated,
    generated (source inferred, method gen:*), inferred (the extractor's reading) or
    missing; and, for an inferred value below the schema's confidence floor, appends an
    open question so a guess is confirmed before it is treated as a fact (audit H2/F4c).
    Idempotent: a question already present for the field is not added twice."""
    gf = (out.get("loop2_golden") or {}).get("fields") or {}
    try:
        floor = float(json.loads((HERE / "golden-brief" / "golden_brief.schema.json").read_text())
                      .get("confidence_floor") or 0.6)
    except Exception:  # noqa: BLE001 — a missing schema must not fail the run
        floor = 0.6
    prov, qs = {}, out["loop2_brief"].setdefault("open_questions", [])
    asked = {q.get("blocks_field") for q in qs if isinstance(q, dict)}
    for fid, e in gf.items():
        if not isinstance(e, dict):
            continue
        src = e.get("source")
        if src == "client_stated":
            kind = "client_stated"
        elif src == "inferred" and str(e.get("method") or "").startswith("gen:"):
            kind = "generated"
        elif src == "inferred":
            kind = "inferred"
        else:
            kind = "missing"
        conf = _conf(e.get("confidence"))
        prov[fid] = {"kind": kind, "mark": PROVENANCE_MARKS[kind], "confidence": conf}
        if kind == "inferred" and _golden_text(e.get("value")) and (conf is None or conf < floor) and fid not in asked:
            label = fid.replace("_", " ")
            qs.append({"question": f"Confirm the {label}: it is our assumption"
                                   + (f" at confidence {conf:.2f}" if conf is not None else " with no confidence reported")
                                   + ", not something the brief states.",
                       "why_it_matters": "an inferred value below the confidence floor must not be treated as a client fact",
                       "priority": "medium", "blocks_field": fid})
    out["loop2_golden"]["provenance"] = prov


def _warm_validator() -> None:
    """Warm the RAG validator chain (brief_context.warm_validator) while the capture runs,
    so the five per-field validations do not each pay jev's cold start (audit JL-1). A
    no-op when validation is off; never raises into the run."""
    try:
        _load_retriever()
        import brief_context
        warmed = brief_context.warm_validator()
        if warmed:
            print(f"[i] validator warm-up: {warmed}", file=sys.stderr)
    except Exception as e:      # noqa: BLE001
        print(f"[i] validator warm-up skipped ({e.__class__.__name__}: {e})", file=sys.stderr)


def _require_claude(provider: str) -> None:
    """Raise NoClaudeAvailable when the lead provider is anthropic, non-Claude links are not
    allowed (BRIEF_ALLOW_NONCLAUDE unset) and the chosen transport has no way to reach Claude:
    `api` needs ANTHROPIC_API_KEY, `cli` needs the `claude` CLI, `auto` needs either. A
    no-op for any other provider. Checked once at the start of run()."""
    if provider != "anthropic" or _allow_nonclaude():
        return
    t = _claude_transport()
    have_key, have_cli = bool(os.environ.get("ANTHROPIC_API_KEY")), bool(shutil.which("claude"))
    if (t == "api" and have_key) or (t == "cli" and have_cli) or (t == "auto" and (have_key or have_cli)):
        return
    raise NoClaudeAvailable(
        f"no Claude available on transport '{t}': "
        + ("set ANTHROPIC_API_KEY" if t == "api" else
           "install and log in to Claude Code (`claude`)" if t == "cli" else
           "set ANTHROPIC_API_KEY or install Claude Code")
        + ", or BRIEF_CLAUDE_TRANSPORT=cli|auto, or BRIEF_ALLOW_NONCLAUDE=1 to let a non-Claude "
          "link write the brief (not for production).")


class _Inline:
    """A stand-in for ThreadPoolExecutor that runs each submit() at once (BRIEF_PARALLEL=0):
    the same code path, the same futures, one step at a time."""

    def __enter__(self):
        """Nothing to start."""
        return self

    def __exit__(self, *exc):
        """Nothing to join."""
        return False

    def submit(self, fn, *a, **k):
        """Run fn now; return a completed Future holding its result or its exception."""
        from concurrent.futures import Future
        f = Future()
        try:
            f.set_result(fn(*a, **k))
        except Exception as e:
            f.set_exception(e)
        return f


def run(path: Path | None, client=None, project=None, loops37=False, golden=False,
        raw_text: str | None = None, source_name: str | None = None) -> dict:
    """Run the briefing pipeline on one brief and return the brief object (main() writes it
    to brief_object.json). Resets the LLM call ledger first.

    Parameters:
      path         the brief file to ingest; not read when raw_text is given.
      client       client label, copied into meta.
      project      project label, copied into meta.
      loops37      also run Loops 3–7 (retrieval) and, when they ran and loop2_golden
                   exists, the zone-3 generation (fill_derivable_fields).
      golden       run the schema-grounded extraction (extract_golden_brief) -> loop2_golden.
      raw_text     brief text already in hand (pasted email, stdin, --text, or a file with
                   --attach context folded in); skips ingest().
      source_name  source label for meta.source_files and the ledger; defaults to
                   'pasted-input' with raw_text, else path.name.

    Stage graph (a 4-worker thread pool; the arrows are waits):
      capture_toon ∥ how_to_win_toon ∥ extract_golden_brief (golden only), all on the raw
      brief -> shape_loop2 + review_loop2, build_ledger + review_loop1 on the capture ->
      score_betterbriefs (in the pool) ∥ loops_3_7(synthesize=False) (this thread) ->
      fill_derivable_fields ∥ _synthesize_loops37 (only when synthesis was deferred).
      Generation open questions are appended to loop2_brief.open_questions.

    Env switches:
      BRIEF_CAPTURE=json   skip the TOON capture calls and use extract_llm's JSON capture.
      BRIEF_PARALLEL=0     run the same steps one at a time (_Inline); the fill reads it too.
      BRIEF_ALLOW_NONCLAUDE=1  let non-Claude links stay in a Claude-led chain (off by
                           default: without it, and with no route to Claude, run() raises
                           NoClaudeAvailable before any call).

    Stage graph, in order of what waits for what: the scorecard, capture, how-to-win and
    golden extraction all start at once on the raw text; retrieval starts when the golden
    extraction lands; the strategy fill starts when retrieval lands; the loop synthesis
    runs alongside the fill; the capture, how-to-win and scorecard are collected last.

    Capture fallbacks: TOON capture (how_to_win from its own call) -> JSON extract_llm
    (how_to_win from its reply; a finished how_to_win_toon result is discarded) ->
    extract_heuristic (no how_to_win, no LLM open questions, extraction_mode 'heuristic').
    meta.capture_format records which one ran.

    Returns a dict with:
      meta                    client, project, source_files, parsed_at, parser_version,
                              extraction_mode, capture_format, prompt_version, and llm_stats
                              (the call ledger plus wall_seconds).
      loop1_capture           fields, how_to_win, no_loss_ledger, review.
      loop2_brief             the shaped brief, its open_questions and review.
      betterbriefs_scorecard  the BetterBriefs judge (or its heuristic fallback).
      loops3_7                only when loops37 is set.
      loop2_golden            only when the golden extraction returned a result."""
    _stats_reset()
    _t_run0 = dt.datetime.now()
    schema = json.loads((HERE / "brief_object.schema.json").read_text())
    if raw_text is not None:                       # pasted email / stdin / --text
        text = raw_text
        src_name = source_name or "pasted-input"
    else:
        text, _mime = ingest(path)
        src_name = path.name
    segs = segment(text)

    provider = resolve_provider()
    _require_claude(provider)
    # Stages run as a dependency graph, not a queue (measured 2026-09-23: every stage
    # waited for the one before it, 316 s per brief). Three reads of the raw brief start
    # together with the scorecard (text only); retrieval starts when the golden extraction
    # lands; the strategy fill waits ONLY for the golden extraction and retrieval, which is
    # all it reads (audit CC1: it used to wait for the capture and how-to-win too, 15.6 s
    # idle per brief on Opus 4.6); the loop synthesis feeds review notes only, so it runs
    # alongside. BRIEF_PARALLEL=0 runs the same steps one at a time.
    from concurrent.futures import ThreadPoolExecutor
    parallel = os.environ.get("BRIEF_PARALLEL", "1").lower() not in ("0", "false", "no")
    toon = os.environ.get("BRIEF_CAPTURE", "toon").lower() != "json"
    # BRIEF_RETRIEVE_FROM=golden (default since 2026-09-24): retrieval reads the golden
    # extraction and starts as soon as it lands, alongside the capture; "capture" waits for
    # the capture. A/B on 3 real briefs, Sonnet-judged: health 216 vs 216 in total (per
    # brief 64/76/76 vs 75/63/78 — within run-to-run swing), 16-26 s faster per brief.
    from_golden = (os.environ.get("BRIEF_RETRIEVE_FROM", "golden").lower() == "golden"
                   and loops37 and golden)
    golden_schema = (json.loads((HERE / "golden-brief" / "golden_brief.schema.json").read_text())
                     if loops37 else None)
    with _stats_scope() as ledger, (ThreadPoolExecutor(max_workers=8) if parallel else _Inline()) as ex:
        f_score = ex.submit(_scoped(score_betterbriefs), text, None)     # text only: t=0
        if loops37:
            ex.submit(_warm_validator)               # pay the validator's cold start now
        f_cap = ex.submit(_scoped(capture_toon), segs) if toon else None
        f_htw = ex.submit(_scoped(how_to_win_toon), segs) if toon else None
        f_gold = ex.submit(_scoped(extract_golden_brief), text) if golden else None
        f_l37 = f_fill = None
        if from_golden:
            def _retrieve_from_golden():
                """Wait for the golden extraction, then retrieve from it (None if it failed).
                Never raises into the run: a failure here means retrieval from the capture."""
                try:
                    gb0 = f_gold.result()
                    rf = _retrieval_fields_from_golden(gb0) if gb0 else {}
                    return loops_3_7({}, rf, synthesize=False) if rf else None
                except Exception as e:
                    print(f"[!] retrieval from the golden extraction failed "
                          f"({e.__class__.__name__}: {e}); retrying from the capture.", file=sys.stderr)
                    return None
            f_l37 = ex.submit(_scoped(_retrieve_from_golden))

            def _fill_early():
                """The strategy fill as soon as the golden extraction and retrieval land.
                None when either is missing (run() then takes the sequential path)."""
                gb0, l37_0 = f_gold.result(), f_l37.result()
                if not (gb0 and l37_0 and l37_0.get("enabled")):
                    return None
                return fill_derivable_fields(gb0.setdefault("fields", {}), l37_0, golden_schema,
                                             brief_text=text)
            f_fill = ex.submit(_scoped(_fill_early))

        llm = f_cap.result() if f_cap else None
        capture_format = "toon" if llm else "json"
        if llm:
            how_to_win = f_htw.result()
        else:                                          # BRIEF_CAPTURE=json, or TOON failed
            llm = extract_llm(text, schema)
            how_to_win = (llm or {}).get("how_to_win", {})
        if llm:
            fields = llm.get("fields", {})
            llm_oqs = llm.get("open_questions", []); used = None
            mode = f"{provider}:{model_for(provider)}"
        else:
            fields, used = extract_heuristic(segs); how_to_win = {}; llm_oqs = []
            mode = "heuristic"; capture_format = "heuristic"

        loop2 = shape_loop2(fields, llm_oqs)
        loop2["review"] = review_loop2(loop2)

        ledger_l1 = build_ledger(segs, used, fields, src_name, how_to_win, loop2["open_questions"])
        loop1 = {"fields": fields, "how_to_win": how_to_win, "no_loss_ledger": ledger_l1}
        loop1["review"] = review_loop1(ledger_l1, fields)

        out = {
            "meta": {"client": client, "project": project, "source_files": [src_name],
                     "parsed_at": dt.datetime.now().isoformat(timespec="seconds"),
                     "parser_version": PARSER_VERSION, "extraction_mode": mode,
                     "capture_format": capture_format, "prompt_version": PROMPT_VERSION},
            "loop1_capture": loop1, "loop2_brief": loop2,
            "betterbriefs_scorecard": None,            # filled when its call returns
        }
        # Loops 3–7 (RAG) only when explicitly enabled — key is omitted otherwise, so
        # output is byte-for-byte identical to a Loops 1–2 run. Retrieval never raises
        # into the run: a failure is a disabled stub with its reason.
        if loops37:
            l37_early = f_l37.result() if f_l37 else None
            if l37_early is None:
                try:
                    l37_early_or_cap = loops_3_7(loop2, fields, synthesize=False)
                except Exception as e:
                    print(f"[!] retrieval failed ({e.__class__.__name__}: {e}); Loops 3-7 skipped.",
                          file=sys.stderr)
                    l37_early_or_cap = {"enabled": False,
                                        "reason": f"retrieval failed: {e.__class__.__name__}: {e}"}
                out["loops3_7"] = l37_early_or_cap
            else:
                out["loops3_7"] = l37_early
            out["loops3_7"]["retrieved_from"] = "golden" if l37_early else "capture"
        l37 = out.get("loops3_7") or {}
        f_synth = (ex.submit(_scoped(_synthesize_loops37), l37["gist"], l37["intent"], l37["loops"])
                   if l37.get("synthesis_mode") == "deferred" else None)
        gb = f_gold.result() if f_gold else None
        if gb:
            out["loop2_golden"] = gb

        # Loop 4+5: fill insight + desired_response from IPA precedents + playbooks.
        # Only runs when loops37 ran successfully AND golden extraction produced fields.
        if loops37 and l37.get("enabled") and out.get("loop2_golden"):
            filled = f_fill.result() if f_fill else None
            if filled is None:
                gf = out["loop2_golden"].setdefault("fields", {})
                # Generates insight/smp/rtb/desired_response from the brief + retrieved IPA
                # precedent, schema-driven and rubric-gated. Mutates gf in place; never
                # overwrites a client_stated field. Failures become open questions.
                filled = fill_derivable_fields(gf, l37, golden_schema, brief_text=text)
            _fills, gen_open_qs = filled
            if gen_open_qs:
                out["loop2_golden"]["generation_open_questions"] = gen_open_qs
                out["loop2_brief"].setdefault("open_questions", []).extend(gen_open_qs)
        if out.get("loop2_golden"):
            _mark_provenance(out)
        if f_synth:
            l37["synthesis_mode"] = f_synth.result()
        sc = f_score.result()
        if isinstance(sc, dict) and sc.get("mode") == "heuristic":
            sc = scorecard_heuristic(fields)           # the t=0 call had no capture to read
        out["betterbriefs_scorecard"] = sc
        # Snapshot the LLM call ledger (a deep copy: the critic call that follows run()
        # must not change a finished brief's numbers) so optimisation work is measured per run.
        stats = _stats_snapshot()
    stats["wall_seconds"] = round((dt.datetime.now() - _t_run0).total_seconds(), 1)
    out["meta"]["llm_stats"] = stats
    # The brief's label is the link that ANSWERED most of its calls, not the configured
    # default (audit F12: every Opus 5.5 run was labelled claude-opus-4-6), and the chain
    # it walked is kept beside it. A non-Claude link that answered is named in
    # meta.fallback_links so the brief is never mistaken for a Claude brief.
    answered = stats.get("answered_by") or {}
    if answered and mode != "heuristic":
        out["meta"]["extraction_mode"] = max(answered, key=answered.get)
    out["meta"]["model_chain"] = [f"{p}:{m}" for p, m in _model_chain()] if provider else []
    fallback_links = sorted(l for l in answered if not l.startswith("anthropic:"))
    if provider == "anthropic" and fallback_links:
        out["meta"]["fallback_links"] = fallback_links
    # Which transport the Claude links ran on (api, cli, or api→cli after an `auto` switch),
    # so a brief made on the Claude Code login is never mistaken for an API run.
    out["meta"]["claude_transport"] = transport_used()
    return out


def main():
    """CLI entry point. Parses the arguments, resolves the input (inline --text, '-' or piped
    stdin, or a file path; --attach files are ingested and appended as supporting context)
    and calls run(). Loops 3–7 run with --loops37 or BRIEF_LOOPS37; the golden extraction
    runs with --golden, BRIEF_GOLDEN, or whenever Loops 3–7 run. --provider and --model set
    BRIEF_PROVIDER and BRIEF_MODEL; --check lists the provider's models and exits.
    Writes brief_object.json, client_brief.md (the deliverable), review.md and any --format
    docx/pdf into --out (default outputs/<slug of --project or the source name>), then
    prints a run summary. Exits with a message when the input or an attachment is missing."""
    ap = argparse.ArgumentParser(description="Briefing tool MVP — Loops 1 & 2")
    ap.add_argument("brief", nargs="?", default=None,
                    help="path to brief (.txt/.md/.docx/.pdf/.eml or an image .png/.jpg), "
                         "or '-' to read pasted text from stdin")
    ap.add_argument("--text", default=None,
                    help="brief text inline (e.g. a copy-pasted email) instead of a file")
    ap.add_argument("--attach", action="append", default=[], metavar="FILE",
                    help="supplementary file(s) folded in as context (e.g. brand guidelines); "
                         "any supported type incl. images/PDF. Repeatable.")
    ap.add_argument("--format", default="md",
                    help="output formats, comma-separated: md,docx,pdf (default: md). "
                         "JSON is always written.")
    ap.add_argument("--out", default=None, help="output dir (default: outputs/<name>)")
    ap.add_argument("--client")
    ap.add_argument("--project")
    ap.add_argument("--provider", help="nim | openai | ollama | anthropic (else auto-detect)")
    ap.add_argument("--model", help="override model id (e.g. nvidia/llama-3.1-nemotron-70b-instruct)")
    ap.add_argument("--check", action="store_true",
                    help="connectivity check: list available models (esp. Nemotron) and exit")
    ap.add_argument("--loops37", action="store_true",
                    help="also run Loops 3–7 (RAG-grounded strategy from rag/index). "
                         "Off by default; needs a built index (cd rag && ./build_rag.sh).")
    ap.add_argument("--golden", action="store_true",
                    help="run schema-grounded Golden Brief extraction pass (loop2_golden).")
    args = ap.parse_args()
    loops37 = args.loops37 or os.environ.get("BRIEF_LOOPS37", "").lower() in ("1", "true", "yes")
    # golden always runs when loops37 is on — insight fill (Loop 4) depends on it
    golden = args.golden or loops37 or os.environ.get("BRIEF_GOLDEN", "").lower() in ("1", "true", "yes")

    if args.provider:
        os.environ["BRIEF_PROVIDER"] = args.provider
    if args.model:
        os.environ["BRIEF_MODEL"] = args.model

    if args.check:
        prov = resolve_provider() or "nim"
        try:
            ids = list_models(prov)
            nem = [m for m in ids if "nemotron" in m.lower()]
            print(f"✓ reachable via '{prov}'. {len(ids)} models. Nemotron ids:")
            print("\n".join(f"  - {m}" for m in nem[:20]) or "  (none found)")
        except Exception as e:
            print(f"✗ can't reach '{prov}': {e.__class__.__name__}: {e}")
        return

    # Resolve the input: inline --text, '-'/piped stdin (pasted email), or a file.
    path = None
    raw_text = None
    source_name = None
    if args.text is not None:
        raw_text = ingest_email_text(args.text)
        source_name = "pasted-email"
    elif args.brief == "-" or (args.brief is None and not sys.stdin.isatty()):
        raw_text = ingest_email_text(sys.stdin.read())
        source_name = "pasted-email"
        if not raw_text.strip():
            sys.exit("No input on stdin. Paste the email then Ctrl-D, or pass a file / --text.")
    elif args.brief:
        path = Path(args.brief).expanduser().resolve()
        if not path.exists():
            sys.exit(f"File not found: {path}")
    else:
        sys.exit("Give a brief: a file path, '-' for stdin, or --text \"...\".")

    # Fold any --attach files (brand guidelines, etc.) into the brief as context.
    if args.attach:
        if raw_text is None:                      # ingest the primary file here so we can append
            raw_text, _ = ingest(path)
            source_name = source_name or path.name
            path = None                           # raw_text now carries it; don't re-ingest in run()
        for att in args.attach:
            ap_path = Path(att).expanduser().resolve()
            if not ap_path.exists():
                sys.exit(f"Attachment not found: {ap_path}")
            print(f"[i] attaching context: {ap_path.name}", file=sys.stderr)
            atext, _ = ingest(ap_path)
            raw_text += (f"\n\n===== ATTACHMENT: {ap_path.name} "
                         f"(supporting context — e.g. brand guidelines) =====\n{atext}")

    try:
        brief = run(path, args.client, args.project, loops37=loops37, golden=golden,
                    raw_text=raw_text, source_name=source_name)
    except NoClaudeAvailable as e:
        sys.exit(f"[!] {e}")

    stem = source_name or (path.stem if path else "brief")
    name = re.sub(r"[^a-z0-9]+", "-", (args.project or stem).lower()).strip("-")
    out_dir = Path(args.out).resolve() if args.out else (HERE / "outputs" / name)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "brief_object.json").write_text(json.dumps(brief, indent=2), encoding="utf-8")
    # The deliverable: only the final brief. PDF/DOCX derive from this.
    md_text = render_client_brief(brief)
    md_file = out_dir / "client_brief.md"
    md_file.write_text(md_text, encoding="utf-8")
    # The machinery (capture, ledger, reviews, scorecard, loop narratives) — for the team, not the client.
    (out_dir / "review.md").write_text(render_markdown(brief), encoding="utf-8")
    rich = write_rich_formats(md_text, md_file, args.format.split(","))

    led = brief["loop1_capture"]["no_loss_ledger"]
    print(f"✓ {source_name or path.name}  [mode: {brief['meta']['extraction_mode']}]")
    print(f"  Loop 1 no-loss: {led['coverage_pct']}% "
          f"({led['mapped_segments']}/{led['total_segments']})  "
          f"review: {'pass' if brief['loop1_capture']['review']['passed'] else 'needs pass'}")
    print(f"  Loop 2 open questions: {len(brief['loop2_brief']['open_questions'])}  "
          f"review: {'pass' if brief['loop2_brief']['review']['passed'] else 'gaps'}")
    sc = brief["betterbriefs_scorecard"]
    verdicts = [d["verdict"] for d in sc["dimensions"]]
    sm = sc["single_mindedness"]
    print(f"  BetterBriefs scorecard ({sc['mode']}): "
          f"{verdicts.count('pass')} pass / {verdicts.count('vague')} vague / "
          f"{verdicts.count('missing')} missing"
          + (f"  ⚠️ split into {len(sm['split_into'])} briefs"
             if sm["verdict"] == "multiple" else ""))
    s37 = brief.get("loops3_7")
    if s37:
        if s37.get("enabled"):
            print(f"  Loops 3–7 ({s37['synthesis_mode']}): intent={s37['intent']}, "
                  f"{len(s37['sources_used'])} playbook sections cited")
        else:
            print(f"  Loops 3–7: skipped — {s37.get('reason', '')}")
    if brief.get("loop2_golden"):
        gf = brief["loop2_golden"].get("fields", {})
        filled = sum(1 for v in gf.values() if isinstance(v, dict) and v.get("source") != "missing")
        loop4_ran = "insight" in gf and (gf["insight"] or {}).get("method") == "loop4_fill"
        loop5_ran = "desired_response" in gf and (gf.get("desired_response") or {}).get("method") == "loop5_fill"
        print(f"  Golden Brief: {filled}/{len(gf)} fields filled"
              + (" · insight filled (loop4)" if loop4_ran else "")
              + (" · desired_response filled (loop5)" if loop5_ran else ""))
    formats_written = ["json", "md"] + rich
    print(f"  Output -> {out_dir}  [{', '.join(formats_written)}]")
    ls = brief["meta"].get("llm_stats") or {}
    if ls.get("calls"):
        tok = (f"{ls['prompt_tokens']}+{ls['completion_tokens']} tok"
               if ls.get("prompt_tokens") else f"{ls['input_chars']}+{ls['output_chars']} chars")
        print(f"  LLM: {ls.get('logical_calls', '?')} calls ({ls['calls']} link attempts) · {tok} · "
              f"retries {ls['retries']} · rate-limited {ls['rate_limited']} · {ls.get('wall_seconds', '?')}s")


if __name__ == "__main__":
    main()
