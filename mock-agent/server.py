#!/usr/bin/env python3
"""
Napkin Studio — local mock agent server.

A dependency-free stand-in for the real inference backend — kept as the
keyless demo. The production backend is `engine/agent-server/server.py` (the
napkin briefing pipeline); both listen on :8787, so run one at a time.

It receives the
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

Grounding modes (NAPKIN_RETRIEVE):
    digest   (default) every pack digest rides the cached prefix — as before.
    agentic  additionally, a cheap Claude Code run with Read/Grep/Glob searches
             a staged corpus of owned brief-methodology docs and returns rules
             with real file citations, injected into the volatile suffix.

             python3 mock-agent/server.py                      # digest
             NAPKIN_RETRIEVE=agentic python3 mock-agent/server.py

             Retrieval is embedding-free and store-free: no Qdrant, no vectors,
             no embedding key. It grounds craft and QA only — the award corpora
             are licensed and absent, so case evidence still comes from digests.
             Env: NAPKIN_RETRIEVE_MODEL (haiku), NAPKIN_RETRIEVE_TURNS (6),
             NAPKIN_RETRIEVE_TIMEOUT (240), NAPKIN_CORPUS (os.pathsep dirs).
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("NAPKIN_MOCK_PORT", "8787"))
MODEL = os.environ.get("NAPKIN_MOCK_MODEL", "opus")  # haiku | sonnet | opus | fable
# Neutral cwd so Claude Code doesn't load this repo's CLAUDE.md / hooks.
WORKDIR = tempfile.mkdtemp(prefix="napkin-mock-")

# --- Retrieval mode -------------------------------------------------------
# "digest"  (default) — every pack digest goes in the stable prefix, as before.
# "agentic" — ADDITIVELY, a cheap Claude Code run with Read/Grep/Glob searches
#   a staged corpus of owned brief-methodology docs and returns compact findings
#   with real file citations; those go in the VOLATILE suffix so the digest
#   prefix stays byte-identical and keeps riding the prompt cache.
#
# Why two calls instead of giving the drafting model tools: measured on this
# corpus, an agentic Opus draft billed 128k cache-creation tokens ($1.37/draft)
# because Claude Code's own system prompt and tool schemas re-cache as the loop
# grows — and a tool-using draft can't share a stable cache prefix at all.
# Splitting it (haiku retrieves, opus drafts off the still-cached prefix) measured
# $0.078 + $0.19 — ~5x cheaper than the agentic draft, and only ~$0.08 over
# digest-only. The cost is per-TURN scaffolding, not corpus size, which is why
# turns are capped low and the retrieve prompt asks for economy: unbounded, the
# same retrieval took 12 turns and $0.29; at 6 it takes 5 turns and $0.078.
RETRIEVE = os.environ.get("NAPKIN_RETRIEVE", "digest").strip().lower()
RETRIEVE_MODEL = os.environ.get("NAPKIN_RETRIEVE_MODEL", "haiku")
RETRIEVE_TURNS = int(os.environ.get("NAPKIN_RETRIEVE_TURNS", "6"))
RETRIEVE_TIMEOUT = int(os.environ.get("NAPKIN_RETRIEVE_TIMEOUT", "240"))
# Corpus = documents we own outright. NOT the award corpora (licensed, absent);
# this grounds craft/QA, never case evidence — Loops 4/6 stay on the digests.
CORPUS_SRC = os.environ.get("NAPKIN_CORPUS", "")

# Knowledge digests: paraphrased pattern notes per pack (engine/packs_dist/,
# written offline by engine/scripts/distil_pack.py and committed — the only
# knowledge artifact that ships). Loaded once and placed in the STABLE prompt
# prefix so they are byte-identical every call and ride the prompt cache:
# paid for once, then ~free on every draft and regenerate after.
from pathlib import Path
_DIGEST_DIR = Path(os.environ.get(
    "NAPKIN_DIGESTS", Path(__file__).resolve().parent.parent / "engine" / "packs_dist"))


def _load_digests() -> str:
    parts = []
    if _DIGEST_DIR.is_dir():
        for d in sorted(_DIGEST_DIR.glob("*/digest.md")):
            text = d.read_text(errors="replace").strip()
            if text:
                parts.append(f"### {d.parent.name}\n{text}")
    return "\n\n".join(parts)


DIGESTS = _load_digests()

# --- Corpus staging (agentic mode only) -----------------------------------
# Copied into WORKDIR rather than searched in place: `--tools` confines the file
# tools to the working directory, so a staged copy is what keeps the retriever
# off the rest of the repo (and off this repo's CLAUDE.md / hooks).
_REPO = Path(__file__).resolve().parent.parent
_CORPUS_DEFAULT = [
    _REPO / "engine" / "golden-brief",           # brief structure + field contracts
    _REPO / "engine" / "reference" / "betterbriefs",   # the scorecard
]
_CORPUS_EXT = {".md", ".json", ".txt"}
CORPUS_DIR = None


def _stage_corpus():
    """Copy the owned methodology docs into WORKDIR/corpus. Returns (dir, files)."""
    roots = ([Path(p) for p in CORPUS_SRC.split(os.pathsep) if p.strip()]
             if CORPUS_SRC else _CORPUS_DEFAULT)
    dest = Path(WORKDIR) / "corpus"
    dest.mkdir(parents=True, exist_ok=True)
    staged = []
    for root in roots:
        if not root.is_dir():
            continue
        for f in sorted(root.rglob("*")):
            if f.is_file() and f.suffix.lower() in _CORPUS_EXT:
                try:
                    (dest / f.name).write_bytes(f.read_bytes())
                    staged.append(f.name)
                except OSError:
                    pass
    return (dest, staged) if staged else (None, [])


# --- Call tracing (NAPKIN_DUMP=<dir>) -------------------------------------
# Writes the exact request, the exact prompt, the retrieval result and the
# response for every call, so a session can be inspected after the fact. Off
# unless the env var is set; every write is best-effort and can never fail a
# draft. Payloads can contain client material — keep the dir out of the repo.
DUMP_DIR = os.environ.get("NAPKIN_DUMP", "").strip()
_CALL_N = [0]


def _dump(call_id: str, name: str, obj):
    if not DUMP_DIR:
        return
    try:
        d = Path(DUMP_DIR)
        d.mkdir(parents=True, exist_ok=True)
        text = obj if isinstance(obj, str) else json.dumps(obj, indent=2, default=str)
        (d / f"{call_id}.{name}").write_text(text, errors="replace")
    except Exception as e:                      # tracing must never break a call
        print(f"  … dump failed ({e.__class__.__name__}) — continuing", flush=True)


# Findings keyed by the brief text they were fetched for — redrafting the same
# input costs nothing the second time.
_RETRIEVAL_CACHE = {}
_RETRIEVAL_CACHE_MAX = 32
# Most recent draft's findings, reused by the regenerate calls that follow it
# (see retrieve_grounding: a regenerate never runs its own agent loop).
_LAST_FINDINGS = []

# Running token/cost tally across the session (see GET /stats).
TOTALS = {"calls": 0, "input": 0, "output": 0, "cache_read": 0, "cache_creation": 0,
          "cost_usd": 0.0, "retrieval_calls": 0, "retrieval_cost_usd": 0.0}


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


def call_claude(prompt: str, attempts: int = 2, model=None, tools="",
                max_turns=1, cwd=None, timeout=180):
    """Run Claude Code headless; return the parsed JSON envelope. Retries once on
    a transient non-zero exit (overload / rate limit hiccups).

    Defaults are the drafting call: no tools, one turn, single text completion.
    The retrieval call passes tools="Read,Grep,Glob" and max_turns>1; `--tools`
    also makes Claude Code ignore user/project settings and confines the file
    tools to `cwd`, so the retriever can only see the staged corpus."""
    cmd = [
        "claude", "-p", prompt,
        "--model", model or MODEL,
        "--output-format", "json",
        "--tools", tools,     # "" disables ALL tools — force a single completion
        "--max-turns", str(max_turns),
    ]
    if tools:
        # Pre-approve the read-only set so a headless run never blocks on a prompt.
        cmd += ["--allowedTools"] + tools.split(",")
    last = "(no output)"
    for i in range(attempts):
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd or WORKDIR),
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


_RETRIEVE_PROMPT = """\
You are a retrieval step, not a writer. Search ./corpus with Grep/Glob/Read for
guidance that bears on the brief below. The corpus holds brief-craft methodology
and a scoring rubric — NOT campaign case studies, so do not claim it has any.

BRIEF (what to retrieve for):
{gist}

Find the rules that should govern this brief's proposition, its reasons-to-believe,
and how it will be judged. Prefer specific, quotable rules over general advice.

Be economical — each round-trip costs. Two greps and at most two targeted reads
should be enough for a corpus this size; do not enumerate the whole thing.

Output ONLY a JSON object, no prose and no fences:
{{"findings": [{{"path": "corpus/<file>", "rule": "<one specific rule, <= 30 words>"}}],
 "queries": ["<the greps you actually ran>"]}}
At most {k} findings. If the corpus has nothing relevant, return {{"findings": [], "queries": [...]}}.
"""


def retrieve_grounding(gist: str, k: int = 6, task: str = "draft_brief"):
    """Agentic retrieval over the staged corpus, on a cheap model. Returns
    (findings, meta). Never raises — grounding is additive, so any failure just
    means the draft proceeds on the digests alone.

    Only a draft retrieves. A regenerate reuses the draft's findings: the craft
    rules for a brief don't change field-to-field, and re-running the agent loop
    would add ~20s to what should be the app's fastest interaction."""
    global _LAST_FINDINGS
    if RETRIEVE != "agentic" or not CORPUS_DIR:
        return [], {}
    if task != "draft_brief":
        return (_LAST_FINDINGS, {"cached": True}) if _LAST_FINDINGS else ([], {})
    if not gist.strip():
        return [], {}
    key = (gist[:2000], k)
    if key in _RETRIEVAL_CACHE:
        return _RETRIEVAL_CACHE[key], {"cached": True}
    try:
        env = call_claude(
            _RETRIEVE_PROMPT.format(gist=gist[:2000], k=k),
            attempts=1, model=RETRIEVE_MODEL, tools="Read,Grep,Glob",
            max_turns=RETRIEVE_TURNS, cwd=CORPUS_DIR.parent,
            timeout=RETRIEVE_TIMEOUT,
        )
        obj = extract_json(env.get("result", "") or "")
        findings = [f for f in (obj.get("findings") or [])
                    if isinstance(f, dict) and f.get("rule")][:k]
        u = usage_of(env)
        meta = {"turns": env.get("num_turns"), "cost_usd": u["cost_usd"],
                "queries": obj.get("queries") or [], "denials": env.get("permission_denials")}
    except Exception as e:
        print(f"  … retrieval skipped ({e.__class__.__name__}: {e}) — digests only",
              flush=True)
        return [], {"error": str(e)}
    if len(_RETRIEVAL_CACHE) >= _RETRIEVAL_CACHE_MAX:
        _RETRIEVAL_CACHE.clear()
    _RETRIEVAL_CACHE[key] = findings
    _LAST_FINDINGS = findings
    return findings, meta


def grounding_block(findings) -> str:
    """Render findings for the VOLATILE suffix — the stable prefix must not move."""
    if not findings:
        return ""
    lines = [f"  - [{f.get('path') or 'corpus'}] {f['rule']}" for f in findings]
    return ("RETRIEVED CRAFT RULES (from the briefing methodology corpus; obey these "
            "for proposition, RTBs and self-check — they are craft rules, not case "
            "evidence):\n" + "\n".join(lines) + "\n\n")


def build_prompt(payload: dict, clan: dict, grounding: str = "") -> str:
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
        + ((
            "=====\n"
            "KNOWLEDGE DIGESTS — paraphrased patterns from award-effectiveness corpora "
            "(IPA, Cannes, D&AD) and planning playbooks. Let these shape the insight, "
            "proposition and reasons-to-believe: prefer a named mechanism over a generic "
            "claim, obey the craft rules, avoid the traps. Never copy their wording.\n"
            + DIGESTS + "\n") if DIGESTS else "")
        + "=====\n"
    )

    # --- VOLATILE SUFFIX ---
    if task == "regenerate_field" and field:
        suffix = (
            f"BRIEF SO FAR:\n{current}\n\n"
            f"GUIDANCE / NOTES:\n{context[:600]}\n\n"
            + grounding +
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
            + grounding +
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
        _CALL_N[0] += 1
        call_id = f"{time.strftime('%H%M%S')}-{_CALL_N[0]:03d}"
        t0 = time.time()
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8", "replace")
        _dump(call_id, "1-request.json", raw)
        try:
            body = json.loads(raw) if raw else {}
        except Exception as e:
            return self._send(400, {"error": f"bad json: {e}"})

        payload = body.get("payload") or {}
        clan = body.get("clan") or {}

        # Retrieval (agentic mode only) runs BEFORE the draft: its findings are
        # volatile-suffix material, so the cached digest prefix never moves.
        gist = (payload.get("input") or "").strip() or json.dumps(clan.get("data") or {})
        findings, rmeta = retrieve_grounding(
            gist, task=payload.get("task", "draft_brief"))
        if findings:
            print(f"  ⌕ retrieved {len(findings)} rule(s) from corpus"
                  f"{' (cached)' if rmeta.get('cached') else ''}"
                  + ("" if rmeta.get("cached") else
                     f"  turns={rmeta.get('turns')} cost=${rmeta.get('cost_usd', 0):.4f}"),
                  flush=True)
            for f in findings:
                print(f"      [{f.get('path')}] {str(f.get('rule'))[:90]}", flush=True)
            if not rmeta.get("cached"):
                TOTALS["retrieval_calls"] += 1
                TOTALS["retrieval_cost_usd"] += rmeta.get("cost_usd") or 0.0
                if rmeta.get("denials"):
                    print(f"      ! permission denials: {rmeta['denials']}", flush=True)
        _dump(call_id, "2-retrieval.json", {"findings": findings, "meta": rmeta})
        prompt = build_prompt(payload, clan, grounding=grounding_block(findings))
        _dump(call_id, "3-prompt.txt", prompt)

        # Context cost breakdown — where the input tokens go, so we can trim.
        sect = {
            "digests": len(DIGESTS),
            "grounding": len(grounding_block(findings)),
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
            _dump(call_id, "4-envelope.json",
                  {k: v for k, v in env.items() if k != "result"})
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
                f" out={TOTALS['output']:,} cost=${TOTALS['cost_usd']:.4f}"
                + (f" (+{TOTALS['retrieval_calls']} retrieval"
                   f" ${TOTALS['retrieval_cost_usd']:.4f})"
                   if TOTALS["retrieval_calls"] else ""),
                flush=True,
            )
            # Citations travel with the fields so the app can show what grounded
            # the draft. Additive key — the schema allows extra properties.
            #
            # DRAFTS ONLY. A regenerate returns one field, and the template picks
            # it with `Object.keys(res.data).filter(k => k!=='rationale' && k!=='context')[0]`
            # when the exact key is missing — an extra key here is a candidate for
            # that fallback, so a regenerate that came back with only a rationale
            # would patch the field with this citation array.
            if (findings and isinstance(fields, dict)
                    and payload.get("task", "draft_brief") == "draft_brief"):
                fields.setdefault("grounding", [
                    {"citation": f.get("path"), "rule": f.get("rule")} for f in findings
                ])
            _dump(call_id, "5-response.json", fields)
            print(f"  ⤺ {time.time() - t0:.1f}s wall  → 200"
                  + (f"  (trace {call_id}.* in {DUMP_DIR})" if DUMP_DIR else ""),
                  flush=True)
            return self._send(200, fields)
        except Exception as e:
            # Capture the raw model text too — extract_json failures are the
            # common case here and are unreadable without it.
            _dump(call_id, "5-error.txt",
                  f"{e.__class__.__name__}: {e}\n\n--- raw result ---\n"
                  + str((locals().get('env') or {}).get('result', '(no envelope)')))
            print(f"  ✗ {e}  ({time.time() - t0:.1f}s)", flush=True)
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
    if RETRIEVE == "agentic":
        CORPUS_DIR, _staged = _stage_corpus()
        if CORPUS_DIR:
            _bytes = sum((CORPUS_DIR / f).stat().st_size for f in _staged)
            print(f"  retrieval: agentic — {len(_staged)} doc(s), {_bytes:,} chars staged "
                  f"at {CORPUS_DIR}  (retriever={RETRIEVE_MODEL}, max_turns={RETRIEVE_TURNS})")
            print(f"  corpus: {', '.join(_staged)}")
            print("  note: craft/QA grounding only — award-case evidence still comes "
                  "from the digests.")
        else:
            print("  retrieval: agentic requested but no corpus staged — digests only.")
    else:
        print(f"  retrieval: digest ({len(DIGESTS):,} chars, cached prefix). "
              "NAPKIN_RETRIEVE=agentic adds corpus retrieval.")
    print("Point Napkin at it: it's the default agent URL. Ctrl-C to stop.")
    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)
