"""The capability object (W2-C1, decision M3).

A handler receives already-scoped data and one `Capabilities` instance. It
never receives credentials, never reads the environment, and never computes
its own scope: the scope is bound here at construction, from auth (in
development, the fixed tenant in configuration), and no method accepts a
scope argument. Every call is attributed to the handler name, version, job
and scope — in the log and in the peripherals' `X-Napkin-Handler` /
`X-Napkin-Job` headers — so a slow or expensive handler is visible.

  caps.model      structured(purpose, system, payload, schema, images?, vision?) -> validated dict
  caps.research   search(query, lens, market, entity?, category?) -> sources
  caps.retrieval  packs(), retrieve(query, k, packs?, where?, purpose?) -> verbatim passages
  caps.layers     the layers protocol, bound to the scope
  caps.jobs       progress(done, total), for long tasks

`caps.capture_view()` is the object Brief Maker's extract stage gets: model
and jobs only. It has no `retrieval`, `research` or `layers` attribute, so
Loop-1 capture cannot reach a passage (peripherals.md §3.4) rather than being
trusted not to.
"""

from __future__ import annotations

import json
import logging
import threading
import time

from . import runlog
from .jobs import Cancelled
from .model import ModelError, ModelPort, Usage
from .research import ResearchError, ResearchPort
from .retrieval import RetrievalError, RetrievalPort

CAPABILITY_VERSION = 1
log = logging.getLogger("napkin.caps")


def _not_cancelled(stopped):
    if stopped is not None and stopped.is_set():
        raise Cancelled()


class ModelCap:
    def __init__(self, port: ModelPort | None, usage: Usage, attribution: dict, semaphore: threading.Semaphore | None,
                 runlog_of=lambda: runlog.NULL, stopped: threading.Event | None = None):
        self._port, self._usage, self._attr, self._sem = port, usage, attribution, semaphore
        self._log = runlog_of
        self._stopped = stopped

    @property
    def model_id(self) -> str | None:
        return self._port.model if self._port else None

    @property
    def vision_model_id(self) -> str | None:
        return self._port.vision_model if self._port else None

    @property
    def api(self) -> str | None:
        return self._port.api if self._port else None

    def structured(self, purpose: str, system: str, payload: dict, schema: dict, max_tokens: int | None = None,
                   effort: str | None = None, images=None, vision: bool = False) -> dict:
        if self._port is None:
            raise ModelError("no model is configured", "server")
        attr = _attr_str(self._attr)
        mine = Usage()  # this call's own tokens and attempts, for the run log; folded into the job's below
        model = self._port.vision_model if vision else None
        kw = dict(usage=mine, attribution=attr, max_tokens=max_tokens, effort=effort, images=images,
                  model=model,
                  headers={"X-Napkin-Handler": str(self._attr.get("handler") or "-"),
                           "X-Napkin-Job": str(self._attr.get("job") or "-")})
        _not_cancelled(self._stopped)
        t0, out, err = time.monotonic(), None, None
        try:
            if self._sem is None:
                out = self._port.call(purpose, system, payload, schema, **kw)
            else:
                with self._sem:
                    _not_cancelled(self._stopped)  # it may have waited for a slot
                    out = self._port.call(purpose, system, payload, schema, **kw)
            return out
        except Exception as e:
            err = f"{getattr(e, 'kind', type(e).__name__)}: {str(e)[:300]}"
            raise
        finally:
            with self._usage._lock:
                self._usage.calls += mine.calls
                self._usage.input_tokens += mine.input_tokens
                self._usage.output_tokens += mine.output_tokens
            log_ = self._log()
            if log_.on:
                body = runlog.bodies()
                pj = json.dumps(payload, ensure_ascii=False, default=str)
                log_.model(purpose=purpose, model=model or self._port.model, secs=time.monotonic() - t0,
                           tin=mine.input_tokens, tout=mine.output_tokens, attempts=mine.calls, ok=err is None,
                           error=err, payload_chars=len(pj) + len(system or ""),
                           reply_chars=len(json.dumps(out, ensure_ascii=False)) if out is not None else None,
                           payload={"system": system, "payload": payload} if body else None, reply=out if body else None)


class ResearchCap:
    def __init__(self, port: ResearchPort | None, attribution: dict, semaphore: threading.Semaphore,
                 runlog_of=lambda: runlog.NULL, stopped: threading.Event | None = None):
        self._port, self._attr, self._sem = port, attribution, semaphore
        self._log = runlog_of
        self._stopped = stopped

    def search(self, query: str, lens: str, market: str, entity: str | None = None, category: str | None = None,
               max_sources: int = 6) -> dict:
        if self._port is None:
            raise ResearchError("no research service is configured")
        _not_cancelled(self._stopped)
        with self._sem:
            _not_cancelled(self._stopped)  # it may have waited for a slot
            t0, out, err = time.monotonic(), None, None
            try:
                out = self._port.search(query, lens, market, entity=entity, category=category,
                                        max_sources=max_sources, attribution=dict(self._attr))
                return out
            except Exception as e:
                err = f"{type(e).__name__}: {str(e)[:300]}"
                raise
            finally:
                log.info("research %s/%s %.1fs [%s]", lens, market, time.monotonic() - t0, _attr_str(self._attr))
                tr = (out or {}).get("trace") or {}
                self._log().research(lens=lens, market=market, query=query, secs=time.monotonic() - t0,
                                     sources=len((out or {}).get("sources") or []) if out is not None else None,
                                     urls=[x.get("url") for x in (out or {}).get("sources") or []][:12] or None,
                                     cost=tr.get("cost_usd"), cached=tr.get("cached"), error=err,
                                     queries=tr.get("queries") or None)


class JevCap:
    """jev's yes/no and choice answers about one text (napkin.jev). `configured` False when no
    key is set: a caller then goes on unchecked."""

    def __init__(self, port, runlog_of=lambda: runlog.NULL, stopped: threading.Event | None = None):
        self._port, self._log, self._stopped = port, runlog_of, stopped

    @property
    def configured(self) -> bool:
        return self._port is not None

    def ask(self, state: dict, questions: dict, what: str = "-") -> dict:
        from .jev import JevError
        if self._port is None:
            raise JevError("no jev key is configured")
        _not_cancelled(self._stopped)
        t0, out, err = time.monotonic(), None, None
        chars = len(json.dumps(state, ensure_ascii=False, default=str)) + len(json.dumps(questions))
        try:
            out = self._port.ask(state, questions)
            return out
        except Exception as e:
            err = f"{type(e).__name__}: {str(e)[:300]}"
            raise
        finally:
            # jev bills input tokens only, about $0.04 per million (Sai's 2026-09-24 lab); ~4 chars a token
            self._log().jev(check=what, questions=len(questions), secs=time.monotonic() - t0, state_chars=chars,
                            cost=round(chars / 4 / 1e6 * 0.04, 6), error=err)


class RetrievalCap:
    """Passages from the knowledge packs, under the bound scope. The packs list
    is read once per job."""

    def __init__(self, port: RetrievalPort | None, scope: dict, attribution: dict, runlog_of=lambda: runlog.NULL,
                 stopped: threading.Event | None = None):
        self._port, self._scope, self._attr = port, scope, attribution
        self._log = runlog_of
        self._stopped = stopped
        self._packs = None
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return self._port is not None

    def packs(self) -> list[dict]:
        if self._port is None:
            raise RetrievalError("no retrieval service is configured")
        with self._lock:
            if self._packs is None:
                self._packs = self._port.packs(self._scope, dict(self._attr))
            return [dict(p) for p in self._packs]

    def retrieve(self, query: str, k: int, packs=None, where=None, purpose: str | None = None) -> dict:
        if self._port is None:
            raise RetrievalError("no retrieval service is configured")
        _not_cancelled(self._stopped)
        t0, out, err = time.monotonic(), None, None
        try:
            out = self._port.retrieve(self._scope, dict(self._attr), query, k, packs=packs, where=where,
                                      purpose=purpose)
            return out
        except Exception as e:
            err = f"{type(e).__name__}: {str(e)[:300]}"
            raise
        finally:
            log.info("retrieval %s %.1fs [%s]", purpose or "-", time.monotonic() - t0, _attr_str(self._attr))
            found = None
            if isinstance(out, dict):
                found = len(out.get("passages") or out.get("hits") or [])
            self._log().retrieval(purpose=purpose, k=k, secs=time.monotonic() - t0, found=found, error=err,
                                  query=query)


class JobCap:
    def __init__(self):
        self._lock = threading.Lock()
        self.done, self.total = 0, 1
        self.stopped = threading.Event()  # set when a person cancels the job

    def stop(self):
        self.stopped.set()

    @property
    def cancelled(self) -> bool:
        return self.stopped.is_set()

    def progress(self, done: int, total: int | None = None):
        with self._lock:
            if total is not None:
                self.total = total
            self.done = max(self.done, min(done, self.total))


class CaptureView:
    """What Loop-1 capture may use: the model and the job. Nothing that reads
    a pack, the web or the layers."""

    __slots__ = ("model", "jobs", "handler")

    def __init__(self, model: ModelCap, jobs: JobCap, handler: str):
        self.model, self.jobs, self.handler = model, jobs, handler


def _attr_str(a: dict) -> str:
    return f"{a.get('handler')} job={a.get('job')} org={a.get('org')} brand={a.get('brand')}"


class Capabilities:
    version = CAPABILITY_VERSION

    def __init__(self, *, handler: str, scope: dict, model_port, research_port, layer_store,
                 research_semaphore: threading.Semaphore, jobs: JobCap | None = None, retrieval_port=None,
                 model_semaphore: threading.Semaphore | None = None, jev_port=None):
        self._scope = dict(scope)
        self.handler = handler
        self.attribution = {"handler": handler, "job": "-", "org": scope["org"], "brand": scope["brand"]}
        self.usage = Usage()
        self.runlog = runlog.NULL  # the dogfood run log, opened when a job is bound
        of = lambda: self.runlog  # noqa: E731
        self.jobs = jobs or JobCap()
        st = self.jobs.stopped
        self.model = ModelCap(model_port, self.usage, self.attribution, model_semaphore, of, st)
        self.research = ResearchCap(research_port, self.attribution, research_semaphore, of, st)
        self.retrieval = RetrievalCap(retrieval_port, self._scope, self.attribution, of, st)
        self.jev = JevCap(jev_port, of, st)
        try:
            self.layers = layer_store.open(self._scope, self.attribution)
        except TypeError:  # a store whose open() takes the scope only
            self.layers = layer_store.open(self._scope)

    def bind_job(self, job_id: str):
        """The job id every later call is attributed to, and the job's run log."""
        self.attribution["job"] = job_id
        self.runlog = runlog.open_log(job_id, self.handler)

    def capture_view(self) -> CaptureView:
        return CaptureView(self.model, self.jobs, self.handler)

    @property
    def scope(self) -> dict:
        """Read-only: reported in trace.scope, never an input."""
        return dict(self._scope)

    def model_ran(self) -> bool:
        return self.usage.calls > 0
