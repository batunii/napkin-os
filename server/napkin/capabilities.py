"""The capability object (W2-C1, decision M3).

A handler receives already-scoped data and one `Capabilities` instance. It
never receives credentials, never reads the environment, and never computes
its own scope: the scope is bound here at construction, from auth (in
development, the fixed tenant in configuration), and no method accepts a
scope argument. Every call is attributed to the handler name, version and
scope in the log, so a slow or expensive handler is visible.

  caps.model     structured(purpose, system, payload, schema)   -> validated dict
  caps.research  search(query, lens, market, entity?, category?) -> sources
  caps.layers    the layers protocol, bound to the scope
  caps.jobs      progress(done, total), for long tasks
"""

from __future__ import annotations

import logging
import threading
import time

from .model import ModelError, ModelPort, Usage
from .research import ResearchError, ResearchPort

CAPABILITY_VERSION = 1
log = logging.getLogger("napkin.caps")


class ModelCap:
    def __init__(self, port: ModelPort | None, usage: Usage, attribution: str):
        self._port, self._usage, self._attr = port, usage, attribution

    @property
    def model_id(self) -> str | None:
        return self._port.model if self._port else None

    def structured(self, purpose: str, system: str, payload: dict, schema: dict, max_tokens: int | None = None,
                   effort: str | None = None) -> dict:
        if self._port is None:
            raise ModelError("no model is configured")
        return self._port.call(purpose, system, payload, schema, usage=self._usage, attribution=self._attr,
                               max_tokens=max_tokens, effort=effort)


class ResearchCap:
    def __init__(self, port: ResearchPort | None, attribution: str, semaphore: threading.Semaphore):
        self._port, self._attr, self._sem = port, attribution, semaphore

    def search(self, query: str, lens: str, market: str, entity: str | None = None, category: str | None = None,
               max_sources: int = 6) -> dict:
        if self._port is None:
            raise ResearchError("no research service is configured")
        with self._sem:
            t0 = time.monotonic()
            try:
                return self._port.search(query, lens, market, entity=entity, category=category,
                                         max_sources=max_sources)
            finally:
                log.info("research %s/%s %.1fs [%s]", lens, market, time.monotonic() - t0, self._attr)


class JobCap:
    def __init__(self):
        self._lock = threading.Lock()
        self.done, self.total = 0, 1

    def progress(self, done: int, total: int | None = None):
        with self._lock:
            if total is not None:
                self.total = total
            self.done = max(self.done, min(done, self.total))


class Capabilities:
    version = CAPABILITY_VERSION

    def __init__(self, *, handler: str, scope: dict, model_port, research_port, layer_store,
                 research_semaphore: threading.Semaphore, jobs: JobCap | None = None):
        self._scope = dict(scope)
        self.handler = handler
        attribution = f"{handler} org={scope['org']} brand={scope['brand']}"
        self.usage = Usage()
        self.model = ModelCap(model_port, self.usage, attribution)
        self.research = ResearchCap(research_port, attribution, research_semaphore)
        self.layers = layer_store.open(self._scope)
        self.jobs = jobs or JobCap()

    @property
    def scope(self) -> dict:
        """Read-only: reported in trace.scope, never an input."""
        return dict(self._scope)

    def model_ran(self) -> bool:
        return self.usage.calls > 0
