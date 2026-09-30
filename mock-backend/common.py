"""What every family of the mock backend shares: configuration, the request and
response objects, the one Claude semaphore, the `claude -p` runner, the disk
cache and the log line.

No middleware logic lives here or anywhere in the mock (contract 5, §5.6):
peripherals store, search, fetch and generate.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

HERE = Path(__file__).resolve().parent
REPO = HERE.parent


def env_int(name: str, default: int) -> int:
    v = os.environ.get(name, "").strip()
    return int(v) if v else default


def env_float(name: str, default: float) -> float:
    v = os.environ.get(name, "").strip()
    return float(v) if v else default


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() not in ("", "0", "false", "no", "off")


_SCRATCH = Path("/tmp/claude-1000/-home-batunii-Documents-Code-napkin-os/"
                "8548b27c-f3fd-4056-9479-bb6b716c6517/scratchpad")


def _default_data() -> Path:
    # The documented default is the session scratchpad; elsewhere (CI, another
    # machine) fall back to the system temp directory.
    if _SCRATCH.is_dir():
        return _SCRATCH / "mock-backend"
    return Path(tempfile.gettempdir()) / "napkin-mock-backend"


FAMILIES = ("model", "research", "retrieval", "layers")
CLAUDE_FAMILIES = ("model", "research", "retrieval")


class Config:
    """Read from the environment once per process (tests start a fresh one)."""

    def __init__(self) -> None:
        self.host = os.environ.get("MOCK_BACKEND_HOST", "127.0.0.1")
        self.port = env_int("MOCK_BACKEND_PORT", 8797)
        raw = os.environ.get("MOCK_FAKES", ",".join(FAMILIES))
        fakes = [f.strip() for f in raw.split(",") if f.strip()]
        unknown = [f for f in fakes if f not in FAMILIES]
        if unknown:
            raise SystemExit(f"MOCK_FAKES: unknown famil{'ies' if len(unknown) > 1 else 'y'} "
                             f"{', '.join(unknown)} (known: {', '.join(FAMILIES)})")
        self.fakes = tuple(f for f in FAMILIES if f in fakes)
        self.concurrency = max(1, env_int("MOCK_CONCURRENCY", 8))
        self.timeout = {
            "model": env_float("MOCK_TIMEOUT_MODEL", 180.0),
            "research": env_float("MOCK_TIMEOUT_RESEARCH", 420.0),
            "retrieval": env_float("MOCK_TIMEOUT_RETRIEVAL", 180.0),
        }
        q = os.environ.get("MOCK_QUEUE_TIMEOUT", "").strip()
        self.queue_timeout = float(q) if q else None  # None = the family's timeout
        self.max_body = env_int("MOCK_MAX_BODY_BYTES", 32 * 1024 * 1024)
        self.max_turns = env_int("MOCK_MAX_TURNS", 4)
        self.claude_bin = os.environ.get("MOCK_CLAUDE_BIN", "claude")
        self.research_model = os.environ.get("MOCK_RESEARCH_MODEL", "sonnet")
        self.retrieval_model = os.environ.get("MOCK_RETRIEVAL_MODEL", "sonnet")
        self.retrieval_max_chars = env_int("MOCK_RETRIEVAL_MAX_CHARS", 150_000)
        self.packs_dir = Path(os.environ.get("MOCK_PACKS_DIR") or REPO / "engine" / "packs_dist")
        self.brief_corpus = os.environ.get("BRIEF_CORPUS", "").strip() or None
        self.agency_packs_dir = os.environ.get("MOCK_AGENCY_PACKS_DIR", "").strip() or None
        self.data = Path(os.environ.get("MOCK_DATA", "").strip() or _default_data())
        self.layers_db = os.environ.get("MOCK_LAYERS_DB", "").strip() or str(self.data / "layers.sqlite")
        self.no_cache = env_flag("MOCK_NO_CACHE")
        # Where the research and retrieval answers are cached. Default: under MOCK_DATA. Point it at a
        # run's own directory to record that run's web answers, or at an earlier run's to replay them.
        self.cache_root = Path(os.environ.get("MOCK_CACHE_ROOT", "").strip() or self.data)
        self.max_budget = os.environ.get("MOCK_MAX_BUDGET_USD", "").strip() or None
        self.token = os.environ.get("MOCK_TOKEN", "").strip() or None
        # Request bodies carry client-confidential material. They are NEVER
        # written anywhere unless this explicitly named variable is set.
        self.dump_dir = os.environ.get("MOCK_DUMP_REQUEST_BODIES_TO", "").strip() or None

    def queue_timeout_for(self, family: str) -> float:
        return self.queue_timeout if self.queue_timeout is not None else self.timeout[family]


# ----------------------------------------------------------------- requests


@dataclass
class Request:
    method: str
    path: str                      # the raw path, query string removed
    query: dict                    # parse_qs result
    headers: dict                  # lower-cased names
    body: bytes = b""
    req_id: str = "-"

    @classmethod
    def build(cls, method: str, target: str, headers: dict, body: bytes = b"", req_id: str = "-") -> "Request":
        parts = urlsplit(target)
        return cls(method.upper(), parts.path, parse_qs(parts.query, keep_blank_values=True),
                   {k.lower(): v for k, v in headers.items()}, body, req_id)

    def header(self, name: str) -> str | None:
        v = self.headers.get(name.lower())
        return v.strip() if isinstance(v, str) else None

    def q(self, name: str) -> str | None:
        v = self.query.get(name)
        return v[0] if v else None

    def segments(self) -> list[str]:
        return [unquote(s) for s in self.path.split("/") if s]


@dataclass
class Response:
    status: int
    body: dict
    headers: dict = field(default_factory=dict)
    close: bool = False
    note: str = ""                 # metadata for the log line (never content)


def json_response(status: int, body: dict, **headers) -> Response:
    return Response(status, body, dict(headers))


class PeripheralError(Exception):
    """research / retrieval / layers error shape: {"error": {"type", "message"}}.
    The message never carries request content."""

    def __init__(self, status: int, type_: str, message: str, headers: dict | None = None):
        super().__init__(message)
        self.status, self.type, self.message = status, type_, message
        self.headers = headers or {}

    def response(self) -> Response:
        return Response(self.status, {"error": {"type": self.type, "message": self.message}}, dict(self.headers))


def parse_json_object(raw: bytes) -> dict:
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise PeripheralError(400, "invalid_input", f"body is not JSON: {type(e).__name__}") from None
    if not isinstance(body, dict):
        raise PeripheralError(400, "invalid_input", "body must be a JSON object")
    return body


SCOPE_KEYS = ("scope", "org", "org_id", "tenant", "tenant_id", "brand_scope")


def refuse_scope_in_body(body: dict) -> None:
    named = [k for k in SCOPE_KEYS if k in body]
    if named:
        raise PeripheralError(400, "invalid_input",
                              f"scope travels in the X-Napkin-Org / X-Napkin-Brand headers, never in the body "
                              f"(found `{named[0]}`)")


# ----------------------------------------------------------------- logging


def log(msg: str) -> None:
    print(f"[mock-backend {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def dump(cfg: Config, req_id: str, name: str, data: bytes) -> None:
    if not cfg.dump_dir:
        return
    try:
        d = Path(cfg.dump_dir) / req_id
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_bytes(data)
    except OSError as e:
        log(f"dump failed ({e}); continuing")


# ----------------------------------------------------------------- claude


class Slots:
    """One semaphore of MOCK_CONCURRENCY Claude subprocesses across all families."""

    def __init__(self, n: int):
        self.n = n
        self._sem = threading.BoundedSemaphore(n)
        self._lock = threading.Lock()
        self.in_flight = 0
        self.peak = 0

    def acquire(self, timeout: float) -> bool:
        if not self._sem.acquire(timeout=timeout):
            return False
        with self._lock:
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
        return True

    def release(self) -> None:
        with self._lock:
            self.in_flight -= 1
        self._sem.release()


class ClaudeFailure(Exception):
    """kind: spawn | timeout | no_envelope"""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind, self.message = kind, message


@dataclass
class ClaudeCall:
    alias: str
    prompt: str                           # stdin, never argv
    system: str | None = None             # a pipe fd, never argv or disk; None = the CLI's own
    json_schema: dict | None = None
    effort: str | None = None
    images: list = field(default_factory=list)  # [(media_type, base64 data)]
    tools: list = field(default_factory=list)   # [] = no tools
    max_turns: int | None = None
    permission_mode: str | None = None
    trace_tools: bool = False                    # stream the CLI's events so tool uses can be counted


def child_env() -> dict:
    env = dict(os.environ)
    # The CLI must reach the real backend, never route back into this mock.
    env.pop("ANTHROPIC_BASE_URL", None)
    env.pop("OPENAI_BASE_URL", None)
    return env


def _streams(call: ClaudeCall) -> bool:
    """Whether the CLI's events are streamed back: for images, for research (tool counts), and, with
    MOCK_TRACE_MODEL=1, for any call that carries a schema, so a schema rejection can be recorded."""
    return bool(call.images) or call.trace_tools or bool(os.environ.get("MOCK_TRACE_MODEL") and call.json_schema is not None)


def claude_argv(cfg: Config, call: ClaudeCall, system_fd: int | None) -> list[str]:
    stream = _streams(call)
    cmd = [cfg.claude_bin, "-p", "--model", call.alias]
    if call.images:
        # Image blocks only travel through stream-json input, which requires
        # stream-json output (verified against Claude Code 2.1.281).
        cmd += ["--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]
    elif stream:
        cmd += ["--output-format", "stream-json", "--verbose"]  # text stdin, event stdout
    else:
        cmd += ["--output-format", "json"]
    cmd += ["--no-session-persistence", "--setting-sources", "", "--strict-mcp-config",
            "--disable-slash-commands"]
    if call.max_turns:
        cmd += ["--max-turns", str(call.max_turns)]
    if system_fd is not None:
        cmd += ["--system-prompt-file", f"/dev/fd/{system_fd}"]
    if call.json_schema is not None:
        cmd += ["--json-schema", json.dumps(call.json_schema, separators=(",", ":"))]
    if call.effort:
        cmd += ["--effort", call.effort]
    if call.permission_mode:
        cmd += ["--permission-mode", call.permission_mode]
    if cfg.max_budget:
        cmd += ["--max-budget-usd", cfg.max_budget]
    # Variadic flags last; the prompt goes on stdin so they cannot swallow it.
    if call.tools:
        cmd += ["--tools", *call.tools, "--allowedTools", *call.tools]
    else:
        cmd += ["--tools", ""]
    return cmd


def _stdin_for(call: ClaudeCall) -> str:
    if not call.images:
        return call.prompt
    content = [{"type": "image", "source": {"type": "base64", "media_type": mt, "data": data}}
               for mt, data in call.images]
    content.append({"type": "text", "text": call.prompt})
    return json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n"


def _envelope_from(out: str, stream: bool):
    if not stream:
        return json.loads(out)
    last = None
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict) and ev.get("type") == "result":
            last = ev
    if last is None:
        raise json.JSONDecodeError("no result event", out, 0)
    return last


def record_cache_hit(cfg: Config, req: dict) -> None:
    """A research answer served from the disk cache: no subprocess ran, so no cost."""
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()), **CALL_CTX.get(), "family": "research",
           "lens": req.get("lens"), "market": req.get("market"), "cached": True, "secs": 0, "cost_usd": 0}
    try:
        with _LEDGER_LOCK, open(cfg.data / "metrics.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


CALL_CTX: contextvars.ContextVar = contextvars.ContextVar("call_ctx", default={})
_LEDGER_LOCK = threading.Lock()


def _schema_errors(out: str) -> list[str]:
    """The CLI's structured-output rejections in a stream-json run: the path and rule it names, cut short
    (the allowed-values list it prints can be long). Each one is a turn the model had to redo."""
    found = []
    for line in out.splitlines():
        if "does not match required schema" not in line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        content = ((ev.get("message") or {}).get("content")) if isinstance(ev, dict) else None
        for b in content if isinstance(content, list) else []:
            text = b.get("content") if isinstance(b, dict) else None
            if isinstance(text, str) and "does not match required schema" in text:
                found.append(text.replace("Output does not match required schema: ", "")[:160])
    return found


def _turn_trace(out: str) -> list[dict]:
    """One entry per model message in a stream-json run: what it held (thinking, text, tool_use name), the
    tokens it wrote and why it stopped, plus the first words of any tool result the CLI sent back. Shows what
    each extra turn was."""
    turns, seen = [], {}
    for line in out.splitlines():
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = ev.get("message") if isinstance(ev, dict) else None
        if not isinstance(msg, dict):
            continue
        if ev.get("type") == "assistant":
            mid = msg.get("id")
            if mid not in seen:
                seen[mid] = {"blocks": [], "out": None, "stop": None}
                turns.append(seen[mid])
            t = seen[mid]
            t["blocks"] += [b.get("name") or b.get("type") for b in msg.get("content") or [] if isinstance(b, dict)]
            t["out"] = (msg.get("usage") or {}).get("output_tokens", t["out"])
            t["stop"] = msg.get("stop_reason") or t["stop"]
        elif ev.get("type") == "user":
            for b in msg.get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    c = b.get("content")
                    turns.append({"tool_result": (c if isinstance(c, str) else json.dumps(c))[:120]})
    return turns


def _tool_uses(out: str) -> dict:
    """{tool name: count} from a stream-json run's assistant events."""
    counts: dict[str, int] = {}
    for line in out.splitlines():
        if '"tool_use"' not in line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        content = ((ev.get("message") or {}).get("content")) if isinstance(ev, dict) else None
        for b in content if isinstance(content, list) else []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                counts[b.get("name", "?")] = counts.get(b.get("name", "?"), 0) + 1
    return counts


def _by_model(usage) -> dict | None:
    """{model id: {in, out, cache_read, cache_write, cost}} from the CLI's `modelUsage`. The CLI runs a
    second, smaller model behind WebSearch and WebFetch; its spend is in `total_cost_usd` but not in the
    main `usage` block, so only this split shows where a research unit's money went."""
    if not isinstance(usage, dict):
        return None
    return {m: {"in": u.get("inputTokens"), "out": u.get("outputTokens"), "cache_read": u.get("cacheReadInputTokens"),
                "cache_write": u.get("cacheCreationInputTokens"), "cost": u.get("costUSD")}
            for m, u in usage.items() if isinstance(u, dict)}


def record_call(cfg: Config, call: ClaudeCall, secs: float, envelope: dict | None, ok: bool, tools: dict | None,
                failure: str | None = None, schema_errors: list | None = None,
                turn_trace: list | None = None) -> None:
    """One JSON line per `claude -p` subprocess in <MOCK_DATA>/metrics.jsonl:
    who asked (handler, job, from the request headers), what for (the model
    purpose, or the research lens and market, read from the prompt), how long,
    what the CLI reported it cost, its tokens and the tools it used. Metadata
    only: never a prompt or a reply."""
    env = envelope or {}
    u = env.get("usage") if isinstance(env.get("usage"), dict) else {}
    m = re.match(r"Task: (\S+)", call.prompt)
    lens, market = re.search(r"^Lens: (\w+)", call.prompt, re.M), re.search(r"^Market: (\w+)", call.prompt, re.M)
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()), **CALL_CTX.get(),
           "family": "research" if call.tools else "model", "purpose": m.group(1) if m else None,
           "lens": lens.group(1) if lens else None, "market": market.group(1) if market else None,
           "alias": call.alias, "secs": round(secs, 2), "ok": ok, "failure": failure,
           "cost_usd": env.get("total_cost_usd"), "turns": env.get("num_turns"),
           "in_fresh": u.get("input_tokens"), "cache_write": u.get("cache_creation_input_tokens"),
           "cache_read": u.get("cache_read_input_tokens"), "out": u.get("output_tokens"),
           "prompt_chars": len(call.prompt), "system_chars": len(call.system or ""),
           "web_searches": (tools or {}).get("WebSearch"), "web_fetches": (tools or {}).get("WebFetch"),
           "server_tool_use": u.get("server_tool_use"),
           "by_model": _by_model(env.get("modelUsage")), "schema_rejections": schema_errors or None,
           "turn_trace": turn_trace if (turn_trace and len(turn_trace) > 2) else None,
           "tools": tools}
    try:
        with _LEDGER_LOCK, open(cfg.data / "metrics.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def run_claude(cfg: Config, call: ClaudeCall, timeout: float, cwd_root: Path | None = None) -> dict:
    """Runs one call and writes its ledger line (see `record_call`)."""
    t0 = time.monotonic()
    try:
        env = _run_claude(cfg, call, timeout, cwd_root)
    except ClaudeFailure as f:
        record_call(cfg, call, time.monotonic() - t0, None, False, None, f.kind)
        raise
    ok = not (env.get("_exit") or env.get("is_error"))
    record_call(cfg, call, time.monotonic() - t0, env, ok, env.pop("_tools", None), schema_errors=env.pop("_schema_errors", None),
                turn_trace=env.pop("_turn_trace", None))
    return env


def _run_claude(cfg: Config, call: ClaudeCall, timeout: float, cwd_root: Path | None = None) -> dict:
    """One fresh `claude -p` in an empty temporary directory; the parsed
    envelope. Raises ClaudeFailure. The caller holds a slot."""
    rfd = wfd = None
    if call.system is not None:
        rfd, wfd = os.pipe()
    stream = _streams(call)
    try:
        if cwd_root is not None:
            cwd_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="claude-", dir=cwd_root) as cwd:
            try:
                proc = subprocess.Popen(
                    claude_argv(cfg, call, rfd), cwd=cwd, env=child_env(),
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    pass_fds=(rfd,) if rfd is not None else (),
                    start_new_session=True,  # a timeout kills the whole group
                    text=True, encoding="utf-8", errors="replace")
            except OSError as e:
                raise ClaudeFailure("spawn", f"could not start the claude CLI ({cfg.claude_bin}): "
                                             f"{type(e).__name__}") from None
            writer = None
            if rfd is not None:
                os.close(rfd)
                rfd = None
                sys_bytes = call.system.encode("utf-8")
                w, wfd = wfd, None

                def _write():
                    try:
                        with os.fdopen(w, "wb") as f:
                            f.write(sys_bytes)
                    except OSError:
                        pass
                writer = threading.Thread(target=_write, daemon=True)
                writer.start()
            try:
                out, err = proc.communicate(input=_stdin_for(call), timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.communicate()
                raise ClaudeFailure("timeout", f"the claude CLI did not answer within {timeout:g}s") from None
            if writer:
                writer.join(timeout=5)
    finally:
        for fd in (rfd, wfd):
            if fd is not None:
                os.close(fd)
    try:
        envelope = _envelope_from(out, stream)
    except json.JSONDecodeError:
        # stderr can quote the prompt back in some failures; keep only its tail,
        # and never the stdout (which could be the model's reply).
        tail = (err or "").strip()[-300:]
        raise ClaudeFailure("no_envelope", f"the claude CLI exited {proc.returncode} without a JSON result"
                                           + (f": {tail}" if tail else "")) from None
    if not isinstance(envelope, dict):
        raise ClaudeFailure("no_envelope", "the claude CLI result is not an object")
    envelope.setdefault("_exit", proc.returncode)
    if call.trace_tools:
        envelope["_tools"] = _tool_uses(out)
    if stream:
        envelope["_schema_errors"] = _schema_errors(out)
        envelope["_turn_trace"] = _turn_trace(out)
    return envelope


# ----------------------------------------------------------------- cache


class DiskCache:
    """sha256-keyed JSON files under <MOCK_DATA>/cache/<family>/. Holds what
    the family's docstring says it holds, and never a failure."""

    def __init__(self, root: Path, family: str):
        self.dir = root / "cache" / family
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def lock(self, key: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    def get(self, key: str) -> dict | None:
        p = self.dir / f"{key}.json"
        if not p.is_file():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log(f"unreadable cache entry {p.name}; re-running")
            return None

    def put(self, key: str, value: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        p = self.dir / f"{key}.json"
        tmp = p.with_suffix(f".{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)


def sha256_hex(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
