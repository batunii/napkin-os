"""Jobs: long tasks run in a background worker, outliving the request that
started them. A job belongs to the tenant and the document it was started on;
any other lookup is the same 404 as an id that never existed.

A long job's output is computed once and kept, so every later poll of a done
job returns the same change. A failure is `failed` with `{type, message}` —
never a silent success.

No job holds a document for ever. A stage runs in its own thread for at most
its time (`bounded`); one that outlives it is left behind (a thread cannot be
killed) and its job goes on without it. A job whose worker has died while it
still says `running` no longer blocks its document (`unfinished`).

A person may cancel a job (`cancel_job`). It is `cancelled` at once and no
longer holds its document; what it already sent stays, a stage still running
is left behind like one that ran out of time, and every model, search,
retrieval or jev call it would still make is refused (`Cancelled`), so a
cancelled job stops spending.
"""

from __future__ import annotations

import logging
import threading
import time
import traceback

from .util import TaskError, iso, rid

log = logging.getLogger("napkin.jobs")
MAX_JOBS = 500
UNFINISHED = ("queued", "running", "needs_input")


class Cancelled(Exception):
    """Raised by a capability once a person has cancelled the job it serves."""

    def __init__(self, message="the job was cancelled"):
        super().__init__(message)


class Abandoned(Exception):
    """Raised in a stage's thread once its job has gone on without it: what it
    would still write is thrown away."""


def abandon(thread):
    """Mark the thread a job went on without. The mark is on the Thread itself,
    never its ident: Python hands a dead thread's ident to the next thread."""
    thread.napkin_abandoned = True


def abandoned() -> bool:
    """The calling thread is one a job went on without (`abandon`)."""
    return getattr(threading.current_thread(), "napkin_abandoned", False)


def bounded(fn, seconds, name, stop=None):
    """Run `fn()` in its own thread for at most `seconds`, or until `stop()`
    says the job was cancelled.

    -> (value, error, left): `error` is what it raised (None when it returned);
    `left` is the thread still running after `seconds` (None when it finished),
    which the caller marks abandoned (`abandon`) so nothing it writes later
    lands. An `Abandoned` raised in a thread nobody left behind is an error,
    never a clean return."""
    box = {}

    def go():
        try:
            box["value"] = fn()
        except Abandoned as e:
            if not abandoned():
                box["error"] = e
        except BaseException as e:  # noqa: BLE001 - handed to the job, which records it as a gap
            box["error"] = e
    t = threading.Thread(target=go, name=name, daemon=True)
    t.start()
    end = time.monotonic() + seconds
    while t.is_alive() and not (stop is not None and stop()):
        left = end - time.monotonic()
        if left <= 0:
            break
        t.join(min(left, 0.5) if stop is not None else left)
    if t.is_alive():
        return None, None, t
    return box.get("value"), box.get("error"), None


def _blocking(job) -> bool:
    """The job still holds its document: unfinished, and its worker alive."""
    if getattr(job, "state", None) not in UNFINISHED:
        return False
    th = getattr(job, "thread", None)
    return th is None or th.is_alive() or getattr(job, "state", None) == "queued" and th.ident is None


class LongJob:
    def __init__(self, jid, task, handler, doc, scope, caps, total, work):
        self.id, self.task, self.handler, self.doc, self.scope = jid, task, handler, doc, scope
        self.caps, self.work = caps, work
        caps.jobs.progress(0, total)
        self.state = "queued"
        self.started_at, self.finished_at = iso(), None
        self.error = None
        self.result = self.change = None
        self.hits = []
        self._lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, name=f"{task}-{jid}", daemon=True)

    def start(self):
        self.thread.start()

    def cancel(self) -> bool:
        """Stop the job: nothing it computes from here on is sent."""
        with self._lock:
            if self.state not in ("queued", "running"):
                return False
            self.state, self.finished_at = "cancelled", iso()
        self.caps.jobs.stop()
        return True

    def _run(self):
        with self._lock:
            if self.state == "cancelled":
                return
            self.state = "running"
        try:
            out = self.work()
            with self._lock:
                if self.state == "cancelled":
                    return  # what it computed after the person cancelled it is dropped
                self.result, self.change, self.hits = out
                self.caps.jobs.progress(self.caps.jobs.total)
                self.finished_at = iso()
                self.state = "done"
        except Cancelled:
            return
        except TaskError as e:
            if self.state == "cancelled":
                return
            self.error, self.finished_at, self.state = {"type": e.etype, "message": e.message}, iso(), "failed"
        except Exception as e:
            if self.state == "cancelled":
                return
            log.error("%s %s failed: %s\n%s", self.handler, self.id, e, traceback.format_exc())
            self.error = {"type": "internal", "message": f"{self.handler} failed ({type(e).__name__})"}
            self.finished_at, self.state = iso(), "failed"

    def view(self):
        j = self.caps.jobs
        done = j.total if self.state == "done" else min(j.done, max(0, j.total - 1)) if self.state != "failed" else j.done
        return {"id": self.id, "state": self.state, "progress": {"done": done, "total": j.total},
                "started_at": self.started_at, "finished_at": self.finished_at, "error": self.error}


class JobStore:
    def __init__(self):
        self._jobs: dict[str, object] = {}
        self._lock = threading.Lock()

    def new_id(self) -> str:
        return rid("job_", 20)

    def add(self, job):
        with self._lock:
            self._jobs[job.id] = job
            if len(self._jobs) > MAX_JOBS:
                finished = [k for k, j in self._jobs.items()
                            if getattr(j, "state", "") in ("done", "failed", "cancelled")]
                for k in finished[: len(self._jobs) - MAX_JOBS]:
                    del self._jobs[k]

    def get(self, jid, scope: dict, doc: str):
        if not isinstance(jid, str) or not jid:
            raise TaskError(400, "invalid_input", "input.job_id is required")
        with self._lock:
            job = self._jobs.get(jid)
        if job is None or job.scope != scope or job.doc != doc:
            raise TaskError(404, "unknown_job", "no such job for this document")
        return job

    def unfinished(self, scope: dict, doc: str, tasks) -> list:
        with self._lock:
            return [j for j in self._jobs.values() if getattr(j, "task", None) in tasks and j.doc == doc
                    and j.scope == scope and _blocking(j)]

    def unfinished_campaign(self, scope: dict, doc: str):
        with self._lock:
            return next((j for j in self._jobs.values() if getattr(j, "task", None) == "start_campaign"
                         and j.doc == doc and j.scope == scope and _blocking(j)), None)
