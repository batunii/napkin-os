"""POST /v1/messages answered by headless Claude Code.

    python -m mock_llm            # listens on 127.0.0.1:8791
    ANTHROPIC_BASE_URL=http://127.0.0.1:8791 ANTHROPIC_API_KEY=dummy <middleware>

Swapping to the real API is unsetting ANTHROPIC_BASE_URL and setting a real key.
See README.md for env vars and the divergences from the real API.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .translate import ApiError, CliCall, build_call, message_id, parse_body, to_response


def _env_int(name: str, default: int) -> int:
    v = os.environ.get(name, "").strip()
    return int(v) if v else default


def _env_float(name: str, default: float) -> float:
    v = os.environ.get(name, "").strip()
    return float(v) if v else default


HOST = os.environ.get("MOCK_LLM_HOST", "127.0.0.1")
PORT = _env_int("MOCK_LLM_PORT", 8791)
CONCURRENCY = _env_int("MOCK_LLM_CONCURRENCY", 4)
TIMEOUT = _env_float("MOCK_LLM_TIMEOUT", 180.0)            # per subprocess, seconds -> 504
QUEUE_TIMEOUT = _env_float("MOCK_LLM_QUEUE_TIMEOUT", TIMEOUT)  # waiting for a slot -> 529
MAX_BODY = _env_int("MOCK_LLM_MAX_BODY_BYTES", 32 * 1024 * 1024)  # the real API's cap -> 413
DRAIN_LIMIT = 256 * 1024 * 1024  # an oversize body up to this is read and discarded, never kept
MAX_TURNS = _env_int("MOCK_LLM_MAX_TURNS", 4)  # structured output costs the CLI 2 turns
CLAUDE_BIN = os.environ.get("MOCK_LLM_CLAUDE_BIN", "claude")

# Request bodies carry client-confidential material (briefs, attachments,
# grounding). They are NEVER written to disk unless this explicitly named
# variable is set to a directory. Do not turn it on for convenience, and do not
# add a default: an always-on dump is how client data ends up in a bug report.
DUMP_DIR = os.environ.get("MOCK_LLM_DUMP_REQUEST_BODIES_TO", "").strip()

_slots = threading.BoundedSemaphore(CONCURRENCY)
_inflight = 0
_inflight_peak = 0
_inflight_lock = threading.Lock()


def log(msg: str) -> None:
    print(f"[mock-llm] {msg}", file=sys.stderr, flush=True)


def _dump(req_id: str, name: str, data: bytes) -> None:
    if not DUMP_DIR:
        return
    try:
        d = Path(DUMP_DIR) / req_id
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_bytes(data)
    except OSError as e:
        log(f"dump failed ({e}); continuing")


def child_env() -> dict:
    env = dict(os.environ)
    # The CLI must reach the real backend, not route back into this mock when
    # the mock was started from a shell that points the SDK at it.
    env.pop("ANTHROPIC_BASE_URL", None)
    return env


def claude_cmd(call: CliCall, system_fd: int | None) -> list[str]:
    cmd = [
        CLAUDE_BIN, "-p",
        "--model", call.alias,
        "--output-format", "json",
        "--tools", "",                    # no tools: a single completion
        "--max-turns", str(MAX_TURNS),
        "--no-session-persistence",       # stateless; never --resume, never a session id
        "--setting-sources", "",          # ignore user/project settings, hooks, CLAUDE.md
        "--strict-mcp-config",            # and every MCP server
        "--disable-slash-commands",
    ]
    # --system-prompt-file replaces Claude Code's agentic system prompt. It reads
    # from a pipe fd so the system text is never in argv (`ps`) or on disk.
    cmd += ["--system-prompt-file", f"/dev/fd/{system_fd}"] if system_fd is not None \
        else ["--system-prompt", "You are a helpful assistant."]
    if call.json_schema is not None:
        cmd += ["--json-schema", json.dumps(call.json_schema, separators=(",", ":"))]
    if call.effort:
        cmd += ["--effort", call.effort]
    return cmd


def run_claude(call: CliCall) -> dict:
    """One fresh subprocess; returns the parsed JSON envelope. Raises ApiError."""
    rfd = wfd = None
    if call.system is not None:
        rfd, wfd = os.pipe()
    try:
        with tempfile.TemporaryDirectory(prefix="mock-llm-") as cwd:  # empty: nothing to read
            try:
                proc = subprocess.Popen(
                    claude_cmd(call, rfd), cwd=cwd, env=child_env(),
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    pass_fds=(rfd,) if rfd is not None else (),
                    start_new_session=True,  # so a timeout can kill the whole group
                    text=True, encoding="utf-8", errors="replace",
                )
            except FileNotFoundError:
                raise ApiError(500, "api_error", f"claude binary not found: {CLAUDE_BIN}") from None
            writer = None
            if rfd is not None:
                os.close(rfd)
                rfd = None
                sys_bytes = call.system.encode("utf-8")
                w = wfd
                wfd = None

                def _write():
                    try:
                        with os.fdopen(w, "wb") as f:
                            f.write(sys_bytes)
                    except (BrokenPipeError, OSError):
                        pass
                writer = threading.Thread(target=_write, daemon=True)
                writer.start()
            try:
                out, err = proc.communicate(input=call.prompt, timeout=TIMEOUT)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.communicate()
                raise ApiError(504, "api_error",
                               f"claude CLI did not answer within {TIMEOUT:g}s (MOCK_LLM_TIMEOUT)") from None
            if writer:
                writer.join(timeout=5)
    finally:
        for fd in (rfd, wfd):
            if fd is not None:
                os.close(fd)
    try:
        envelope = json.loads(out)
    except json.JSONDecodeError:
        tail = (err or out or "(no output)").strip()[-500:]
        raise ApiError(500, "api_error", f"claude CLI exited {proc.returncode} without a JSON envelope: {tail}") from None
    if not isinstance(envelope, dict):
        raise ApiError(500, "api_error", "claude CLI envelope is not an object")
    return envelope


class Handler(BaseHTTPRequestHandler):
    server_version = "mock-llm/0.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet default access log; we log our own line
        pass

    def _send(self, status: int, body: dict, req_id: str, close: bool = False) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("request-id", req_id)
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        req_id = "req_" + message_id()[4:]
        if self.path.rstrip("/") == "/health":
            with _inflight_lock:
                body = {"ok": True, "concurrency": CONCURRENCY, "inflight": _inflight,
                        "inflight_peak": _inflight_peak, "timeout_s": TIMEOUT}
            return self._send(200, body, req_id)
        self._send(404, ApiError(404, "not_found_error", f"Not found: GET {self.path}").body(), req_id)

    def do_POST(self):
        global _inflight, _inflight_peak
        req_id = "req_" + message_id()[4:]
        t0 = time.monotonic()
        status, model = 0, "-"
        try:
            if self.path.split("?")[0].rstrip("/") != "/v1/messages":
                # The body is not read: answer and close the connection.
                raise ApiError(404, "not_found_error", f"Not found: POST {self.path}")
            length = self.headers.get("Content-Length")
            if length is None:
                raise ApiError(411 if "chunked" in (self.headers.get("Transfer-Encoding") or "") else 400,
                               "invalid_request_error", "Content-Length is required")
            try:
                n = int(length)
            except ValueError:
                raise ApiError(400, "invalid_request_error", "Content-Length is not an integer") from None
            if n > MAX_BODY:
                # Never buffer it. Discard it in chunks so a client that writes the
                # whole body before reading still sees the 413 rather than a
                # broken pipe; past a hard limit, just close on it.
                if n <= DRAIN_LIMIT:
                    left = n
                    while left > 0:
                        chunk = self.rfile.read(min(left, 1 << 16))
                        if not chunk:
                            break
                        left -= len(chunk)
                raise ApiError(413, "request_too_large",
                               f"request body is {n} bytes; the cap is {MAX_BODY} (MOCK_LLM_MAX_BODY_BYTES)")
            raw = self.rfile.read(n)
            _dump(req_id, "request.json", raw)
            body = parse_body(raw)
            model = str(body.get("model", "-"))
            call = build_call(body)
            if not _slots.acquire(timeout=QUEUE_TIMEOUT):
                raise ApiError(529, "overloaded_error",
                               f"all {CONCURRENCY} claude slots busy for {QUEUE_TIMEOUT:g}s (MOCK_LLM_CONCURRENCY)")
            try:
                with _inflight_lock:
                    _inflight += 1
                    _inflight_peak = max(_inflight_peak, _inflight)
                envelope = run_claude(call)
            finally:
                with _inflight_lock:
                    _inflight -= 1
                _slots.release()
            _dump(req_id, "envelope.json", json.dumps(envelope).encode("utf-8"))
            resp = to_response(envelope, call, body["model"])
            status = 200
            self._send(200, resp, req_id)
        except ApiError as e:
            status = e.status
            self._send(e.status, e.body(), req_id, close=e.status in (404, 411, 413))
        except Exception as e:  # never crash the server on one bad request
            status = 500
            log(f"{req_id} internal error: {type(e).__name__}: {e}")
            self._send(500, ApiError(500, "api_error", f"mock-llm internal error: {type(e).__name__}").body(),
                       req_id, close=True)
        finally:
            # Metadata only: never the prompt or the reply.
            log(f"{req_id} POST {self.path} model={model} -> {status} in {time.monotonic() - t0:.2f}s")


def startup_check() -> None:
    if shutil.which(CLAUDE_BIN) is None:
        log(f"the `{CLAUDE_BIN}` binary is not on PATH. Install Claude Code and log in:\n"
            "    curl -fsSL https://claude.ai/install.sh | bash   # or: npm install -g @anthropic-ai/claude-code\n"
            "    claude    # then /login\n"
            "or point MOCK_LLM_CLAUDE_BIN at it.")
        sys.exit(1)


def main() -> None:
    startup_check()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    httpd.daemon_threads = True
    log(f"listening on http://{HOST}:{PORT}  (claude={shutil.which(CLAUDE_BIN)}, "
        f"concurrency={CONCURRENCY}, timeout={TIMEOUT:g}s, max_body={MAX_BODY}B)")
    log("usage comes from the claude CLI envelope and is zero where absent - never estimated; "
        "cache_read/cache_creation_input_tokens are always 0; stop_reason is always end_turn")
    if DUMP_DIR:
        log(f"WARNING: MOCK_LLM_DUMP_REQUEST_BODIES_TO is set; request bodies (client-confidential) "
            f"are being written to {DUMP_DIR}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
