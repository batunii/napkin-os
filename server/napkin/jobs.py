"""Jobs: long tasks run in a background worker, outliving the request that
started them. A job belongs to the tenant and the document it was started on;
any other lookup is the same 404 as an id that never existed.

A long job's output is computed once and kept, so every later poll of a done
job returns the same change. A failure is `failed` with `{type, message}` —
never a silent success.
"""

from __future__ import annotations

import logging
import threading
import traceback

from .util import TaskError, iso, rid

log = logging.getLogger("napkin.jobs")
MAX_JOBS = 500


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
        self.thread = threading.Thread(target=self._run, name=f"{task}-{jid}", daemon=True)

    def start(self):
        self.thread.start()

    def _run(self):
        self.state = "running"
        try:
            self.result, self.change, self.hits = self.work()
            self.caps.jobs.progress(self.caps.jobs.total)
            self.finished_at = iso()
            self.state = "done"
        except TaskError as e:
            self.error, self.finished_at, self.state = {"type": e.etype, "message": e.message}, iso(), "failed"
        except Exception as e:
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
                finished = [k for k, j in self._jobs.items() if getattr(j, "state", "") in ("done", "failed")]
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
                    and j.scope == scope and j.state in ("queued", "running", "needs_input")]

    def unfinished_campaign(self, scope: dict, doc: str):
        with self._lock:
            return next((j for j in self._jobs.values() if getattr(j, "task", None) == "start_campaign"
                         and j.doc == doc and j.scope == scope and j.state in ("queued", "running", "needs_input")),
                        None)
