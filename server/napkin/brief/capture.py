"""The extract stage (middleware-api.md §10.3): transcription, Loop-1 no-loss
capture, the working brief's captured fields, the BetterBriefs scorecard.

This module is given a `CaptureView` — the model and the job, nothing else. It
cannot reach retrieval or research: Loop-1 capture is RAG-free (the engine's
one hard rule), because the no-loss ledger measures fidelity to the client's
own words and injected text craters it.

Ported from engine/parse_brief.py: `EXTRACTION_SYSTEM`, `SCORECARD_SYSTEM`,
`_VISION_PROMPT`, `segment`, `build_ledger`, `CORE_FIELDS`; the working brief
from engine/agent-server/mapping.py `map_brief` (without the golden fill: a
Loop-2 client fact is never an insight or an SMP).
"""

from __future__ import annotations

import base64
import hashlib
import re

from .. import backend
from ..rules.quotes import contains_word, find_quote
from ..util import bad

LOOP1_KEYS = ["background_context", "business_problem", "objective", "target_audience", "deliverables",
              "mandatories", "budget", "timeline", "success_metrics", "key_message", "proof_points",
              "evaluation_criteria", "strategic_angle", "anti_target", "competitors_market", "tone_and_brand",
              "decision_makers", "constraints"]
HOW_TO_WIN = ["stated_evaluation_criteria", "unstated_needs", "likely_landmines", "winning_themes", "proof_required"]
SCORECARD_DIMENSIONS = ["objectives_quality", "audience_vividness", "single_minded_message", "evaluation_criteria",
                        "budget_interlock", "strategic_clarity", "language"]
OBJECTIVE_TYPES = ["commercial", "behavioural", "attitudinal"]
IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")
MAX_IMAGE = 5 * 1024 * 1024
MAX_CHARS = 60000

TRANSCRIBE_SYSTEM = (
    "Transcribe this document image into clean, faithful text for an advertising-brief pipeline.\n"
    "Rules: (1) Capture ALL text verbatim — headings, body, bullets, labels, captions, table cells, prices, "
    "names, figures. Lose nothing. (2) Preserve reading order and structure (use markdown headings / bullets / "
    "tables to mirror the layout). (3) For a meaningful non-text visual (a chart, an org diagram, a product photo "
    "with a caption), add a short note of what it shows to `visuals`. (4) Do NOT summarise, interpret, or "
    "invent — transcribe only. Put the transcription in `text`.")

EXTRACTION_SYSTEM = """You are Loop 1 of an ad-agency briefing system, the Client Brief Parser. Convert a messy
client brief (the materials, each with a material_id) into a faithful structured capture.

`items` uses ONLY these keys (never invent a key):
  background_context  - the situation/context behind the brief
  business_problem    - the core problem/challenge to solve (ALWAYS capture this if stated)
  objective           - what the work must achieve (a handful at most; each its own item)
  target_audience     - who we are talking to
  deliverables        - what we must produce
  mandatories         - non-negotiables: legal, brand, naming, claims
  budget              - money
  timeline            - dates/deadlines
  success_metrics     - KPIs / how success is measured
  key_message         - the ONE single-minded message the client wants to land
  proof_points        - evidence/claims supporting the key message
  evaluation_criteria - how the client says ideas/work will be judged
  strategic_angle     - any strategic direction/approach the client suggests
  anti_target         - who the brand is explicitly NOT for / NOT targeting
  competitors_market  - competitors and market context
  tone_and_brand      - tone of voice, brand heritage, style
  decision_makers     - who decides / who to win
  constraints         - other limits
If something does not fit a key, attach it to the CLOSEST key. List-like keys take one item per entry.

Each item: {key, value, status, quote, material_id, objective_type}.
- status: "fact" (the material states it) or "assumption" (you inferred it). Leave out what is not provided.
- EVERY fact MUST carry a quote copied character for character from its material, and that material's id.
- objective items carry objective_type: "commercial" | "behavioural" | "attitudinal" (the three should link:
  attitude shift -> behaviour change -> commercial outcome); other items null.
- LOSE NOTHING: every concrete sentence in the materials must be reflected in some item's value or quote.

how_to_win holds ONLY what the materials reveal (stated_evaluation_criteria, unstated_needs, likely_landmines,
winning_themes, proof_required) as {kind, point, evidence, material_id}; evidence is a verbatim quote. Do NOT
invent strategy.

open_questions = what the materials FAIL to answer, each {question, why_it_matters, priority}. Do NOT ask about
anything the materials already state.

client: the advertiser (client company) ONLY if the materials name it, with the verbatim quote that names it;
otherwise null. project_name: ONLY if the materials name the project or campaign, with its quote; otherwise
null. Never invent either."""

SCORECARD_SYSTEM = """You are a brief-quality judge applying the BetterBriefs rubric (the global study on
briefing) to a CLIENT brief. Judge ONLY what the materials say — quote evidence verbatim (with its material_id),
do not invent.

Score exactly these dimensions, each {dimension, verdict: pass|vague|missing, evidence, material_id, fix}:
  objectives_quality    - a handful at most, benchmarked + time-stamped, the commercial/behavioural/attitudinal
                          chain linked, not wishful, clear hierarchy (not a shopping list)
  audience_vividness    - a vivid picture (demographics + psychographics + needs); FLAG demographic cliches
                          ("millennials", "everyone", bare age ranges) as vague; states who it is NOT for
  single_minded_message - ONE key message, supported by relevant proof points
  evaluation_criteria   - how the work will be judged is stated
  budget_interlock      - budget, objectives and audience are mutually feasible (flag mass-market ambitions on
                          small money)
  strategic_clarity     - a clear strategic angle/choice, including what NOT to do
  language              - simple, jargon-free, succinct; no category-speak
evidence is a verbatim quote from a material (null when the verdict is missing); fix is one line, null on pass.

Also judge the single-mindedness of the WHOLE brief: one brief = one strategy. If it bundles mutually exclusive
strategies or several separate jobs, verdict "multiple" and say how to split it (one line per brief).
summary: one sentence on overall brief quality."""

# core field -> (fallbacks that also satisfy it, why it matters, priority) — engine CORE_FIELDS
CORE_FIELDS = {
    "business_problem": ([], "We can't position the work without the real problem.", "blocker"),
    "objective": (["success_metrics"], "Objectives are the most critical yet most poorly defined element of a "
                  "brief — 61% of marketers and 71% of agencies rank them #1 (BetterBriefs).", "blocker"),
    "target_audience": ([], "65% of agencies can't picture the target from the briefs they get; if we can't "
                        "picture them, neither can creatives (BetterBriefs).", "blocker"),
    "key_message": ([], "A good brief lands ONE single-minded message backed by proof points — not a shopping "
                    "list (BetterBriefs).", "important"),
    "evaluation_criteria": (["success_metrics"], "Only 30% of clients define how work will be judged, and 88% of "
                            "agencies are unclear on it — agreeing criteria upfront prevents subjective rounds of "
                            "rework (BetterBriefs).", "important"),
    "budget": ([], "Budget, objectives and audience must interlock — scope and ambition depend on the money "
               "(BetterBriefs).", "important"),
    "timeline": ([], "Drives feasibility and the pitch date.", "important"),
    "success_metrics": (["objective"], "Objectives need benchmarks and a time stamp; Loop 7 (IPA QA) can't score "
                        "without KPIs.", "important"),
    "mandatories": ([], "Missing mandatories = legal/brand risk downstream.", "important"),
    "decision_makers": ([], "We win the room by knowing who decides — 62% of marketers vs 43% of agencies say the "
                        "right people sign off (BetterBriefs).", "important"),
}
EVAL_CRITERIA_QUESTION = (
    "How will the work be evaluated? Can we agree criteria that (1) connect to real-world business outcomes, "
    "(2) give oxygen to the creative idea rather than reduce its impact, and (3) indicate whether the work builds "
    "mental availability or fame?")


def _obj(props: dict) -> dict:
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


_S = {"type": "string"}
_NS = {"anyOf": [{"type": "string"}, {"type": "null"}]}
_NAMED = {"anyOf": [_obj({"value": _S, "quote": _S, "material_id": _S}), {"type": "null"}]}

TRANSCRIBE_SCHEMA = _obj({"text": _S, "visuals": {"type": "array", "items": _S}})
CAPTURE_SCHEMA = _obj({
    "items": {"type": "array", "items": _obj({
        "key": {"type": "string", "enum": LOOP1_KEYS}, "value": _S,
        "status": {"type": "string", "enum": ["fact", "assumption"]}, "quote": _NS, "material_id": _S,
        "objective_type": {"anyOf": [{"type": "string", "enum": OBJECTIVE_TYPES}, {"type": "null"}]}})},
    "how_to_win": {"type": "array", "items": _obj({"kind": {"type": "string", "enum": HOW_TO_WIN}, "point": _S,
                                                     "evidence": _S, "material_id": _S})},
    "open_questions": {"type": "array", "items": _obj({
        "question": _S, "why_it_matters": _S,
        "priority": {"type": "string", "enum": ["blocker", "important", "nice_to_have"]}})},
    "client": _NAMED, "project_name": _NAMED,
})
SCORECARD_SCHEMA = _obj({
    "dimensions": {"type": "array", "items": _obj({
        "dimension": {"type": "string", "enum": SCORECARD_DIMENSIONS},
        "verdict": {"type": "string", "enum": ["pass", "vague", "missing"]},
        "evidence": _NS, "material_id": _NS, "fix": _NS})},
    "single_mindedness": _obj({"verdict": {"type": "string", "enum": ["single", "multiple"]},
                               "split_into": {"type": "array", "items": _S}}),
    "summary": _S,
})


# ---------------------------------------------------------------------------
# Materials
# ---------------------------------------------------------------------------

def norm_sha(s) -> str:
    s = str(s or "").strip().lower()
    return s[7:] if s.startswith("sha256:") else s


def material_id(sha_hex: str) -> str:
    return "mat_" + sha_hex[:16]


class Material:
    def __init__(self, mid, kind, name, sha, text=None, media_type=None, asset=None, image=None, known=False):
        self.id, self.kind, self.name, self.sha = mid, kind, name, sha
        self.text, self.media_type, self.asset, self.image, self.known = text, media_type, asset, image, known
        self.transcribed = None  # {model, backend} when its text is a vision transcription

    @property
    def readable(self) -> bool:
        return isinstance(self.text, str) and bool(self.text.strip())

    def entry(self, received_at: str) -> dict:
        e = {"kind": self.kind, "name": self.name, "sha256": self.sha, "received_at": received_at,
             "licence": "client-confidential"}
        if self.media_type:
            e["media_type"] = self.media_type
        if self.asset:
            e["asset"] = self.asset
        if not self.readable:
            e["unread"] = True
        if self.transcribed:
            e["transcribed"] = dict(self.transcribed)
        return e


def _kind(name: str, media_type: str | None, is_image: bool) -> str:
    n, mt = (name or "").lower(), (media_type or "").lower()
    if is_image or mt.startswith("image/"):
        return "image"
    if n.endswith((".eml", ".msg")) or mt == "message/rfc822":
        return "email"
    if n.endswith((".ppt", ".pptx", ".key")) or "presentation" in mt:
        return "deck"
    if "brief" in n:
        return "client_brief"
    return "other"


def build_materials(inp: dict, data: dict) -> list[Material]:
    """Validate `draft_brief`'s input (§10.1) into materials. Raises 400."""
    known = data.get("materials") if isinstance(data.get("materials"), dict) else {}
    mats: list[Material] = []
    prompt = inp.get("prompt")
    if prompt is not None and not isinstance(prompt, str):
        raise bad("input.prompt must be a string")
    if prompt and prompt.strip():
        sha = hashlib.sha256(prompt.encode()).hexdigest()
        mid = material_id(sha)
        mats.append(Material(mid, "prompt", "prompt", sha, text=prompt, media_type="text/plain", known=mid in known))
    atts = inp.get("attachments", [])
    if not isinstance(atts, list):
        raise bad("input.attachments must be a list")
    for a in atts:
        if not isinstance(a, dict) or not isinstance(a.get("name"), str) or not a.get("sha256"):
            raise bad("each attachment needs a name and a sha256")
        sha = norm_sha(a["sha256"])
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise bad("an attachment has a malformed sha256")
        image = a.get("image")
        if image is not None:
            if not isinstance(image, dict) or image.get("media_type") not in IMAGE_TYPES \
                    or not isinstance(image.get("data"), str):
                raise bad("an image attachment must be png, jpeg, gif or webp, base64")
            try:
                raw = base64.b64decode(image["data"], validate=True)
            except (ValueError, TypeError):
                raise bad("an image attachment is not valid base64") from None
            if len(raw) > MAX_IMAGE:
                raise bad("an image attachment is over 5 MB")
        text = a.get("text") if isinstance(a.get("text"), str) and a["text"].strip() else None
        mt = a.get("media_type") if isinstance(a.get("media_type"), str) else (image or {}).get("media_type")
        mid = material_id(sha)
        if any(m.id == mid for m in mats):
            continue
        mats.append(Material(mid, _kind(a["name"], mt, image is not None and not text), a["name"], sha, text=text,
                             media_type=mt, asset=a.get("asset") if isinstance(a.get("asset"), str) else None,
                             image=image if not text else None, known=mid in known))
    if not any(m.readable or m.image for m in mats):
        raise bad("nothing to read: no prompt, no attachment text and no image")
    return mats


def transcribe(view, mats: list[Material]) -> list[str]:
    """Each picture becomes its material's text (§1.6). Returns notes on any
    picture that could not be read (it is then recorded unread)."""
    notes = []
    for m in mats:
        if m.readable or not m.image:
            continue
        try:
            out = view.model.structured("transcribe", TRANSCRIBE_SYSTEM, {"material_id": m.id, "name": m.name},
                                        TRANSCRIBE_SCHEMA, max_tokens=8000, images=[m.image], vision=True)
        except Exception as e:  # the picture grounds nothing; the stage goes on with the rest
            notes.append(f"{m.name} could not be transcribed ({getattr(e, 'kind', type(e).__name__)})")
            continue
        text = (out.get("text") or "").strip()
        vis = [v.strip() for v in out.get("visuals") or [] if isinstance(v, str) and v.strip()]
        if vis:
            text = (text + "\n\n" + "\n".join(f"[{v}]" for v in vis)).strip()
        if text:
            m.text = text
            m.transcribed = {"model": view.model.vision_model_id or "", "backend": backend()}
        else:
            notes.append(f"{m.name} held no text the vision model could read")
    return notes


def material_payload(mats: list[Material]) -> list[dict]:
    out, budget = [], MAX_CHARS
    for m in mats:
        if not m.readable:
            continue
        t = m.text[:max(0, budget)]
        budget -= len(t)
        out.append({"material_id": m.id, "kind": m.kind, "name": m.name, "text": t,
                    **({"truncated": True} if len(t) < len(m.text) else {})})
    return out


# ---------------------------------------------------------------------------
# Loop 1 — no-loss capture
# ---------------------------------------------------------------------------

def cap_id(key: str, value: str, mid: str) -> str:
    return "cap_" + hashlib.sha256(f"{key}\n{value}\n{mid}".encode()).hexdigest()[:16]


def _locate(mats: dict, mid, quote):
    """(material, verbatim substring) for a quote, the named material first."""
    order = ([mats[mid]] if mid in mats else []) + [m for k, m in mats.items() if k != mid]
    for m in order:
        if not m.readable:
            continue
        pos = find_quote(m.text, quote or "")
        if pos:
            return m, m.text[pos[0]:pos[1]]
    return None, None


def run_capture(view, mats: list[Material]) -> dict:
    """ONE structured call over the materials; every fact quote checked
    verbatim. Returns the capture pieces (items, gaps, how_to_win, ledger,
    open questions, client/project) and what was dropped."""
    by = {m.id: m for m in mats if m.readable}
    raw = view.model.structured("capture", EXTRACTION_SYSTEM, {"materials": material_payload(mats)}, CAPTURE_SCHEMA,
                                max_tokens=12000)
    items, dropped = {}, 0
    for it in raw.get("items") or []:
        value = (it.get("value") or "").strip()
        key = it.get("key")
        if not value or key not in LOOP1_KEYS:
            continue
        m, quote = _locate(by, it.get("material_id"), it.get("quote")) if it.get("quote") else (None, None)
        if it.get("status") == "fact":
            if m is None:
                dropped += 1  # an unverified fact: its words stay unmapped
                continue
            status = "fact"
        else:
            status = "assumption"
            if m is None:
                m = by.get(it.get("material_id"))
                if m is None:
                    dropped += 1
                    continue
        item = {"key": key, "value": value, "status": status, "material_id": m.id}
        if quote:
            item["quote"] = quote
        if key == "objective" and it.get("objective_type") in OBJECTIVE_TYPES:
            item["objective_type"] = it["objective_type"]
        items.setdefault(cap_id(key, value, m.id), item)
    htw = []
    for h in raw.get("how_to_win") or []:
        m, ev = _locate(by, h.get("material_id"), h.get("evidence"))
        if m is None or not (h.get("point") or "").strip():
            continue
        htw.append({"kind": h["kind"], "point": h["point"].strip(), "evidence": ev, "material_id": m.id})
    oqs = [{"question": q["question"].strip(), "why_it_matters": (q.get("why_it_matters") or "").strip(),
            "priority": q.get("priority")} for q in raw.get("open_questions") or [] if (q.get("question") or "").strip()]
    named = {}
    for k in ("client", "project_name"):
        n = raw.get(k)
        if not isinstance(n, dict) or not (n.get("value") or "").strip():
            continue
        m, quote = _locate(by, n.get("material_id"), n.get("quote"))
        v = n["value"].strip()
        if m is not None and (contains_word(quote, v) or contains_word(m.text, v) and k == "client"):
            named[k] = {"value": v, "quote": quote, "material_id": m.id}
    gaps = [k for k in LOOP1_KEYS if not any(i["key"] == k for i in items.values())]
    return {"items": items, "gaps": gaps, "how_to_win": htw, "open_questions": oqs, "named": named,
            "dropped": dropped, "ledger": build_ledger(mats, items, htw, oqs)}


def segment(text: str) -> list[str]:
    """Coalesce soft-wrapped lines into blocks, then sentence-split (engine)."""
    blocks, buf = [], []

    def flush():
        if buf:
            blocks.append(" ".join(buf).strip())
            buf.clear()

    for raw in text.splitlines():
        stripped = raw.strip()
        is_bullet = bool(re.match(r"^\s*[-*•]\s+", raw))
        is_label = bool(re.match(r"^[A-Za-z /]{3,30}\s*[:=]\s+\S", stripped))
        if not stripped:
            flush()
            continue
        if is_bullet or is_label:
            flush()
        buf.append(stripped.lstrip("-*• \t"))
    flush()
    segs = []
    for block in blocks:
        for piece in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'])", block):
            piece = piece.strip()
            if len(piece) >= 4:
                segs.append(piece)
    return segs


def _norm(s) -> str:
    return re.sub(r"[^a-z0-9 ]", "", str(s).lower())


def build_ledger(mats, items, htw, oqs) -> dict:
    """The no-loss ledger (engine `build_ledger`): a segment is mapped on a
    substring hit, or when most of its content words appear in one quote."""
    quotes = []
    for it in items.values():
        quotes += [_norm(it[k]) for k in ("quote", "value") if it.get(k)]
    for h in htw:
        quotes += [_norm(h["evidence"]), _norm(h["point"])]
    quotes += [_norm(q["question"]) for q in oqs]
    qw = [(q, set(q.split())) for q in quotes if q]
    total, unmapped = 0, []
    for m in mats:
        if not m.readable:
            continue
        for seg in segment(m.text):
            total += 1
            ns = _norm(seg)
            sw = set(ns.split())
            hit = any(ns and (ns in q or q in ns) or (len(sw) >= 4 and len(sw & w) / len(sw) >= 0.7) for q, w in qw)
            if not hit:
                unmapped.append({"segment": seg, "material_id": m.id})
    mapped = total - len(unmapped)
    return {"total_segments": total, "mapped_segments": mapped,
            "coverage_pct": round(100 * mapped / total, 1) if total else 0.0, "unmapped": unmapped}


def core_gap_questions(items: dict) -> list[dict]:
    """Loop 2's questions for genuine gaps in the core fields (engine shape_loop2)."""
    have = {i["key"] for i in items.values()}
    out = []
    for key, (fallbacks, why, prio) in CORE_FIELDS.items():
        if key in have or any(f in have for f in fallbacks):
            continue
        q = EVAL_CRITERIA_QUESTION if key == "evaluation_criteria" else f"What is the {key.replace('_', ' ')}?"
        out.append({"question": q, "why_it_matters": why, "priority": prio})
    return out


def format_question(q: dict) -> str:
    """engine mapping._format_question: `[<priority>] <question> — <why>`."""
    text = q["question"].strip()
    if q.get("priority"):
        text = f"[{q['priority']}] {text}"
    if q.get("why_it_matters"):
        text = f"{text} — {q['why_it_matters']}"
    return text


# ---------------------------------------------------------------------------
# Loop 2 — the working brief's captured fields (engine map_brief, no golden fill)
# ---------------------------------------------------------------------------

def working_brief(items: dict, named: dict) -> dict:
    """app key -> {value, caps: [cap ids], mats: [material ids]} for every
    captured field the capture supports. Absent keys: nothing supports them."""
    by: dict[str, list] = {}
    for cid, it in items.items():
        by.setdefault(it["key"], []).append((cid, it))
    out = {}

    def text(*keys):
        for k in keys:
            got = by.get(k)
            if got:
                return "; ".join(it["value"] for _, it in got), [c for c, _ in got], sorted({it["material_id"] for _, it in got})
        return None

    for app, keys in (("background", ("business_problem", "background_context")), ("audience", ("target_audience",)),
                      ("competitor_context", ("competitors_market",))):
        t = text(*keys)
        if t:
            out[app] = {"value": t[0], "caps": t[1], "mats": t[2]}
    objs = {}
    for cid, it in by.get("objective", []):
        objs.setdefault(it.get("objective_type") or "commercial", []).append((cid, it))
    if not objs and by.get("success_metrics"):
        objs["commercial"] = by["success_metrics"]
    for t, got in objs.items():
        out[f"objectives.{t}"] = {"value": "; ".join(it["value"] for _, it in got), "caps": [c for c, _ in got],
                                  "mats": sorted({it["material_id"] for _, it in got})}
    for app, key in (("tone_and_world", "tone_and_brand"), ("mandatories", "mandatories")):
        got = by.get(key)
        if got:
            out[app] = {"value": [it["value"] for _, it in got], "caps": [c for c, _ in got],
                        "mats": sorted({it["material_id"] for _, it in got})}
    scope = [text(k) for k in ("deliverables", "budget", "timeline")]
    scope = [s for s in scope if s]
    if scope:
        out["budget_and_scope"] = {"value": " · ".join(s[0] for s in scope), "caps": [c for s in scope for c in s[1]],
                                   "mats": sorted({m for s in scope for m in s[2]})}
    for k in ("client", "project_name"):
        if k in named:
            out[k] = {"value": named[k]["value"], "caps": [], "mats": [named[k]["material_id"]],
                      "quote": named[k]["quote"]}
    return out


def capture_certainty(items: dict, caps: list[str], mats: dict) -> tuple[str, str]:
    """§10.7: high — every cited item a fact found verbatim in typed material;
    medium — an assumption among them, or a quote from a transcribed image;
    low — assumptions alone."""
    cited = [items[c] for c in caps if c in items]
    if not cited:
        return "high", "the value is the client's own words, found verbatim in the material"
    if all(i["status"] == "assumption" for i in cited):
        return "low", "it rests on inferences from the material alone, no stated fact"
    if any(i["status"] == "assumption" for i in cited):
        return "medium", "an inference sits among the client's stated facts"
    if any(getattr(mats.get(i["material_id"]), "transcribed", None) for i in cited):
        return "medium", "a quote comes from a transcribed image: the vision model's reading, not typed words"
    return "high", "every cited item is a stated fact whose quote was found verbatim in typed material"


# ---------------------------------------------------------------------------
# The BetterBriefs scorecard
# ---------------------------------------------------------------------------

def run_scorecard(view, mats: list[Material]) -> dict:
    by = {m.id: m for m in mats if m.readable}
    raw = view.model.structured("scorecard", SCORECARD_SYSTEM, {"materials": material_payload(mats)},
                                SCORECARD_SCHEMA, max_tokens=4000)
    got = {}
    for d in raw.get("dimensions") or []:
        if d.get("dimension") in got:
            continue
        e = {"dimension": d["dimension"], "verdict": d["verdict"]}
        if d["verdict"] != "missing" and (d.get("evidence") or "").strip():
            m, ev = _locate(by, d.get("material_id"), d["evidence"])
            if ev:
                e["evidence"] = ev  # verbatim, checked; one not found is dropped
        if (d.get("fix") or "").strip() and d["verdict"] != "pass":
            e["fix"] = d["fix"].strip()
        got[d["dimension"]] = e
    dims = [got.get(k) or {"dimension": k, "verdict": "vague", "fix": "Not scored by the judge; review by hand."}
            for k in SCORECARD_DIMENSIONS]
    sm = raw.get("single_mindedness") or {}
    return {"dimensions": dims,
            "single_mindedness": {"verdict": "multiple" if sm.get("verdict") == "multiple" else "single",
                                  "split_into": [s.strip() for s in sm.get("split_into") or [] if s.strip()]},
            "summary": (raw.get("summary") or "").strip() or "The scorecard judge gave no summary."}
