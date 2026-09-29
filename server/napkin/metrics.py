"""The run ledger: one JSON line per model attempt, research call and stage.

A served middleware (`python -m napkin.app`) appends to `runs/metrics/middleware.jsonl`
by default; set `NAPKIN_METRICS_FILE` to another path, or to an empty string to
switch it off. Tests and embedded use write nothing unless the variable is set. Metadata only, never a prompt or a reply. Read it back with
`server/tools/run_report.py`.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

_LOCK = threading.Lock()
DEFAULT_FILE = Path(__file__).resolve().parents[2] / "runs" / "metrics" / "middleware.jsonl"


def enable_by_default() -> None:
    """Called by the server's `main`: turn the ledger on unless the operator set the variable (empty = off)."""
    if "NAPKIN_METRICS_FILE" not in os.environ:
        DEFAULT_FILE.parent.mkdir(parents=True, exist_ok=True)
        os.environ["NAPKIN_METRICS_FILE"] = str(DEFAULT_FILE)


def emit(kind: str, **fields) -> None:
    """Append one ledger line. Never raises: metrics must not fail a job."""
    path = os.environ.get("NAPKIN_METRICS_FILE", "").strip()
    if not path:
        return
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()), "kind": kind, **fields}
    try:
        with _LOCK, open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except (OSError, TypeError, ValueError):
        pass
