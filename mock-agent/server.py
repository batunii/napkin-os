#!/usr/bin/env python3
"""
Napkin Studio — local mock agent server.

A dependency-free stand-in for the real inference backend. It receives the
enriched payload Napkin Studio sends (the client's input + the artifact's
provenance: schema, current data, decision chain, context, lineage), calls
**Claude Code** headless to produce the structured brief fields, and returns
them as JSON the app places straight into the boxes.

Run:
    python3 mock-agent/server.py            # listens on :8787 (the default agent URL)
    NAPKIN_MOCK_MODEL=sonnet python3 mock-agent/server.py

Requires: the `claude` CLI on PATH and an existing Claude Code login (or
ANTHROPIC_API_KEY). No Python packages needed.

It prints how much context each call carries, so we can see how much an agent
actually needs.
"""

import json
import os
import subprocess
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("NAPKIN_MOCK_PORT", "8787"))
MODEL = os.environ.get("NAPKIN_MOCK_MODEL", "opus")  # haiku | sonnet | opus | fable
# Neutral cwd so Claude Code doesn't load this repo's CLAUDE.md / hooks.
WORKDIR = tempfile.mkdtemp(prefix="napkin-mock-")

# Running token/cost tally across the session (see GET /stats).
TOTALS = {"calls": 0, "input": 0, "output": 0, "cache_read": 0, "cache_creation": 0, "cost_usd": 0.0}


def usage_of(env: dict) -> dict:
    """Normalize the CLI's usage block (field names vary across versions)."""
    u = env.get("usage") or {}
    g = lambda *ks: next((u[k] for k in ks if isinstance(u.get(k), int)), 0)
    return {
        "input": g("input_tokens"),
        "output": g("output_tokens"),
        "cache_read": g("cache_read_input_tokens", "cache_read_tokens"),
        "cache_creation": g("cache_creation_input_tokens", "cache_creation_tokens"),
        "cost_usd": env.get("total_cost_usd") or 0.0,
    }


def approx_tokens(chars: int) -> int:
    return chars // 4  # rough chars→tokens for attributing context sections


def call_claude(prompt: str, attempts: int = 2):
    """Run Claude Code headless; return the parsed JSON envelope. Retries once on
    a transient non-zero exit (overload / rate limit hiccups)."""
    cmd = [
        "claude", "-p", prompt,
        "--model", MODEL,
        "--output-format", "json",
        "--tools", "",        # disable ALL tools — force a single text completion
        "--max-turns", "1",   # (belt & suspenders) no agentic loop
    ]
    last = "(no output)"
    for i in range(attempts):
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=180,
            cwd=WORKDIR,
            stdin=subprocess.DEVNULL,   # don't wait 3s for stdin; deterministic
        )
        if proc.returncode == 0:
            return json.loads(proc.stdout)
        # Claude Code often reports the real reason on stdout (json) even on a
        # non-zero exit, so surface both streams.
        last = (proc.stderr or "").strip() or (proc.stdout or "").strip() or "(no output)"
        print(f"  … claude exit {proc.returncode} (attempt {i+1}/{attempts}): {last[:200]}", flush=True)
    raise RuntimeError(f"claude exit nonzero after {attempts} tries: {last[:600]}")


def extract_json(text: str):
    """Pull a JSON object out of the model's text (tolerate ``` fences / prose)."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1] if "```" in t[3:] else t.strip("`")
        t = t[4:] if t.lower().startswith("json") else t
    start, end = t.find("{"), t.rfind("}")
    if start != -1 and end != -1:
        t = t[start:end + 1]
    return json.loads(t)


def build_prompt(payload: dict, clan: dict) -> str:
    """Prompt = STABLE prefix (system + full schema + rules; byte-identical every
    call → cacheable) + VOLATILE suffix (data, task). Full schema always; lean,
    output-capped regenerate; locked fields are flagged so a draft preserves them.
    """
    task = payload.get("task", "draft_brief")
    field = payload.get("field")
    user_input = payload.get("input", "") or ""
    data = clan.get("data", {}) or {}
    current = json.dumps(data, indent=2)
    context = clan.get("context", "") or ""

    # --- STABLE PREFIX (identical across draft + regenerate → cache prefix) ---
    schema = json.dumps(clan.get("schema", {}), indent=2)
    prefix = (
        "You are an expert advertising strategist working on a creative brief.\n"
        "Fields you may fill (JSON Schema):\n" + schema + "\n"
        "Rules: `objectives` and `desired_response` are nested objects; "
        "`reasons_to_believe`, `tone_and_world`, `mandatories`, `open_questions` are arrays "
        "of strings. Output a single JSON object — no markdown fences, no commentary.\n"
        "=====\n"
    )

    # --- VOLATILE SUFFIX ---
    if task == "regenerate_field" and field:
        suffix = (
            f"BRIEF SO FAR:\n{current}\n\n"
            f"GUIDANCE / NOTES:\n{context[:600]}\n\n"
            f'TASK: Rewrite ONLY "{field}" — sharper and consistent with the brief.'
            + (f" Extra instruction from the human: {user_input}" if user_input else "")
            + "\n"
            f'Output ONLY: {{ "{field}": <value>, "rationale": "<= 20 words" }}'
        )
    else:
        att = payload.get("attachments") or data.get("reference_assets") or []
        _al = []
        for a in att:
            label = a.get("label") or a.get("name")
            _al.append(f"  - {label} ({a.get('type')})")
            txt = (a.get("extracted_text") or "").strip()
            if txt:
                _al.append(f"    ┌─ extracted content of {label} ─")
                _al.append("\n".join("    │ " + ln for ln in txt.splitlines()))
                _al.append("    └─")
        att_lines = "\n".join(_al) or "  (none)"
        locked = data.get("locked_fields") or []
        lock_line = ", ".join(locked) if locked else "(none)"
        immersive = data.get("brief_style") == "immersive"
        theme_line = (
            '  - "theme": { "bg": "#RRGGBB", "accent": "#RRGGBB", "text": "#RRGGBB" } '
            "— a brand palette expressing tone_and_world/mood. bg and text MUST be "
            "strongly contrasting (one dark, one light) for legibility; accent is a "
            "vivid on-brand hue. Optionally add \"font\": \"serif\"|\"sans\"|\"mono\".\n"
            if immersive else ""
        )
        suffix = (
            f"PURPOSE / CONTEXT:\n{context}\n\n"
            f"CURRENT DATA (keep good values):\n{current}\n\n"
            f"DECISION HISTORY:\n{json.dumps(clan.get('decision_chain', {}))}\n\n"
            f"CLIENT INPUT (messy brief):\n{user_input}\n\n"
            f"ATTACHED REFERENCE FILES:\n{att_lines}\n\n"
            f"LOCKED FIELDS — do NOT change these, keep their current values: {lock_line}\n\n"
            "TASK: Draft the full creative brief from the client input AND the "
            "extracted content of the attached reference files above.\n"
            "Output a JSON object with the schema's top-level keys, PLUS:\n"
            '  - "rationale": <= 2 sentences on the key choices.\n'
            '  - "context": a SHORT markdown brief for the next agent (client, requirement, '
            'audience, key decisions) — <= 120 words.\n'
            + theme_line +
            'Keep values tight. Use "" or [] only when genuinely unknowable.'
        )
    return prefix + suffix


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quieter default logging
        print(a)
        pass

    def do_GET(self):
        # GET /stats → cumulative token + cost tally for the session.
        if self.path.rstrip("/") == "/stats":
            return self._send(200, TOTALS)
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8", "replace")
        try:
            body = json.loads(raw) if raw else {}
        except Exception as e:
            return self._send(400, {"error": f"bad json: {e}"})

        payload = body.get("payload") or {}
        clan = body.get("clan") or {}
        prompt = build_prompt(payload, clan)

        # Context cost breakdown — where the input tokens go, so we can trim.
        sect = {
            "schema": len(json.dumps(clan.get("schema", {}))),
            "data": len(json.dumps(clan.get("data", {}))),
            "chain": len(json.dumps(clan.get("decision_chain", {}))),
            "context": len(clan.get("context", "") or ""),
            "input": len(payload.get("input", "") or ""),
            "attach_text": sum(
                len(a.get("extracted_text") or "")
                for a in (payload.get("attachments") or [])
            ),
        }
        task = payload.get("task", "draft_brief")
        print(
            f"→ {task}{(' field='+payload.get('field')) if payload.get('field') else ''}"
            f"  | prompt {len(prompt):,} chars  model={MODEL}",
            flush=True,
        )
        print(
            "  context: " + "  ".join(
                f"{k} {v:,}c(~{approx_tokens(v)}t)" for k, v in sect.items() if v
            ),
            flush=True,
        )
        try:
            env = call_claude(prompt)
            fields = extract_json(env.get("result", "") or "")
            u = usage_of(env)
            for k in ("input", "output", "cache_read", "cache_creation"):
                TOTALS[k] += u[k]
            TOTALS["cost_usd"] += u["cost_usd"]
            TOTALS["calls"] += 1
            print(
                f"  ✓ tokens in={u['input']:,} out={u['output']:,}"
                f" cache(read={u['cache_read']:,} create={u['cache_creation']:,})"
                f"  cost=${u['cost_usd']:.4f}",
                flush=True,
            )
            print(
                f"  Σ session: calls={TOTALS['calls']} in={TOTALS['input']:,}"
                f" out={TOTALS['output']:,} cost=${TOTALS['cost_usd']:.4f}",
                flush=True,
            )
            return self._send(200, fields)
        except Exception as e:
            print(f"  ✗ {e}", flush=True)
            return self._send(502, {"error": str(e)})

    def _send(self, code, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    print(f"Napkin mock agent on http://localhost:{PORT}  (model={MODEL}, cwd={WORKDIR})")
    print("Point Napkin at it: it's the default agent URL. Ctrl-C to stop.")
    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)
