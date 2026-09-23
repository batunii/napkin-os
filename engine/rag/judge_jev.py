#!/usr/bin/env python3
"""
judge_jev.py — the jev (TypeSafe) relevance-validation backend.

jev is the one broadly calibrated judgement model in the architecture: asked a yes/no
question (a "Noul") about a piece of content (the "state"), it returns the probability
that the answer is yes, and that probability is calibrated by the vendor. So this backend
sets `score`, applies a plain threshold to it, and never explains (`why` is None).

    state      the brief: Query.text plus Query.context, clipped, identical in every request
    questions  one Noul per passage: "Is PASSAGE directly useful evidence for this brief?"
    answer     response.nouls[name].noul, a probability in [0, 1]

Why the brief is the state and each passage a question, rather than the other way round:
jev evaluates every question in a request in parallel against ONE state, so the thing
that is shared (the brief) belongs in the state and the things that vary (the passages)
belong in the questions. One request then judges many passages for the price of one
state. It also keeps the state small, which matters because TypeSafe documents that
"accuracy falls as the state grows with content unrelated to the decision": a state made
of fifty passages would make every passage a distractor for every other.

Limits (docs.typesafe.ai/models, jev-1.13): 64k tokens per request, and 32k for the state
plus the longest single question. Tokens are estimated here without a tokeniser (none is
published) at four ASCII characters per token and one token per non-ASCII character, and
budgets are set at 75% of the hard limits. The headroom means even a tokeniser that gets
only three ASCII characters per token stays inside the real limit. Passages are clipped
so any one question always fits beside the state, then packed greedily into as few
requests as the budget allows, and the requests run concurrently.

Configuration (env, each overridable by a constructor argument):

    TYPESAFE_API_KEY         required; passed to the SDK explicitly
    TYPESAFE_BASE_URL        optional API root, http(s)://host (default https://api.typesafe.ai)
    RAG_JEV_MODEL            model or alias sent on every call (default jev-latest)
    RAG_JEV_THRESHOLD        value = score >= this (default 0.5)
    RAG_JEV_STATE_CHARS      clip for the brief state (default 4000)
    RAG_JEV_PASSAGE_CHARS    clip for each passage (default 6000)
    RAG_JEV_BATCH_QUESTIONS  most Nouls per request (default 50)
    RAG_JEV_CONCURRENCY      most requests in flight at once (default 4)

Settings are read from the `env` mapping when one is injected (tests), otherwise from
os.environ after engine/.env has been loaded into it (via rag.py, without overriding what
is already set), so a key placed in engine/.env works however the backend is reached.
The key and base URL are passed to the SDK explicitly rather than left for it to read
from os.environ, so the one mapping this backend was given governs every setting.

Failure behaviour follows judge_base: a missing SDK, a missing key, a malformed base URL
or a nonsensical setting raises BackendNotConfigured at construction; anything that goes
wrong on the wire raises BackendUnavailable from score(), with `kind` mapped from the
SDK's exception classes (see _map_error). SDK retries are switched off: the chain's next
backend is the retry, and an SDK backoff of up to five seconds would otherwise sit inside
our deadline.

Deadline and abandoned work. score() returns or raises by deadline_s whatever the server
does, because it waits on its workers with a wall-clock timeout. What happens to a worker
it gives up on is bounded three ways:
  - Workers are daemon threads, one per concurrent request, never a ThreadPoolExecutor:
    executor workers are joined at interpreter exit, so one server that keeps a socket
    open would hang process shutdown (measured: a trickling server held the process open
    indefinitely until it was killed).
  - The real client is built on a deadline-aware transport (see _deadline_types) that
    checks the wall clock after every chunk of the response body and fails the read
    once the deadline has passed. httpx timeouts are per socket read, not per request,
    so without this a server that sends one byte every few hundred milliseconds would
    keep a worker, and its connection, alive for as long as it liked.
  - The response headers phase has no such hook, so every abandoned worker still alive
    is counted as stranded, and while MAX_STRANDED of them are alive score() refuses at
    once (kind="timeout", no request sent). A hung endpoint therefore costs at most
    MAX_STRANDED threads and sockets however many briefs arrive, as in judge_nemotron.
  Batches not yet started when the call gives up are never sent.

The SDK (typesafe-sdk 0.7.1) is imported lazily, so this module loads, and its tests run,
on a machine that has never installed it. Every fact about the SDK used here was read
from its source; the file and line are cited where each is used.
"""
from __future__ import annotations

import functools
import json
import math
import os
import threading
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from judge_base import BackendNotConfigured, BackendUnavailable, Passage, Query, Verdict, check_verdicts
import judge_base  # noqa: E402  (shared HTTP classifier)

NAME = "jev"
SDK_PIN = "typesafe-sdk==0.7.1"

# Hard limits documented for jev-1.13 (docs.typesafe.ai/models).
HARD_REQUEST_TOKENS = 64_000
HARD_STATE_PLUS_QUESTION_TOKENS = 32_000
HEADROOM = 0.75
REQUEST_BUDGET = int(HARD_REQUEST_TOKENS * HEADROOM)                  # 48 000
PAIR_BUDGET = int(HARD_STATE_PLUS_QUESTION_TOKENS * HEADROOM)         # 24 000
REQUEST_OVERHEAD_TOKENS = 64        # "model", "state", "questions" keys and JSON framing
ASCII_CHARS_PER_TOKEN = 4

# SDK constants.py:14 — the API root used when TYPESAFE_BASE_URL is unset.
DEFAULT_BASE_URL = "https://api.typesafe.ai"

# Most abandoned workers allowed alive at once before score() refuses to start more. Two
# calls' worth at the default concurrency: enough that a single slow brief does not lock
# the backend out, few enough that a hung endpoint cannot pile up threads and sockets.
MAX_STRANDED = 8

DEFAULTS = {
    "model": "jev-latest",
    "threshold": 0.5,
    "state_chars": 4000,
    "passage_chars": 6000,
    "batch_questions": 50,
    "concurrency": 4,
}

QUESTION = "Is PASSAGE directly useful evidence for this brief?"
CRITERIA = {
    "true": ("PASSAGE states a fact, finding, example or principle that the brief could cite "
             "or act on directly: it bears on this brand, category, audience, problem or "
             "objective."),
    "false": ("PASSAGE is off-topic, only loosely related, generic advice that would apply to "
              "any brief, or would not change what the brief says."),
}


# ---- token estimation and request shaping -----------------------------------------
def estimate_tokens(obj: Any) -> int:
    """Conservative token count for a JSON-able value as it goes on the wire.

    Four ASCII characters per token is typical for English under modern tokenisers; a
    non-ASCII character is counted as a whole token because CJK and emoji-heavy text can
    tokenise at close to one token per character. Counting the JSON-serialised form
    includes keys, quotes and escapes, which only ever overestimates."""
    s = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    non_ascii = sum(1 for ch in s if ord(ch) > 127)
    return math.ceil((len(s) - non_ascii) / ASCII_CHARS_PER_TOKEN) + non_ascii


def build_state(query: Query, max_chars: int) -> dict:
    """The brief as jev's state: the retrieval query, then brief context, within max_chars.

    The query is kept whole where possible and context is clipped first, the same order
    Query.combined uses, because the query is what retrieval answered and context only
    sharpens it. The state is a JSON object (SDK json_types.py:17-20 accepts a mapping) so
    jev sees which part is which, and it is built once per score() call so every batch is
    judged against an identical state."""
    text = query.text[:max_chars]
    state = {"retrieval_query": text}
    room = max_chars - len(text)
    if query.context and room > 0:
        state["brief_context"] = query.context[:room]
    return state


def build_question(passage_text: str) -> dict:
    """One Noul question for one passage, in the SDK's raw dictionary form.

    The dictionary form (`NoulModel`, SDK question_types.py:26-36, accepted by
    normalize_questions, questions.py:16-22) is used instead of the `Noul` class so this
    module needs nothing from the SDK to build a request, and the tests can assert the
    exact wire shape. `instructions` may be a JSON object (question_types.py:33), which
    keeps the passage delimited from the question rather than pasted into a sentence."""
    return {"type": "noul",
            "instructions": {"question": QUESTION, "passage": passage_text},
            "criteria": dict(CRITERIA)}


def question_name(i: int) -> str:
    """The name a passage's Noul is sent under: its position in the score() call.

    Positions, not cite ids, because cite ids are caller data (they could repeat or hold
    characters a key should not) and the answer is joined back by name, never by order."""
    return f"p{i:03d}"


def plan_batches(state: dict, texts: list[str], *, passage_chars: int,
                 batch_questions: int) -> list[list[tuple[int, dict]]]:
    """Clip each passage and pack the questions, in input order, into request-sized batches.

    Every batch satisfies, by the conservative estimate:
        state + longest question  <= PAIR_BUDGET     (the 32k rule)
        overhead + state + all    <= REQUEST_BUDGET  (the 64k rule)
        len(batch)                <= batch_questions
    A passage is clipped to passage_chars first, then clipped further only in the
    pathological case where it still would not fit beside the state. Greedy packing in
    input order keeps batches contiguous, which makes a failure easy to read in a trace."""
    state_tokens = estimate_tokens(state)
    empty_q = estimate_tokens({question_name(0): build_question("")})
    room = PAIR_BUDGET - state_tokens - empty_q
    if room <= 0:
        raise BackendUnavailable(f"{NAME}: state alone ({state_tokens} tokens) leaves no room "
                                 "for a passage", kind="error")
    batches: list[list[tuple[int, dict]]] = []
    current: list[tuple[int, dict]] = []
    used = REQUEST_OVERHEAD_TOKENS + state_tokens
    for i, text in enumerate(texts):
        clipped = text[:passage_chars]
        q = build_question(clipped)
        q_tokens = estimate_tokens({question_name(i): q})
        while state_tokens + q_tokens > PAIR_BUDGET and clipped:
            # Halving ends within a few passes; `room` > 0 guarantees the empty text fits.
            clipped = clipped[:len(clipped) // 2]
            q = build_question(clipped)
            q_tokens = estimate_tokens({question_name(i): q})
        if current and (used + q_tokens > REQUEST_BUDGET or len(current) >= batch_questions):
            batches.append(current)
            current, used = [], REQUEST_OVERHEAD_TOKENS + state_tokens
        current.append((i, q))
        used += q_tokens
    if current:
        batches.append(current)
    return batches


# ---- SDK error mapping ---------------------------------------------------------------
def _map_error(exc: BaseException) -> BackendUnavailable:
    """Translate an SDK exception into BackendUnavailable with the right `kind`.

    Matched on class NAMES in the exception's MRO, not isinstance, so the mapper needs no
    SDK import and a test can reproduce the hierarchy exactly. Hierarchy, from
    typesafe_sdk/_core/errors.py (0.7.1):

        TypeSafeError                                  68   base; client-side problems
          TypeSafeAPIError(status, body, headers)      72   any non-2xx HTTP response
            TypeSafeBadRequestError           400      118
            TypeSafeAuthenticationError       401      122
            TypeSafePermissionDeniedError     403      126
            TypeSafeNotFoundError             404      130
            TypeSafeUnprocessableEntityError  422      134
            TypeSafeRateLimitError            429      138  (.retry_after_ms)
            TypeSafeInternalServerError       5xx      148
            TypeSafeAPIResponseValidationError         176  2xx with a malformed body
          TypeSafeAPIConnectionError(ConnectionError)  152  no HTTP response at all
            TypeSafeAPITimeoutError(TimeoutError)      156

    Order matters: ResponseValidation is a TypeSafeAPIError and Timeout is a
    ConnectionError, so the more specific names are tested first."""
    names = {c.__name__ for c in type(exc).__mro__}
    status = getattr(exc, "status", None)
    status = status if isinstance(status, int) else None
    msg = f"{NAME}: {type(exc).__name__}: {exc}"
    if "TypeSafeAPIResponseValidationError" in names:
        return BackendUnavailable(msg, kind="bad_response", status=status)
    if "TypeSafeAPITimeoutError" in names:
        return BackendUnavailable(msg, kind="timeout")
    if "TypeSafeAPIConnectionError" in names:
        return BackendUnavailable(msg, kind="http_error")
    if "TypeSafeAPIError" in names:
        # One classifier for every backend (judge_base.classify_http), so a kind means the
        # same owner whichever backend raised it. Two jev-specific cases sit in front:
        # 402 is a plan that does not cover the model (the vendor's side), and 408 is a
        # server-side timeout. A bare 404 is http_error like nemotron's — a wrong model
        # alias in our config (RAG_JEV_MODEL), with the status recorded to say so.
        if "TypeSafeRateLimitError" in names:
            kind = "rate_limited"
        elif status == 402:
            kind = "not_entitled"
        elif status == 408:
            kind = "timeout"
        else:
            kind = judge_base.classify_http(status, str(getattr(exc, "body", "") or exc))
        return BackendUnavailable(msg, kind=kind, status=status)
    if isinstance(exc, TimeoutError):
        return BackendUnavailable(msg, kind="timeout")
    return BackendUnavailable(msg, kind="error")


# ---- abandoned workers and the wall-clock body bound ----------------------------------
_stranded = 0                       # abandoned workers still alive (see MAX_STRANDED)
_stranded_lock = threading.Lock()   # guards _stranded and every worker record's flags

# The deadline of the request the current thread is sending, read by the deadline-aware
# transport. Thread-local because the SDK calls the transport synchronously on the thread
# that called system_one, and each worker thread sends one request at a time.
_request_deadline = threading.local()


def stranded_workers() -> int:
    """How many workers score() has abandoned that are still running. For the run trace
    and for tests; the count only goes down when those workers actually exit."""
    with _stranded_lock:
        return _stranded


def _strand_unfinished(records: list[dict]) -> None:
    """Count every worker in `records` that has not exited yet as stranded.

    Done under the same lock the worker takes on exit, so a worker is counted exactly
    once and uncounted exactly once, whichever of the two runs first."""
    global _stranded
    with _stranded_lock:
        for r in records:
            if not r["done"]:
                r["stranded"] = True
                _stranded += 1


def _worker_exited(record: dict) -> None:
    """Mark a worker as finished, releasing its stranded slot if it had been abandoned."""
    global _stranded
    with _stranded_lock:
        record["done"] = True
        if record["stranded"]:
            _stranded -= 1


def _load_httpx():
    """Import httpx2, the SDK's HTTP library, on first use. Kept in a function so the
    module imports without it and a test can simulate its absence."""
    import httpx2  # noqa: PLC0415 - deliberately lazy
    return httpx2


@functools.lru_cache(maxsize=None)
def _deadline_types(httpx):
    """Build the deadline-aware transport and stream classes on top of an httpx2 module.

    Built in a function because both must subclass httpx2 base classes (the client asserts
    the response stream is a SyncByteStream, httpx2 _client.py:1078) and httpx2 is only
    imported lazily. Cached so each httpx2 module gets one pair of classes.

    Why a transport and not a timeout: httpx applies its timeout to each socket read
    separately, so a server that keeps sending a byte at a time is never timed out. The
    stream below checks the clock after every chunk the socket yields and raises
    httpx2.ReadTimeout once the thread's request deadline has passed; the client then
    closes the response (httpx2 _client.py:992-993), which releases the connection, and
    the SDK maps the error to TypeSafeAPITimeoutError (transport.py:83-84)."""

    class DeadlineStream(httpx.SyncByteStream):
        """A response body that stops yielding, with ReadTimeout, after `deadline_at`."""

        def __init__(self, inner, deadline_at: float, request):
            """Wrap the transport's own body stream for one request."""
            self._inner, self._deadline_at, self._request = inner, deadline_at, request

        def __iter__(self):
            """Yield the body chunk by chunk, checking the wall clock after each one."""
            for chunk in self._inner:
                if time.monotonic() >= self._deadline_at:
                    raise httpx.ReadTimeout(f"{NAME}: response body still arriving at the deadline",
                                            request=self._request)
                yield chunk

        def close(self) -> None:
            """Close the wrapped stream, which returns or drops its connection."""
            close = getattr(self._inner, "close", None)
            if callable(close):
                close()

    class DeadlineTransport(httpx.BaseTransport):
        """Delegates to an inner transport and bounds each response body by the deadline
        _call recorded for the current thread; with no deadline recorded it is a no-op."""

        def __init__(self, inner):
            """Wrap `inner` (normally httpx2.HTTPTransport())."""
            self._inner = inner

        def handle_request(self, request):
            """Send via the inner transport, then wrap the body in a DeadlineStream."""
            response = self._inner.handle_request(request)
            deadline_at = getattr(_request_deadline, "at", None)
            if deadline_at is not None:
                response.stream = DeadlineStream(response.stream, deadline_at, request)
            return response

        def close(self) -> None:
            """Close the inner transport and its connection pool."""
            self._inner.close()

    return DeadlineTransport, DeadlineStream


def deadline_transport(inner=None):
    """A transport for TypeSafeClient(transport=...) that bounds response bodies in wall
    time. JevBackend builds its own client on one; use this when injecting a client built
    elsewhere so it keeps the same bound. `inner` defaults to httpx2.HTTPTransport()."""
    httpx = _load_httpx()
    transport_cls, _ = _deadline_types(httpx)
    return transport_cls(inner if inner is not None else httpx.HTTPTransport())


# ---- configuration -------------------------------------------------------------------
def _load_env() -> None:
    """Make engine/.env visible in os.environ exactly as the rest of the module sees it.

    rag.py loads engine/.env when it is imported (without overriding variables already
    set), so importing it is the one loader rather than a second copy of its parsing, the
    same choice judge_nemotron makes. A tree without rag.py (a copied-out backend) simply
    reads os.environ as it stands. Kept as a named function so tests can replace it and
    stay hermetic."""
    try:
        import rag  # noqa: F401,PLC0415  (imported for its _load_dotenv() side effect)
    except ImportError:
        pass


def _base_url(env: Mapping[str, str], problems: list[str]) -> str:
    """TYPESAFE_BASE_URL from `env`, or the SDK default, checked to be an http(s) URL.

    The SDK only strips a trailing slash (config.py:61), so a value without a scheme or
    with a misspelt one would construct fine and then fail every request as a connection
    error: the chain would fall through silently on every brief instead of refusing to
    start. Checking it here makes it the configuration error it is."""
    raw = (env.get("TYPESAFE_BASE_URL") or "").strip() or DEFAULT_BASE_URL
    try:
        parts = urlsplit(raw)
        ok = parts.scheme.lower() in ("http", "https") and bool(parts.hostname)
        parts.port                       # raises ValueError on a non-numeric port
    except ValueError:
        ok = False
    if not ok:
        problems.append(f"TYPESAFE_BASE_URL={raw!r} is not an http(s) URL with a host: use "
                        f"e.g.  TYPESAFE_BASE_URL={DEFAULT_BASE_URL}  or unset it")
    return raw.rstrip("/")


def _setting(value, env: Mapping[str, str], var: str, default, cast, problems: list[str]):
    """One setting: the constructor argument if given, else the env var, else the default.
    A value that will not cast is recorded in `problems` rather than raised, so a single
    BackendNotConfigured can list every bad setting at once."""
    if value is not None:
        raw = value
    else:
        raw = (env.get(var) or "").strip() or None
        if raw is None:
            return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        problems.append(f"{var}={raw!r} is not a valid {cast.__name__}")
        return default


def _load_sdk():
    """Import typesafe_sdk on first use. Kept in a function so the module imports without
    the SDK and a test can substitute a fake."""
    import typesafe_sdk  # noqa: PLC0415 - deliberately lazy
    return typesafe_sdk


class JevBackend:
    """Relevance validation by TypeSafe's jev: one calibrated Noul per passage.

    name        "jev"
    capacity    50 passages per query within the deadline
    calibrated  True: jev's own calibration (vendor-trained), so no Platt layer

    `client` injects anything with the SDK's `system_one(state, questions, *, model,
    timeout)` method and `SystemOneResponse`-shaped result; when given, no SDK or key is
    needed. `sdk` injects the SDK module itself (tests of the real construction path).
    `env` replaces os.environ for reading settings; when it is not given, engine/.env is
    loaded into os.environ first (see _load_env). A client injected from elsewhere does
    not get the wall-clock body bound unless it was built with deadline_transport()."""

    name = NAME
    capacity = 50
    calibrated = True
    # jev's probabilities are vendor-calibrated, but the 0.5 threshold that turns one into
    # a verdict has never been checked on Napkin data. Provisional until it is, so the
    # trace says so alongside nemotron's and local's provisional Platt fits.
    provisional = True

    def __init__(self, client: Any = None, *, sdk: Any = None, env: Mapping[str, str] | None = None,
                 model: str | None = None, threshold: float | None = None,
                 state_chars: int | None = None, passage_chars: int | None = None,
                 batch_questions: int | None = None, concurrency: int | None = None):
        """Resolve settings and build the SDK client. Raises BackendNotConfigured, naming
        every problem and exactly how to fix it, if the backend cannot run as asked.

        engine/.env is loaded only when `env` is None, so an injected mapping is the whole
        environment the backend sees and tests stay hermetic."""
        if env is None:
            _load_env()
            env = os.environ
        problems: list[str] = []
        self.model = _setting(model, env, "RAG_JEV_MODEL", DEFAULTS["model"], str, problems)
        self.threshold = _setting(threshold, env, "RAG_JEV_THRESHOLD", DEFAULTS["threshold"], float, problems)
        self.state_chars = _setting(state_chars, env, "RAG_JEV_STATE_CHARS", DEFAULTS["state_chars"], int, problems)
        self.passage_chars = _setting(passage_chars, env, "RAG_JEV_PASSAGE_CHARS", DEFAULTS["passage_chars"], int, problems)
        self.batch_questions = _setting(batch_questions, env, "RAG_JEV_BATCH_QUESTIONS",
                                        DEFAULTS["batch_questions"], int, problems)
        self.concurrency = _setting(concurrency, env, "RAG_JEV_CONCURRENCY", DEFAULTS["concurrency"], int, problems)

        if not (0.0 <= self.threshold <= 1.0):          # also rejects NaN
            problems.append(f"RAG_JEV_THRESHOLD={self.threshold} must be a probability in [0, 1]")
        for label, v in (("RAG_JEV_STATE_CHARS", self.state_chars), ("RAG_JEV_PASSAGE_CHARS", self.passage_chars),
                         ("RAG_JEV_BATCH_QUESTIONS", self.batch_questions), ("RAG_JEV_CONCURRENCY", self.concurrency)):
            if v < 1:
                problems.append(f"{label}={v} must be at least 1")
        # The worst-case state must leave room for a question under the 32k rule, or every
        # passage would be clipped to nothing. Checked here so it is a config error, not a
        # run-time surprise.
        worst_state = estimate_tokens({"retrieval_query": "x" * self.state_chars})
        if worst_state + estimate_tokens({question_name(0): build_question("")}) >= PAIR_BUDGET:
            problems.append(f"RAG_JEV_STATE_CHARS={self.state_chars} leaves no room for a passage "
                            f"under the {HARD_STATE_PLUS_QUESTION_TOKENS}-token state-plus-question limit")

        if client is None:
            client = self._build_client(sdk, env, problems)
        if problems:
            raise BackendNotConfigured(f"{NAME}: " + "; ".join(problems))
        self._client = client
        self.last_call: dict = {}

    def _build_client(self, sdk, env: Mapping[str, str], problems: list[str]):
        """Construct the real TypeSafeClient, recording (not raising) what is missing.

        TypeSafeClient(*, api_key, model, retry, timeout, headers, transport, http_client,
        base_url) — SDK _core/client/sync/client.py:22-33. The key and base URL are read
        from `env` and passed explicitly, because the SDK would otherwise read them from
        os.environ (config.py:21-33, 61) and an injected mapping would govern everything
        but them. The SDK still validates the key's characters (config.py:31-32). Retries
        are disabled with RetryPolicy(max_retries=0) (retry.py:52-53) because the SDK
        default retries twice with up to 5 s backoff inside a 30 s budget (retry.py:52-82),
        which would overrun any validation deadline. The transport is the deadline-aware
        one (see _deadline_types); it needs httpx2, which the real SDK always imports
        (config.py:8), so it is left out only for a stand-in SDK that lacks it."""
        if sdk is None:
            try:
                sdk = _load_sdk()
            except ImportError:
                problems.append(f"the TypeSafe SDK is not installed: run  pip install '{SDK_PIN}'  "
                                "(or  uv add typesafe-sdk==0.7.1 ) in the engine's environment")
        key = (env.get("TYPESAFE_API_KEY") or "").strip()
        if not key:
            problems.append("TYPESAFE_API_KEY is not set: add  TYPESAFE_API_KEY=<key>  to engine/.env "
                            "or the environment, or remove 'jev' from RAG_VALIDATOR")
        base_url = _base_url(env, problems)
        if sdk is None or problems:
            return None
        kwargs: dict = {}
        try:
            kwargs["transport"] = deadline_transport()
        except ImportError:
            pass
        try:
            return sdk.TypeSafeClient(api_key=key, model=self.model, base_url=base_url,
                                      retry=sdk.RetryPolicy(max_retries=0), **kwargs)
        except Exception as exc:   # TypeSafeError: malformed key (config.py:31-32) or bad timeout
            problems.append(f"TypeSafeClient could not be built: {exc}")
            return None

    def close(self) -> None:
        """Release the SDK client's HTTP connections (TypeSafeClient.close, client.py:225)."""
        close = getattr(self._client, "close", None)
        if callable(close):
            close()

    # ---- the Backend protocol ---------------------------------------------------------
    def describe(self) -> dict:
        """What this backend is running, for the trace: model, capacity, threshold and
        whether that threshold is provisional. No key or client details."""
        return {"backend": self.name, "model": self.model, "capacity": self.capacity,
                "threshold": self.threshold, "provisional": self.provisional,
                "passage_chars": self.passage_chars, "state_chars": self.state_chars}

    def score(self, query: Query, passages: list[Passage], *, deadline_s: float) -> list[Verdict]:
        """Judge every passage against `query`: one Verdict per passage, input order.

        Batches run concurrently on daemon worker threads (see _run_batches), and the wait
        for them is bounded by deadline_s in wall time, because the per-request HTTP
        timeout alone does not bound it (httpx applies a float timeout to each connect,
        write, read and pool phase separately, and to each socket read). Workers still
        running at the deadline are abandoned, not joined, and counted as stranded until
        they exit; while MAX_STRANDED are alive this refuses at once with kind="timeout"
        and sends nothing. Any failed batch fails the whole call: the contract forbids
        partial output."""
        started = time.monotonic()
        if not passages:
            return []
        stranded = stranded_workers()
        if stranded >= MAX_STRANDED:
            raise BackendUnavailable(f"{NAME}: {stranded} abandoned requests are still hung; "
                                     "not starting another", kind="timeout")
        state = build_state(query, self.state_chars)
        batches = plan_batches(state, [p.text for p in passages],
                               passage_chars=self.passage_chars, batch_questions=self.batch_questions)
        remaining = deadline_s - (time.monotonic() - started)
        if remaining <= 0:
            raise BackendUnavailable(f"{NAME}: deadline {deadline_s}s spent before any request",
                                     kind="timeout")
        results = self._run_batches(state, batches, started + deadline_s, deadline_s)

        probs: dict[int, float] = {}
        models, input_tokens = set(), 0
        for batch_probs, model_name, tokens in results:
            probs.update(batch_probs)
            models.add(model_name)
            input_tokens += tokens or 0
        self.last_call = {"requests": len(batches), "models": sorted(m for m in models if m),
                          "input_tokens": input_tokens, "seconds": round(time.monotonic() - started, 3)}
        verdicts = [Verdict(value=probs[i] >= self.threshold, score=probs[i], why=None,
                            backend=NAME, raw=probs[i]) for i in range(len(passages))]
        return check_verdicts(NAME, passages, verdicts)

    def _run_batches(self, state: dict, batches: list, deadline_at: float, deadline_s: float) -> list:
        """Send every batch on at most `concurrency` daemon threads; return results in batch order.

        Why plain daemon threads rather than concurrent.futures.ThreadPoolExecutor: executor
        workers are non-daemon and joined at interpreter exit, so a single request the
        server never finishes would hang process shutdown, and judge.py's chain thread
        being a daemon does not help because the pool's worker is a separate thread.
        Measured with the real SDK against a server trickling one byte every 0.2 s: the
        executor version returned on time but the process could not exit until killed.

        Workers pull the next batch index from a shared counter, so a batch is only sent
        once a slot is free and never after the call has given up (`cancelled`) or once
        another batch has failed. The waiter returns as soon as every batch has succeeded
        or any has failed, or at `deadline_at`, whichever comes first. The reported failure
        is the lowest-numbered failed batch, so the error is stable across runs."""
        lock = threading.Lock()
        finished = threading.Event()
        cancelled = threading.Event()
        results: dict[int, tuple] = {}
        errors: dict[int, BaseException] = {}
        cursor = [0]
        n_workers = min(self.concurrency, len(batches))
        records = [{"done": False, "stranded": False} for _ in range(n_workers)]

        def work(record: dict) -> None:
            """Worker body: send batches until none is left, one fails, or the call gives up."""
            try:
                while not cancelled.is_set():
                    with lock:
                        if errors or cursor[0] >= len(batches):
                            return
                        j = cursor[0]
                        cursor[0] += 1
                    try:
                        out = self._call(state, batches[j], deadline_at)
                    except BaseException as exc:   # noqa: BLE001 - handed to the waiter
                        with lock:
                            errors[j] = exc
                        finished.set()
                        return
                    with lock:
                        results[j] = out
                        if len(results) == len(batches):
                            finished.set()
            finally:
                _worker_exited(record)

        threads = [threading.Thread(target=work, args=(rec,), name=f"jev-worker-{k}", daemon=True)
                   for k, rec in enumerate(records)]
        for t in threads:
            t.start()
        finished.wait(max(0.0, deadline_at - time.monotonic()))
        cancelled.set()
        with lock:
            errs, done = dict(errors), dict(results)
        if not errs and len(done) == len(batches):
            # Every batch answered: the workers are only returning, so wait the moment it
            # takes rather than count them as stranded.
            for t in threads:
                t.join(max(0.0, deadline_at - time.monotonic()))
        _strand_unfinished(records)
        if errs:
            exc = errs[min(errs)]
            if isinstance(exc, BackendUnavailable):
                raise exc
            if isinstance(exc, Exception):
                raise _map_error(exc)
            raise BackendUnavailable(f"{NAME}: {type(exc).__name__} raised in a worker: {exc}",
                                     kind="error")
        if len(done) < len(batches):
            raise BackendUnavailable(f"{NAME}: {len(batches) - len(done)} of {len(batches)} requests "
                                     f"unfinished at the {deadline_s}s deadline", kind="timeout")
        return [done[j] for j in range(len(batches))]

    def _call(self, state: dict, batch: list[tuple[int, dict]], deadline_at: float):
        """Send one batch and read one probability per question.

        system_one(state, questions, *, model, retry, timeout, extra_headers, extra_body,
        response_model) — client.py:129-140. The response is a SystemOneResponse whose
        `nouls` maps question name to NoulAnswer (response_types.py:112-115), and
        NoulAnswer.noul is "Probability of a yes answer ... from 0 to 1" (_schemas/
        models.py:73-80). `model` is the versioned name that answered (models.py:220-225),
        and `usage.input_tokens` the billed tokens (models.py:151-154). Returns
        ({position: probability}, model, input_tokens).

        The HTTP timeout is the time left until `deadline_at` when the batch actually
        starts, so a batch queued behind the concurrency limit does not get a stale,
        larger budget. The SDK rejects a timeout <= 0 (config.py:36-39), so an exhausted
        budget is reported as a timeout here instead of sending. The deadline is also
        recorded for this thread so the deadline-aware transport can end a body that is
        still arriving when it passes."""
        questions = {question_name(i): q for i, q in batch}
        timeout = deadline_at - time.monotonic()
        if timeout <= 0:
            raise BackendUnavailable(f"{NAME}: deadline passed before the request was sent",
                                     kind="timeout")
        _request_deadline.at = deadline_at          # read by the deadline-aware transport
        try:
            response = self._client.system_one(state=state, questions=questions,
                                               model=self.model, timeout=timeout)
        except BackendUnavailable:
            raise
        except Exception as exc:
            raise _map_error(exc) from exc
        finally:
            _request_deadline.at = None
        try:
            nouls = response.nouls
        except Exception as exc:
            raise BackendUnavailable(f"{NAME}: response has no readable nouls: {exc}",
                                     kind="bad_response") from exc
        out: dict[int, float] = {}
        for i, _ in batch:
            name = question_name(i)
            answer = nouls.get(name)
            if answer is None:
                raise BackendUnavailable(f"{NAME}: no noul answer for {name}", kind="bad_response")
            p = getattr(answer, "noul", None)
            if isinstance(p, bool) or not isinstance(p, (int, float)) or not (0.0 <= p <= 1.0):
                raise BackendUnavailable(f"{NAME}: {name} probability {p!r} is not in [0, 1]",
                                         kind="bad_response")
            out[i] = float(p)
        usage = getattr(response, "usage", None)
        return out, getattr(response, "model", None), getattr(usage, "input_tokens", None)
