#!/usr/bin/env python3
"""One command, one backend, knowledge included:

    python3 serve.py          # then: cd app && npm install && npx tauri dev
                              #   or: cargo run -p napkin-web

Picks the backend for you — you never choose:
  * Anthropic credentials (ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / an
    `ant auth login` profile) -> the 1-pass synthesis agent on the Messages
    API, grounded on the shipped pack digests behind a prompt-cache breakpoint.
  * another chat key (GROQ / CEREBRAS / NVIDIA / GEMINI / OPENAI) -> the full
    engine pipeline (Loops 1-7, golden fill), grounded on the same digests.
  * no key but the `claude` CLI is installed and logged in -> the same
    synthesis agent, billed to your Claude subscription instead.
  * neither -> tells you the two ways to fix that, and exits.

Override with NAPKIN_BACKEND=engine to force the formal pipeline.

The knowledge needs no setup in any case: paraphrased pack digests are
committed at engine/packs_dist/ and load automatically. (Vector retrieval via
Qdrant is a maintainer-side upgrade; nothing here requires it.)
"""
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# Chat keys that mean "the engine pipeline can run". ANTHROPIC_* is deliberately
# not in here: it routes to the synthesis agent instead, just below.
ENGINE_KEYS = ("GROQ_API_KEY", "CEREBRAS_API_KEY", "NVIDIA_API_KEY",
               "GEMINI_API_KEY", "OPENAI_API_KEY")


def _load_env():
    """Load engine/.env so keys are visible before the routing decision."""
    env_path = ROOT / "engine" / ".env"
    if not env_path.is_file():
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(env_path, override=False)
        return
    except ImportError:
        pass
    # Minimal fallback: KEY=VALUE lines, no interpolation, comments stripped.
    for line in env_path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.split("#", 1)[0].strip()
        if k and v and k not in os.environ:
            os.environ[k] = v


def _anthropic_credentials() -> bool:
    """An unset ANTHROPIC_API_KEY does not mean there are no credentials: the
    SDK also reads ANTHROPIC_AUTH_TOKEN and an `ant auth login` profile."""
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    if not shutil.which("ant"):
        return False
    try:
        return subprocess.run(["ant", "auth", "status"], capture_output=True,
                              timeout=10).returncode == 0
    except Exception:
        return False


def _run(script: Path, **extra_env) -> int:
    env = {**os.environ, **extra_env}
    return subprocess.call([sys.executable, str(script)], env=env)


AGENT = ROOT / "mock-agent" / "server.py"
ENGINE = ROOT / "engine" / "agent-server" / "server.py"


def main() -> int:
    _load_env()

    if os.environ.get("NAPKIN_BACKEND", "").strip().lower() == "engine":
        print("[serve] NAPKIN_BACKEND=engine -> formal engine pipeline on :8787")
        return _run(ENGINE, BRIEF_LOOPS37="1", BRIEF_GOLDEN="1")

    if _anthropic_credentials():
        if importlib.util.find_spec("anthropic") is None:
            print("[serve] Anthropic credentials found, but the `anthropic` package "
                  "is not installed.")
            print("[serve]   pip install -r mock-agent/requirements.txt")
            if not shutil.which("claude"):
                return 1
            print("[serve] falling back to the Claude Code CLI for now.")
        else:
            print("[serve] Anthropic credentials -> 1-pass synthesis agent "
                  "(Messages API, cached digest prefix) on :8787")
        return _run(AGENT)

    other = next((k for k in ENGINE_KEYS if os.environ.get(k)), None)
    if other:
        print(f"[serve] {other} found -> engine pipeline on :8787")
        return _run(ENGINE, BRIEF_LOOPS37="1", BRIEF_GOLDEN="1")

    if shutil.which("claude"):
        print("[serve] no chat key -> synthesis agent via the Claude Code CLI on :8787")
        return _run(AGENT)

    sys.exit(
        "[serve] no backend available. Either:\n"
        "  * set ANTHROPIC_API_KEY in engine/.env (and pip install -r "
        "mock-agent/requirements.txt), or\n"
        "  * install Claude Code and run `claude` once to sign in."
    )


if __name__ == "__main__":
    raise SystemExit(main())
