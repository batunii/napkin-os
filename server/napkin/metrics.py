"""The run ledger: one JSON line per model attempt, research call and stage.

A served middleware (`python -m napkin.app`) appends to `runs/metrics/middleware.jsonl`
by default; set `NAPKIN_METRICS_FILE` to another path, or to an empty string to
switch it off. Tests and embedded use write nothing unless the variable is set. The ledger is
metadata only, never a prompt or a reply. Read it back with `server/tools/run_report.py`.

`NAPKIN_RECORD_DIR` is a separate, opt-in switch for test recordings: when set, every model
attempt is also written in full (system prompt, input, schema, reply) to
`<dir>/model_calls.jsonl`, so a stage can be replayed or a cheaper model tried on the same
input. Recordings hold whatever the job read; keep the directory out of git and away from
client material.
"""

from __future__ import annotations

import contextvars
import json
import os
import threading
import time
from pathlib import Path

_LOCK = threading.Lock()
UNIT: contextvars.ContextVar = contextvars.ContextVar("napkin_unit", default=None)  # "<lens>/<market>" while a unit runs
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
    if "unit" not in rec and UNIT.get():
        rec["unit"] = UNIT.get()
    try:
        with _LOCK, open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except (OSError, TypeError, ValueError):
        pass


def record(kind: str, **fields) -> None:
    """Append one full recording line to `$NAPKIN_RECORD_DIR/<kind>.jsonl`. Off unless the variable is set. Never raises."""
    d = os.environ.get("NAPKIN_RECORD_DIR", "").strip()
    if not d:
        return
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()), "unit": UNIT.get(), **fields}
    try:
        Path(d).mkdir(parents=True, exist_ok=True)
        with _LOCK, open(Path(d) / f"{kind}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except (OSError, TypeError, ValueError):
        pass
