#!/usr/bin/env python3
"""One command, one backend, knowledge included:

    python3 serve.py          # then: cd app && npm install && npx tauri dev

Picks the backend for you — you never choose:
  * a chat key in the env (GROQ_API_KEY / CEREBRAS_API_KEY / NVIDIA_API_KEY /
    GEMINI_API_KEY / OPENAI_API_KEY / ANTHROPIC_API_KEY) -> the full engine
    pipeline (Loops 1-7, golden fill), grounded on the shipped pack digests.
  * no key but the `claude` CLI is installed and logged in -> the mock-agent,
    briefs via your Claude login, grounded on the same digests.
  * neither -> tells you the two ways to fix that, and exits.

The knowledge needs no setup in either case: paraphrased pack digests are
committed at engine/packs_dist/ and load automatically. (Vector retrieval via
Qdrant is a maintainer-side upgrade; nothing here requires it.)
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CHAT_KEYS = ("GROQ_API_KEY", "CEREBRAS_API_KEY", "NVIDIA_API_KEY",
             "GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")


def main() -> int:
    key = next((k for k in CHAT_KEYS if os.environ.get(k)), None)
    if key:
        env = {**os.environ}
        env.setdefault("BRIEF_LOOPS37", "1")   # digest grounding on by default
        env.setdefault("BRIEF_GOLDEN", "1")
        print(f"[serve] {key} found -> engine pipeline (digest-grounded) on :8787")
        return subprocess.call([sys.executable, str(ROOT / "engine" / "agent-server" / "server.py")],
                               env=env)
    if shutil.which("claude"):
        print("[serve] no chat key -> Claude Code via mock-agent (digest-grounded) on :8787")
        return subprocess.call([sys.executable, str(ROOT / "mock-agent" / "server.py")])
    sys.exit("[serve] no backend available. Either:\n"
             "  1. install Claude Code and log in  (https://claude.ai/code), or\n"
             "  2. export any one chat key: " + " / ".join(CHAT_KEYS))


if __name__ == "__main__":
    raise SystemExit(main())
