"""Pause, resume and waiting out limits, for a long seeding run.

A run owns a directory (--run-dir). In it:

  PAUSE        touch it to pause: no new step starts until it is removed
  STOP         touch it (or Ctrl-C) to stop: in-flight steps finish, the run
               exits cleanly; the same command later resumes
  status.json  progress, rewritten as the run goes
  cache/       every research and model answer, so a resumed run never pays
               for the same call twice

A temporary failure never fails a unit. Research timeouts, 5xx and 429, and
model rate_limited / overloaded / server / timeout are retried with growing
waits (30 s up to 10 min). A rate limit pauses the whole run, not just one
worker, since every worker would hit the same limit. After three units in a
row give up this way, the run waits 30 minutes before going on. A unit that
still cannot finish is left for the next run; nothing is marked done that
was not.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import threading
import time
from pathlib import Path

from ..model import ModelError
from ..research import ResearchError

DELAYS = [30, 60, 120, 300, 600]
LONG_WAIT = 1800
TRANSIENT_MODEL = {"rate_limited", "overloaded", "server", "timeout"}
LIMIT_MODEL = {"rate_limited", "overloaded"}


class Stopped(Exception):
    """The run is stopping; the unit is left for the next run."""


class GaveUp(Exception):
    """A temporary failure outlasted every retry; the unit is left for the next run."""


def transient(e: Exception) -> bool:
    if isinstance(e, ModelError):
        return e.kind in TRANSIENT_MODEL
    if isinstance(e, ResearchError):
        m = str(e)
        return "timed out" in m or "unreachable" in m or any(f"returned {c}" in m for c in (429, 500, 502, 503, 504))
    return False


def is_limit(e: Exception) -> bool:
    if isinstance(e, ModelError):
        return e.kind in LIMIT_MODEL
    return isinstance(e, ResearchError) and ("returned 429" in str(e) or "returned 503" in str(e))


class Control:
    def __init__(self, run_dir: Path):
        self.dir = run_dir
        (run_dir / "cache").mkdir(parents=True, exist_ok=True)
        self.pause_file, self.stop_file = run_dir / "PAUSE", run_dir / "STOP"
        self.stop_file.unlink(missing_ok=True)       # a new start is a resume
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._capacity_until = 0.0
        self._reason = None
        self._gave_up_in_a_row = 0
        self.progress = {"total": 0, "done": 0, "cells_written": 0, "skipped_fresh": 0, "left_for_next_run": 0,
                         "failed": 0, "started": time.strftime("%Y-%m-%dT%H:%M:%S")}

    # -- signals --------------------------------------------------------------
    def install_signals(self):
        def handler(signum, frame):
            if self._stop.is_set():
                os._exit(130)                         # a second Ctrl-C: out now
            self._stop.set()
            print(json.dumps({"stopping": "finishing in-flight steps; Ctrl-C again to quit now"}), flush=True)
        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)

    # -- state ------------------------------------------------------------------
    def stopping(self) -> bool:
        return self._stop.is_set() or self.stop_file.exists()

    def wait_turn(self):
        """Block while paused or waiting out a limit; raise Stopped when stopping."""
        shown = False
        while True:
            if self.stopping():
                raise Stopped()
            wait = self._capacity_until - time.time()
            if not self.pause_file.exists() and wait <= 0:
                if shown:
                    self.write_status()
                return
            if not shown:
                self.write_status()
                shown = True
            time.sleep(min(5.0, max(wait, 1.0)) if wait > 0 else 5.0)

    def hold_all(self, seconds: float, reason: str):
        with self._lock:
            self._capacity_until = max(self._capacity_until, time.time() + seconds)
            self._reason = reason
        self.write_status()

    def unit_done(self, row: dict):
        with self._lock:
            p = self.progress
            p["done"] += 1
            cell = str(row.get("cell") or "")
            if cell.startswith("v"):
                p["cells_written"] += 1
            elif cell == "skipped (fresh)":
                p["skipped_fresh"] += 1
            if row.get("left_for_next_run"):
                p["left_for_next_run"] += 1
            elif row.get("error"):
                p["failed"] += 1
            if row.get("gave_up"):
                self._gave_up_in_a_row += 1
                long_wait = self._gave_up_in_a_row >= 3
            else:
                self._gave_up_in_a_row = 0
                long_wait = False
        if long_wait:
            self.hold_all(LONG_WAIT, "three units in a row could not get through; waiting 30 minutes")
        self.write_status()

    def write_status(self):
        with self._lock:
            wait = self._capacity_until - time.time()
            s = dict(self.progress,
                     paused=self.pause_file.exists(), stopping=self.stopping(),
                     waiting_seconds=round(wait) if wait > 0 else 0,
                     waiting_because=self._reason if wait > 0 else None,
                     updated=time.strftime("%Y-%m-%dT%H:%M:%S"))
        # One writer at a time, each through its own temporary file: four workers
        # sharing one tmp name raced, and the loser's rename failed its unit.
        # Progress reporting must never fail a unit, so a failed write is only logged.
        with self._write_lock:
            tmp = self.dir / f"status.json.{os.getpid()}.{threading.get_ident()}.tmp"
            try:
                tmp.write_text(json.dumps(s, indent=1))
                tmp.replace(self.dir / "status.json")
            except OSError as e:
                print(json.dumps({"warning": f"status.json not written: {e}"}), flush=True)

    # -- calls ------------------------------------------------------------------
    def call(self, what: str, fn, cache_key=None):
        """fn(), cached on disk by cache_key, retried through temporary failures."""
        path = None
        if cache_key is not None:
            h = hashlib.sha256(json.dumps(cache_key, sort_keys=True, default=str).encode()).hexdigest()
            path = self.dir / "cache" / f"{what}-{h[:32]}.json"
            if path.exists():
                return json.loads(path.read_text())
        for i in range(len(DELAYS) + 1):
            self.wait_turn()
            try:
                out = fn()
                break
            except Exception as e:
                if not transient(e):
                    raise
                if i == len(DELAYS):
                    raise GaveUp(f"{what}: {e}") from e
                if is_limit(e):
                    self.hold_all(DELAYS[i], f"{what} hit a limit ({e}); the whole run waits")
                else:
                    self._sleep(DELAYS[i])
        if path is not None:
            tmp = path.with_suffix(f".{threading.get_ident()}.tmp")
            tmp.write_text(json.dumps(out, default=str))
            tmp.replace(path)
        return out

    def _sleep(self, seconds: float):
        end = time.time() + seconds
        while time.time() < end:
            if self.stopping():
                raise Stopped()
            time.sleep(min(5.0, end - time.time()))
