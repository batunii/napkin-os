"""The local cross-encoder backend, with the model faked: no weights, no downloads, no torch.
Run: cd engine/rag && RAG_STORE=local RAG_INDEX=./_index_v3 python3 -m pytest test_judge_local.py -q
"""
from __future__ import annotations

import json
import math
import sys
import threading
import time
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import pytest  # noqa: E402
import judge_base as jb  # noqa: E402
import judge_local as jl  # noqa: E402
import judge  # noqa: E402  (the chain, to test this backend the way it is really called)


class FakeModel:
    """Stands in for sentence_transformers.CrossEncoder: records every predict() call and
    scores each pair with `fn` (default: passage length minus 5, so short passages score
    negative and long ones positive, and order mistakes are visible)."""

    def __init__(self, fn=None):
        """Remember the scoring function and start an empty call log."""
        self.fn = fn or (lambda q, p: float(len(p)) - 5.0)
        self.calls: list[tuple[list, dict]] = []

    def predict(self, pairs, **kw):
        """Score pairs the way CrossEncoder.predict would, logging what it was given."""
        self.calls.append((list(pairs), kw))
        return [self.fn(q, p) for q, p in pairs]


NO_CALIBRATION = HERE / "calibration" / "__no_such_file__.json"


def _backend(model=None, **kw) -> jl.LocalCrossEncoderBackend:
    """A backend over a fake model with no calibration file unless one is given (the real
    calibration/local.json, if one is ever fitted, must not change what these tests see)."""
    kw.setdefault("calibration_path", NO_CALIBRATION)
    kw.setdefault("device", "cpu")
    return jl.LocalCrossEncoderBackend(model=model or FakeModel(), **kw)


def _passages(*texts: str) -> list[jb.Passage]:
    """Passages p0, p1, ... carrying the given texts."""
    return [jb.Passage(f"p{i}", t) for i, t in enumerate(texts)]


def _fake_weights(d: Path) -> Path:
    """A directory that passes the weights check: config.json plus a safetensors file."""
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text("{}")
    (d / "model.safetensors").write_bytes(b"")
    return d


def _drain_worker():
    """Wait until the shared worker has finished everything queued, so one test's slow
    fake prediction cannot eat into the next test's deadline."""
    jl._executor().submit(lambda: None).result(timeout=10)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Every test starts with an empty model cache and no environment overrides."""
    monkeypatch.setattr(jl, "_MODELS", {})
    monkeypatch.delenv("RAG_LOCAL_DEVICE", raising=False)
    monkeypatch.delenv("RAG_LOCAL_RERANKER", raising=False)
    yield
    _drain_worker()


# ---- the contract -----------------------------------------------------------------
def test_satisfies_the_backend_protocol_and_states_its_own_limits():
    """judge.py needs name, capacity, calibrated and score(); this backend also states the
    deadline it needs, because local inference is slower than the chain default."""
    b = _backend()
    assert isinstance(b, jb.Backend)
    assert b.name == "local" and b.deadline_s == jl.DEFAULT_DEADLINE_S and b.calibrated is False


def test_capacity_defaults_to_what_was_measured_for_the_device():
    """One call at capacity must fit the deadline with HEADROOM, and cpu is slower than
    mps, so cpu gets the narrower pool; an unmeasured device gets the cautious (cpu)
    value; an explicit one wins."""
    assert _backend(device="cpu").capacity == jl.CAPACITY_BY_DEVICE["cpu"] == 6
    assert _backend(device="mps").capacity == jl.CAPACITY_BY_DEVICE["mps"] == 20
    assert _backend(device="cuda").capacity == jl.FALLBACK_CAPACITY
    assert _backend(device="mps", capacity=5).capacity == 5


def test_one_verdict_per_passage_in_input_order_with_raw_logit_unmodified():
    """Input order survives the internal shortest-first sort, and raw is the logit."""
    texts = ("a" * 30, "b" * 2, "c" * 12, "d" * 7)
    b = _backend()
    vs = b.score(jb.Query("q"), _passages(*texts), deadline_s=5)
    assert [v.raw for v in vs] == [len(t) - 5.0 for t in texts]
    assert all(v.backend == "local" for v in vs)


def test_the_model_sees_pairs_shortest_first_and_raw_logits_not_a_sigmoid():
    """Length-sorted batches cut padding; activation must be the identity, not CrossEncoder's
    default sigmoid, or `raw` would be a probability."""
    m = FakeModel()
    _backend(m).score(jb.Query("q"), _passages("x" * 9, "y", "z" * 4), deadline_s=5)
    pairs, kw = m.calls[0]
    assert [len(p) for _, p in pairs] == [1, 4, 9]
    assert kw["activation_fct"](7.5) == 7.5
    assert kw["show_progress_bar"] is False and kw["batch_size"] == jl.BATCH_SIZE


def test_uncalibrated_decides_at_logit_zero_and_leaves_score_none():
    """No calibration file: value is raw > 0 and score is None, never a made-up probability."""
    vs = _backend().score(jb.Query("q"), _passages("abc", "abcdefgh", "abcde"), deadline_s=5)
    assert [(v.value, v.score, v.why) for v in vs] == [(False, None, None), (True, None, None),
                                                       (False, None, None)]


def test_calibrated_scores_are_platt_probabilities_thresholded(tmp_path):
    """A calibration file makes the backend calibrated: score = probability(raw), value by
    the calibration's threshold, and why stays empty (scorers do not explain)."""
    cal = tmp_path / "local.json"
    cal.write_text(json.dumps({"a": 1.0, "b": 0.0, "threshold": 0.9, "fitted_on": "golden",
                               "n": 100, "provisional": True}))
    b = _backend(calibration_path=cal)
    assert b.calibrated and b.calibration.threshold == 0.9
    vs = b.score(jb.Query("q"), _passages("abcdef", "a" * 20), deadline_s=5)   # raw 1, 15
    assert vs[0].score == pytest.approx(jb.sigmoid(1.0)) and vs[0].value is False
    assert vs[1].score == pytest.approx(jb.sigmoid(15.0)) and vs[1].value is True
    assert all(v.why is None for v in vs)
    assert b.describe()["calibration"]["provisional"] is True


@pytest.mark.parametrize("body", ["not json", json.dumps({"a": 1.0}),
                                  json.dumps({"a": 1.0, "b": 0.0, "threshold": 3}),
                                  json.dumps({"a": "x", "b": 0.0})])
def test_an_unreadable_calibration_is_a_configuration_error(tmp_path, body):
    """A calibration someone fitted but we cannot read must not silently become
    'uncalibrated': every verdict would change without a word."""
    p = tmp_path / "local.json"
    p.write_text(body)
    with pytest.raises(jb.BackendNotConfigured):
        _backend(calibration_path=p)


def test_missing_calibration_file_means_uncalibrated_and_says_so_once(tmp_path, monkeypatch, capsys):
    """No file is a legitimate state, not an error, but the log says it once per process."""
    monkeypatch.setattr(jl, "_warned_uncalibrated", False)
    assert _backend(calibration_path=tmp_path / "absent.json").calibrated is False
    _backend(calibration_path=tmp_path / "absent.json")
    assert capsys.readouterr().err.count("running uncalibrated") == 1


def test_calibration_argument_overrides_the_file(tmp_path):
    """An explicit Calibration wins over whatever is (or is not) on disk."""
    cal = jb.Calibration(a=2.0, b=0.0, threshold=0.5)
    b = _backend(calibration=cal, calibration_path=tmp_path / "absent.json")
    assert b.calibrated and b.calibration is cal
    v, = b.score(jb.Query("q"), _passages("abcdef"), deadline_s=5)       # raw 1
    assert v.score == pytest.approx(jb.sigmoid(2.0)) and v.value is True


def test_empty_pool_returns_nothing_without_calling_the_model():
    """Nothing to judge means no inference at all."""
    m = FakeModel()
    assert _backend(m).score(jb.Query("q"), [], deadline_s=5) == []
    assert m.calls == []


def test_inputs_are_clipped_to_the_measured_lengths():
    """Passages are clipped to PASSAGE_CHARS and query plus context to QUERY_CHARS,
    context first, so latency matches what was measured."""
    m = FakeModel()
    q = jb.Query("QUERY", context="C" * 5000)
    _backend(m).score(q, _passages("p" * 5000), deadline_s=5)
    (qt, pt), = m.calls[0][0]
    assert len(pt) == jl.PASSAGE_CHARS
    assert len(qt) == jl.QUERY_CHARS and qt.startswith("QUERY")


def test_own_deadline_applies_when_the_caller_gives_none():
    """score() without deadline_s uses the backend's stated deadline."""
    gate = threading.Event()
    b = _backend(FakeModel(lambda q, p: gate.wait(5) and 0.0), deadline_s=0.2)
    t = time.monotonic()
    with pytest.raises(jb.BackendUnavailable) as e:
        b.score(jb.Query("q"), _passages("x"))
    gate.set()
    assert e.value.kind == "timeout" and time.monotonic() - t < 1.5


# ---- the deadline -----------------------------------------------------------------
def test_timeout_raises_within_deadline_and_abandoned_queued_work_never_runs():
    """A running prediction cannot be stopped, so score() stops waiting at the deadline.
    A call queued behind it that also times out is cancelled before it starts, so
    abandoned work does not pile up on the worker."""
    gate = threading.Event()
    runs: list[str] = []

    def slow(q, p):
        """Block until the test opens the gate, recording which call ran."""
        runs.append(p)
        gate.wait(10)
        return 1.0

    b = _backend(FakeModel(slow))
    first = threading.Thread(target=lambda: pytest.raises(jb.BackendUnavailable, b.score,
                                                          jb.Query("q"), _passages("first"),
                                                          deadline_s=0.3))
    first.start()
    time.sleep(0.1)                               # "first" is now running on the worker
    t = time.monotonic()
    with pytest.raises(jb.BackendUnavailable) as e:
        b.score(jb.Query("q"), _passages("second"), deadline_s=0.3)
    elapsed = time.monotonic() - t
    first.join(5)
    assert e.value.kind == "timeout" and "background" in str(e.value)
    assert elapsed < 1.0
    gate.set()
    _drain_worker()
    assert runs == ["first"]                      # "second" was cancelled, never ran


def test_zero_deadline_times_out_rather_than_blocking():
    """A spent budget is a timeout, never an unbounded wait."""
    gate = threading.Event()
    b = _backend(FakeModel(lambda q, p: gate.wait(5) and 0.0))
    with pytest.raises(jb.BackendUnavailable) as e:
        b.score(jb.Query("q"), _passages("x"), deadline_s=0)
    gate.set()
    assert e.value.kind == "timeout"


# ---- sizing: one combined call per brief, measured on brief-shaped pairs -------------
class TimedModel(FakeModel):
    """A fake whose predict() sleeps for a given time per call, looked up by pool size, so
    the chain can be run against measured latencies scaled down to test speed."""

    def __init__(self, seconds_for_n):
        """`seconds_for_n(n)` is how long a call of n pairs takes."""
        super().__init__(lambda q, p: 1.0)
        self.seconds_for_n = seconds_for_n

    def predict(self, pairs, **kw):
        """Sleep as the real model would for this many pairs, then score them."""
        time.sleep(self.seconds_for_n(len(pairs)))
        return super().predict(pairs, **kw)


# Real seconds -> test seconds. The ratio of latency to deadline is what matters, and it
# survives scaling; thread start-up (~1ms) is small against the scaled numbers.
SCALE = 0.08


def _chain_over_measured_latency(capacity: int, deadline_s: float, slowdown: float = 1.0,
                                 device: str = "mps"):
    """A judge.Chain holding only this backend, over a fake whose latency is the measured
    brief-shaped p90 for the pool size times `slowdown`, scaled to test speed."""
    per_call = jl.LATENCY_P90_S[device]
    b = _backend(TimedModel(lambda n: per_call[n] * slowdown * SCALE), device=device,
                 capacity=capacity, deadline_s=deadline_s * SCALE)
    return judge.Chain([b])


def _brief_passages(n: int) -> list[jb.Passage]:
    """n passages standing in for every bucket of one brief, combined into one list."""
    return _passages(*(f"bucket{i % 4} " + "p" * 40 for i in range(n)))


def test_capacity_is_sized_per_call_from_brief_shaped_measurements():
    """The sizing rule: every device's capacity is a pool size that was measured with
    brief-shaped queries, and HEADROOM x its p90 fits the deadline. The old cpu sizing
    (pool 10) was never measured with context and does not appear in the table."""
    for device, cap in jl.CAPACITY_BY_DEVICE.items():
        assert cap in jl.LATENCY_P90_S[device], f"{device}: capacity {cap} was never measured"
        assert jl.HEADROOM * jl.LATENCY_P90_S[device][cap] <= jl.DEFAULT_DEADLINE_S, device
    assert jl.HEADROOM >= 1.5 and jl.FALLBACK_CAPACITY == jl.CAPACITY_BY_DEVICE["cpu"]


def test_one_combined_call_per_brief_answers_on_a_machine_twice_as_slow_as_measured():
    """The contract the capacity is sized for: all of a brief's buckets in ONE chain.judge
    call. At the default capacity and deadline, on a machine HEADROOM times slower than
    the measured p90, it answers and leaves the backend up for the next brief."""
    cap = jl.CAPACITY_BY_DEVICE["mps"]
    chain = _chain_over_measured_latency(cap, jl.DEFAULT_DEADLINE_S, slowdown=jl.HEADROOM)
    for brief in ("first brief", "next brief"):
        r = chain.judge(jb.Query(brief, context="gist"), _brief_passages(cap))
        assert r.backend_used == "local" and [a["outcome"] for a in r.attempts] == ["ok"]
        assert r.judged == cap and r.truncated == 0


def test_per_bucket_parallel_calls_break_the_contract_and_take_the_backend_down():
    """Why the contract is one call per brief: four per-bucket calls in parallel queue on
    the single worker, each wait counts against its own deadline, the last one times out
    and the chain marks the backend down, so the next brief goes unvalidated. This is
    the reviewer's reproduction, at the measured brief-shaped pool-20 latency."""
    chain = _chain_over_measured_latency(20, jl.DEFAULT_DEADLINE_S)
    res = [None] * 4

    def bucket(i):
        """One bucket's own judge() call, the pattern the contract rules out."""
        res[i] = chain.judge(jb.Query(f"bucket {i}"), _brief_passages(20))

    threads = [threading.Thread(target=bucket, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    outcomes = sorted(r.attempts[0]["outcome"] for r in res)
    assert "ok" in outcomes and "timeout" in outcomes
    after = chain.judge(jb.Query("the next brief"), _brief_passages(20))
    assert after.backend_used is None and after.attempts[0]["outcome"] == "skipped_down"


# ---- run-time failures ------------------------------------------------------------
def test_inference_errors_become_unavailable_error():
    """Out of memory or a broken model at run time: the chain falls through."""
    def boom(q, p):
        """Fail the way an MPS out-of-memory does."""
        raise RuntimeError("MPS backend out of memory")
    with pytest.raises(jb.BackendUnavailable) as e:
        _backend(FakeModel(boom)).score(jb.Query("q"), _passages("x"), deadline_s=5)
    assert e.value.kind == "error" and "out of memory" in str(e.value)


def test_wrong_count_or_non_finite_output_is_a_bad_response():
    """Never attach a short list of scores to the wrong passages, never pass a NaN on."""
    class Short(FakeModel):
        """Drops the last score."""
        def predict(self, pairs, **kw):
            """Return one score fewer than pairs."""
            return [0.0] * (len(pairs) - 1)
    with pytest.raises(jb.BackendUnavailable) as e:
        _backend(Short()).score(jb.Query("q"), _passages("a", "b"), deadline_s=5)
    assert e.value.kind == "bad_response"
    with pytest.raises(jb.BackendUnavailable) as e:
        _backend(FakeModel(lambda q, p: math.nan)).score(jb.Query("q"), _passages("a"), deadline_s=5)
    assert e.value.kind == "bad_response"


# ---- configuration ----------------------------------------------------------------
def test_device_resolution(monkeypatch):
    """mps when available, else cpu; RAG_LOCAL_DEVICE overrides; an explicit request for a
    device the machine lacks, or an unknown one, is a configuration error, not a quiet
    fallback to a 10x slower device."""
    monkeypatch.setattr(jl, "_mps_available", lambda: True)
    assert jl.resolve_device() == "mps"
    monkeypatch.setenv("RAG_LOCAL_DEVICE", "cpu")
    assert jl.resolve_device() == "cpu"
    monkeypatch.delenv("RAG_LOCAL_DEVICE")
    monkeypatch.setattr(jl, "_mps_available", lambda: False)
    assert jl.resolve_device() == "cpu"
    with pytest.raises(jb.BackendNotConfigured):
        jl.resolve_device("mps")
    with pytest.raises(jb.BackendNotConfigured):
        jl.resolve_device("tpu")
    monkeypatch.setattr(jl, "_cuda_available", lambda: False)
    with pytest.raises(jb.BackendNotConfigured):
        jl.resolve_device("cuda")


def test_model_name_comes_from_env_else_default(monkeypatch):
    """RAG_LOCAL_RERANKER picks the model; the argument beats both."""
    assert jl.resolve_model() == jl.DEFAULT_MODEL
    monkeypatch.setenv("RAG_LOCAL_RERANKER", "BAAI/bge-reranker-base")
    assert jl.resolve_model() == "BAAI/bge-reranker-base"
    assert jl.resolve_model("x/y") == "x/y"


def test_absent_weights_are_not_configured_and_name_the_download_command(monkeypatch):
    """Construction never downloads: a cache miss is a hard error that says how to fix it.
    huggingface_hub is faked, so this cannot reach the network."""
    seen = {}

    def snapshot_download(repo, **kw):
        """Behave like a cache miss, recording that the lookup was local-only."""
        seen.update(kw)
        raise FileNotFoundError("not cached")

    monkeypatch.setitem(sys.modules, "huggingface_hub",
                        types.SimpleNamespace(snapshot_download=snapshot_download))
    with pytest.raises(jb.BackendNotConfigured) as e:
        jl.LocalCrossEncoderBackend("org/absent-model", device="cpu", calibration_path=NO_CALIBRATION)
    assert seen == {"local_files_only": True}
    assert "--download org/absent-model" in str(e.value)
    assert "--download\n" not in jl.download_command(jl.DEFAULT_MODEL)
    assert jl.download_command(jl.DEFAULT_MODEL).endswith("--download")


def test_an_incomplete_snapshot_is_not_configured(tmp_path, monkeypatch):
    """An interrupted download leaves a snapshot directory without weights; that must not
    pass for a configured backend."""
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "config.json").write_text("{}")
    monkeypatch.setitem(sys.modules, "huggingface_hub",
                        types.SimpleNamespace(snapshot_download=lambda repo, **kw: str(snap)))
    with pytest.raises(jb.BackendNotConfigured) as e:
        jl.weights_path("org/half")
    assert "incomplete" in str(e.value)


def test_a_local_model_directory_is_used_as_is(tmp_path):
    """A saved-model directory needs no cache lookup; an empty one is not configured."""
    d = _fake_weights(tmp_path / "m")
    assert jl.weights_path(str(d)) == d
    (tmp_path / "empty").mkdir()
    with pytest.raises(jb.BackendNotConfigured):
        jl.weights_path(str(tmp_path / "empty"))


def test_model_loads_once_per_process_even_under_concurrent_construction(tmp_path):
    """The model is a process-wide singleton: many backends, many threads, one load."""
    d = _fake_weights(tmp_path / "m")
    loads = []

    def loader(path, device, max_length):
        """Count loads, slowly enough that racing threads would overlap."""
        loads.append((path, device, max_length))
        time.sleep(0.05)
        return FakeModel()

    built = []
    threads = [threading.Thread(target=lambda: built.append(jl.LocalCrossEncoderBackend(
        str(d), device="cpu", calibration_path=NO_CALIBRATION, loader=loader))) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert len(built) == 4 and loads == [(d, "cpu", jl.MAX_LENGTH)]
    assert len({id(b._model) for b in built}) == 1


def test_preload_false_defers_the_load_to_the_first_score(tmp_path):
    """preload=False checks the weights at construction but loads at first use."""
    d = _fake_weights(tmp_path / "m")
    loads = []

    def loader(path, device, max_length):
        """Record the load and hand back a fake model."""
        loads.append(path)
        return FakeModel()

    b = jl.LocalCrossEncoderBackend(str(d), device="cpu", calibration_path=NO_CALIBRATION,
                                    preload=False, loader=loader)
    assert loads == []
    b.score(jb.Query("q"), _passages("abcdefg"), deadline_s=5)
    b.score(jb.Query("q"), _passages("abcdefg"), deadline_s=5)
    assert loads == [d]


def test_a_broken_model_fails_construction_or_falls_through_when_deferred(tmp_path):
    """With preload a load failure is a construction error; deferred, the same failure at
    first score() is a run-time BackendUnavailable so the chain can fall through."""
    d = _fake_weights(tmp_path / "m")

    def loader(path, device, max_length):
        """Fail the way a corrupt weights file does."""
        raise jb.BackendNotConfigured("local: could not load: corrupt")

    with pytest.raises(jb.BackendNotConfigured):
        jl.LocalCrossEncoderBackend(str(d), device="cpu", calibration_path=NO_CALIBRATION, loader=loader)
    b = jl.LocalCrossEncoderBackend(str(d), device="cpu", calibration_path=NO_CALIBRATION,
                                    preload=False, loader=loader)
    with pytest.raises(jb.BackendUnavailable) as e:
        b.score(jb.Query("q"), _passages("x"), deadline_s=5)
    assert e.value.kind == "error"


@pytest.mark.parametrize("kw", [{"capacity": 0}, {"deadline_s": 0}])
def test_nonsense_limits_are_rejected(kw):
    """A capacity or deadline that cannot work is a configuration error."""
    with pytest.raises(jb.BackendNotConfigured):
        _backend(**kw)


# ---- the CLI ----------------------------------------------------------------------
def test_download_fetches_one_weights_format(monkeypatch, tmp_path):
    """Repos that ship safetensors, .bin and ONNX copies of the same weights get only the
    safetensors; a repo with only .bin gets the .bin."""
    calls = []

    class Api:
        """Fake HfApi listing whichever files the test sets."""
        files: list[str] = []

        def list_repo_files(self, repo):
            """Return the configured file list."""
            return self.files

    def snapshot_download(repo, allow_patterns):
        """Record the patterns instead of downloading."""
        calls.append(allow_patterns)
        return str(tmp_path)

    monkeypatch.setitem(sys.modules, "huggingface_hub",
                        types.SimpleNamespace(HfApi=Api, snapshot_download=snapshot_download))
    Api.files = ["config.json", "model.safetensors", "pytorch_model.bin", "onnx/model.onnx"]
    jl.download("org/m")
    Api.files = ["config.json", "pytorch_model.bin"]
    jl.download("org/m")
    assert "*.safetensors" in calls[0] and "*.bin" not in calls[0]
    assert "*.bin" in calls[1] and "*.safetensors" not in calls[1]
    assert not any("onnx" in p for c in calls for p in c)


def test_cli_check_reports_not_configured_with_exit_2(tmp_path, capsys):
    """`--check` on a model with no weights exits 2 and says why."""
    (tmp_path / "empty").mkdir()
    assert jl.main(["--check", str(tmp_path / "empty"), "--device", "cpu"]) == 2
    assert "not configured" in capsys.readouterr().err


def test_cli_download_defaults_to_the_configured_model(monkeypatch, tmp_path, capsys):
    """`--download` with no model fetches RAG_LOCAL_RERANKER, else the default."""
    got = []
    monkeypatch.setattr(jl, "download", lambda name: got.append(name) or tmp_path)
    assert jl.main(["--download"]) == 0
    monkeypatch.setenv("RAG_LOCAL_RERANKER", "BAAI/bge-reranker-base")
    assert jl.main(["--download"]) == 0
    assert got == [jl.DEFAULT_MODEL, "BAAI/bge-reranker-base"]
