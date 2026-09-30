"""
engine_env.py — the one loader for engine/.env (audit C15: four loaders, two parsers).

    import engine_env; engine_env.load()

Reads KEY=VALUE lines (an optional `export ` prefix, quotes stripped, `#` comments and
blank lines skipped) into os.environ WITHOUT overriding anything already set, so the shell
and the test fixtures always win. No dependency: python-dotenv is not installed in every
environment, and a silently unloaded .env once dropped every API key and sent the whole
pipeline to heuristic mode. Loading twice is harmless (already-set keys are kept).
"""
from __future__ import annotations

import os
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent / ".env"


def parse(text: str) -> dict:
    """The KEY=VALUE pairs of a .env text, in order; malformed lines are skipped."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k:
            out[k] = v
    return out


def load(path: "Path | str | None" = None) -> int:
    """Load `path` (default engine/.env) into os.environ, never overriding a set variable.
    Returns how many variables it set; 0 when the file is missing."""
    p = Path(path) if path else ENV_PATH
    try:
        pairs = parse(p.read_text(encoding="utf-8", errors="replace"))
    except FileNotFoundError:
        return 0
    n = 0
    for k, v in pairs.items():
        if k not in os.environ:
            os.environ[k] = v
            n += 1
    return n
