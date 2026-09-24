#!/usr/bin/env python3
"""Napkin's ONE mock backend: a single process that fakes every peripheral the
middleware talks to (docs/contracts/peripherals.md, contract 5, §5).

    python3 mock-backend/server.py            # http://127.0.0.1:8797

| family    | routes                                           | answered by                      |
|-----------|--------------------------------------------------|----------------------------------|
| model     | POST /v1/messages, POST /v1/chat/completions,    | claude -p (Anthropic / OpenAI)   |
|           | GET /v1/models                                   |                                  |
| research  | POST /v1/research                                | claude -p + WebSearch, WebFetch  |
| retrieval | GET /v1/packs, POST /v1/retrieve                 | pack files + claude -p           |
| layers    | /v1/layers/...                                   | SQLite, no model                 |
| -         | GET /healthz                                     | what it fakes                    |
| -         | /v1/embeddings and anything else                 | 404                              |

It holds NO middleware logic: no tiering, confidence, merge, reasoning or
judging. Peripherals store, search, fetch and generate. Each family swaps to
the real service by changing one base URL in the middleware; MOCK_FAKES turns
families off here so a real peripheral can take one family's place.

Python 3.11+, standard library only. Environment: see README.md.
"""

from __future__ import annotations

import json
import secrets
import shutil
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import CLAUDE_FAMILIES, Config, PeripheralError, Request, Response, Slots, dump, log  # noqa: E402

SERVICE = "napkin-mock-backend"
FORBIDDEN_PORTS = {8080, 8090, 8787, 8788, 8790, 8791, 8792, 8795, 8796}
DRAIN_LIMIT = 256 * 1024 * 1024   # an oversize body up to this is read and discarded, never kept

MODEL_PATHS = {"/v1/messages", "/v1/chat/completions", "/v1/models"}
RETRIEVAL_PATHS = {"/v1/packs", "/v1/retrieve"}


def family_of(path: str) -> str | None:
    p = path.rstrip("/") or "/"
    if p in MODEL_PATHS:
        return "model"
    if p == "/v1/research":
        return "research"
    if p in RETRIEVAL_PATHS:
        return "retrieval"
    if p == "/v1/layers" or p.startswith("/v1/layers/"):
        return "layers"
    return None


class Backend:
    """The routing core, independent of the socket (tests drive it directly)."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.slots = Slots(cfg.concurrency)
        self.families: dict = {}
        if "research" in cfg.fakes:
            from research_port import Research
            self.families["research"] = Research(cfg, self.slots)
        if "retrieval" in cfg.fakes:
            from retrieval_port import Retrieval
            self.families["retrieval"] = Retrieval(cfg, self.slots)
        if "layers" in cfg.fakes:
            from layers_port import LayersApp
            self.families["layers"] = LayersApp(cfg.layers_db)

    def health(self) -> dict:
        fakes = {}
        if "model" in self.cfg.fakes:
            import model_port
            fakes["model"] = {"api": "napkin.model/1", "shapes": ["anthropic", "openai"],
                              "models": dict(model_port.MODELS), "images": True, "embeddings": False}
        for name in ("research", "retrieval", "layers"):
            if name in self.families:
                fakes[name] = self.families[name].health()
        return {"ok": True, "service": SERVICE, "fakes": fakes, "concurrency": self.slots.n,
                "in_flight": self.slots.in_flight, "peak": self.slots.peak}

    def authorised(self, req: Request) -> bool:
        tok = self.cfg.token
        return not tok or req.header("authorization") == f"Bearer {tok}"

    def handle(self, req: Request) -> Response:
        path = req.path.rstrip("/") or "/"
        if path == "/healthz":
            if not self.authorised(req):
                return PeripheralError(401, "unauthenticated", "a bearer token is required").response()
            if req.method != "GET":
                return PeripheralError(405, "method_not_allowed", "use GET").response()
            return Response(200, self.health())
        fam = family_of(path)
        if fam is None or fam not in self.cfg.fakes:
            why = "embeddings belong to the retrieval service; the mock serves none" \
                if path.startswith("/v1/embeddings") else f"no route {req.path}"
            return PeripheralError(404, "not_found", why).response()
        if fam == "model":
            import model_port   # the model family answers auth in its wire's own shape
            return model_port.handle(self.cfg, self.slots, req)
        if not self.authorised(req):
            return PeripheralError(401, "unauthenticated", "a bearer token is required").response()
        return self.families[fam].handle(req)


def make_handler(backend: Backend):
    cfg = backend.cfg

    class Handler(BaseHTTPRequestHandler):
        server_version = "napkin-mock-backend/1"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):   # our own metadata line instead
            pass

        def _send(self, resp: Response, req_id: str) -> None:
            data = json.dumps(resp.body, ensure_ascii=False).encode("utf-8")
            self.send_response(resp.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("request-id", req_id)
            for k, v in resp.headers.items():
                self.send_header(k, v)
            if resp.close:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            self.wfile.write(data)

        def _error_for(self, path: str, status: int, message: str) -> Response:
            fam = family_of(path.split("?")[0])
            if fam == "model":
                import model_port
                e = model_port.WireError(status, "request_too_large" if status == 413 else "invalid_request_error",
                                         message)
                r = e.anthropic() if path.startswith("/v1/messages") else e.openai()
            else:
                r = PeripheralError(status, "invalid_input", message).response()
            r.close = True
            return r

        def _read_body(self) -> tuple[bytes | None, Response | None]:
            if self.command in ("GET", "HEAD", "DELETE") and not self.headers.get("Content-Length"):
                return b"", None
            length = self.headers.get("Content-Length")
            if length is None:
                if "chunked" in (self.headers.get("Transfer-Encoding") or ""):
                    return None, self._error_for(self.path, 411, "Content-Length is required")
                return b"", None
            try:
                n = int(length)
            except ValueError:
                return None, self._error_for(self.path, 400, "Content-Length is not an integer")
            if n > cfg.max_body:
                # Never buffer it: discard in chunks so a client that writes the whole
                # body before reading still sees the 413; past a hard limit, close.
                if n <= DRAIN_LIMIT:
                    left = n
                    while left > 0:
                        chunk = self.rfile.read(min(left, 1 << 16))
                        if not chunk:
                            break
                        left -= len(chunk)
                return None, self._error_for(self.path, 413, f"request body is {n} bytes; the cap is "
                                                             f"{cfg.max_body} (MOCK_MAX_BODY_BYTES)")
            return self.rfile.read(n), None

        def _serve(self):
            req_id = "req_" + secrets.token_hex(12)
            t0 = time.monotonic()
            status, note = 0, ""
            try:
                body, err = self._read_body()
                if err is not None:
                    resp = err
                else:
                    if body:
                        dump(cfg, req_id, "request.body", body)
                    req = Request.build(self.command, self.path, dict(self.headers.items()), body, req_id)
                    resp = backend.handle(req)
                status, note = resp.status, resp.note
                self._send(resp, req_id)
            except (BrokenPipeError, ConnectionResetError):
                status = status or 499
            except Exception as e:   # never crash the server on one request
                status = 500
                log(f"{req_id} internal error: {type(e).__name__}: {e}")
                try:
                    self._send(PeripheralError(500, "internal", f"internal error: {type(e).__name__}").response(),
                               req_id)
                except OSError:
                    pass
            finally:
                # Metadata only: never a request or response body.
                handler = self.headers.get("X-Napkin-Handler", "-")
                job = self.headers.get("X-Napkin-Job", "-")
                log(f"{req_id} {self.command} {self.path.split('?')[0]} -> {status} "
                    f"{time.monotonic() - t0:.2f}s handler={handler} job={job}" + (f" {note}" if note else ""))

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _serve

    return Handler


def startup_check(cfg: Config) -> None:
    if cfg.port in FORBIDDEN_PORTS:
        raise SystemExit(f"MOCK_BACKEND_PORT {cfg.port} is reserved for another server; use 8797 or a free port")
    needs = [f for f in cfg.fakes if f in CLAUDE_FAMILIES]
    if needs and shutil.which(cfg.claude_bin) is None:
        raise SystemExit(
            f"the `{cfg.claude_bin}` binary is not on PATH and these families need it: {', '.join(needs)}.\n"
            "Install Claude Code and sign in (`claude`, then /login), point MOCK_CLAUDE_BIN at it, or serve only "
            "the layers with MOCK_FAKES=layers.")


def main() -> None:
    cfg = Config()
    startup_check(cfg)
    cfg.data.mkdir(parents=True, exist_ok=True)
    backend = Backend(cfg)
    httpd = ThreadingHTTPServer((cfg.host, cfg.port), make_handler(backend))
    httpd.daemon_threads = True
    log(f"listening on http://{cfg.host}:{cfg.port}  fakes={','.join(cfg.fakes)}  concurrency={cfg.concurrency}  "
        f"data={cfg.data}")
    if cfg.dump_dir:
        log(f"WARNING: MOCK_DUMP_REQUEST_BODIES_TO is set; request bodies (client-confidential) are being written "
            f"to {cfg.dump_dir}. Delete it afterwards.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
