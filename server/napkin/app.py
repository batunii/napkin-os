"""The FastAPI app: `POST /v1/tasks`, `GET /healthz` (napkin.middleware/1).

    cd server && uv run python -m napkin.app          # http://127.0.0.1:8795/v1/tasks

Scope comes from auth (in development: the fixed tenant in configuration) and
is reported in `trace.scope`; a request that names a scope is refused (M3).
Every error is `{"error": {"type", "message"}}` with the contract's status,
and a message never carries request content. Never a 200 with a fallback.
"""

from __future__ import annotations

import json
import logging
import threading

import anthropic
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import API, BACKEND, registry
from .capabilities import Capabilities
from .config import Settings
from .doc import STAGES
from .jobs import JobStore, LongJob
from .layers import open_store
from .model import ModelPort
from .research import ResearchPort
from .util import TaskError, bad, iso

log = logging.getLogger("napkin.app")
MAX_BODY = 64 * 1024 * 1024


class Req:
    """What a handler is given: the document id and version, the request's
    clan (already scoped by the host), the task input and its own handler id."""

    def __init__(self, doc, base, clan, inp, handler):
        self.doc, self.base, self.clan, self.inp, self.handler = doc, base, clan, inp, handler


class Middleware:
    def __init__(self, settings: Settings, model_client=None, research_port=None, layer_store=None):
        self.settings = settings
        self.resolved = registry.resolve_at_startup(settings.pipelines)
        if model_client is None:
            model_client = anthropic.Anthropic()  # ANTHROPIC_BASE_URL / credentials from the environment
        self.model_port = ModelPort(model_client, settings.model, settings.model_timeout)
        if research_port is None and settings.research_url:
            research_port = ResearchPort(settings.research_url, settings.research_timeout)
        self.research_port = research_port
        self.layer_store = layer_store or open_store(settings.layers)
        self.jobs = JobStore()
        self.research_sem = threading.Semaphore(max(1, settings.research_concurrency))

    def caps(self, handler: str) -> Capabilities:
        return Capabilities(handler=handler, scope=self.settings.scope, model_port=self.model_port,
                            research_port=self.research_port, layer_store=self.layer_store,
                            research_semaphore=self.research_sem)

    # -- envelope -----------------------------------------------------------
    def trace(self, caps: Capabilities | None, hits=()) -> dict:
        ran = caps is not None and caps.model_ran()
        return {"scope": self.settings.scope, "backend": BACKEND,
                "model": caps.model.model_id if ran else None,
                "hits": list({(h["id"], h["source"]): h for h in hits}.values()),
                "usage": caps.usage.as_dict() if caps is not None else {"input_tokens": 0, "output_tokens": 0}}

    def envelope(self, task, handler, job, result, change, caps, hits=()):
        return {"api": API, "task": task, "handler": handler, "job": job, "result": result, "change": change,
                "trace": self.trace(caps, hits)}

    def campaign_envelope(self, job, change):
        return self.envelope("start_campaign", job.handler, job.view(),
                             {"summary": job.summary(), "messages": job.messages()}, change, job.caps,
                             job.hits if job.state == "done" else [])

    # -- dispatch -----------------------------------------------------------
    def handle(self, body) -> dict:
        if not isinstance(body, dict):
            raise bad("the body must be a JSON object")
        if body.get("request_kind") != "middleware":
            raise bad("request_kind must be 'middleware'")
        payload = body.get("payload")
        if not isinstance(payload, dict):
            raise bad("payload must be an object")
        inp = payload.get("input", {})
        if not isinstance(inp, dict):
            raise bad("payload.input must be an object")
        clan = body.get("clan")
        if any(_has_scope(x) for x in (body, payload, inp, clan)):
            raise bad("scope is derived from auth and must not appear in the request (M3)")
        task = payload.get("task")
        if not isinstance(task, str) or not task:
            raise bad("payload.task is required")
        if task not in registry.TASKS:
            raise bad(f"unknown task '{task}'", "unknown_task")
        if not isinstance(clan, dict) or not isinstance(clan.get("id"), str) or not clan["id"]:
            raise bad("clan.id is required: a change is computed for one document")
        doc = clan["id"]
        scope = self.settings.scope
        if task == "job_status":
            job = self.jobs.get(inp.get("job_id"), scope, doc)
            if job.task == "start_campaign":
                return self.campaign_envelope(job, job.reply_change(clan))
            done = job.state == "done"
            return self.envelope(job.task, job.handler, job.view(),
                                 job.result if done else {"summary": _long_summary(job)},
                                 job.change if done else None, job.caps, job.hits if done else [])
        base = clan.get("version")
        if not isinstance(base, (str, int)) or isinstance(base, bool) or base == "":
            raise bad("clan.version is required: a change records the version it read")
        mod, handler = registry.resolve(task, clan)
        req = Req(doc, base, clan, inp, handler)

        if mod.KIND == "answer":
            job = self.jobs.get(inp.get("job_id"), scope, doc)
            if job.task != "start_campaign":
                raise TaskError(409, "job_state", "that job does not wait for answers")
            mod.answer(job, req)
            return self.campaign_envelope(job, job.reply_change(clan))
        if mod.KIND == "campaign":
            if self.jobs.unfinished_campaign(scope, doc):
                raise TaskError(409, "job_state", "a start_campaign job on this document is still running or "
                                                  "waiting for an answer; one composition of a document at a time")
            caps = self.caps(handler)
            job = mod.start(req, caps, self.settings, self.jobs.new_id())
            self.jobs.add(job)
            job.start()
            return self.campaign_envelope(job, None)
        if mod.KIND == "short":
            if task == "compose_report" and self.jobs.unfinished_campaign(scope, doc):
                raise TaskError(409, "job_state", "a start_campaign job on this document is unfinished; one "
                                                  "composition of a document at a time")
            caps = self.caps(handler)
            jid = self.jobs.new_id()
            t0 = iso()
            if task == "compose_report":
                result, change, hits = mod.run(req, caps, jid)
            else:
                result, change, hits = mod.run(req, caps)
            job = {"id": jid, "state": "done", "progress": {"done": 1, "total": 1}, "started_at": t0,
                   "finished_at": iso(), "error": None}
            if task == "compose_report":
                job.update(stage="report", question=None)
            return self.envelope(task, handler, job, result, change, caps, hits)
        # long
        caps = self.caps(handler)
        total, work = mod.prepare(req, caps, self.settings)
        job = LongJob(self.jobs.new_id(), task, handler, doc, scope, caps, total, work)
        self.jobs.add(job)
        job.start()
        return self.envelope(task, handler, {**job.view(), "state": "queued", "progress": {"done": 0, "total": total}},
                             {"summary": f"queued: {total} run(s)"}, None, caps)


def _long_summary(job) -> str:
    v = job.view()
    if job.state == "failed":
        return f"failed: {job.error['message']}"
    return f"{v['progress']['done']} of {v['progress']['total']} run(s) done"


def _has_scope(obj) -> bool:
    return isinstance(obj, dict) and any(k in obj for k in ("scope", "tenant", "org", "org_id", "tenant_id"))


def create_app(settings: Settings | None = None, **components) -> FastAPI:
    settings = settings or Settings.from_env()
    mw = Middleware(settings, **components)
    app = FastAPI(title="napkin-middleware", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.mw = mw

    def err(status, etype, message):
        return JSONResponse({"error": {"type": etype, "message": message}}, status_code=status)

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "api": API, "backend": BACKEND, "model": settings.model,
                "research": bool(mw.research_port), "stages": STAGES}

    @app.post("/v1/tasks")
    async def tasks(request: Request):
        if settings.token:
            auth = request.headers.get("authorization", "")
            given = auth[7:] if auth.lower().startswith("bearer ") else request.headers.get("x-api-key", "")
            if given != settings.token:
                return err(401, "unauthenticated", "missing or wrong credentials")
        raw = await request.body()
        if not raw or len(raw) > MAX_BODY:
            return err(400, "invalid_input", "empty or oversized body")
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return err(400, "invalid_input", "the body is not JSON")
        task = (body.get("payload") or {}).get("task", "?") if isinstance(body, dict) else "?"
        try:
            import anyio
            out = await anyio.to_thread.run_sync(mw.handle, body)
        except TaskError as e:
            log.info("%s %s %s", e.status, task, e.etype)
            return err(e.status, e.etype, e.message)
        except Exception as e:
            log.error("500 %s %s: %s", task, type(e).__name__, e, exc_info=True)
            return err(500, "internal", "the middleware failed to handle this task")
        log.info("200 %s %s job=%s", task, out["handler"], out["job"]["state"])
        return JSONResponse(out)

    @app.exception_handler(404)
    async def not_found(request, exc):
        return err(404, "not_found", "no such route")

    return app


def main():
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    if settings.port in (8080, 8790):
        raise SystemExit(f"refusing to bind port {settings.port}")
    app = create_app(settings)
    log.info("%s on http://%s:%d/v1/tasks scope=%s model=%s research=%s layers=%s", API, settings.host,
             settings.port, settings.scope, settings.model, settings.research_url or "none", settings.layers)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="warning")


if __name__ == "__main__":
    main()
