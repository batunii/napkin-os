#!/usr/bin/env python3
"""One command, one backend, knowledge included:

    python3 serve.py                 # then: cd app && npm install && npx tauri dev
    python3 serve.py --claude-code   # the full engine, every Claude call on your Claude Code login
    python3 serve.py --api           # the full engine on the API key (fails if the key has no credit)
    python3 serve.py --auto          # the full engine on the API key, switching to the Claude Code
                                     # login if the key has no credit or is rejected
    python3 serve.py --mock-agent    # the old lightweight mock agent (one Claude Code call, no checks)

With no flag it picks the backend for you (engine/.env is read first, never overriding the
shell), always the full engine pipeline (Loops 1-7, golden fill):
  * BRIEF_CLAUDE_TRANSPORT set -> that transport.
  * ANTHROPIC_API_KEY set -> transport `auto` when Claude Code is installed (a key with no
    credit hands over to the login), else `api`.
  * no Anthropic key but the `claude` CLI installed and logged in -> transport `cli`: every
    Claude call on your Claude Code login, no API key needed.
  * another chat key only -> the engine, which stops with a clear error (the chain is
    Claude-only unless BRIEF_ALLOW_NONCLAUDE=1).
  * nothing -> tells you the ways to fix that, and exits.
Until 2026-09-29 the no-key case started the mock-agent, a second backend with its own
prompt and no checks (audit C7), and only the shell was checked, so keys kept in
engine/.env were missed (N10). --mock-agent still starts it on request.

--claude-code, --api and --auto set BRIEF_CLAUDE_TRANSPORT (cli / api / auto) for the
engine, which decides how every `anthropic:` link in the model chain is sent; see
engine/parse_brief.py `_claude_transport` and engine/rag/docs/adr/0005-claude-transport.md.
An explicit BRIEF_CLAUDE_TRANSPORT in the environment is used when no flag is given.

NAPKIN_BACKEND overrides all of this for a deployment: `engine` starts the engine, `api`
the mock agent on the Messages API with the key it is given (it refuses to start without one).

Override with NAPKIN_BACKEND=api to require the Messages API (what a deployment
sets: no CLI fallback, and a missing key is fatal at boot), or
NAPKIN_BACKEND=engine to force the formal pipeline.

The knowledge needs no setup in any case: paraphrased pack digests are
committed at engine/packs_dist/ and load automatically. (Vector retrieval via
Qdrant is a maintainer-side upgrade; nothing here requires it.)
"""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CHAT_KEYS = ("GROQ_API_KEY", "CEREBRAS_API_KEY", "NVIDIA_API_KEY",
             "GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")
ENGINE_SERVER = ROOT / "engine" / "agent-server" / "server.py"
MOCK_SERVER = ROOT / "mock-agent" / "server.py"


def _engine_env(transport: "str | None") -> dict:
    """The environment for the engine server: digest grounding on by default, and the
    Claude transport when one was chosen (flag first, then an existing env setting)."""
    env = {**os.environ}
    env.setdefault("BRIEF_LOOPS37", "1")   # digest grounding on by default
    env.setdefault("BRIEF_GOLDEN", "1")
    if transport:
        env["BRIEF_CLAUDE_TRANSPORT"] = transport
    return env


def _run_engine(transport: "str | None", why: str) -> int:
    """Start the full engine pipeline server on :8787 with the chosen Claude transport."""
    shown = transport or os.environ.get("BRIEF_CLAUDE_TRANSPORT") or "api"
    print(f"[serve] {why} -> engine pipeline (digest-grounded) on :8787, Claude transport: {shown}")
    return subprocess.call([sys.executable, str(ENGINE_SERVER)], env=_engine_env(transport))


def _run_mock() -> int:
    """Start the lightweight mock agent (Claude Code login, one call per request) on :8787.
    The API key is not passed on: the mock exists to use the login, and with the key in
    its environment `claude -p` would bill the key instead."""
    print("[serve] Claude Code via mock-agent (digest-grounded) on :8787")
    env = {**os.environ}
    env.pop("ANTHROPIC_API_KEY", None)
    return subprocess.call([sys.executable, str(MOCK_SERVER)], env=env)


def choose(env: dict, has_claude: bool) -> "tuple[str, str | None, str]":
    """What a run with no flag starts: ("engine", transport or None, why), or ("none", None,
    "") when there is no way to reach a model. Pure, so the choice is testable."""
    if env.get("BRIEF_CLAUDE_TRANSPORT"):
        return "engine", None, f"BRIEF_CLAUDE_TRANSPORT={env['BRIEF_CLAUDE_TRANSPORT']}"
    if env.get("ANTHROPIC_API_KEY"):
        return "engine", ("auto" if has_claude else "api"), "ANTHROPIC_API_KEY found"
    if has_claude:
        return "engine", "cli", "no Anthropic key, Claude Code installed"
    key = next((k for k in CHAT_KEYS if env.get(k)), None)
    if key:
        # The engine's chain is Claude-only by default (2026-09-25): this stops with a clear
        # error instead of a brief written by another model, unless BRIEF_ALLOW_NONCLAUDE=1.
        return "engine", None, f"{key} found"
    return "none", None, ""


def _load_engine_env() -> None:
    """Read engine/.env into this process (never overriding the shell), as the engine does,
    so the choice below sees the same keys the engine will."""
    sys.path.insert(0, str(ROOT / "engine"))
    try:
        import engine_env
        engine_env.load()
    except Exception as e:  # noqa: BLE001 — a missing or odd .env never blocks start-up
        print(f"[serve] engine/.env not read: {e}", file=sys.stderr)


def main(argv=None) -> int:
    """Pick the backend from the flag, or automatically when no flag is given."""
    ap = argparse.ArgumentParser(description="Start the Napkin Studio backend on :8787.")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--claude-code", action="store_true",
                   help="full engine; every Claude call goes through `claude -p` (your Claude Code login)")
    g.add_argument("--api", action="store_true", help="full engine; Claude calls use ANTHROPIC_API_KEY")
    g.add_argument("--auto", action="store_true",
                   help="full engine; API key first, Claude Code login if the key has no credit or is rejected")
    g.add_argument("--mock-agent", action="store_true", help="the lightweight mock agent instead of the engine")
    a = ap.parse_args(argv)

    # What a deployment sets; no guessing, and a misconfigured host fails loudly.
    backend = os.environ.get("NAPKIN_BACKEND", "").strip().lower()
    if backend == "engine":
        return _run_engine(None, "NAPKIN_BACKEND=engine")
    if backend == "api":
        print("[serve] NAPKIN_BACKEND=api -> mock agent, Messages API only, on :8787")
        return subprocess.call([sys.executable, str(MOCK_SERVER)], env={**os.environ})

    if a.mock_agent:
        if not shutil.which("claude"):
            sys.exit("[serve] --mock-agent needs the `claude` CLI installed and logged in.")
        return _run_mock()
    if a.claude_code:
        if not shutil.which("claude"):
            sys.exit("[serve] --claude-code needs the `claude` CLI installed and logged in.")
        return _run_engine("cli", "--claude-code")
    if a.api:
        return _run_engine("api", "--api")
    if a.auto:
        return _run_engine("auto", "--auto")

    _load_engine_env()
    kind, transport, why = choose(dict(os.environ), bool(shutil.which("claude")))
    if kind == "engine":
        return _run_engine(transport, why)
    sys.exit("[serve] no backend available. Either:\n"
             "  1. install Claude Code and log in  (https://claude.ai/code), then run with --claude-code, or\n"
             "  2. export any one chat key: " + " / ".join(CHAT_KEYS))


if __name__ == "__main__":
    raise SystemExit(main())
