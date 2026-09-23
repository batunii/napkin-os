#!/usr/bin/env python3
"""
judge_local.py — a cross-encoder on this machine, the vendor-free last line of the chain.

Why it exists. Three hosted NVIDIA rerankers were retired this year, and the NVIDIA key
the hosted backend runs on is a prototyping key. Every other validation backend can be
switched off by somebody else's decision. This one cannot: the weights sit in the local
Hugging Face cache, inference runs on this machine's GPU (Apple MPS) or CPU, and nothing
leaves the process. It is slower and somewhat weaker than the hosted reranker, which is
why it sits at the END of judge.py's failsafe chain rather than the front.

    backend = LocalCrossEncoderBackend()             # loads once per process
    verdicts = backend.score(Query("..."), [Passage("ipa:1", "...")], deadline_s=10)

Model. RAG_LOCAL_RERANKER, default BAAI/bge-reranker-v2-m3 (XLM-RoBERTa large, 568M
parameters, multilingual, 8k context of which we use 512 tokens). The measured
alternative, BAAI/bge-reranker-base, is kept one environment variable away; see the
README section for the numbers behind the choice.

Getting the weights. Construction never downloads: a brief must not stall for minutes
fetching 2.3 GB because a cache was cleared, and a server should not reach Hugging Face
at request time. Fetch them once, explicitly:

    python3 judge_local.py --download                      # the default model
    python3 judge_local.py --download BAAI/bge-reranker-base
    python3 judge_local.py --check                         # load, score one pair, report

Absent weights raise BackendNotConfigured at construction with that command in the
message — a configuration error is a hard error (judge_base invariant M4).

Scores. `raw` is the model's logit, unmodified: CrossEncoder applies a sigmoid by default
for single-label models, and we switch it off, because the calibration is fitted on the
logit and the logit is what the hosted backend records too. With a calibration file at
calibration/local.json the backend is calibrated: score = Calibration.probability(raw)
and value = score >= threshold. Without one it is uncalibrated: score stays None and
value = raw > 0, the model's own decision boundary (sigmoid 0.5).

Deadline. CrossEncoder.predict cannot be interrupted once it starts, so inference runs
on a worker thread and score() stops WAITING at the deadline, raising
BackendUnavailable(kind="timeout"). The computation itself carries on in the background
until it finishes and its result is discarded. A queued call whose caller has already
given up is cancelled before it starts, so abandoned work never piles up behind the one
that is running.

Concurrency contract: ONE call per brief. Every local inference in the process goes
through the same single worker, so concurrent score() calls queue, and each one's queue
wait counts against its own deadline. The caller therefore judges all of a brief's
buckets in one combined score() call (passages from every bucket in one list, at most
`capacity` of them), not one call per bucket in parallel. Measured at pool 20 on mps,
four per-bucket calls submitted together finished after 12.0-12.4s; the last one misses
an 8s deadline, the chain marks the backend down for a minute, and because this backend
is the chain's last line that minute is validation off for every brief. Capacity is
therefore sized per call: one call at capacity must fit the deadline at p90 with
HEADROOM to spare. Callers that break the contract are not refused; they time out.
"""
from __future__ import annotations

import argparse
import concurrent.futures
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

from judge_base import (  # noqa: E402  (after sys.path tweak)
    BackendNotConfigured, BackendUnavailable, Calibration, Passage, Query, Verdict,
    check_verdicts,
)
import judge_base  # noqa: E402  (shared loader and HTTP classifier)

NAME = "local"
DEFAULT_MODEL = "BAAI/bge-reranker-v2-m3"
CALIBRATION_PATH = HERE / "calibration" / f"{NAME}.json"

# Input shaping. 512 tokens is what bge rerankers were trained at and what the hosted
# pilot compared against; the model accepts 8k, but attention cost is quadratic and the
# measured gain from longer passages was not the question here. 1500 characters of a
# chunk is ~350 tokens of English, which leaves room for the query inside 512.
MAX_LENGTH = 512
PASSAGE_CHARS = 1500
# The query side. Query.combined() truncates brief context before the query itself, so a
# long context cannot squeeze the passage out of the 512-token window entirely.
QUERY_CHARS = 1000

# Pairs per forward pass. Measured on MPS at pool 20 (p50, two rounds each): 4 -> 1.9-2.0s,
# 8 -> 2.0-2.1s, 16 -> 2.4-2.7s, 32 -> 2.2-2.3s, before length sorting. Small batches win
# because each batch pads to its longest pair; 8 keeps that benefit without paying a
# host round-trip per handful of pairs. The differences are within machine noise, so this
# is a tidy default, not a tuned constant.
BATCH_SIZE = 8

# Single-call p90 latency in seconds, by device and pool size, of v2-m3 on an M1 Pro,
# 16 GB, with other jobs loading the machine (load average 4-32 during the runs). Measured
# on BRIEF-SHAPED pairs: the query plus brief context clipped at QUERY_CHARS (every query
# side is 1000 characters) and passages clipped at PASSAGE_CHARS, which is what production
# sends. The earlier measurement with bare golden queries (~30 characters) read 45-60%
# low, because nearly every production pair fills the 512-token window.
#   mps (12 queries per size): p50 1.01 1.38 1.67 1.78 2.33 2.96 at 6 8 10 12 16 20
#   cpu: only pools 4 and 6 were re-measured brief-shaped (p50 1.64, 2.34) before that run
#        was stopped; cpu is not the production path (this Mac uses mps, the DGX Spark
#        uses cuda). The earlier bare-query cpu figure at pool 10 was p90 4.1-4.4s, and
#        the reviewer measured 4.49s at pool 10 with context, unloaded.
LATENCY_P90_S = {
    "mps": {6: 1.05, 8: 1.44, 10: 1.75, 12: 2.04, 16: 2.71, 20: 3.37},
    "cpu": {4: 1.70, 6: 2.48},
}

# The rule: HEADROOM x single-call p90 at capacity must fit inside the deadline. 2x covers
# a busier machine than the one measured: the reviewer's estimate for mps pool 20 with
# context under heavier load was 5.5-6.5s, and 2 x 3.37 = 6.7s still fits 8s.
#   mps  pool 20: 2 x 3.37 = 6.7s <= 8s  -> 20 (largest measured; unchanged)
#   cpu  pool 6:  2 x 2.48 = 5.0s <= 8s  -> 6, the largest brief-shaped measurement.
#        Pool 10 with context is ~4.5s unloaded, 9.0s at 2x: does not fit. Narrowed from 10.
# The chain default deadline suits a hosted call (~0.5s); local inference needs longer,
# which is why the backend states its own. An unmeasured device (cuda) gets the cpu value:
# too narrow is a thinner pool, too wide is a timeout on every brief.
HEADROOM = 2.0
CAPACITY_BY_DEVICE = {"mps": 20, "cpu": 6}
FALLBACK_CAPACITY = CAPACITY_BY_DEVICE["cpu"]
DEFAULT_CAPACITY = CAPACITY_BY_DEVICE["mps"]
DEFAULT_DEADLINE_S = 8.0

DEVICES = ("mps", "cpu", "cuda")

# Process-wide state. Loading v2-m3 costs seconds and ~2.3 GB of memory, so a process
# holds at most one copy per (model, device): every backend instance shares it.
_MODELS: dict[tuple[str, str, int], object] = {}
_MODELS_LOCK = threading.Lock()
_EXECUTOR: concurrent.futures.ThreadPoolExecutor | None = None
_EXECUTOR_LOCK = threading.Lock()
_warned_uncalibrated = False                      # log the uncalibrated fallback once
_WARN_LOCK = threading.Lock()


# ---- configuration ----------------------------------------------------------------
def resolve_model(model_name: str | None = None) -> str:
    """The model to load: the argument, else RAG_LOCAL_RERANKER, else DEFAULT_MODEL. May
    be a Hugging Face repo id or a local directory holding a saved model."""
    return model_name or os.environ.get("RAG_LOCAL_RERANKER") or DEFAULT_MODEL


def _mps_available() -> bool:
    """True when torch is importable and Apple's Metal backend can run. Isolated so tests
    can choose the answer without torch."""
    try:
        import torch
    except ImportError:
        return False
    return bool(getattr(torch.backends, "mps", None) and torch.backends.mps.is_available())


def _cuda_available() -> bool:
    """True when torch is importable and a CUDA device is visible."""
    try:
        import torch
    except ImportError:
        return False
    return bool(torch.cuda.is_available())


def resolve_device(device: str | None = None) -> str:
    """The torch device to run on: the argument, else RAG_LOCAL_DEVICE, else mps when it
    is available and cpu otherwise.

    An explicit request for a device this machine lacks is a configuration error, not a
    reason to fall back quietly: someone who set `mps` and silently got `cpu` would get a
    backend 2.5-3.5x slower (measured, pool 20) and less than half the capacity, and
    would read the timeouts as a model problem."""
    want = (device or os.environ.get("RAG_LOCAL_DEVICE") or "").strip().lower()
    if not want:
        return "mps" if _mps_available() else "cpu"
    if want not in DEVICES:
        raise BackendNotConfigured(
            f"local: RAG_LOCAL_DEVICE={want!r} is not one of {', '.join(DEVICES)}")
    if want == "mps" and not _mps_available():
        raise BackendNotConfigured("local: device 'mps' requested but torch reports MPS unavailable")
    if want == "cuda" and not _cuda_available():
        raise BackendNotConfigured("local: device 'cuda' requested but torch reports no CUDA device")
    return want


def _has_weights(d: Path) -> bool:
    """True if a directory holds a loadable model: a config plus a weights file. A
    snapshot directory can exist with only some files linked (an interrupted download),
    so the presence of the directory alone proves nothing."""
    if not (d / "config.json").is_file():
        return False
    return any(d.glob("*.safetensors")) or (d / "pytorch_model.bin").is_file()


def download_command(model_name: str) -> str:
    """The exact command that fetches `model_name`, quoted in every not-configured error."""
    suffix = "" if model_name == DEFAULT_MODEL else f" {model_name}"
    return f"python3 {HERE / 'judge_local.py'} --download{suffix}"


def weights_path(model_name: str) -> Path:
    """Where the model's files are on disk, WITHOUT touching the network.

    A local directory is used as it is. A repo id is looked up in the Hugging Face cache
    with local_files_only. Raises BackendNotConfigured naming the download command when
    the weights are absent or incomplete."""
    p = Path(model_name).expanduser()
    if p.is_dir():
        if _has_weights(p):
            return p
        raise BackendNotConfigured(f"local: {p} has no config.json plus weights file")
    try:
        from huggingface_hub import snapshot_download
    except ImportError as e:
        raise BackendNotConfigured(
            "local: huggingface_hub is not installed (it ships with sentence-transformers)") from e
    try:
        snap = Path(snapshot_download(model_name, local_files_only=True))
    except Exception as e:                        # LocalEntryNotFoundError and kin
        raise BackendNotConfigured(
            f"local: weights for {model_name} are not in the Hugging Face cache "
            f"({type(e).__name__}). Fetch them once with: {download_command(model_name)}") from e
    if not _has_weights(snap):
        raise BackendNotConfigured(
            f"local: the cached snapshot of {model_name} is incomplete ({snap}). "
            f"Re-run: {download_command(model_name)}")
    return snap


def load_calibration(path: Path | str = CALIBRATION_PATH, *, expect: dict | None = None
                     ) -> Calibration | None:
    """The Platt calibration for this backend, or None when there is no file. Delegates
    to judge_base.load_calibration, the loader every scoring backend shares, so the two
    mean the same thing by "calibrated" — including refusing a file fitted for a
    different model, max length or passage clipping (`expect`)."""
    return judge_base.load_calibration(path, backend=NAME, expect=expect)


def _warn_uncalibrated(path: Path) -> None:
    """Say once per process, on stderr, that verdicts use the model's own boundary (logit
    0) rather than a fitted threshold. Once, because backends are built per run and the
    line would otherwise drown the log."""
    global _warned_uncalibrated
    with _WARN_LOCK:
        if _warned_uncalibrated:
            return
        _warned_uncalibrated = True
    print(f"[judge_local] no calibration at {path}; running uncalibrated "
          f"(score=None, value = logit > 0)", file=sys.stderr)


# ---- the model --------------------------------------------------------------------
def _identity(x):
    """Activation passed to CrossEncoder.predict so it returns the raw logit. Its default
    for a single-label model is a sigmoid, which would make `raw` a probability and break
    the comparison with recorded logits. A plain function keeps torch out of this module."""
    return x


def _load_cross_encoder(path: Path, device: str, max_length: int):
    """Build a sentence-transformers CrossEncoder from local files and warm it up.

    local_files_only is belt and braces: `path` is already a local directory, so nothing
    should reach the network, and if some component tried, it fails rather than
    downloading. The warm-up predict moves the weights to the device and compiles the MPS
    kernels, so that cost is paid here, at construction, and not inside the first brief's
    deadline."""
    try:
        from sentence_transformers import CrossEncoder
    except ImportError as e:
        raise BackendNotConfigured(
            "local: sentence-transformers is not installed (pip install sentence-transformers)") from e
    try:
        model = CrossEncoder(str(path), device=device, max_length=max_length, local_files_only=True)
        model.predict([("warm up", "warm up")], activation_fn=_identity,
                      show_progress_bar=False, convert_to_numpy=True)
    except Exception as e:
        raise BackendNotConfigured(f"local: could not load {path} on {device}: "
                                   f"{type(e).__name__}: {e}") from e
    return model


def get_model(model_name: str, device: str, max_length: int = MAX_LENGTH, *, loader=None):
    """The process-wide model for (model, device, max_length), loaded on first request.

    The lock is held while loading so two threads constructing backends at once load one
    copy, not two — at 2.3 GB each that is the difference between fitting and swapping.
    `loader(path, device, max_length)` is injectable for tests."""
    key = (model_name, device, max_length)
    m = _MODELS.get(key)
    if m is not None:
        return m
    with _MODELS_LOCK:
        m = _MODELS.get(key)
        if m is None:
            m = (loader or _load_cross_encoder)(weights_path(model_name), device, max_length)
            _MODELS[key] = m
    return m


def _executor() -> concurrent.futures.ThreadPoolExecutor:
    """The single worker thread every local inference runs on.

    One worker, not one per call: a single GPU does not go faster with two predictions
    fighting over it, and two concurrent predicts would double activation memory. The
    price is that concurrent calls queue, first come first served, which is why the
    contract is one combined call per brief (see the module docstring). Its
    thread is joined at interpreter exit, so a prediction still running then delays exit
    by at most one prediction."""
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        if _EXECUTOR is None:
            _EXECUTOR = concurrent.futures.ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="judge-local")
        return _EXECUTOR


# ---- the backend ------------------------------------------------------------------
class LocalCrossEncoderBackend:
    """A cross-encoder reranker run in-process, satisfying judge_base.Backend.

    name         "local"
    capacity     passages per call whose p90 latency, times HEADROOM, fits inside
                 deadline_s; by default the value measured for the device
                 (CAPACITY_BY_DEVICE). Per call, not per bucket: a brief's buckets are
                 judged in one combined call (module docstring, concurrency contract)
    deadline_s   this backend's own deadline; used when score() is not given one
    calibrated   True only when calibration/local.json is present and valid
    calibration  the Calibration in force, or None

    Construction checks everything that can be checked without scoring — model name,
    device, weights on disk, calibration file — and, with preload (the default), loads
    the model, so every configuration problem surfaces as BackendNotConfigured before
    the first brief rather than as a run-time fall-through. preload=False defers the
    load to the first score(), where it runs inside that call's deadline.

    `model` injects a ready object with CrossEncoder's predict(); tests use it to run
    without weights, and a caller that already holds a model can share it. `calibration`
    overrides the file; otherwise `calibration_path` (default CALIBRATION_PATH) is read
    if it exists — the same arguments judge_nemotron takes."""

    name = NAME

    def __init__(self, model_name: str | None = None, *, device: str | None = None,
                 capacity: int | None = None, deadline_s: float = DEFAULT_DEADLINE_S,
                 calibration: Calibration | None = None,
                 calibration_path: Path | str | None = None,
                 batch_size: int = BATCH_SIZE, max_length: int = MAX_LENGTH,
                 preload: bool = True, model=None, loader=None):
        """Resolve configuration and (by default) load the shared model. Raises
        BackendNotConfigured naming the fix for any missing piece."""
        if capacity is not None and capacity < 1:
            raise BackendNotConfigured(f"local: capacity must be >= 1, got {capacity}")
        if not deadline_s > 0:
            raise BackendNotConfigured(f"local: deadline_s must be > 0, got {deadline_s}")
        self.model_name = resolve_model(model_name)
        self.deadline_s = float(deadline_s)
        self.batch_size = int(batch_size)
        self.max_length = int(max_length)
        path = Path(calibration_path) if calibration_path is not None else CALIBRATION_PATH
        self.calibration = calibration if calibration is not None else load_calibration(
            path, expect={"model": self.model_name, "max_length": self.max_length,
                          "passage_chars": PASSAGE_CHARS})
        self.calibrated = self.calibration is not None
        if not self.calibrated:
            _warn_uncalibrated(path)
        self._loader = loader
        if model is not None:
            self.device = device or "injected"
            self.capacity = int(capacity or CAPACITY_BY_DEVICE.get(self.device, FALLBACK_CAPACITY))
            self._model = model
            return
        self.device = resolve_device(device)
        self.capacity = int(capacity or CAPACITY_BY_DEVICE.get(self.device, FALLBACK_CAPACITY))
        weights_path(self.model_name)             # fail now, not at the first brief
        self._model = (get_model(self.model_name, self.device, self.max_length, loader=loader)
                       if preload else None)

    def describe(self) -> dict:
        """What the trace should record about this backend: which model on which device,
        and whether its scores are calibrated (and on what). A provisional calibration
        must be visible in the trace, per judge_base.Calibration."""
        cal = self.calibration
        return {"backend": self.name, "model": self.model_name, "device": self.device,
                "capacity": self.capacity, "deadline_s": self.deadline_s,
                "calls_per_brief": 1,
                "max_length": self.max_length, "passage_chars": PASSAGE_CHARS,
                "calibrated": self.calibrated,
                "calibration": None if cal is None else {
                    "fitted_on": cal.fitted_on, "n": cal.n, "provisional": cal.provisional,
                    "threshold": cal.threshold}}

    def _pairs(self, query: Query, passages: list[Passage]) -> list[tuple[str, str]]:
        """(query, passage) text pairs, clipped to the lengths the measurements used."""
        q = query.combined(max_chars=QUERY_CHARS)
        return [(q, p.text[:PASSAGE_CHARS]) for p in passages]

    def _predict(self, pairs: list[tuple[str, str]]) -> list[float]:
        """Run the model on the worker thread and return one float logit per pair, in the
        order of `pairs`. Loads the shared model first when construction deferred it
        (preload=False).

        Pairs are fed to the model shortest first and the scores put back afterwards.
        CrossEncoder pads each batch to its longest member, so batching similar lengths
        together wastes less compute: measured p50 at pool 20 on MPS fell from 2.0s to
        1.6s, with logits unchanged to 2e-5."""
        if self._model is None:
            self._model = get_model(self.model_name, self.device, self.max_length,
                                    loader=self._loader)
        order = sorted(range(len(pairs)), key=lambda i: len(pairs[i][0]) + len(pairs[i][1]))
        out = self._model.predict([pairs[i] for i in order], batch_size=self.batch_size,
                                  activation_fn=_identity, show_progress_bar=False,
                                  convert_to_numpy=True)
        out = list(out)
        if len(out) != len(pairs):                # checked again in score(); guard the unsort
            return [float(x) for x in out]
        logits = [0.0] * len(pairs)
        for j, i in enumerate(order):
            logits[i] = float(out[j])
        return logits

    def _verdict(self, logit: float) -> Verdict:
        """One Verdict from one logit. Calibrated: score is the Platt probability and the
        calibration's threshold decides. Uncalibrated: score stays None and the model's own
        boundary (logit 0) decides — a threshold is never applied to a missing score."""
        if self.calibration is not None:
            p = self.calibration.probability(logit)
            return Verdict(value=p >= self.calibration.threshold, score=p, why=None,
                           backend=self.name, raw=logit)
        return Verdict(value=logit > 0.0, score=None, why=None, backend=self.name, raw=logit)

    def score(self, query: Query, passages: list[Passage], *,
              deadline_s: float | None = None) -> list[Verdict]:
        """Judge every passage against `query`: one Verdict per passage, in input order.

        `deadline_s` is the caller's budget for this call, queue wait included; when it
        is None the backend's own deadline_s applies. Call it once per brief with every
        bucket's passages combined (module docstring): concurrent calls queue on the one
        worker. Raises BackendUnavailable with kind "timeout" when the worker has not
        answered in time (the computation keeps running in the background and is
        discarded), "bad_response" when the model returns the wrong number of scores or
        a non-finite one, and "error" for any other inference failure (out of memory, a
        corrupt model)."""
        if not passages:
            return []
        budget = self.deadline_s if deadline_s is None else float(deadline_s)
        pairs = self._pairs(query, passages)
        fut = _executor().submit(self._predict, pairs)
        try:
            logits = fut.result(timeout=max(0.0, budget))
        except concurrent.futures.TimeoutError:
            fut.cancel()                          # only stops it if it has not started yet
            raise BackendUnavailable(
                f"local: {self.model_name} on {self.device} did not score {len(passages)} "
                f"passages within {budget:.1f}s (capacity {self.capacity}); the running "
                f"prediction finishes in the background and is discarded",
                kind="timeout") from None
        except BackendUnavailable:
            raise
        except BackendNotConfigured as e:         # deferred load found the model broken
            raise BackendUnavailable(str(e), kind="error") from e
        except Exception as e:
            raise BackendUnavailable(f"local: inference failed: {type(e).__name__}: {e}",
                                     kind="error") from e
        if len(logits) != len(passages):
            raise BackendUnavailable(
                f"local: model returned {len(logits)} scores for {len(passages)} passages",
                kind="bad_response")
        if not all(math.isfinite(x) for x in logits):
            raise BackendUnavailable("local: model returned a non-finite logit", kind="bad_response")
        return check_verdicts(self.name, passages, [self._verdict(x) for x in logits])


# ---- CLI --------------------------------------------------------------------------
def download(model_name: str) -> Path:
    """Fetch `model_name` into the Hugging Face cache and return the snapshot path.

    Only what CrossEncoder needs is fetched: config, tokenizer and one weights format.
    Some repos ship the same weights three times (safetensors, pytorch .bin, ONNX), so
    fetching everything would triple the download for bge-reranker-base."""
    from huggingface_hub import HfApi, snapshot_download
    files = HfApi().list_repo_files(model_name)
    patterns = ["*.json", "*.model", "*.txt"]
    patterns.append("*.safetensors" if any(f.endswith(".safetensors") for f in files) else "*.bin")
    return Path(snapshot_download(model_name, allow_patterns=patterns))


def main(argv: list[str] | None = None) -> int:
    """CLI: `--download [model]` fetches weights; `--check [model]` builds the backend,
    scores one relevant and one irrelevant pair and prints what it found. Exit 0 on
    success, 2 when the backend is not configured."""
    ap = argparse.ArgumentParser(description="Local cross-encoder validation backend")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--download", nargs="?", const="", metavar="MODEL",
                   help=f"fetch weights into the Hugging Face cache (default {DEFAULT_MODEL})")
    g.add_argument("--check", nargs="?", const="", metavar="MODEL",
                   help="load the model from the cache and score a sample pair")
    ap.add_argument("--device", default=None, help="mps | cpu | cuda (default: RAG_LOCAL_DEVICE, else auto)")
    a = ap.parse_args(argv)
    if a.download is not None:
        name = resolve_model(a.download or None)
        t = time.monotonic()
        path = download(name)
        print(f"{name}: {path} ({time.monotonic() - t:.1f}s)")
        return 0
    name = resolve_model(a.check or None)
    t = time.monotonic()
    try:
        b = LocalCrossEncoderBackend(name, device=a.device)
    except BackendNotConfigured as e:
        print(f"not configured: {e}", file=sys.stderr)
        return 2
    load_s = time.monotonic() - t
    q = Query("how did a challenger brand grow penetration without discounting")
    ps = [Passage("rel", "The challenger grew household penetration by 12 points while holding "
                         "price, by reaching light buyers with broad-reach television."),
          Passage("irr", "Store the paint cans upright in a dry place away from frost.")]
    t = time.monotonic()
    vs = b.score(q, ps)
    print(json.dumps({**b.describe(), "load_s": round(load_s, 2),
                      "score_s": round(time.monotonic() - t, 3),
                      "verdicts": {p.id: v.as_dict() for p, v in zip(ps, vs)}}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
