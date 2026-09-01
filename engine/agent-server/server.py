#!/usr/bin/env python3
"""Napkin Brief Maker agent server — the real backend for the app's Generate flow.

Implements the same HTTP contract as mock-agent/server.py (which remains the
keyless demo): the Napkin Studio host POSTs {request_kind, payload, clan} and
expects a 2xx response whose body is a bare JSON object of brief fields.

  python3 engine/agent-server/server.py          # listens on :8787

Tasks:
  draft_brief       → full pipeline: research dossier → parse_brief.run() → map_brief()
  regenerate_field  → one focused LLM call, golden-brief rubric-enriched for insight/SMP

Config (env; engine/.env is auto-loaded by parse_brief on import):
  NAPKIN_AGENT_PORT   listen port (default 8787 — same slot as mock-agent; run one)
  BRIEF_LOOPS37=1     enable RAG-grounded Loops 3–7 (golden fill forced on with it)
  BRIEF_GOLDEN=1      golden-brief fill without loops37
  BRIEF_RESEARCH=0    disable the research dossier (default: on when keys exist)
  RAG_STORE=qdrant    remote vector store; NVIDIA_API_KEY is required for query
                      embedding in this mode, not just ingestion

Error contract: failures return non-2xx. The host strips error bodies (the UI
only ever shows "upstream returned N"), so every diagnostic is printed here.
Never return 200 with {"error": ...} — the app would patch an `error` field
into the brief data.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENGINE_ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ENGINE_ROOT))

import parse_brief  # noqa: E402  (auto-loads engine/.env on import)
from mapping import FIELD_TYPES, build_context, map_brief  # noqa: E402

try:
    import research  # noqa: E402
except Exception as e:  # research is optional; the draft path degrades cleanly
    research = None
    print(f"[i] research module unavailable: {e}", file=sys.stderr)

PORT = int(os.environ.get("NAPKIN_AGENT_PORT", "8787"))

_env_flag = lambda name, default="0": os.environ.get(name, default).strip().lower() in ("1", "true", "yes")

# One draft at a time: parse_brief keeps run-scoped module globals (_LLM_STATS
# is reset per run), so concurrent runs would corrupt each other's ledgers.
_DRAFT_LOCK = threading.Lock()

_TOTALS_LOCK = threading.Lock()
TOTALS = {"drafts": 0, "regens": 0, "logical_calls": 0,
          "prompt_tokens": 0, "completion_tokens": 0, "wall_secs": 0.0}

# App dotted field → golden-brief schema field id (rubric enrichment for regen)
GOLDEN_IDS = {
    "insight": "insight",
    "single_minded_proposition": "smp",
    "reasons_to_believe": "reasons_to_believe",
    "background": "background",
}


def _golden_rubric(app_field: str) -> str:
    gid = GOLDEN_IDS.get(app_field)
    if not gid:
        return ""
    try:
        schema = json.loads((ENGINE_ROOT / "golden-brief" / "golden_brief.schema.json").read_text())
        spec = next(f for f in schema["fields"] if f["id"] == gid)
    except (OSError, KeyError, StopIteration, json.JSONDecodeError):
        return ""
    parts = [f"Field rubric — {spec.get('label', gid)}:", spec.get("prompt", "")]
    if spec.get("good_example"):
        parts.append(f"Good example: {spec['good_example']}")
    if spec.get("bad_example"):
        bad = spec["bad_example"]
        why = spec.get("bad_reason", "")
        parts.append(f"Bad example: {bad}" + (f" (why it fails: {why})" if why else ""))
    if spec.get("max_words"):
        parts.append(f"Hard limit: {spec['max_words']} words.")
    return "\n".join(p for p in parts if p)


def _accumulate(stats: dict | None, kind: str, wall: float):
    with _TOTALS_LOCK:
        TOTALS[kind] += 1
        TOTALS["wall_secs"] += wall
        for src, dst in (("logical_calls", "logical_calls"),
                         ("prompt_tokens", "prompt_tokens"),
                         ("completion_tokens", "completion_tokens")):
            if stats and isinstance(stats.get(src), (int, float)):
                TOTALS[dst] += stats[src]


def _assemble_text(payload: dict, clan_data: dict) -> str:
    text = str(payload.get("input") or clan_data.get("brief_input") or "").strip()
    attachments = payload.get("attachments") or clan_data.get("reference_assets") or []
    for att in attachments:
        if not isinstance(att, dict):
            continue
        extracted = str(att.get("extracted_text") or "").strip()
        if extracted:
            name = att.get("name") or att.get("label") or "attachment"
            text += (f"\n\n===== ATTACHMENT: {name} "
                     f"(supporting context — not part of the client brief) =====\n{extracted}")
    return text


def _derive_names(text: str, clan_data: dict) -> dict:
    """The engine extracts no project/client names; one small call fills the
    app's title bar. Returns {} when no provider is available (keyless mode)."""
    if clan_data.get("project_name") and clan_data.get("client"):
        return {}
    obj = parse_brief._json_call(
        "From this client brief, extract the advertiser (client) name and invent a short, "
        "natural project/campaign name if none is stated. Reply as JSON: "
        '{"project_name": "...", "client": "..."}\n\nBRIEF:\n' + text[:4000],
        system="You name advertising projects. JSON only.",
        max_tokens=120,
        accept=lambda o: isinstance(o, dict) and (o.get("project_name") or o.get("client")),
    )
    if not isinstance(obj, dict):
        return {}
    return {k: str(v).strip() for k, v in obj.items()
            if k in ("project_name", "client") and v and not clan_data.get(k)}


def do_draft(payload: dict, clan: dict) -> tuple[int, dict]:
    clan_data = (clan or {}).get("data") or {}
    text = _assemble_text(payload, clan_data)
    if not text:
        print("[!] draft_brief with no input text", file=sys.stderr)
        return 400, {"error": "no brief text: payload.input / clan.data.brief_input empty"}

    loops37 = bool(payload.get("loops37", _env_flag("BRIEF_LOOPS37")))
    golden = bool(payload.get("golden", _env_flag("BRIEF_GOLDEN"))) or loops37

    # Research NEVER enters the engine input: Loop-1 capture must stay a
    # faithful record of the client's own brief (measured by the no-loss
    # ledger — feeding it a dossier craters coverage). Strategy grounding
    # comes from Loops 3–7's own retrieval; the dossier goes to the planner
    # via the context panel.
    research_summary = None
    if research is not None and _env_flag("BRIEF_RESEARCH", "1"):
        try:
            _dossier, research_summary = research.gather(text, clan_data)
        except Exception:
            print("[!] research failed (continuing without dossier):", file=sys.stderr)
            traceback.print_exc()
    engine_input = text

    t0 = time.time()
    with _DRAFT_LOCK:
        try:
            brief = parse_brief.run(None, client=clan_data.get("client"),
                                    project=clan_data.get("project_name"),
                                    loops37=loops37, golden=golden,
                                    raw_text=engine_input, source_name="napkin-intake")
        except Exception:
            if not loops37:
                raise
            # Loops 3–7 retrieval errors (Qdrant/network/dim mismatch) escape
            # run(); a good Loops 1–2 brief is still worth returning.
            print("[!] loops37 run failed — retrying with loops37 off:", file=sys.stderr)
            traceback.print_exc()
            brief = parse_brief.run(None, client=clan_data.get("client"),
                                    project=clan_data.get("project_name"),
                                    loops37=False, golden=False,
                                    raw_text=engine_input, source_name="napkin-intake")
    wall = time.time() - t0

    fields = map_brief(brief, clan_data)
    ctx = build_context(brief, research_summary=research_summary)
    if ctx:
        fields["context"] = ctx
    try:
        fields.update(_derive_names(text, clan_data))
    except Exception:
        print("[!] name derivation failed (non-fatal):", file=sys.stderr)
        traceback.print_exc()

    stats = (brief.get("meta") or {}).get("llm_stats")
    _accumulate(stats, "drafts", wall)
    mode = (brief.get("meta") or {}).get("extraction_mode", "?")
    print(f"[✓] draft_brief mode={mode} loops37={loops37} wall={wall:.1f}s "
          f"fields={sorted(set(fields) - {'rationale', 'context'})}")
    return 200, fields


def do_regen(payload: dict, clan: dict) -> tuple[int, dict]:
    field = str(payload.get("field") or "")
    if field not in FIELD_TYPES:
        print(f"[!] regenerate_field for unknown field {field!r}", file=sys.stderr)
        return 400, {"error": f"unknown field {field!r}"}
    clan_data = (clan or {}).get("data") or {}
    guidance = str(payload.get("input") or "").strip()
    ftype = FIELD_TYPES[field]

    # Stable prefix (cache-friendly, mirrors mock-agent) + volatile suffix.
    system = ("You are the strategy engine behind a creative-brief tool. You regenerate "
              "exactly one field of the brief. Reply with JSON only: "
              f'{{"{field}": <value>, "rationale": "<≤20 words>"}}. '
              + ("The value must be a JSON array of strings." if ftype == "array"
                 else "The value must be a single string.")
              + " Use the literal field name shown, including any dot.")
    rubric = _golden_rubric(field)
    if rubric:
        system += "\n\n" + rubric

    data_view = {k: v for k, v in clan_data.items()
                 if k not in ("field_styles", "theme", "reference_assets", "brief_input",
                              "locked", "locked_fields", "brief_style")}
    user = ("CURRENT BRIEF DATA:\n" + json.dumps(data_view, indent=1)[:8000]
            + f"\n\nREGENERATE FIELD: {field}"
            + (f"\nGUIDANCE FROM THE PLANNER: {guidance}" if guidance else ""))

    def accept(obj):
        if not isinstance(obj, dict) or field not in obj:
            return False
        v = obj[field]
        return isinstance(v, list) if ftype == "array" else isinstance(v, str) and v.strip()

    t0 = time.time()
    obj = parse_brief._json_call(user, system=system, retries=1, max_tokens=800, accept=accept)
    wall = time.time() - t0
    if not obj:
        print(f"[!] regen {field}: provider chain exhausted (no keys or all links failed)",
              file=sys.stderr)
        return 502, {"error": "no provider produced a usable value"}

    if ftype == "array":
        obj[field] = [str(x) for x in obj[field]]
    out = {field: obj[field], "rationale": str(obj.get("rationale") or f"Regenerated {field}.")}
    _accumulate(None, "regens", wall)
    print(f"[✓] regenerate_field {field} wall={wall:.1f}s")
    return 200, out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # keep stdout for our own structured lines
        pass

    def _send(self, code: int, obj: dict):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/") == "/stats":
            with _TOTALS_LOCK:
                self._send(200, dict(TOTALS))
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            payload = body.get("payload") or {}
            clan = body.get("clan") or {}
            task = payload.get("task", "draft_brief")
            print(f"→ {task}" + (f" field={payload.get('field')}" if payload.get("field") else ""))
            if task == "regenerate_field":
                code, obj = do_regen(payload, clan)
            else:
                code, obj = do_draft(payload, clan)
            self._send(code, obj)
        except Exception:
            traceback.print_exc()
            self._send(500, {"error": "internal error — see server log"})


def main():
    provider = parse_brief.resolve_provider()
    print(f"Napkin brief engine server on :{PORT}  "
          f"(provider={provider}, loops37={_env_flag('BRIEF_LOOPS37')}, "
          f"research={'on' if research and _env_flag('BRIEF_RESEARCH', '1') else 'off'})")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
