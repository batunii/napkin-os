"""The hosted NVIDIA reranker backend. No network: requests.post is replaced in every test.
Run: cd engine/rag && RAG_STORE=local RAG_INDEX=./_index_v3 python3 -m pytest test_judge_nemotron.py -q
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import pytest  # noqa: E402
import requests  # noqa: E402
import judge_base as jb  # noqa: E402
import judge_nemotron as jn  # noqa: E402


class FakeRaw:
    """The slice of urllib3's HTTPResponse the backend streams from: read1(amt).

    Hands the body back in pieces of at most `piece` bytes, optionally sleeping `delay`
    seconds before each one, which is how a trickling server looks to the reader."""

    def __init__(self, body: bytes, piece: int = 1 << 20, delay: float = 0.0, endless: bool = False):
        """`endless` keeps yielding pieces forever, like a server that never finishes."""
        self._body, self._piece, self._delay, self._endless = body, piece, delay, endless
        self.reads = 0

    def read1(self, amt: int, decode_content: bool = True) -> bytes:
        """Return the next piece (never more than `amt`), or b"" at end of body."""
        self.reads += 1
        if self._delay:
            time.sleep(self._delay)
        if self._endless:
            return b" "
        n = min(amt, self._piece)
        out, self._body = self._body[:n], self._body[n:]
        return out


class FakeResponse:
    """The slice of requests.Response the backend reads: status_code, raw, close()."""

    def __init__(self, status: int = 200, payload=None, text: str | None = None, raw: FakeRaw | None = None):
        """A response with `payload` as its JSON body, or raw `text` that may not parse,
        streamed through `raw` (a FakeRaw over that text unless one is given)."""
        self.status_code = status
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload)
        self.raw = raw if raw is not None else FakeRaw(self.text.encode("utf-8"))
        self.closed = False

    def close(self) -> None:
        """Record that the backend released the connection."""
        self.closed = True


class Recorder:
    """A stand-in for requests.post that records every call and replays one outcome."""

    def __init__(self, outcome):
        """`outcome` is a FakeResponse to return, an exception to raise, or a callable
        taking the request kwargs and returning either."""
        self.outcome = outcome
        self.calls: list[dict] = []

    def __call__(self, url, **kw):
        """Record the call, then return or raise the configured outcome."""
        self.calls.append({"url": url, **kw})
        out = self.outcome(kw) if callable(self.outcome) else self.outcome
        if isinstance(out, BaseException):
            raise out
        return out


def ok_for(logits: list[float]) -> FakeResponse:
    """A 200 response for these input-order logits, with rankings SORTED BY SCORE as the
    service returns them, so the backend has to map back by index."""
    order = sorted(range(len(logits)), key=lambda i: -logits[i])
    return FakeResponse(200, {"rankings": [{"index": i, "logit": logits[i]} for i in order],
                              "usage": {"prompt_tokens": 1, "total_tokens": 1}})


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fail loudly if any test reaches the real requests.post, and start every test with
    the once-per-process warning re-armed and no stray env knobs."""
    def refuse(*a, **kw):
        """Guard: a test that forgot to install a fake must not call NVIDIA."""
        raise AssertionError("test attempted a real HTTP call")
    monkeypatch.setattr(jn.requests, "post", refuse)
    monkeypatch.setattr(jn, "_warned_uncalibrated", False)
    for k in ("RAG_NEMOTRON_CHARS", "RAG_NEMOTRON_QUERY_CHARS"):
        monkeypatch.delenv(k, raising=False)
    assert jn.stranded_workers() == 0, "an earlier test left a worker running"
    yield
    wait_for_no_stranded()


def wait_for_no_stranded(limit_s: float = 3.0) -> float:
    """Wait until every abandoned worker has exited; return how long that took. Fails
    the test if one is still alive after `limit_s`, which is the defect being guarded."""
    t = time.monotonic()
    while jn.stranded_workers() and time.monotonic() - t < limit_s:
        time.sleep(0.01)
    assert jn.stranded_workers() == 0, f"{jn.stranded_workers()} worker(s) outlived score() by {limit_s}s"
    return time.monotonic() - t


def make(tmp_path, **kw) -> jn.NemotronBackend:
    """A backend with a test key and, unless told otherwise, no calibration file."""
    kw.setdefault("api_key", "test-key")
    kw.setdefault("calibration_path", tmp_path / "absent.json")
    return jn.NemotronBackend(**kw)


def passages(n: int, text: str = "passage") -> list[jb.Passage]:
    """n passages with distinct ids and texts."""
    return [jb.Passage(f"p{i}", f"{text} {i}") for i in range(n)]


def install(monkeypatch, outcome) -> Recorder:
    """Replace requests.post with a Recorder replaying `outcome`."""
    rec = Recorder(outcome)
    monkeypatch.setattr(jn.requests, "post", rec)
    return rec


# ---- shape and ordering ------------------------------------------------------------
def test_satisfies_the_backend_protocol(tmp_path):
    """The backend is a judge_base.Backend with the measured capacity, uncalibrated by default."""
    b = make(tmp_path)
    assert isinstance(b, jb.Backend)
    assert (b.name, b.capacity, b.calibrated, b.calibration) == ("nemotron", 40, False, None)


def test_sorted_rankings_are_mapped_back_to_input_order(tmp_path, monkeypatch):
    """Rankings arrive sorted by score; verdicts must come back in INPUT order by index."""
    logits = [-3.0, 8.5, 0.5, -9.1, 4.2]
    rec = install(monkeypatch, ok_for(logits))
    ps = passages(5)
    vs = make(tmp_path).score(jb.Query("q"), ps, deadline_s=2)
    served = [r["index"] for r in rec.outcome._payload["rankings"]]
    assert served == [1, 4, 2, 0, 3]                          # the fake really is score-sorted
    assert [v.raw for v in vs] == logits                      # ...and verdicts are input-ordered
    assert [v.value for v in vs] == [False, True, True, False, True]
    assert all(v.backend == "nemotron" for v in vs)


def test_empty_pool_makes_no_call(tmp_path, monkeypatch):
    """Nothing to judge costs nothing: no request is made."""
    rec = install(monkeypatch, ok_for([]))
    assert make(tmp_path).score(jb.Query("q"), [], deadline_s=2) == []
    assert rec.calls == []


def test_request_body_headers_and_timeout(tmp_path, monkeypatch):
    """The body, auth header and (connect, read) timeout match the measured API."""
    rec = install(monkeypatch, ok_for([1.0, -1.0]))
    make(tmp_path).score(jb.Query("the query", context="ctx"), passages(2), deadline_s=2.5)
    (call,) = rec.calls
    assert call["url"] == jn.URL
    assert call["headers"]["Authorization"] == "Bearer test-key"
    assert call["timeout"] == (2.5, 2.5)
    assert call["stream"] is True                            # the body is read under our clock
    body = call["json"]
    assert body["model"] == "nvidia/llama-nemotron-rerank-vl-1b-v2"
    assert body["truncate"] == "END"
    assert body["query"] == {"text": "the query\n\nBrief context:\nctx"}
    assert body["passages"] == [{"text": "passage 0"}, {"text": "passage 1"}]


# ---- truncation --------------------------------------------------------------------
def test_long_passages_are_clipped_to_the_default_cap(tmp_path, monkeypatch):
    """Passages longer than DEFAULT_CHARS are clipped from the end, keeping the header."""
    rec = install(monkeypatch, ok_for([1.0, 1.0]))
    long = [jb.Passage("a", "H" * 50 + "x" * 5000), jb.Passage("b", "short")]
    make(tmp_path).score(jb.Query("q"), long, deadline_s=2)
    sent = [p["text"] for p in rec.calls[0]["json"]["passages"]]
    assert len(sent[0]) == jn.DEFAULT_CHARS == 1500
    assert sent[0].startswith("H" * 50)                      # the header survives the clip
    assert sent[1] == "short"


def test_env_overrides_passage_and_query_caps(tmp_path, monkeypatch):
    """RAG_NEMOTRON_CHARS and RAG_NEMOTRON_QUERY_CHARS change the clips; context is cut first."""
    monkeypatch.setenv("RAG_NEMOTRON_CHARS", "10")
    monkeypatch.setenv("RAG_NEMOTRON_QUERY_CHARS", "12")
    rec = install(monkeypatch, ok_for([1.0]))
    b = make(tmp_path)
    b.score(jb.Query("QUERY", context="C" * 500), [jb.Passage("a", "y" * 100)], deadline_s=2)
    body = rec.calls[0]["json"]
    assert body["passages"][0]["text"] == "y" * 10
    assert body["query"]["text"] == "QUERY\n\nBrief"           # context goes before the query does
    assert (b.chars, b.query_chars) == (10, 12)


def test_default_query_cap_bounds_a_long_brief_context(tmp_path, monkeypatch):
    """A huge brief context cannot multiply the per-pair cost past DEFAULT_QUERY_CHARS."""
    rec = install(monkeypatch, ok_for([1.0]))
    make(tmp_path).score(jb.Query("q", context="c" * 10_000), passages(1), deadline_s=2)
    assert len(rec.calls[0]["json"]["query"]["text"]) == jn.DEFAULT_QUERY_CHARS


@pytest.mark.parametrize("value", ["abc", "0", "-5"])
def test_bad_cap_in_env_is_a_configuration_error(tmp_path, monkeypatch, value):
    """A cap that is not a positive integer fails at construction rather than being guessed."""
    monkeypatch.setenv("RAG_NEMOTRON_CHARS", value)
    with pytest.raises(jb.BackendNotConfigured):
        make(tmp_path)


# ---- configuration -----------------------------------------------------------------
def test_missing_key_is_a_hard_error_at_construction(tmp_path, monkeypatch):
    """No NVIDIA_API_KEY (unset or blank) raises BackendNotConfigured, not a run-time fallthrough."""
    monkeypatch.setattr(jn, "_load_env", lambda: None)
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    with pytest.raises(jb.BackendNotConfigured, match="NVIDIA_API_KEY"):
        jn.NemotronBackend(calibration_path=tmp_path / "absent.json")
    monkeypatch.setenv("NVIDIA_API_KEY", "   ")
    with pytest.raises(jb.BackendNotConfigured):
        jn.NemotronBackend(calibration_path=tmp_path / "absent.json")


def test_key_is_read_after_engine_env_is_loaded(tmp_path, monkeypatch):
    """The key is looked up after the .env loader runs, and never leaks into describe()."""
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    loaded = []

    def fake_load():
        """Stand-in for rag's .env loader: sets the key the way engine/.env would."""
        loaded.append(True)
        monkeypatch.setenv("NVIDIA_API_KEY", "from-dotenv")
    monkeypatch.setattr(jn, "_load_env", fake_load)
    b = jn.NemotronBackend(calibration_path=tmp_path / "absent.json")
    assert loaded and b._key == "from-dotenv"
    assert "from-dotenv" not in json.dumps(b.describe())     # the trace never carries the key


def test_real_env_loader_is_rags():
    """The production loader is rag.py's, so the key is found exactly as embeddings find it."""
    jn._load_env()
    assert "rag" in sys.modules and hasattr(sys.modules["rag"], "_load_dotenv")


def test_construction_makes_no_network_call(tmp_path, monkeypatch):
    """Building the backend never calls the endpoint; reachability is a score()-time question."""
    rec = install(monkeypatch, AssertionError("constructed a backend over the network"))
    make(tmp_path)
    assert rec.calls == []


# ---- run-time failure classes ------------------------------------------------------
@pytest.mark.parametrize("status,text,kind", [
    (410, '{"detail":"This endpoint has reached its end of life on 2026-06-01."}', "retired"),
    (404, '{"status":404,"title":"Not Found","detail":"Not found for account"}', "not_entitled"),
    (404, "404 page not found\n", "http_error"),
    (403, '{"status":403,"title":"Forbidden","detail":"Authorization failed"}', "not_entitled"),
    (429, '{"detail":"Too Many Requests"}', "rate_limited"),
    (400, '{"detail":"bad request"}', "http_error"),
    (500, "Internal Server Error", "http_error"),
    (503, "", "http_error"),
])
def test_http_failures_are_classified(tmp_path, monkeypatch, status, text, kind):
    """Each measured HTTP failure maps to its kind and status, with exactly one attempt."""
    rec = install(monkeypatch, FakeResponse(status, text=text))
    with pytest.raises(jb.BackendUnavailable) as e:
        make(tmp_path).score(jb.Query("q"), passages(2), deadline_s=2)
    assert (e.value.kind, e.value.status) == (kind, status)
    assert len(rec.calls) == 1                               # never retried inside score()


@pytest.mark.parametrize("exc,kind", [
    (requests.exceptions.ReadTimeout("read timed out"), "timeout"),
    (requests.exceptions.ConnectTimeout("connect timed out"), "timeout"),
    (requests.exceptions.ConnectionError("refused"), "http_error"),
    (requests.exceptions.SSLError("bad cert"), "http_error"),
    (RuntimeError("something odd"), "error"),
])
def test_transport_failures_are_classified(tmp_path, monkeypatch, exc, kind):
    """Timeouts map to 'timeout'; other transport failures to 'error'; one attempt each."""
    rec = install(monkeypatch, exc)
    with pytest.raises(jb.BackendUnavailable) as e:
        make(tmp_path).score(jb.Query("q"), passages(2), deadline_s=2)
    assert e.value.kind == kind
    assert len(rec.calls) == 1


def test_a_hung_call_returns_at_the_deadline(tmp_path, monkeypatch):
    """A call that never answers raises timeout at deadline_s, not when the socket gives up."""
    release = threading.Event()

    def hang(kw):
        """Behave like the 5-in-154 pilot calls: accept the request, never answer."""
        release.wait(10)
        return ok_for([1.0])
    install(monkeypatch, hang)
    t = time.monotonic()
    try:
        with pytest.raises(jb.BackendUnavailable) as e:
            make(tmp_path).score(jb.Query("q"), passages(1), deadline_s=0.2)
        elapsed = time.monotonic() - t
    finally:
        release.set()
    assert e.value.kind == "timeout"
    assert elapsed < 0.6


def test_a_trickled_body_does_not_keep_the_worker_alive(tmp_path, monkeypatch):
    """The reviewer's probe: headers arrive, then the body trickles forever, resetting the
    read timeout on every byte. score() times out at the deadline AND the worker stops
    at the same wall-clock deadline and closes the connection, instead of living on."""
    raw = FakeRaw(b"", delay=0.05, endless=True)
    resp = FakeResponse(200, text="", raw=raw)
    install(monkeypatch, resp)
    t = time.monotonic()
    with pytest.raises(jb.BackendUnavailable) as e:
        make(tmp_path).score(jb.Query("q"), passages(1), deadline_s=0.3)
    assert e.value.kind == "timeout" and time.monotonic() - t < 0.6
    assert wait_for_no_stranded(limit_s=0.5) < 0.5            # gone within one read of the deadline
    assert resp.closed and raw.reads > 1                      # it streamed, then released the socket


def test_a_slow_body_within_the_deadline_is_read_whole(tmp_path, monkeypatch):
    """Streaming is not a behaviour change for a healthy reply: a body arriving in small
    pieces is reassembled and parsed, and the connection is closed afterwards."""
    body = json.dumps({"rankings": [{"index": 1, "logit": 2.0}, {"index": 0, "logit": -1.0}]})
    resp = FakeResponse(200, text=body, raw=FakeRaw(body.encode(), piece=7, delay=0.001))
    install(monkeypatch, resp)
    vs = make(tmp_path).score(jb.Query("q"), passages(2), deadline_s=2)
    assert [v.raw for v in vs] == [-1.0, 2.0] and resp.closed


def test_a_read_timeout_while_streaming_is_a_timeout(tmp_path, monkeypatch):
    """urllib3 raises its own ReadTimeoutError from read1(); that is still kind='timeout'."""
    from urllib3.exceptions import ReadTimeoutError

    class TimingOutRaw(FakeRaw):
        """A body whose first read times out at the socket, as urllib3 reports it."""

        def read1(self, amt, decode_content=True):
            """Raise urllib3's read timeout instead of returning bytes."""
            raise ReadTimeoutError(None, "/", "Read timed out.")
    install(monkeypatch, FakeResponse(200, text="", raw=TimingOutRaw(b"")))
    with pytest.raises(jb.BackendUnavailable) as e:
        make(tmp_path).score(jb.Query("q"), passages(1), deadline_s=2)
    assert e.value.kind == "timeout"


def test_an_oversized_body_is_bad_response(tmp_path, monkeypatch):
    """A body past MAX_BODY_BYTES is abandoned as bad_response rather than read into memory."""
    monkeypatch.setattr(jn, "MAX_BODY_BYTES", 100)
    install(monkeypatch, FakeResponse(200, text="x" * 500, raw=FakeRaw(b"x" * 500, piece=40)))
    with pytest.raises(jb.BackendUnavailable) as e:
        make(tmp_path).score(jb.Query("q"), passages(1), deadline_s=2)
    assert (e.value.kind, e.value.status) == ("bad_response", 200)


def test_hung_workers_are_capped_and_the_cap_releases(tmp_path, monkeypatch):
    """A hang before headers (DNS, connect, trickled header block) cannot be interrupted,
    so abandoned workers are counted: at MAX_STRANDED, score() refuses at once without
    starting another request, and starts calling again once they exit."""
    monkeypatch.setattr(jn, "MAX_STRANDED", 2)
    release = threading.Event()

    def hang(kw):
        """Hold the request inside requests.post, before any response exists."""
        release.wait(10)
        return ok_for([1.0])
    rec = install(monkeypatch, hang)
    b = make(tmp_path)
    try:
        for _ in range(2):
            with pytest.raises(jb.BackendUnavailable):
                b.score(jb.Query("q"), passages(1), deadline_s=0.05)
        assert jn.stranded_workers() == 2 and len(rec.calls) == 2
        t = time.monotonic()
        with pytest.raises(jb.BackendUnavailable, match="still hung") as e:
            b.score(jb.Query("q"), passages(1), deadline_s=1.0)
        assert e.value.kind == "timeout" and time.monotonic() - t < 0.2
        assert len(rec.calls) == 2                            # refused: no third request, no third thread
    finally:
        release.set()
    wait_for_no_stranded()
    rec.outcome = ok_for([1.0])
    assert b.score(jb.Query("q"), passages(1), deadline_s=2)[0].value is True


def test_a_healthy_call_is_never_counted_as_stranded(tmp_path, monkeypatch):
    """Only workers score() abandoned count toward the cap, so completed calls leave it at 0."""
    install(monkeypatch, lambda kw: ok_for([1.0]))           # a fresh body per call: bodies stream once
    b = make(tmp_path)
    for _ in range(jn.MAX_STRANDED + 2):
        b.score(jb.Query("q"), passages(1), deadline_s=2)
    assert jn.stranded_workers() == 0


@pytest.mark.parametrize("deadline", [0, -1.0])
def test_no_time_left_means_no_call(tmp_path, monkeypatch, deadline):
    """A spent deadline raises timeout immediately without sending a request."""
    rec = install(monkeypatch, ok_for([1.0]))
    with pytest.raises(jb.BackendUnavailable) as e:
        make(tmp_path).score(jb.Query("q"), passages(1), deadline_s=deadline)
    assert e.value.kind == "timeout" and rec.calls == []


@pytest.mark.parametrize("resp", [
    FakeResponse(200, text="<html>gateway</html>"),                                     # not JSON
    FakeResponse(200, {"usage": {}}),                                                   # no rankings
    FakeResponse(200, {"rankings": "nope"}),
    FakeResponse(200, {"rankings": [{"index": 0, "logit": 1.0}]}),                      # short
    FakeResponse(200, {"rankings": [{"index": 0, "logit": 1.0}, {"index": 0, "logit": 2.0}]}),  # repeat
    FakeResponse(200, {"rankings": [{"index": 0, "logit": 1.0}, {"index": 2, "logit": 2.0}]}),  # range
    FakeResponse(200, {"rankings": [{"index": 0, "logit": 1.0}, {"index": 1, "logit": "hi"}]}),
    FakeResponse(200, {"rankings": [{"index": 0, "logit": 1.0}, {"index": True, "logit": 2.0}]}),
    FakeResponse(200, text='{"rankings": [{"index": 0, "logit": 1.0}, {"index": 1, "logit": NaN}]}'),
    FakeResponse(200, {"rankings": [{"index": 0, "logit": 1.0}, "junk"]}),
    FakeResponse(200, [1, 2]),
])
def test_malformed_or_misaligned_bodies_are_bad_response(tmp_path, monkeypatch, resp):
    """Non-JSON, short, repeated, out-of-range or non-numeric rankings are all bad_response."""
    install(monkeypatch, resp)
    with pytest.raises(jb.BackendUnavailable) as e:
        make(tmp_path).score(jb.Query("q"), passages(2), deadline_s=2)
    assert e.value.kind == "bad_response"


# ---- calibrated vs uncalibrated ----------------------------------------------------
def test_uncalibrated_uses_the_models_boundary_and_leaves_score_none(tmp_path, monkeypatch):
    """Without calibration: value = logit > 0, score None, raw the unmodified logit."""
    install(monkeypatch, ok_for([7.4, 0.0, -7.4]))
    vs = make(tmp_path).score(jb.Query("q"), passages(3), deadline_s=2)
    assert [v.value for v in vs] == [True, False, False]     # logit 0 is not relevant
    assert all(v.score is None and v.why is None for v in vs)
    assert [v.raw for v in vs] == [7.4, 0.0, -7.4]


def test_uncalibrated_warning_is_logged_once(tmp_path, capsys):
    """The uncalibrated fallback is announced once per process, not per construction."""
    make(tmp_path)
    make(tmp_path)
    assert capsys.readouterr().err.count("running uncalibrated") == 1


def test_calibrated_scores_are_probabilities_thresholded(tmp_path, monkeypatch):
    """With calibration: score = Platt probability, value = score >= threshold, raw kept."""
    cal = jb.Calibration(a=0.5, b=-1.0, threshold=0.7, fitted_on="golden", n=154)
    install(monkeypatch, ok_for([7.4, 2.0, -7.4]))
    b = make(tmp_path, calibration=cal)
    assert b.calibrated and b.calibration is cal
    vs = b.score(jb.Query("q"), passages(3), deadline_s=2)
    assert [v.score for v in vs] == [cal.probability(x) for x in (7.4, 2.0, -7.4)]
    assert [v.value for v in vs] == [True, False, False]     # sigmoid(0) = 0.5 < 0.7
    assert [v.raw for v in vs] == [7.4, 2.0, -7.4]           # raw stays the unmodified logit
    assert all(v.why is None for v in vs)                    # a scorer never explains


def test_calibration_is_read_from_file_with_extras(tmp_path, monkeypatch, capsys):
    """The calibration JSON is loaded, provisional is exposed, and unknown keys are kept."""
    path = tmp_path / "nemotron.json"
    path.write_text(json.dumps({"a": 1.0, "b": 0.0, "threshold": 0.5, "fitted_on": "golden-holdout",
                                "n": 154, "provisional": True, "auc": 0.983}))
    install(monkeypatch, ok_for([3.0, -3.0]))
    b = make(tmp_path, calibration_path=path)
    assert b.calibrated and b.calibration.provisional and b.calibration.extra == {"auc": 0.983}
    assert b.describe()["provisional"] is True
    vs = b.score(jb.Query("q"), passages(2), deadline_s=2)
    assert [v.value for v in vs] == [True, False]
    assert vs[0].score == pytest.approx(jb.sigmoid(3.0))
    assert "running uncalibrated" not in capsys.readouterr().err


@pytest.mark.parametrize("content", ["{not json", json.dumps({"b": 0.0}), json.dumps([1, 2]),
                                     json.dumps({"a": 1.0, "b": 0.0, "threshold": 1.5}),
                                     json.dumps({"a": "x", "b": 0.0})])
def test_a_broken_calibration_file_is_a_configuration_error(tmp_path, content):
    """A calibration file that exists but is unusable is a hard error, not silent uncalibration."""
    path = tmp_path / "nemotron.json"
    path.write_text(content)
    with pytest.raises(jb.BackendNotConfigured):
        make(tmp_path, calibration_path=path)


def test_default_calibration_path_is_the_shared_location():
    """The default file is engine/rag/calibration/nemotron.json, where the calibration step writes."""
    assert jn.CALIBRATION_PATH == HERE / "calibration" / "nemotron.json"
