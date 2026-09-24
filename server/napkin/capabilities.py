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

import logging
import threading
import time

from .model import ModelError, ModelPort, Usage
from .research import ResearchError, ResearchPort
from .retrieval import RetrievalError, RetrievalPort

CAPABILITY_VERSION = 1
log = logging.getLogger("napkin.caps")


class ModelCap:
    def __init__(self, port: ModelPort | None, usage: Usage, attribution: dict, semaphore: threading.Semaphore | None):
        self._port, self._usage, self._attr, self._sem = port, usage, attribution, semaphore

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
        kw = dict(usage=self._usage, attribution=attr, max_tokens=max_tokens, effort=effort, images=images,
                  model=self._port.vision_model if vision else None,
                  headers={"X-Napkin-Handler": str(self._attr.get("handler") or "-"),
                           "X-Napkin-Job": str(self._attr.get("job") or "-")})
        if self._sem is None:
            return self._port.call(purpose, system, payload, schema, **kw)
        with self._sem:
            return self._port.call(purpose, system, payload, schema, **kw)


class ResearchCap:
    def __init__(self, port: ResearchPort | None, attribution: dict, semaphore: threading.Semaphore):
        self._port, self._attr, self._sem = port, attribution, semaphore

    def search(self, query: str, lens: str, market: str, entity: str | None = None, category: str | None = None,
               max_sources: int = 6) -> dict:
        if self._port is None:
            raise ResearchError("no research service is configured")
        with self._sem:
            t0 = time.monotonic()
            try:
                return self._port.search(query, lens, market, entity=entity, category=category,
                                         max_sources=max_sources, attribution=dict(self._attr))
            finally:
                log.info("research %s/%s %.1fs [%s]", lens, market, time.monotonic() - t0, _attr_str(self._attr))


class RetrievalCap:
    """Passages from the knowledge packs, under the bound scope. The packs list
    is read once per job."""

    def __init__(self, port: RetrievalPort | None, scope: dict, attribution: dict):
        self._port, self._scope, self._attr = port, scope, attribution
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
        t0 = time.monotonic()
        try:
            return self._port.retrieve(self._scope, dict(self._attr), query, k, packs=packs, where=where,
                                       purpose=purpose)
        finally:
            log.info("retrieval %s %.1fs [%s]", purpose or "-", time.monotonic() - t0, _attr_str(self._attr))


class JobCap:
    def __init__(self):
        self._lock = threading.Lock()
        self.done, self.total = 0, 1

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
                 model_semaphore: threading.Semaphore | None = None):
        self._scope = dict(scope)
        self.handler = handler
        self.attribution = {"handler": handler, "job": "-", "org": scope["org"], "brand": scope["brand"]}
        self.usage = Usage()
        self.model = ModelCap(model_port, self.usage, self.attribution, model_semaphore)
        self.research = ResearchCap(research_port, self.attribution, research_semaphore)
        self.retrieval = RetrievalCap(retrieval_port, self._scope, self.attribution)
        try:
            self.layers = layer_store.open(self._scope, self.attribution)
        except TypeError:  # a store whose open() takes the scope only
            self.layers = layer_store.open(self._scope)
        self.jobs = jobs or JobCap()

    def bind_job(self, job_id: str):
        """The job id every later call is attributed to."""
        self.attribution["job"] = job_id

    def capture_view(self) -> CaptureView:
        return CaptureView(self.model, self.jobs, self.handler)

    @property
    def scope(self) -> dict:
        """Read-only: reported in trace.scope, never an input."""
        return dict(self._scope)

    def model_ran(self) -> bool:
        return self.usage.calls > 0
