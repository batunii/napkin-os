#!/usr/bin/env python3
"""
judge_base.py — the contract every relevance-validation backend implements.

The validation stage asks one question of each retrieved passage: is this directly useful
evidence for this brief? Several backends can answer it — jev (TypeSafe), a hosted NVIDIA
reranker, a local cross-encoder, an LLM judge — and judge.py chains them so that each is
a failsafe for the one before. This module defines only the shapes they share, so a
backend can be written, tested and swapped without touching the chain or the callers.

    Query     what is being judged against: the retrieval query plus brief context
    Passage   one retrieved chunk, identified by its cite id
    Verdict   one backend's decision on one passage
    Backend   the protocol: name, capacity, calibrated, score()

Two failure classes, because they call for opposite actions (judgement-architecture
invariant M4):

    BackendNotConfigured   raised at CONSTRUCTION — a key, SDK or model file is missing.
                           A configuration error is a hard error: the chain refuses to
                           start rather than silently running without a backend someone
                           asked for.
    BackendUnavailable     raised by score() at RUN time — timeout, 5xx, retired model,
                           not entitled. The chain falls through to the next backend and
                           records why. `kind` names the class so the trace says whether
                           it is our problem (retired: change the config) or the vendor's
                           (not_entitled: ask them).

Invariants every backend must keep (tested per backend):
  * score() returns exactly one Verdict per passage, in the SAME ORDER as the input.
  * `score` is a calibrated probability in [0, 1] or None. Only a backend whose
    `calibrated` is True may populate it; an uncalibrated backend decides `value` itself
    and leaves `score` None. A threshold is never compared against a None score.
  * `raw` carries the backend's native output (a logit, a probability) unmodified, so a
    later calibration or a backend swap can be diffed against recorded runs.
  * `why` is only ever filled by a generative backend. A backend that scores does not
    explain; one that explains does not score.
  * score() never blocks past `deadline_s`; it raises BackendUnavailable(kind="timeout")
    instead.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

# Failure classes a BackendUnavailable may carry. Closed so the trace can be queried.
UNAVAILABLE_KINDS = ("timeout", "http_error", "retired", "not_entitled", "bad_response",
                     "rate_limited", "error")


@dataclass(frozen=True)
class Query:
    """What passages are judged against.

    `text` is the retrieval query. `context` is extra brief material that sharpens the
    judgement — the brief gist, selected research findings, user attachments. Context may
    shape a verdict; it never reaches scope or tenant (those are applied before any
    backend sees a passage)."""
    text: str
    context: str = ""

    def combined(self, max_chars: int | None = None) -> str:
        """Query plus context as one string, for backends that take a single query field.
        Context is appended after the query so truncation removes context first."""
        s = self.text if not self.context else f"{self.text}\n\nBrief context:\n{self.context}"
        return s[:max_chars] if max_chars else s


@dataclass(frozen=True)
class Passage:
    """One retrieved chunk to judge. `id` is its cite id, so a verdict can be joined back
    to the hit it came from."""
    id: str
    text: str


@dataclass(frozen=True)
class Verdict:
    """One backend's decision on one passage.

    value     passed the relevance gate
    score     calibrated probability in [0, 1], calibrated backends only, else None
    why       explanation, generative backends only, else None
    backend   which backend decided — recorded so a swap can be diffed, not trusted
    raw       the backend's native output, unmodified (e.g. a reranker logit)"""
    value: bool
    score: float | None
    why: str | None
    backend: str
    raw: float | None = None

    def __post_init__(self):
        """Reject a score outside [0, 1]: it is a probability, not a logit."""
        if self.score is not None and not (0.0 <= self.score <= 1.0):
            raise ValueError(f"Verdict.score must be in [0, 1] or None, got {self.score!r}")

    def as_contract(self) -> dict:
        """Exactly the rag_io contract's $defs/relevance shape: value, score, backend."""
        return {"value": self.value, "score": self.score, "backend": self.backend}

    def as_dict(self) -> dict:
        """as_contract() plus `raw` and `why`, for the trace (NOT the response: the
        contract's relevance object admits no other keys)."""
        return {**self.as_contract(), "raw": self.raw, "why": self.why}


class BackendNotConfigured(RuntimeError):
    """A backend was requested but cannot be built: missing key, SDK or model file.
    Raised at construction. The message says exactly what to set or install."""


class BackendUnavailable(RuntimeError):
    """A configured backend could not answer this call. The chain falls through.

    `kind` is one of UNAVAILABLE_KINDS. `status` is the HTTP status when there was one."""

    def __init__(self, message: str, *, kind: str = "error", status: int | None = None):
        """Record the failure class alongside the message; an unknown kind becomes 'error'."""
        super().__init__(message)
        self.kind = kind if kind in UNAVAILABLE_KINDS else "error"
        self.status = status


@runtime_checkable
class Backend(Protocol):
    """What judge.py needs from a validation backend.

    name        stable identifier used in RAG_VALIDATOR and recorded on every verdict
    capacity    how many passages this backend can judge per query within its deadline;
                the chain uses it to decide how wide retrieval should go
    calibrated  True if `score` is a calibrated probability a threshold may be applied to
    """
    name: str
    capacity: int
    calibrated: bool

    def score(self, query: Query, passages: list[Passage], *, deadline_s: float) -> list[Verdict]:
        """Judge every passage against `query`. One Verdict per passage, input order.
        Raises BackendUnavailable on any run-time failure, never returns partial output."""
        ...


# ---- helpers shared by backends -------------------------------------------------
def sigmoid(x: float) -> float:
    """Numerically safe logistic function."""
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    z = math.exp(x)
    return z / (1.0 + z)


@dataclass(frozen=True)
class Calibration:
    """Platt scaling from a backend's raw output to a probability: p = sigmoid(a*raw + b),
    and the threshold on p that decides `value`.

    `fitted_on` says what labelled data produced it, and `provisional` is True while that
    data is not brief-shaped (e.g. fitted on golden retrieval queries). A provisional
    calibration is usable but must be surfaced in the trace."""
    a: float
    b: float
    threshold: float = 0.5
    fitted_on: str = ""
    n: int = 0
    provisional: bool = True
    extra: dict = field(default_factory=dict)

    def probability(self, raw: float) -> float:
        """Map a raw backend output to a calibrated probability."""
        return sigmoid(self.a * raw + self.b)

    @classmethod
    def from_dict(cls, d: dict) -> "Calibration":
        """Build from the JSON stored under engine/rag/calibration/<backend>.json."""
        known = {"a", "b", "threshold", "fitted_on", "n", "provisional"}
        return cls(**{k: d[k] for k in known if k in d},
                   extra={k: v for k, v in d.items() if k not in known})


def check_verdicts(backend: str, passages: list[Passage], verdicts: list[Verdict]) -> list[Verdict]:
    """Enforce the one-per-passage, same-order invariant on a backend's output. Raises
    BackendUnavailable(kind='bad_response') rather than letting a short or reordered list
    silently attach verdicts to the wrong hits."""
    if len(verdicts) != len(passages):
        raise BackendUnavailable(
            f"{backend}: returned {len(verdicts)} verdicts for {len(passages)} passages",
            kind="bad_response")
    for v in verdicts:
        if v.backend != backend:
            raise BackendUnavailable(f"{backend}: verdict labelled {v.backend!r}", kind="bad_response")
    return verdicts


def load_calibration(path: Path | str, *, backend: str, expect: dict | None = None
                     ) -> Calibration | None:
    """The Calibration stored at `path` for `backend`, or None if there is no file.

    One loader for every scoring backend, so they all mean the same thing by
    "calibrated". No file means uncalibrated, a legitimate state. A file that exists but
    cannot be read raises BackendNotConfigured: someone fitted a calibration and expects
    its threshold to apply, and silently running uncalibrated would change every verdict.

    `expect` is the backend's current input configuration (model, clipping, max length).
    Every key that also appears in the file's `backend_config` must match: a Platt fit is
    only valid for the model and the input shaping it was fitted on. A mismatch — say
    RAG_LOCAL_RERANKER switched to another model while the old file stayed — raises
    BackendNotConfigured naming the key, instead of applying another model's numbers.
    Keys the file does not record are not compared, so older files still load."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        cal = Calibration.from_dict(d)
        ok = all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)
                 for x in (cal.a, cal.b, cal.threshold))
    except (OSError, ValueError, TypeError, AttributeError) as e:
        raise BackendNotConfigured(f"{backend}: calibration file {p} is unreadable: {e}") from e
    if not ok or not (0.0 <= cal.threshold <= 1.0):
        raise BackendNotConfigured(
            f"{backend}: calibration file {p} needs numeric a, b and a threshold in [0, 1]")
    fitted = cal.extra.get("backend_config") if isinstance(cal.extra.get("backend_config"), dict) else {}
    for key, now in (expect or {}).items():
        if key in fitted and fitted[key] != now:
            raise BackendNotConfigured(
                f"{backend}: calibration file {p} was fitted with {key}={fitted[key]!r} but this "
                f"backend runs {key}={now!r}. Refit it (python3 calibrate.py {backend}) or "
                f"remove the file to run uncalibrated.")
    return cal


def classify_http(status: int | None, text: str = "") -> str:
    """Map an HTTP failure to a BackendUnavailable kind, the same way for every backend,
    so one `kind` always means one owner.

        retired        410, or a body saying end of life: the model is gone — our config
        not_entitled   401 / 403, or 404 "not found for account": the vendor's side
        rate_limited   429
        http_error     anything else, including a bare 404 (a wrong URL or model name)
                       and connection failures (status None)"""
    body = (text or "").lower()
    if status == 410 or "end of life" in body:
        return "retired"
    if status in (401, 403) or (status == 404 and "not found for account" in body):
        return "not_entitled"
    if status == 429:
        return "rate_limited"
    return "http_error"
