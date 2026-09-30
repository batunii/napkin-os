#!/usr/bin/env python3
"""
judge_nemotron.py — relevance validation by NVIDIA's hosted cross-encoder reranker.

    backend  = NemotronBackend()                       # raises BackendNotConfigured if no key
    verdicts = backend.score(Query("challenger brand, nervous CMO"),
                             [Passage("ipa_0042#insight", "..."), ...], deadline_s=3.0)

The model is nvidia/llama-nemotron-rerank-vl-1b-v2, the only NVIDIA reranker still served
to this account (its siblings return 410 end-of-life, and nv-rerank-qa-mistral-4b returns
404 "Not found for account"). It reads the query and each passage TOGETHER, which is
what a similarity search cannot do, and returns one logit per passage. On the held-out
golden set those logits separate relevant from irrelevant almost perfectly (pooled AUC
0.983, medians +7.4 and -7.4), which is why a threshold on them is a usable gate.

Why a probability only when a calibration file exists. A logit is not a probability, and
Verdict.score promises one. engine/rag/calibration/nemotron.json (Platt a, b and a
threshold, written by the calibration step) turns the logit into one; without it the
backend is honest about being uncalibrated: `score` stays None and `value` falls back to
the model's own decision boundary, logit > 0. The logit is always kept in `raw`.

Why a hard wall-clock deadline rather than a requests timeout alone. The pilot saw 5 of
154 calls hang past 20 s while the median was 0.49 s. A requests timeout bounds the
connect and each socket read separately, not the call: a server that trickles a byte
every half second resets the read timeout on every byte and can hold a call open
indefinitely, and DNS resolution is not covered at all. So the call runs in a daemon
worker thread and score() stops waiting at `deadline_s`, raising
BackendUnavailable(kind="timeout"). There is no retry here: a retry inside score() would
spend the next backend's time, and the chain already falls through.

What happens to the worker after score() gives up. Python cannot kill a thread, so the
worker is made to end itself where it can and is counted where it cannot:

  - The body is streamed (stream=True) in small read1() chunks, and the worker checks
    the wall clock and a cancel flag after every chunk, then closes the connection. Once
    headers have arrived, the worker outlives score() by at most one socket read, which
    the read timeout bounds at `deadline_s` (so ~2x deadline_s after the call started).
    A trickled body cannot keep it alive.
  - Before headers arrive — DNS, a connect per resolved address, a trickled status line
    or header block — requests gives no hook to check the clock, so that phase is NOT
    bounded in time. Instead every worker score() abandons is counted as stranded, and
    while MAX_STRANDED of them are still alive score() refuses to start another
    (kind="timeout", no request sent). A hung endpoint therefore costs at most
    MAX_STRANDED threads and sockets, however many queries arrive, instead of one per
    query. They end when the endpoint or the OS finally gives up on them.
  - A body larger than MAX_BODY_BYTES is abandoned as bad_response: a 40-passage reply
    is a few KB, so anything that large is not a rerank response.

Failure classes (judge_base.UNAVAILABLE_KINDS), measured against the live endpoint:

    410 "reached its end of life"   retired        change the configured model
    404 "Not found for account"     not_entitled   ask NVIDIA
    404 anything else               http_error     wrong URL or model name (our typo)
    403 "Authorization failed"      http_error     key revoked or wrong: ours to fix
    429                             rate_limited
    other 4xx / 5xx                 http_error
    connect / read timeout, hang    timeout
    MAX_STRANDED calls still hung   timeout        refused at once, no request sent
    other transport failure         error
    unparseable or misaligned body  bad_response
    body over MAX_BODY_BYTES        bad_response

Configuration (env):
    NVIDIA_API_KEY              required; read after engine/.env is loaded (via rag.py)
    RAG_NEMOTRON_CHARS          passage clip, default DEFAULT_CHARS
    RAG_NEMOTRON_QUERY_CHARS    query-plus-context clip, default DEFAULT_QUERY_CHARS
"""
from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:                    # let `import judge_base` resolve next to us
    sys.path.insert(0, str(HERE))

from judge_base import (BackendNotConfigured, BackendUnavailable, Calibration,  # noqa: E402
                        Passage, Query, Verdict, check_verdicts)
import judge_base  # noqa: E402  (shared loader and HTTP classifier)

try:                                             # the one non-stdlib dependency; checked at construction
    import requests
    from urllib3.exceptions import TimeoutError as _Urllib3Timeout   # requests' own transport
except ImportError:                              # pragma: no cover - requests ships with the engine
    requests = None
    _Urllib3Timeout = TimeoutError

NAME = "nemotron"
MODEL = "nvidia/llama-nemotron-rerank-vl-1b-v2"
URL = f"https://ai.api.nvidia.com/v1/retrieval/{MODEL}/reranking"

# Passages per call. Measured latency grows with the pool, roughly 0.08 s + 11 ms per
# passage (p50 0.20 s at 10, 0.32 s at 20, 0.52 s at 40), so 40 stays well inside the
# deadline, and 40 is the pool the pilot's recall and AUC figures were measured on.
# Going wider is untested, not refused.
CAPACITY = 40

# Passage clip. The chain puts the chunk header first in Passage.text, so clipping keeps
# the header and the opening of the body — what the pilot scored. 1500 keeps the whole
# of about half the pool's chunks (median header+text 1,444 chars). Latency is flat in
# the clip (pool-40 p50 0.42 s at 1000, 0.52 s at 1500 and at 2500), so the choice is
# made on evidence instead: the pilot's AUC 0.983 was measured at 1500, 2500 costs ~30%
# more tokens for no measured gain, and the one earlier run at 3000 hung past 60 s.
DEFAULT_CHARS = 1500

# Query clip. The query is paired with EVERY passage, so its length is paid 40 times:
# a 2000-char query-plus-context took a 40-passage call from ~6-8k to ~23-24k prompt
# tokens. 1000 chars keeps the whole retrieval query and a paragraph of brief context;
# Query.combined() truncates context before the query.
DEFAULT_QUERY_CHARS = 1000

# What the chain should pass as deadline_s for this backend at CAPACITY. Healthy calls
# at pool 40 measured p90 0.59 s (pilot, n=154) and 0.63 s (n=8, max 0.63 s); ~5x p90
# absorbs network jitter from a production host. A hang does not recover within
# seconds (the pilot's five ran past 20 s), so waiting longer only delays fallthrough.
RECOMMENDED_DEADLINE_S = 3.0

# Abandoned workers allowed to be alive at once before score() stops starting new calls.
# A worker is abandoned when score() times out while it is still running; healthy calls
# (p90 0.6 s) finish long before the deadline and are never counted, so concurrent
# healthy traffic is not limited by this. Small, because each one holds a thread and a
# socket for as long as the endpoint keeps it hung, and a hung endpoint does not answer
# the next call either: refusing is the same outcome (fall through) at no cost.
MAX_STRANDED = 4

# Streaming read size and body ceiling. read1() returns whatever has arrived, up to
# CHUNK_BYTES, so a trickling server still hands control back after each recv and the
# worker can check the clock. A 40-passage response is ~3-4 KB of JSON.
CHUNK_BYTES = 16 * 1024
MAX_BODY_BYTES = 1_000_000

CALIBRATION_PATH = HERE / "calibration" / f"{NAME}.json"

_warned_uncalibrated = False                     # log the uncalibrated fallback once per process
_warn_lock = threading.Lock()

_stranded = 0                                    # abandoned workers still alive (see MAX_STRANDED)
_stranded_lock = threading.Lock()


def stranded_workers() -> int:
    """How many workers score() has abandoned that are still running. For the run trace
    and for tests; the count only goes down when those workers actually exit."""
    with _stranded_lock:
        return _stranded


class _Reply:
    """The status and body of one finished HTTP exchange, read fully inside the worker.

    Plain data rather than a requests.Response so that nothing handed back to score()
    can touch the socket again: by the time score() sees it, the worker has closed it."""

    def __init__(self, status_code: int | None, body: bytes):
        """Keep the status and the raw body bytes exactly as received."""
        self.status_code = status_code
        self.body = body

    @property
    def text(self) -> str:
        """The body as text. JSON is UTF-8 by RFC 8259; stray bytes are replaced rather
        than raised, since the text is only parsed as JSON or quoted in an error."""
        return self.body.decode("utf-8", errors="replace")

    def json(self):
        """Parse the body as JSON, raising ValueError (json.JSONDecodeError) if it is not."""
        return json.loads(self.text)


class _BodyTooLarge(Exception):
    """Raised inside the worker when a response body passes MAX_BODY_BYTES."""


class _Cancelled(Exception):
    """Raised inside the worker when the wall-clock deadline passes mid-body."""


def _read_body(resp, deadline_at: float, cancelled: threading.Event) -> bytes:
    """Read a streamed response body in chunks, stopping at the wall-clock deadline.

    Each read1() returns as soon as some bytes arrive (or the read timeout fires), so the
    clock and the cancel flag are checked after every recv however slowly the server
    sends. Falls back to read(CHUNK_BYTES) on a urllib3 without read1(), where a trickled
    chunk can take longer to fill but the per-chunk check still ends the loop."""
    raw = resp.raw
    read = getattr(raw, "read1", None) or raw.read
    parts: list[bytes] = []
    size = 0
    while True:
        if cancelled.is_set() or time.monotonic() >= deadline_at:
            raise _Cancelled()
        chunk = read(CHUNK_BYTES, decode_content=True)
        if not chunk:
            return b"".join(parts)
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise _BodyTooLarge(f"body passed {MAX_BODY_BYTES} bytes")
        parts.append(chunk)


def _load_env() -> None:
    """Make engine/.env visible in os.environ exactly as the rest of the module sees it.

    rag.py loads engine/.env when it is imported (without overriding variables already
    set), so importing it is the one loader rather than a second copy of its parsing.
    Kept as a named function so tests can replace it and control the environment."""
    try:
        import rag  # noqa: F401  (imported for its _load_dotenv() side effect)
    except ImportError:
        # rag.py missing or broken: the key may still be in the environment, and if it is
        # not, the constructor raises BackendNotConfigured naming NVIDIA_API_KEY — a
        # configuration error, never a crash out of construction.
        pass


def _env_int(name: str, default: int) -> int:
    """A positive integer from the environment, or `default` when unset. A value that is
    set but not a positive integer is a configuration error, not something to guess at."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        v = 0
    if v <= 0:
        raise BackendNotConfigured(f"{NAME}: {name}={raw!r} must be a positive integer")
    return v


def _warn_uncalibrated(path: Path) -> None:
    """Say once per process that verdicts use the model's own boundary, not a threshold.
    Once, because the backend is built per run and the message would otherwise drown
    the log; stderr, because that is where this module reports everything else."""
    global _warned_uncalibrated
    with _warn_lock:
        if _warned_uncalibrated:
            return
        _warned_uncalibrated = True
    print(f"[judge_nemotron] no calibration at {path}; running uncalibrated "
          f"(score=None, value = logit > 0)", file=sys.stderr)


def load_calibration(path: Path | str = CALIBRATION_PATH, *, expect: dict | None = None
                     ) -> Calibration | None:
    """The Calibration at `path`, or None if there is no file. Delegates to
    judge_base.load_calibration so every scoring backend applies the same rules,
    including refusing a file fitted for a different model or clipping (`expect`)."""
    return judge_base.load_calibration(path, backend=NAME, expect=expect)


def classify_http(status: int, text: str) -> str:
    """Map an HTTP failure to a BackendUnavailable kind: judge_base.classify_http, shared
    so jev and nemotron give the same kind for the same failure. 404 is split on its
    body because the two causes need opposite owners: "Not found for account" is an
    entitlement the vendor withdrew, a bare 404 is a wrong URL or model in our config."""
    return judge_base.classify_http(status, text)


def parse_rankings(payload, n: int) -> list[float]:
    """Logits in INPUT order from a rerank response.

    The service returns `rankings` sorted by score, each carrying the `index` of the
    passage it scored. Every index 0..n-1 must appear exactly once with a finite logit;
    anything else raises bad_response, because a missing or duplicated index would
    attach a verdict to the wrong hit."""
    rankings = payload.get("rankings") if isinstance(payload, dict) else None
    if not isinstance(rankings, list):
        raise BackendUnavailable(f"{NAME}: response has no rankings list", kind="bad_response")
    if len(rankings) != n:
        raise BackendUnavailable(f"{NAME}: {len(rankings)} rankings for {n} passages", kind="bad_response")
    logits: list[float | None] = [None] * n
    for r in rankings:
        idx = r.get("index") if isinstance(r, dict) else None
        logit = r.get("logit") if isinstance(r, dict) else None
        if not isinstance(idx, int) or isinstance(idx, bool) or not 0 <= idx < n:
            raise BackendUnavailable(f"{NAME}: ranking index {idx!r} out of range", kind="bad_response")
        if not isinstance(logit, (int, float)) or isinstance(logit, bool) or not math.isfinite(logit):
            raise BackendUnavailable(f"{NAME}: ranking {idx} has logit {logit!r}", kind="bad_response")
        if logits[idx] is not None:
            raise BackendUnavailable(f"{NAME}: ranking index {idx} repeated", kind="bad_response")
        logits[idx] = float(logit)
    return logits  # type: ignore[return-value]  (every slot filled: n unique in-range indices)


class NemotronBackend:
    """The hosted NVIDIA reranker as a validation Backend (see judge_base.Backend).

    name         "nemotron"
    capacity     passages per call the deadline is sized for (CAPACITY)
    calibrated   True when a calibration was found; then `score` is a probability
    calibration  the Calibration in use, or None — exposed so the chain can report
                 whether it is provisional
    """

    name = NAME
    # The measured recommendation, declared so the chain uses it rather than happening to
    # match its own default (judge.Chain reads deadline_s when a backend declares one).
    deadline_s = RECOMMENDED_DEADLINE_S

    def __init__(self, *, api_key: str | None = None, chars: int | None = None,
                 query_chars: int | None = None, capacity: int = CAPACITY,
                 calibration: Calibration | None = None,
                 calibration_path: Path | str | None = None,
                 url: str = URL, model: str = MODEL):
        """Build the backend or raise BackendNotConfigured saying exactly what is missing.

        `api_key` overrides NVIDIA_API_KEY. `calibration` overrides the file; otherwise
        `calibration_path` (default CALIBRATION_PATH) is read if it exists. No network
        call is made here: whether the endpoint is up is a run-time question, answered
        by score() as BackendUnavailable, not a configuration one."""
        if requests is None:
            raise BackendNotConfigured(f"{NAME}: the `requests` package is not installed (pip install requests)")
        if api_key is None:
            _load_env()
            api_key = os.environ.get("NVIDIA_API_KEY")
        api_key = (api_key or "").strip()
        if not api_key:
            raise BackendNotConfigured(f"{NAME}: NVIDIA_API_KEY is not set (engine/.env or the environment)")
        self._key = api_key
        self.url, self.model = url, model
        self.chars = chars if chars is not None else _env_int("RAG_NEMOTRON_CHARS", DEFAULT_CHARS)
        self.query_chars = (query_chars if query_chars is not None
                            else _env_int("RAG_NEMOTRON_QUERY_CHARS", DEFAULT_QUERY_CHARS))
        if self.chars <= 0 or self.query_chars <= 0 or capacity <= 0:
            raise BackendNotConfigured(f"{NAME}: chars, query_chars and capacity must be positive")
        self.capacity = capacity
        path = Path(calibration_path) if calibration_path is not None else CALIBRATION_PATH
        self.calibration = calibration if calibration is not None else load_calibration(
            path, expect={"model": self.model, "chars": self.chars, "query_chars": self.query_chars})
        self.calibrated = self.calibration is not None
        if not self.calibrated:
            _warn_uncalibrated(path)

    def describe(self) -> dict:
        """What this backend is configured as, for the run trace. Never includes the key."""
        cal = self.calibration
        return {"name": self.name, "model": self.model, "capacity": self.capacity,
                "chars": self.chars, "query_chars": self.query_chars, "calibrated": self.calibrated,
                "provisional": cal.provisional if cal else None,
                "fitted_on": cal.fitted_on if cal else None}

    def request_body(self, query: Query, passages: list[Passage]) -> dict:
        """The JSON body for one rerank call: query and passages clipped to their caps.

        `truncate: END` stays on as a second line of defence — if a clipped pair still
        exceeds the model's window, the service trims it instead of failing the call."""
        return {"model": self.model,
                "query": {"text": query.combined(max_chars=self.query_chars)},
                "passages": [{"text": (p.text or "")[:self.chars]} for p in passages],
                "truncate": "END"}

    def _post_within(self, body: dict, deadline_s: float) -> _Reply:
        """POST `body` and return the reply, never waiting past `deadline_s`.

        The request runs in a daemon worker thread with requests' own (connect, read)
        timeout set to the deadline, and this thread waits at most `deadline_s` for it.
        The worker streams the body and stops at the same wall-clock deadline, so once
        headers are in it cannot outlive the call by more than one socket read. If this
        thread gives up while the worker is still running, the worker is counted as
        stranded until it exits (see MAX_STRANDED and the module docstring). Daemon, so a
        hung call can never hold the interpreter open at exit."""
        global _stranded
        with _stranded_lock:
            if _stranded >= MAX_STRANDED:
                raise BackendUnavailable(
                    f"{NAME}: {_stranded} earlier calls are still hung; not starting another",
                    kind="timeout")
        box: dict = {}
        cancelled = threading.Event()
        deadline_at = time.monotonic() + deadline_s

        def call() -> None:
            """Worker: POST, stream the body until done or the deadline, close the
            connection, and store a _Reply or the exception in `box`. On exit, release
            this worker's stranded slot if score() had abandoned it."""
            global _stranded
            resp = None
            try:
                resp = requests.post(
                    self.url, json=body, timeout=(deadline_s, deadline_s), stream=True,
                    headers={"Authorization": f"Bearer {self._key}", "Accept": "application/json"})
                box["status"] = getattr(resp, "status_code", None)
                box["resp"] = _Reply(getattr(resp, "status_code", None),
                                     _read_body(resp, deadline_at, cancelled))
            except BaseException as e:           # handed back to the caller's thread below
                box["exc"] = e
            finally:
                if resp is not None:
                    try:
                        resp.close()             # drop the socket rather than return it to the pool
                    except Exception:            # closing is best effort; the result is already set
                        pass
                with _stranded_lock:
                    box["done"] = True
                    if box.get("stranded"):
                        _stranded -= 1

        worker = threading.Thread(target=call, name="judge-nemotron", daemon=True)
        worker.start()
        worker.join(deadline_s)
        with _stranded_lock:
            if not box.get("done"):
                cancelled.set()
                box["stranded"] = True
                _stranded += 1
                raise BackendUnavailable(f"{NAME}: no response within {deadline_s:g}s", kind="timeout")
        exc = box.get("exc")
        if exc is not None:
            # A read timeout while streaming the body comes from urllib3 (or the socket),
            # not wrapped by requests as iter_content would, so all three count as timeout.
            if isinstance(exc, (_Cancelled, requests.exceptions.Timeout, _Urllib3Timeout, TimeoutError)):
                raise BackendUnavailable(f"{NAME}: {type(exc).__name__.lstrip('_')} within {deadline_s:g}s",
                                         kind="timeout") from exc
            if isinstance(exc, _BodyTooLarge):
                raise BackendUnavailable(f"{NAME}: {exc}", kind="bad_response",
                                         status=box.get("status")) from exc
            if isinstance(exc, requests.exceptions.RequestException):
                # Connection refused, TLS failure and similar: http_error, as jev and
                # judge_base.classify_http(None) report it, so one kind means one owner
                # across backends. Never "timeout": the trace must not blame latency.
                raise BackendUnavailable(f"{NAME}: {type(exc).__name__}: {exc}", kind="http_error") from exc
            raise BackendUnavailable(f"{NAME}: unexpected {type(exc).__name__}: {exc}", kind="error") from exc
        return box["resp"]

    def _verdict(self, logit: float) -> Verdict:
        """One Verdict from one logit: calibrated probability and threshold if we have a
        calibration, else the model's own boundary (logit > 0) with `score` left None."""
        if self.calibration is not None:
            p = self.calibration.probability(logit)
            return Verdict(value=p >= self.calibration.threshold, score=p, why=None,
                           backend=self.name, raw=logit)
        return Verdict(value=logit > 0, score=None, why=None, backend=self.name, raw=logit)

    def score(self, query: Query, passages: list[Passage], *, deadline_s: float) -> list[Verdict]:
        """Judge every passage against `query` in ONE call. One Verdict per passage, in
        input order, or BackendUnavailable — never partial output, never a retry.

        An empty list returns [] without a call. Passing more than `capacity` passages is
        allowed but the deadline was not sized for it."""
        if not passages:
            return []
        if not deadline_s or deadline_s <= 0:
            raise BackendUnavailable(f"{NAME}: deadline {deadline_s!r}s leaves no time to call", kind="timeout")
        resp = self._post_within(self.request_body(query, passages), deadline_s)
        status = getattr(resp, "status_code", None)
        if status != 200:
            text = getattr(resp, "text", "") or ""
            kind = classify_http(status or 0, text)
            raise BackendUnavailable(f"{NAME}: HTTP {status}: {text[:200]}", kind=kind, status=status)
        try:
            payload = resp.json()
        except ValueError as e:
            raise BackendUnavailable(f"{NAME}: response is not JSON: {e}", kind="bad_response",
                                     status=status) from e
        logits = parse_rankings(payload, len(passages))
        return check_verdicts(self.name, passages, [self._verdict(x) for x in logits])


if __name__ == "__main__":                       # manual check: judge_nemotron.py "query" "passage" ...
    b = NemotronBackend()
    q = sys.argv[1] if len(sys.argv) > 1 else "challenger brand vs entrenched leader"
    ps = [Passage(str(i), t) for i, t in enumerate(sys.argv[2:] or ["a challenger brand took on the leader",
                                                                     "a recipe for lemon cake"])]
    for p, v in zip(ps, b.score(Query(q), ps, deadline_s=RECOMMENDED_DEADLINE_S)):
        print(f"{v.raw:+7.2f}  {v.value!s:5}  {p.text[:70]}")
