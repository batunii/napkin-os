"""The jev (TypeSafe) validation backend, tested without the network.

Run: cd engine/rag && RAG_STORE=local RAG_INDEX=./_index_v3 python3 -m pytest -q test_judge_jev.py

Every fake below is built from the typesafe-sdk 0.7.1 source (pip download
typesafe-sdk==0.7.1 --no-deps; paths are inside the wheel) and cites the file and line it
mirrors, so a change in the SDK shape can be traced to the fake that must change with it.

The tests at the end drive the REAL SDK through an in-memory HTTP transport (httpx2's
MockTransport, never a socket), including the deadline-aware transport against an endless
trickling body. They are skipped unless typesafe_sdk imports; set RAG_JEV_SDK_PATH to a
directory holding the SDK and its dependencies to run them without installing anything.

One test runs a child interpreter (no network) to prove the process can exit while a
request is still hung. An autouse fixture stubs the engine/.env loader so no test ever
reads that file.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import pytest  # noqa: E402

import judge_base as jb  # noqa: E402
import judge_jev as jj  # noqa: E402

KEY_ENV = {"TYPESAFE_API_KEY": "ts-test-key"}


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch):
    """Never let a test load engine/.env: the backend loads it when no `env` is injected,
    and that file points at shared services. Tests of the loader replace this stub."""
    monkeypatch.setattr(jj, "_load_env", lambda: None)


def wait_until(pred, timeout=5.0):
    """Poll `pred` until it is true or `timeout` seconds pass; return its last value."""
    end = time.monotonic() + timeout
    while not pred() and time.monotonic() < end:
        time.sleep(0.01)
    return pred()


def jev_workers():
    """The jev worker threads alive right now."""
    return [t for t in threading.enumerate() if t.name.startswith("jev-worker") and t.is_alive()]


class HangingClient:
    """A client whose system_one blocks until `release` is set, standing in for a server
    that never finishes its response. Records how many calls reached it."""

    def __init__(self):
        """Start blocked, with no calls seen."""
        self.release = threading.Event()
        self.calls = 0
        self.lock = threading.Lock()

    def system_one(self, state, questions, *, model=None, timeout=None, **kw):
        """Count the call, then block until released; then fail like a closed socket."""
        with self.lock:
            self.calls += 1
        self.release.wait()
        raise TypeSafeAPIConnectionError("released by the test")


# ---- fakes shaped exactly like the SDK ------------------------------------------------
# Exception hierarchy: typesafe_sdk/_core/errors.py:68-176. Same names, same bases, same
# constructor arguments, because judge_jev maps on names in the MRO and on `.status`.
class TypeSafeError(Exception):
    """errors.py:68 — base of every SDK failure."""


class TypeSafeAPIError(TypeSafeError):
    """errors.py:72-95 — a non-2xx response; carries status, body and headers."""

    def __init__(self, status, body=None, headers=None, message=None, endpoint=None):
        """Mirror errors.py:75-84: record status, body, headers and endpoint."""
        super().__init__(status, body, headers, message, endpoint)
        self.status, self.body, self.headers, self.endpoint = status, body, headers or {}, endpoint


class TypeSafeBadRequestError(TypeSafeAPIError):
    """errors.py:118 — 400."""


class TypeSafeAuthenticationError(TypeSafeAPIError):
    """errors.py:122 — 401."""


class TypeSafePermissionDeniedError(TypeSafeAPIError):
    """errors.py:126 — 403."""


class TypeSafeNotFoundError(TypeSafeAPIError):
    """errors.py:130 — 404."""


class TypeSafeUnprocessableEntityError(TypeSafeAPIError):
    """errors.py:134 — 422."""


class TypeSafeRateLimitError(TypeSafeAPIError):
    """errors.py:138-145 — 429, with retry_after_ms parsed from the headers."""

    def __init__(self, status, body=None, headers=None, message=None, endpoint=None):
        """Mirror errors.py:141-145."""
        super().__init__(status, body, headers, message, endpoint)
        self.retry_after_ms = None


class TypeSafeInternalServerError(TypeSafeAPIError):
    """errors.py:148 — 5xx."""


class TypeSafeAPIConnectionError(TypeSafeError, ConnectionError):
    """errors.py:152 — no HTTP response at all."""


class TypeSafeAPITimeoutError(TypeSafeAPIConnectionError, TimeoutError):
    """errors.py:156-163 — the request exceeded its timeout; carries the timeout."""

    def __init__(self, timeout):
        """Mirror errors.py:159-162."""
        super().__init__(timeout)
        self.timeout = timeout


class TypeSafeAPIResponseValidationError(TypeSafeAPIError):
    """errors.py:176-185 — 2xx whose body did not match the response schema."""

    def __init__(self, status, body, headers, field_path, endpoint=None):
        """Mirror errors.py:179-185: the fourth argument is a field path, not a message."""
        self.field_path = field_path
        super().__init__(status, body, headers, f"Invalid response data at {field_path!r}.", endpoint)


# Response: typesafe_sdk/_core/response_types.py:22-30 (NoulAnswer), 65-73 (Usage),
# 99-125 (SystemOneResponse with the `nouls` view), wire fields _schemas/models.py:73-80,
# 151-160, 219-235.
@dataclass(frozen=True)
class NoulAnswer:
    """response_types.py:22-30 / models.py:73-80: type='noul' and noul, P(yes) in [0, 1]."""
    noul: float
    type: str = "noul"


@dataclass(frozen=True)
class ChoiceAnswer:
    """response_types.py:33-41 / models.py:11-30 — present so a wrong answer type is testable."""
    choice: str
    confidence: float
    probabilities: dict
    type: str = "choice"


@dataclass(frozen=True)
class Usage:
    """response_types.py:65-73: token counts, None when the API did not report them."""
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True)
class SystemOneResponse:
    """response_types.py:99-125: model, usage, answers keyed by question name, plus the
    `nouls` view that keeps only NoulAnswer entries (response_types.py:112-115)."""
    model: str
    usage: Usage
    answers: dict = field(default_factory=dict)

    @property
    def nouls(self) -> dict:
        """Yes/no answers keyed by question name (response_types.py:112-115)."""
        return {k: v for k, v in self.answers.items() if isinstance(v, NoulAnswer)}


class FakeClient:
    """Stands in for TypeSafeClient.system_one (sync/client.py:129-140).

    `answer(name, question, state)` returns the probability for one question; `delay`
    returns seconds to sleep for a call (to test concurrency and deadlines); `raises`
    is an exception to raise on the Nth call. Calls are recorded for shape assertions.
    Answers are returned in REVERSED name order so the backend cannot rely on order."""

    def __init__(self, answer=None, *, delay=None, raises=None, raise_on=0, shape=None):
        """Configure the fake's answers, latency and failure."""
        self.answer = answer or (lambda name, q, state: 0.9)
        self.delay = delay
        self.raises, self.raise_on = raises, raise_on
        self.shape = shape
        self.calls: list[dict] = []
        self.lock = threading.Lock()
        self.closed = False

    def system_one(self, state, questions, *, model=None, retry=None, timeout=None,
                   extra_headers=None, extra_body=None, response_model=None):
        """Same signature as the SDK (client.py:129-140); returns a SystemOneResponse."""
        with self.lock:
            n = len(self.calls)
            self.calls.append({"state": state, "questions": dict(questions), "model": model,
                               "timeout": timeout, "t": time.monotonic()})
        if self.delay:
            time.sleep(self.delay(n, questions))
        if self.raises is not None and n == self.raise_on:
            raise self.raises
        if self.shape is not None:
            return self.shape(questions)
        answers = {name: NoulAnswer(noul=self.answer(name, q, state))
                   for name, q in reversed(list(questions.items()))}
        return SystemOneResponse(model="jev-1.13.0", usage=Usage(input_tokens=123, output_tokens=len(answers)),
                                 answers=answers)

    def close(self):
        """TypeSafeClient.close (client.py:225)."""
        self.closed = True


def passages(n, size=200, prefix="p"):
    """n passages with distinct ids and texts; each text says its own position."""
    return [jb.Passage(f"{prefix}{i}", f"[{i}] " + "x" * size) for i in range(n)]


def by_position(name, q, state):
    """A deterministic probability derived from the passage's position in the call."""
    i = int(name[1:])
    return round((i % 10) / 10 + 0.05, 2)


Q = jb.Query(text="hybrid consideration for urban professionals", context="brand: BMW; market UK")


# ---- protocol and happy path ------------------------------------------------------------
def test_satisfies_the_backend_protocol():
    """The chain can hold jev as a Backend: name, capacity 50, calibrated."""
    b = jj.JevBackend(FakeClient())
    assert isinstance(b, jb.Backend)
    assert (b.name, b.capacity, b.calibrated) == ("jev", 50, True)


def test_score_is_the_calibrated_probability_value_is_threshold_raw_is_native():
    """score and raw are jev's probability unchanged, value applies the threshold, why stays None."""
    b = jj.JevBackend(FakeClient(by_position), env={})
    ps = passages(10)
    vs = b.score(Q, ps, deadline_s=5)
    assert len(vs) == 10
    for i, v in enumerate(vs):
        p = by_position(jj.question_name(i), None, None)
        assert v.score == p and v.raw == p
        assert v.value is (p >= 0.5)
        assert v.why is None and v.backend == "jev"


def test_threshold_is_inclusive_and_configurable_from_env():
    """RAG_JEV_THRESHOLD moves the gate, and a probability equal to it passes."""
    b = jj.JevBackend(FakeClient(lambda *a: 0.7), env={"RAG_JEV_THRESHOLD": "0.7"})
    assert b.score(Q, passages(1), deadline_s=5)[0].value is True
    b = jj.JevBackend(FakeClient(lambda *a: 0.69), env={"RAG_JEV_THRESHOLD": "0.7"})
    assert b.score(Q, passages(1), deadline_s=5)[0].value is False


def test_no_passages_makes_no_call():
    """Nothing to judge costs nothing: no request is sent."""
    c = FakeClient()
    assert jj.JevBackend(c).score(Q, [], deadline_s=5) == []
    assert c.calls == []


def test_request_wire_shape():
    """State, question names, Noul shape, model and timeout are exactly what the SDK expects."""
    c = FakeClient()
    b = jj.JevBackend(c, env={"RAG_JEV_MODEL": "jev-1.13.0"})
    b.score(Q, passages(2), deadline_s=5)
    (call,) = c.calls
    assert call["model"] == "jev-1.13.0"
    assert 0 < call["timeout"] <= 5
    assert call["state"] == {"retrieval_query": Q.text, "brief_context": Q.context}
    assert list(call["questions"]) == ["p000", "p001"]
    q = call["questions"]["p001"]
    # NoulModel, question_types.py:26-36: exactly type, instructions, criteria{true,false}.
    assert set(q) == {"type", "instructions", "criteria"} and q["type"] == "noul"
    assert q["instructions"] == {"question": jj.QUESTION, "passage": "[1] " + "x" * 200}
    assert set(q["criteria"]) == {"true", "false"}
    assert b.last_call["requests"] == 1 and b.last_call["models"] == ["jev-1.13.0"]


# ---- clipping and batching --------------------------------------------------------------
def test_state_clips_context_before_query():
    """Clipping the state removes brief context before any of the query."""
    s = jj.build_state(jb.Query(text="Q" * 10, context="C" * 100), max_chars=25)
    assert s == {"retrieval_query": "Q" * 10, "brief_context": "C" * 15}
    s = jj.build_state(jb.Query(text="Q" * 40, context="C" * 100), max_chars=25)
    assert s == {"retrieval_query": "Q" * 25}


def test_passages_are_clipped():
    """A passage longer than RAG_JEV_PASSAGE_CHARS is sent clipped."""
    c = FakeClient()
    jj.JevBackend(c, passage_chars=50).score(Q, passages(1, size=500), deadline_s=5)
    assert len(c.calls[0]["questions"]["p000"]["instructions"]["passage"]) == 50


def test_estimator_counts_non_ascii_as_a_token_each():
    """Four ASCII characters per token; each non-ASCII character a whole token."""
    assert jj.estimate_tokens("abcd" * 10) == 10
    assert jj.estimate_tokens("日本語") == 3


def _batch_tokens(state, batch):
    """What plan_batches promises about one batch, recomputed independently."""
    qs = {jj.question_name(i): q for i, q in batch}
    total = jj.REQUEST_OVERHEAD_TOKENS + jj.estimate_tokens(state) + sum(
        jj.estimate_tokens({n: q}) for n, q in qs.items())
    pair = jj.estimate_tokens(state) + max(jj.estimate_tokens({n: q}) for n, q in qs.items())
    return total, pair, len(json.dumps({"state": state, "questions": qs}))


def test_fifty_full_size_passages_split_across_requests_within_limits():
    """At full capacity and full clip size, batches stay in order and inside both token rules."""
    state = jj.build_state(jb.Query(text="q" * 1000, context="c" * 5000), 4000)
    texts = [("[%d] " % i) + "w" * 7000 for i in range(50)]
    batches = jj.plan_batches(state, texts, passage_chars=6000, batch_questions=50)
    assert len(batches) >= 2
    assert [i for b in batches for i, _ in b] == list(range(50))      # contiguous, in order
    for b in batches:
        total, pair, chars = _batch_tokens(state, b)
        assert total <= jj.REQUEST_BUDGET and pair <= jj.PAIR_BUDGET
        # Even at a pessimistic 3 chars/token the whole JSON body fits the hard 64k limit.
        assert chars / 3 <= jj.HARD_REQUEST_TOKENS


def test_non_ascii_passages_are_budgeted_as_one_token_per_character():
    """Dense non-ASCII text is split into more batches instead of overrunning the limit."""
    state = jj.build_state(Q, 4000)
    texts = ["語" * 6000 for _ in range(20)]
    batches = jj.plan_batches(state, texts, passage_chars=6000, batch_questions=50)
    assert len(batches) >= 3
    for b in batches:
        total, pair, _ = _batch_tokens(state, b)
        assert total <= jj.REQUEST_BUDGET and pair <= jj.PAIR_BUDGET


def test_a_passage_too_big_to_sit_beside_the_state_is_clipped_further():
    """A passage that would break the state-plus-question rule is cut until it fits."""
    state = jj.build_state(Q, 4000)
    (batch,) = jj.plan_batches(state, ["語" * 40_000], passage_chars=40_000, batch_questions=50)
    _, pair, _ = _batch_tokens(state, batch)
    assert pair <= jj.PAIR_BUDGET
    assert 0 < len(batch[0][1]["instructions"]["passage"]) < 40_000


def test_batch_question_cap():
    """RAG_JEV_BATCH_QUESTIONS caps the Nouls per request."""
    batches = jj.plan_batches(jj.build_state(Q, 4000), ["t"] * 25, passage_chars=100, batch_questions=10)
    assert [len(b) for b in batches] == [10, 10, 5]


def test_order_preserved_across_batches_that_finish_out_of_order():
    """Verdicts follow input order even when batches and answers come back shuffled."""
    # Later batches return first; answers inside each batch come back reversed.
    c = FakeClient(by_position, delay=lambda n, qs: 0.3 - 0.1 * n)
    b = jj.JevBackend(c, batch_questions=5, concurrency=4)
    ps = passages(15)
    vs = b.score(Q, ps, deadline_s=5)
    assert len(c.calls) == 3
    assert [v.score for v in vs] == [by_position(jj.question_name(i), None, None) for i in range(15)]
    assert all(call["state"] == c.calls[0]["state"] for call in c.calls)   # one stable state


def test_batches_run_concurrently():
    """Four batches of 0.3 s finish in well under 4 x 0.3 s."""
    c = FakeClient(delay=lambda n, qs: 0.3)
    t0 = time.monotonic()
    jj.JevBackend(c, batch_questions=5, concurrency=4).score(Q, passages(20), deadline_s=5)
    assert len(c.calls) == 4 and time.monotonic() - t0 < 0.9


# ---- run-time failures --------------------------------------------------------------------
@pytest.mark.parametrize("exc, kind, status", [
    (TypeSafeAPITimeoutError(4.0), "timeout", None),
    (TypeSafeAPIConnectionError("Connection error: refused"), "http_error", None),
    (TypeSafeRateLimitError(429, {"error": "slow down"}, {}), "rate_limited", 429),
    (TypeSafeAuthenticationError(401, {"error": "bad key"}, {}), "not_entitled", 401),
    (TypeSafePermissionDeniedError(403, None, {}), "not_entitled", 403),
    (TypeSafeNotFoundError(404, {"detail": "model not found"}, {}), "http_error", 404),  # bare 404: our model alias, as for nemotron
    (TypeSafeAPIError(410, None, {}), "retired", 410),
    (TypeSafeAPIError(408, None, {}), "timeout", 408),
    (TypeSafeBadRequestError(400, None, {}), "http_error", 400),
    (TypeSafeUnprocessableEntityError(422, None, {}), "http_error", 422),
    (TypeSafeInternalServerError(503, None, {}), "http_error", 503),
    (TypeSafeAPIResponseValidationError(200, {}, {}, "answers.p000.noul"), "bad_response", 200),
    (TypeSafeError("At least one question is required."), "error", None),
    (RuntimeError("anything else"), "error", None),
])
def test_sdk_errors_map_onto_unavailable_kinds(exc, kind, status):
    """Each SDK exception class becomes the right BackendUnavailable kind and status."""
    b = jj.JevBackend(FakeClient(raises=exc))
    with pytest.raises(jb.BackendUnavailable) as e:
        b.score(Q, passages(3), deadline_s=5)
    assert (e.value.kind, e.value.status) == (kind, status)
    assert type(exc).__name__ in str(e.value)


def test_one_failed_batch_fails_the_whole_call():
    """A single failed batch fails score(): the contract forbids partial output."""
    c = FakeClient(raises=TypeSafeInternalServerError(502, None, {}), raise_on=1)
    with pytest.raises(jb.BackendUnavailable) as e:
        jj.JevBackend(c, batch_questions=2).score(Q, passages(6), deadline_s=5)
    assert e.value.kind == "http_error" and e.value.status == 502


def test_deadline_is_never_overrun():
    """A hung request raises timeout at the deadline instead of blocking."""
    c = FakeClient(delay=lambda n, qs: 2.0)
    t0 = time.monotonic()
    with pytest.raises(jb.BackendUnavailable) as e:
        jj.JevBackend(c).score(Q, passages(3), deadline_s=0.3)
    assert e.value.kind == "timeout"
    assert time.monotonic() - t0 < 0.6
    assert c.calls[0]["timeout"] <= 0.3


def test_spent_deadline_sends_nothing():
    """A zero deadline raises timeout before any request is sent."""
    c = FakeClient()
    with pytest.raises(jb.BackendUnavailable) as e:
        jj.JevBackend(c).score(Q, passages(1), deadline_s=0)
    assert e.value.kind == "timeout" and c.calls == []


def test_abandoned_workers_are_daemon_threads_counted_as_stranded():
    """A request that never returns leaves a DAEMON worker (so it cannot hold the process
    open at exit), counted as stranded until it really ends."""
    base = jj.stranded_workers()
    c = HangingClient()
    try:
        with pytest.raises(jb.BackendUnavailable) as e:
            jj.JevBackend(c, env={}).score(Q, passages(3), deadline_s=0.2)
        assert e.value.kind == "timeout"
        alive = jev_workers()
        assert alive and all(t.daemon for t in alive)
        assert jj.stranded_workers() == base + 1
    finally:
        c.release.set()
    assert wait_until(lambda: jj.stranded_workers() == base)


def test_process_exits_although_a_request_never_returns(tmp_path):
    """The whole interpreter exits promptly with a hung request outstanding. With a
    ThreadPoolExecutor this hung forever, because executor workers are joined at exit."""
    script = tmp_path / "hang.py"
    script.write_text(f"""
import sys, threading
sys.path.insert(0, {str(HERE)!r})
import judge_base as jb, judge_jev as jj


class Hang:
    \"\"\"A client whose request never finishes.\"\"\"

    def system_one(self, state, questions, **kw):
        \"\"\"Block forever.\"\"\"
        threading.Event().wait()


try:
    jj.JevBackend(Hang(), env={{}}).score(jb.Query("q"), [jb.Passage("a", "t")], deadline_s=0.2)
except jb.BackendUnavailable as e:
    print(e.kind)
""")
    t0 = time.monotonic()
    r = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=30,
                       env={**os.environ, "RAG_STORE": "local"})
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "timeout"
    assert time.monotonic() - t0 < 15


def test_batches_not_started_by_the_deadline_are_never_sent():
    """With one slot and three batches, a hung first batch means the other two are never
    sent, even after the hung one ends: the call has already given up."""
    base = jj.stranded_workers()
    c = HangingClient()
    try:
        with pytest.raises(jb.BackendUnavailable) as e:
            jj.JevBackend(c, env={}, batch_questions=1, concurrency=1).score(Q, passages(3), deadline_s=0.2)
        assert e.value.kind == "timeout" and "3 of 3" in str(e.value)
    finally:
        c.release.set()
    assert wait_until(lambda: jj.stranded_workers() == base)
    time.sleep(0.05)
    assert c.calls == 1


def test_too_many_stranded_workers_refuses_without_sending(monkeypatch):
    """While MAX_STRANDED abandoned workers are alive, score() fails at once and sends nothing."""
    base = jj.stranded_workers()
    monkeypatch.setattr(jj, "MAX_STRANDED", base + 1)
    hung = HangingClient()
    try:
        with pytest.raises(jb.BackendUnavailable):
            jj.JevBackend(hung, env={}).score(Q, passages(1), deadline_s=0.2)
        fresh = FakeClient()
        t0 = time.monotonic()
        with pytest.raises(jb.BackendUnavailable) as e:
            jj.JevBackend(fresh, env={}).score(Q, passages(1), deadline_s=5)
        assert e.value.kind == "timeout" and "still hung" in str(e.value)
        assert fresh.calls == [] and time.monotonic() - t0 < 0.1
    finally:
        hung.release.set()
    assert wait_until(lambda: jj.stranded_workers() == base)
    assert jj.JevBackend(FakeClient(), env={}).score(Q, passages(1), deadline_s=5)[0].score == 0.9


def test_a_successful_call_strands_nothing():
    """Workers that finished are joined, not counted as stranded."""
    base = jj.stranded_workers()
    jj.JevBackend(FakeClient(), env={}, batch_questions=2).score(Q, passages(8), deadline_s=5)
    assert jj.stranded_workers() == base


@pytest.mark.parametrize("shape", [
    lambda qs: SystemOneResponse("jev", Usage(), {n: NoulAnswer(0.5) for n in list(qs)[1:]}),  # one missing
    lambda qs: SystemOneResponse("jev", Usage(), {n: NoulAnswer(1.5) for n in qs}),           # out of range
    lambda qs: SystemOneResponse("jev", Usage(), {n: NoulAnswer(math.nan) for n in qs}),      # NaN
    lambda qs: SystemOneResponse("jev", Usage(), {n: ChoiceAnswer("a", 0.9, {}) for n in qs}),  # wrong type
    lambda qs: SimpleNamespace(model="jev"),                                                   # no nouls at all
])
def test_malformed_responses_are_bad_response(shape):
    """Missing, out-of-range, NaN or wrongly typed answers raise bad_response."""
    with pytest.raises(jb.BackendUnavailable) as e:
        jj.JevBackend(FakeClient(shape=shape)).score(Q, passages(3), deadline_s=5)
    assert e.value.kind == "bad_response"


# ---- construction: not configured is a hard error ----------------------------------------
class FakeRetryPolicy:
    """retry.py:36-86 — only max_retries matters here."""

    def __init__(self, max_retries=2, **kw):
        """Record the retry count the backend asked for."""
        self.max_retries = max_retries


def fake_sdk(client_error=None):
    """A stand-in for the typesafe_sdk module: TypeSafeClient and RetryPolicy only."""
    built = []

    def TypeSafeClient(**kw):  # noqa: N802 - mirrors the SDK's class name
        """Keyword-only constructor as in sync/client.py:22-33; may raise TypeSafeError."""
        if client_error:
            raise client_error
        built.append(kw)
        return FakeClient()
    return SimpleNamespace(TypeSafeClient=TypeSafeClient, RetryPolicy=FakeRetryPolicy, built=built)


def test_builds_the_real_client_with_retries_off_and_the_configured_model():
    """The real construction path passes the key, the model and max_retries=0."""
    sdk = fake_sdk()
    b = jj.JevBackend(sdk=sdk, env={**KEY_ENV, "RAG_JEV_MODEL": "jev-1.13.0"})
    (kw,) = sdk.built
    assert kw["api_key"] == "ts-test-key" and kw["model"] == "jev-1.13.0"
    assert kw["retry"].max_retries == 0
    assert b.score(Q, passages(1), deadline_s=5)[0].backend == "jev"


def test_missing_key_is_not_configured_with_instructions():
    """No TYPESAFE_API_KEY is a hard error saying what to set."""
    with pytest.raises(jb.BackendNotConfigured) as e:
        jj.JevBackend(sdk=fake_sdk(), env={"TYPESAFE_API_KEY": "  "})
    assert "TYPESAFE_API_KEY" in str(e.value) and "RAG_VALIDATOR" in str(e.value)


def test_missing_sdk_is_not_configured_with_install_line(monkeypatch):
    """No SDK is a hard error giving the exact install command."""
    def no_sdk():
        """Behave as if typesafe_sdk is not installed."""
        raise ImportError("No module named 'typesafe_sdk'")
    monkeypatch.setattr(jj, "_load_sdk", no_sdk)
    with pytest.raises(jb.BackendNotConfigured) as e:
        jj.JevBackend(env=KEY_ENV)
    assert "pip install 'typesafe-sdk==0.7.1'" in str(e.value)


def test_missing_sdk_and_key_are_reported_together(monkeypatch):
    """Both problems are listed in one error, not discovered one at a time."""
    def no_sdk():
        """Behave as if typesafe_sdk is not installed."""
        raise ImportError("nope")
    monkeypatch.setattr(jj, "_load_sdk", no_sdk)
    with pytest.raises(jb.BackendNotConfigured) as e:
        jj.JevBackend(env={})
    assert "pip install" in str(e.value) and "TYPESAFE_API_KEY" in str(e.value)


def test_sdk_rejecting_the_key_is_not_configured():
    """A key the SDK rejects at construction is a configuration error."""
    err = TypeSafeError("API key must contain only printable ASCII characters without whitespace.")
    with pytest.raises(jb.BackendNotConfigured) as e:
        jj.JevBackend(sdk=fake_sdk(client_error=err), env=KEY_ENV)
    assert "printable ASCII" in str(e.value)


def test_builds_the_client_with_the_base_url_from_the_given_env(monkeypatch):
    """TYPESAFE_BASE_URL is read from the injected mapping and passed explicitly (trailing
    slash dropped); os.environ cannot override it, and the SDK default applies when unset."""
    monkeypatch.setenv("TYPESAFE_BASE_URL", "not a url")
    sdk = fake_sdk()
    jj.JevBackend(sdk=sdk, env={**KEY_ENV, "TYPESAFE_BASE_URL": " http://localhost:8080/ "})
    jj.JevBackend(sdk=sdk, env=KEY_ENV)
    assert [kw["base_url"] for kw in sdk.built] == ["http://localhost:8080", jj.DEFAULT_BASE_URL]


@pytest.mark.parametrize("url", ["api.typesafe.ai", "htps://api.typesafe.ai", "https://",
                                 "http://host:port", "ftp://api.typesafe.ai", "http://[::1"])
def test_malformed_base_url_is_not_configured(url):
    """A base URL without an http(s) scheme and a host is a construction error, not a
    per-request http_error the chain would silently fall through on."""
    with pytest.raises(jb.BackendNotConfigured) as e:
        jj.JevBackend(sdk=fake_sdk(), env={**KEY_ENV, "TYPESAFE_BASE_URL": url})
    assert "TYPESAFE_BASE_URL" in str(e.value) and repr(url) in str(e.value)


def test_bad_base_url_and_missing_key_are_reported_together():
    """The base URL is checked even when the key is missing, so one error lists both."""
    with pytest.raises(jb.BackendNotConfigured) as e:
        jj.JevBackend(sdk=fake_sdk(), env={"TYPESAFE_BASE_URL": "api.typesafe.ai"})
    assert "TYPESAFE_API_KEY" in str(e.value) and "TYPESAFE_BASE_URL" in str(e.value)


def test_engine_env_is_loaded_when_no_env_is_injected(monkeypatch):
    """With no `env`, engine/.env is loaded first, so a key that is only in that file works."""
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("TYPESAFE_BASE_URL", raising=False)

    def load():
        """Stand in for rag.py's loader: put the file's key into os.environ."""
        monkeypatch.setenv("TYPESAFE_API_KEY", "ts-from-dotenv")
    monkeypatch.setattr(jj, "_load_env", load)
    sdk = fake_sdk()
    jj.JevBackend(sdk=sdk)
    assert sdk.built[0]["api_key"] == "ts-from-dotenv"


def test_an_injected_env_never_loads_engine_env(monkeypatch):
    """An injected mapping is the whole environment: engine/.env is not read."""
    def forbidden():
        """Fail the test if engine/.env would be loaded."""
        raise AssertionError("engine/.env loaded although env was injected")
    monkeypatch.setattr(jj, "_load_env", forbidden)
    jj.JevBackend(sdk=fake_sdk(), env=KEY_ENV)


def test_env_loader_tolerates_a_tree_without_rag(monkeypatch):
    """The real loader imports rag.py for its side effect; without rag.py it does nothing."""
    monkeypatch.undo()                       # the real _load_env, not the autouse stub
    monkeypatch.setitem(sys.modules, "rag", None)   # makes `import rag` raise ImportError
    assert jj._load_env() is None


def test_client_is_built_without_the_deadline_transport_only_when_httpx2_is_missing(monkeypatch):
    """A stand-in SDK without httpx2 still builds; the transport is simply not passed."""
    def no_httpx():
        """Behave as if httpx2 is not installed."""
        raise ImportError("No module named 'httpx2'")
    monkeypatch.setattr(jj, "_load_httpx", no_httpx)
    sdk = fake_sdk()
    jj.JevBackend(sdk=sdk, env=KEY_ENV)
    assert "transport" not in sdk.built[0]


@pytest.mark.parametrize("env", [
    {"RAG_JEV_THRESHOLD": "high"},
    {"RAG_JEV_THRESHOLD": "1.5"},
    {"RAG_JEV_THRESHOLD": "nan"},
    {"RAG_JEV_CONCURRENCY": "0"},
    {"RAG_JEV_PASSAGE_CHARS": "lots"},
    {"RAG_JEV_STATE_CHARS": "200000"},      # the state alone would break the 32k rule
])
def test_nonsensical_settings_are_not_configured(env):
    """Unparseable or out-of-range settings fail at construction."""
    with pytest.raises(jb.BackendNotConfigured):
        jj.JevBackend(FakeClient(), env=env)


def test_close_closes_the_client():
    """close() releases the SDK client."""
    c = FakeClient()
    jj.JevBackend(c).close()
    assert c.closed


def test_an_injected_client_never_touches_the_sdk_or_the_key(monkeypatch):
    """With a client injected, neither the SDK nor the key is needed."""
    def forbidden():
        """Fail the test if the SDK is imported."""
        raise AssertionError("SDK imported although a client was injected")
    monkeypatch.setattr(jj, "_load_sdk", forbidden)
    assert jj.JevBackend(FakeClient(), env={}).score(Q, passages(1), deadline_s=5)[0].score == 0.9


# ---- the real SDK, offline ------------------------------------------------------------------
def _real_sdk():
    """Import the real SDK, from RAG_JEV_SDK_PATH if set; skip the test if unavailable."""
    extra = os.environ.get("RAG_JEV_SDK_PATH")
    if extra and extra not in sys.path:
        sys.path.insert(0, extra)
    sdk = pytest.importorskip("typesafe_sdk")
    httpx2 = pytest.importorskip("httpx2")
    wire = pytest.importorskip("typesafe_sdk._schemas.models")
    return sdk, httpx2, wire


def test_real_sdk_round_trip_through_a_mock_transport():
    """The real SDK accepts our request (checked against its own schema) and we read its parsed response."""
    sdk, httpx2, wire = _real_sdk()
    seen = []

    def handler(request):
        """Answer POST /v1/systemone as the API would, after checking the request body
        against the SDK's own generated request schema."""
        body = json.loads(request.content)
        wire.SystemOneRequest.model_validate(body)
        seen.append((request.method, request.url.path, request.headers.get("authorization"), body))
        answers = {name: {"type": "noul", "noul": 0.25 + 0.5 * (int(name[1:]) % 2)}
                   for name in reversed(list(body["questions"]))}
        return httpx2.Response(200, json={"model": "jev-1.13.0", "answers": answers,
                                          "usage": {"input_tokens": 900, "output_tokens": 3}},
                               headers={"x-typesafe-request-id": "req-1"})

    client = sdk.TypeSafeClient(api_key="ts-test-key", retry=sdk.RetryPolicy(max_retries=0),
                                transport=httpx2.MockTransport(handler))
    b = jj.JevBackend(client, batch_questions=2)
    vs = b.score(Q, passages(5), deadline_s=5)
    assert [v.score for v in vs] == [0.25, 0.75, 0.25, 0.75, 0.25]
    assert [v.value for v in vs] == [False, True, False, True, False]
    assert len(seen) == 3 and all(s[:3] == ("POST", "/v1/systemone", "Bearer ts-test-key") for s in seen)
    assert b.last_call["models"] == ["jev-1.13.0"] and b.last_call["input_tokens"] == 2700


@pytest.mark.parametrize("status, kind", [(429, "rate_limited"), (401, "not_entitled"),
                                          (404, "http_error"), (422, "http_error"), (503, "http_error")])
def test_real_sdk_http_errors_map(status, kind):
    """HTTP statuses raised by the real SDK map to the expected kinds."""
    sdk, httpx2, _ = _real_sdk()
    client = sdk.TypeSafeClient(api_key="k", retry=sdk.RetryPolicy(max_retries=0),
                                transport=httpx2.MockTransport(
                                    lambda r: httpx2.Response(status, json={"error": "x"})))
    with pytest.raises(jb.BackendUnavailable) as e:
        jj.JevBackend(client).score(Q, passages(1), deadline_s=5)
    assert (e.value.kind, e.value.status) == (kind, status)


def test_real_sdk_malformed_body_and_timeout_map():
    """The real SDK's response-validation and timeout errors map to bad_response and timeout."""
    sdk, httpx2, _ = _real_sdk()
    bad = sdk.TypeSafeClient(api_key="k", retry=sdk.RetryPolicy(max_retries=0),
                             transport=httpx2.MockTransport(
                                 lambda r: httpx2.Response(200, json={"model": "jev", "answers": {}})))
    with pytest.raises(jb.BackendUnavailable) as e:
        jj.JevBackend(bad).score(Q, passages(1), deadline_s=5)
    assert e.value.kind == "bad_response"

    def slow(request):
        """Raise the transport-level timeout httpx2 raises on a read timeout."""
        raise httpx2.ReadTimeout("read timed out", request=request)
    t = sdk.TypeSafeClient(api_key="k", retry=sdk.RetryPolicy(max_retries=0), transport=httpx2.MockTransport(slow))
    with pytest.raises(jb.BackendUnavailable) as e:
        jj.JevBackend(t).score(Q, passages(1), deadline_s=5)
    assert e.value.kind == "timeout"


def test_backend_builds_its_client_on_the_deadline_transport(monkeypatch):
    """The real construction path hands TypeSafeClient the deadline-aware transport."""
    _, httpx2, _ = _real_sdk()
    sdk = fake_sdk()
    jj.JevBackend(sdk=sdk, env=KEY_ENV)
    transport_cls, _ = jj._deadline_types(httpx2)
    assert isinstance(sdk.built[0]["transport"], transport_cls)


def test_deadline_transport_ends_a_trickling_body():
    """A server that sends one byte every 50 ms forever is cut off at the deadline, and the
    worker then EXITS (not merely abandoned): httpx's per-read timeout would never fire."""
    sdk, httpx2, _ = _real_sdk()
    stop = threading.Event()

    def trickle():
        """An endless response body, one space every 50 ms, until the test stops it."""
        while not stop.is_set():
            time.sleep(0.05)
            yield b" "

    def handler(request):
        """Send headers at once, then trickle the body."""
        return httpx2.Response(200, headers={"content-type": "application/json"}, content=trickle())

    base = jj.stranded_workers()
    before = set(jev_workers())            # earlier tests may still have stranded workers
    client = sdk.TypeSafeClient(api_key="k", retry=sdk.RetryPolicy(max_retries=0),
                                transport=jj.deadline_transport(httpx2.MockTransport(handler)))
    try:
        t0 = time.monotonic()
        with pytest.raises(jb.BackendUnavailable) as e:
            jj.JevBackend(client).score(Q, passages(1), deadline_s=0.3)
        assert e.value.kind == "timeout" and time.monotonic() - t0 < 0.6
        assert wait_until(lambda: jj.stranded_workers() == base, timeout=1.0)
        assert wait_until(lambda: not set(jev_workers()) - before, timeout=1.0)
    finally:
        stop.set()


def test_deadline_transport_is_transparent_within_the_deadline():
    """A normal reply passes through the deadline transport unchanged."""
    sdk, httpx2, _ = _real_sdk()

    def handler(request):
        """Answer every question with 0.8."""
        names = json.loads(request.content)["questions"]
        return httpx2.Response(200, json={"model": "jev-1.13.0",
                                          "answers": {n: {"type": "noul", "noul": 0.8} for n in names},
                                          "usage": {"input_tokens": 5, "output_tokens": 1}})
    client = sdk.TypeSafeClient(api_key="k", retry=sdk.RetryPolicy(max_retries=0),
                                transport=jj.deadline_transport(httpx2.MockTransport(handler)))
    assert [v.score for v in jj.JevBackend(client).score(Q, passages(3), deadline_s=5)] == [0.8] * 3
