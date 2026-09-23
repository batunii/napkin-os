#!/usr/bin/env python3
"""
judge.py — the validation chain: which relevance backend answers, and what happens when
it cannot.

Retrieval finds passages that look like the brief. The validation stage asks whether each
one is directly useful evidence for it, and several backends can answer (see
judge_base.py for the contract they share). This module is the switch the callers use:

    chain = chain_from_env()                    # RAG_VALIDATOR="jev,nemotron,local"
    width = chain.pool_width(default=12)        # retrieve wider only if a validator is live
    result = chain.judge(query, passages, default_width=12)   # first backend that answers
    kept = result.select(hits, floor=2)         # unjudged passages capped at the default
    trace["validation"] = result.as_dict()

Why the caller's default width travels with the result. pool_width() decides to widen
BEFORE the call, on the strength of a backend that looks live. If that backend then fails
and the chain falls to one with a smaller capacity, or nobody answers, the widened pool
has passages nobody judged. select() keeps an unjudged passage only if its fused position
is inside `default_width`, i.e. only if the caller would have kept it with no validator at
all. So a failed widening costs latency, never noise: the worst outcome is the fused order
at the default width, which is exactly validation-off behaviour.

The chain tries backends in priority order and falls through on run-time failure. It
never raises for a backend's failure, because a validator is an improvement to a brief,
not a precondition of one: the worst it may do is leave the fused order as it was. It
DOES raise at construction when a backend someone asked for is not configured, because a
silently absent validator is indistinguishable from a working one that approved
everything (judgement invariant M4).

Why there is no default backend. RAG_VALIDATOR unset, "" or "none" means an empty chain:
validation off, which is today's behaviour. A default that named, say, jev would hard-fail
every run on a machine without jev's key; a default that quietly skipped unconfigured
backends would be exactly the fall-through default M4 rules out. So the only way to get
validation is to write down which validator you want.

Three things the chain enforces itself rather than trusting each backend to:

    deadline     score() runs in a worker thread and the chain stops waiting at the
                 backend's deadline plus a small grace. A backend that ignores its own
                 deadline still cannot hang a brief. An explicitly set chain deadline
                 (RAG_VALIDATOR_DEADLINE_S, `check --deadline`) caps a backend's own.
    breaker      a backend that fails is marked down for `down_for_s`, so four buckets
                 judged in parallel do not each spend a full deadline rediscovering the
                 same outage.
    invariants   the output is checked (one verdict per passage, same order, no score
                 from an uncalibrated backend, never score and why together) before it
                 can attach a verdict to a hit.

    python3 judge.py check                      # probe every backend in RAG_VALIDATOR
    python3 judge.py check --backends jev,local
"""
from __future__ import annotations

import argparse
import importlib
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:                    # let `import judge_*` resolve next to us
    sys.path.insert(0, str(HERE))

from judge_base import (  # noqa: E402  (after sys.path tweak)
    UNAVAILABLE_KINDS, Backend, BackendNotConfigured, BackendUnavailable, Passage, Query,
    Verdict, check_verdicts)

# name -> "module:ClassName". Lazy import strings rather than classes, so that choosing
# `local` never imports an HTTP client and choosing `jev` never loads a model file. It
# also means one backend's missing SDK cannot break a chain that does not name it.
REGISTRY: dict[str, str] = {
    "jev": "judge_jev:JevBackend",
    "nemotron": "judge_nemotron:NemotronBackend",
    "local": "judge_local:LocalCrossEncoderBackend",
    "llm": "judge_llm:LLMJudgeBackend",
}

DEFAULT_DEADLINE_S = 3.0
DEFAULT_DOWN_FOR_S = 60.0
# How long past a backend's own deadline the chain keeps waiting. A backend that honours
# its deadline raises its own timeout inside this window, and that error carries more
# detail (an HTTP status, which call) than the chain's generic one. Small, because every
# fallen-through backend adds its deadline plus this to the brief's latency.
DEFAULT_GRACE_S = 0.1

# Outcomes an attempt may record, beyond "ok": every BackendUnavailable kind, plus
# "skipped_down" for a backend the breaker is holding off. "error" doubles as the kind
# for a programming error inside a backend (TypeError and friends).
OUTCOMES = ("ok", "skipped_down") + UNAVAILABLE_KINDS


# ---- construction -----------------------------------------------------------------
def build_backend(name: str, **kw) -> Backend:
    """Construct the backend registered as `name`, importing its module only now.

    Unknown name -> ValueError listing the known ones (a typo in RAG_VALIDATOR is a
    configuration mistake, not a missing dependency). A module that cannot be imported
    -> BackendNotConfigured naming the module: from the operator's side a missing SDK and
    a missing key are the same problem, "this backend cannot run here". The backend's
    own constructor raises BackendNotConfigured for a missing key or model file."""
    spec = REGISTRY.get(name)
    if spec is None:
        raise ValueError(f"unknown validator backend {name!r}; known: {', '.join(sorted(REGISTRY))}")
    mod_name, cls_name = spec.split(":", 1)
    try:
        mod = importlib.import_module(mod_name)
    except BackendNotConfigured:
        raise
    except Exception as e:                        # ImportError, or a module that fails to load
        raise BackendNotConfigured(
            f"{name}: cannot import module {mod_name!r} ({type(e).__name__}: {e})") from e
    cls = getattr(mod, cls_name, None)
    if cls is None:
        raise BackendNotConfigured(f"{name}: module {mod_name!r} has no class {cls_name!r}")
    return cls(**kw)


def _parse_names(raw: str | None) -> list[str]:
    """RAG_VALIDATOR -> backend names in priority order. [] means validation off.

    Names are lower-cased and stripped. "none" alone switches validation off; "none"
    mixed with real names is refused, because it is impossible to tell which one the
    author meant. Duplicates are refused: a backend listed twice would be tried twice
    against the same outage, and the trace could not say which entry answered."""
    raw = (raw or "").strip()
    if raw.lower() in ("", "none"):
        return []
    names = [n.strip().lower() for n in raw.split(",") if n.strip()]
    if "none" in names:
        raise ValueError(f"RAG_VALIDATOR={raw!r}: 'none' cannot be combined with backend names")
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise ValueError(f"RAG_VALIDATOR={raw!r}: duplicate backend(s) {', '.join(dupes)}")
    unknown = [n for n in names if n not in REGISTRY]
    if unknown:
        raise ValueError(f"RAG_VALIDATOR={raw!r}: unknown backend(s) {', '.join(unknown)}; "
                         f"known: {', '.join(sorted(REGISTRY))}")
    return names


def _deadline_from_env(env) -> float | None:
    """RAG_VALIDATOR_DEADLINE_S as a positive float, or None when it is unset or blank.

    None rather than DEFAULT_DEADLINE_S so the chain can tell "nobody set a deadline" (a
    backend's own deadline applies) from "someone set one" (it caps every backend). A
    value that does not parse is an error rather than a silent default: a deadline someone
    set and the code ignored is a latency budget nobody is enforcing."""
    raw = (env.get("RAG_VALIDATOR_DEADLINE_S") or "").strip()
    if not raw:
        return None
    try:
        v = float(raw)
    except ValueError:
        raise ValueError(f"RAG_VALIDATOR_DEADLINE_S={raw!r} is not a number") from None
    if not v > 0:
        raise ValueError(f"RAG_VALIDATOR_DEADLINE_S={raw!r} must be > 0")
    return v


def chain_from_env(env=None) -> "Chain":
    """The chain RAG_VALIDATOR asks for, e.g. "jev,nemotron,local" (priority order).

    Unset, "" or "none" -> an empty chain: validation off. Every name is checked before
    any backend is built, so a typo fails fast instead of after a model has loaded. A
    named backend that raises BackendNotConfigured is re-raised with RAG_VALIDATOR in the
    message: someone asked for it, so running without it would be a silent downgrade.

    RAG_VALIDATOR_DEADLINE_S, when set, caps every backend's deadline (see
    Chain._deadline_for); unset, each backend keeps its own and the rest use
    DEFAULT_DEADLINE_S. `env` governs chain selection only; backends read their own keys
    from os.environ."""
    env = os.environ if env is None else env
    raw = env.get("RAG_VALIDATOR")
    names = _parse_names(raw)
    deadline = _deadline_from_env(env)
    backends: list[Backend] = []
    for n in names:
        try:
            backends.append(build_backend(n))
        except BackendNotConfigured as e:
            raise BackendNotConfigured(
                f"RAG_VALIDATOR={raw!r} names {n!r}, which is not configured here: {e}. "
                f"Configure it, or remove it from RAG_VALIDATOR.") from e
    return Chain(backends, deadline_s=deadline, requested=",".join(names))


# ---- the result ---------------------------------------------------------------------
@dataclass
class ValidationResult:
    """What one judge() call did, for the caller and for the trace.

    verdicts           one per JUDGED passage (the first `judged` of the pool, fused
                       order); None when nobody answered, and the caller keeps fused order
    backend_requested  the chain as written, e.g. "jev,nemotron"
    backend_used       which backend's verdicts these are, or None
    attempts           every backend tried or skipped, in order:
                       {"backend", "outcome", "ms", "status", "detail"}
    fell_back          True if anything before the used backend failed or was skipped, or
                       if nobody answered at all
    judged             passages sent to the used backend
    truncated          passages past the used backend's capacity: NOT judged
    provisional        the used backend's calibration is provisional (fitted on data that
                       is not brief-shaped) — usable, but the trace must say so
    pool_size          passages the caller offered
    default_width      the width the caller would have used with no validator (passed to
                       judge()); select() keeps no unjudged passage at or past it. None
                       means the caller did not widen and every unjudged passage is kept"""
    verdicts: list[Verdict] | None
    backend_requested: str
    backend_used: str | None
    attempts: list[dict] = field(default_factory=list)
    fell_back: bool = False
    judged: int = 0
    truncated: int = 0
    provisional: bool = False
    pool_size: int = 0
    default_width: int | None = None

    def aligned(self) -> list[Verdict | None]:
        """One entry per pooled passage: its verdict, or None where it was not judged
        (truncated, or nobody answered). Zip this with the hits to feed select()."""
        got = list(self.verdicts or [])
        return got + [None] * (self.pool_size - len(got))

    def select(self, hits: list, *, floor: int = 2) -> list[tuple]:
        """select() over `hits` (the pooled hits, fused order, same length as the pool)
        with this result's verdicts and default_width. The one-call form, so a caller
        that widened cannot forget the cap on unjudged passages."""
        hits = list(hits)
        if len(hits) != self.pool_size:
            raise ValueError(f"select: {len(hits)} hits for a pool of {self.pool_size}")
        return select(list(zip(hits, self.aligned())), floor=floor,
                      default_width=self.default_width)

    def _counts(self) -> tuple[int, int]:
        """(passed, rejected) among judged passages; (0, 0) when nothing was judged."""
        vs = self.verdicts or []
        passed = sum(1 for v in vs if v.value)
        return passed, len(vs) - passed

    def as_contract(self) -> dict:
        """Exactly the rag_io v1 `$defs/validation` shape. That definition does not admit
        attempts, truncated or provisional yet, so they live in the trace (as_dict)."""
        passed, rejected = self._counts()
        return {"backend_requested": self.backend_requested, "backend_used": self.backend_used,
                "pool_size": self.pool_size, "passed": passed, "rejected": rejected,
                "fell_back": self.fell_back}

    def as_dict(self) -> dict:
        """Everything, JSON-serialisable, for the per-brief trace: the contract keys plus
        attempts, judged, truncated, provisional and default_width."""
        return {**self.as_contract(), "judged": self.judged, "truncated": self.truncated,
                "provisional": self.provisional, "default_width": self.default_width,
                "attempts": [dict(a) for a in self.attempts]}


# ---- the chain ----------------------------------------------------------------------
class _HardTimeout(Exception):
    """Internal: the chain stopped waiting for a backend. Becomes outcome 'timeout'."""


def _call_with_deadline(backend: Backend, query: Query, passages: list[Passage],
                        deadline_s: float, grace_s: float) -> list[Verdict]:
    """Run backend.score() in a daemon thread and wait at most deadline_s + grace_s.

    A fresh daemon thread per call rather than a shared ThreadPoolExecutor: a backend that
    truly hangs keeps its thread forever, which would slowly starve a bounded pool, and
    executor threads are joined at interpreter exit, so one hung socket would also hang
    shutdown. Thread start-up costs microseconds against a network call. The abandoned
    thread's eventual result is discarded."""
    box: dict = {}
    done = threading.Event()

    def run() -> None:
        """Worker body: call score() and park its result or exception for the waiter."""
        try:
            box["out"] = backend.score(query, passages, deadline_s=deadline_s)
        except BaseException as e:               # noqa: BLE001 — handed to the waiter
            box["err"] = e
        finally:
            done.set()

    threading.Thread(target=run, name=f"judge-{getattr(backend, 'name', '?')}", daemon=True).start()
    if not done.wait(deadline_s + grace_s):
        raise _HardTimeout(f"no answer within {deadline_s:g}s (+{grace_s:g}s grace)")
    if "err" in box:
        err = box["err"]
        if isinstance(err, Exception):
            raise err
        # SystemExit and the like from inside a backend: contain it as a bug, never let
        # a validator end the process that is writing the brief.
        raise RuntimeError(f"{type(err).__name__} raised inside backend: {err}")
    return box["out"]


def _check_output(backend: Backend, passages: list[Passage], out) -> list[Verdict]:
    """Enforce the judge_base invariants on a backend's output, so a bug in one backend
    cannot attach a wrong verdict to a hit. Any breach -> BackendUnavailable(bad_response)."""
    name = backend.name
    if not isinstance(out, list) or not all(isinstance(v, Verdict) for v in out):
        raise BackendUnavailable(f"{name}: score() must return a list of Verdict", kind="bad_response")
    check_verdicts(name, passages, out)
    for v in out:
        if v.score is not None and not backend.calibrated:
            raise BackendUnavailable(f"{name}: uncalibrated backend populated score", kind="bad_response")
        if v.score is not None and v.why is not None:
            raise BackendUnavailable(f"{name}: verdict has both score and why", kind="bad_response")
    return out


def _unavailable_kind(e: BaseException) -> str | None:
    """The failure kind if `e` is a BackendUnavailable, else None. Duck-typed as well as
    isinstance-checked: a backend that imported judge_base under a package path (e.g.
    `rag.judge_base`) raises a class that is a different object with the same meaning."""
    if isinstance(e, BackendUnavailable):
        return e.kind
    kind = getattr(e, "kind", None)
    if type(e).__name__ == "BackendUnavailable" and kind in UNAVAILABLE_KINDS:
        return kind
    return None


def _provisional(backend: Backend) -> bool:
    """True if the backend exposes a calibration (or a flag) that is provisional."""
    cal = getattr(backend, "calibration", None)
    if cal is not None and hasattr(cal, "provisional"):
        return bool(cal.provisional)
    return bool(getattr(backend, "provisional", False))


class Chain:
    """Relevance backends in priority order, each the failsafe for the one before.

    Thread-safe: judge() is called concurrently for different buckets. The only shared
    mutable state is the breaker table, and it is only touched under `_lock`; the lock is
    never held while a backend runs."""

    def __init__(self, backends: list[Backend], *, deadline_s: float | None = None,
                 down_for_s: float = DEFAULT_DOWN_FOR_S, clock=time.monotonic,
                 grace_s: float = DEFAULT_GRACE_S, requested: str | None = None):
        """Validate the backends up front, so a malformed one fails at start-up rather
        than on the first brief. `requested` is RAG_VALIDATOR as written; it defaults to
        the backends' names joined. `clock` drives the breaker only (inject a fake one in
        tests); latency in the trace is always real time.

        `deadline_s` None means nobody set one: a backend that declares its own deadline
        keeps it and the rest get DEFAULT_DEADLINE_S. A number is an explicit budget and
        caps every backend, its own deadline included (`deadline_explicit` records which)."""
        names: list[str] = []
        for b in backends:
            if not isinstance(b, Backend):
                raise TypeError(f"{b!r} does not implement judge_base.Backend "
                                f"(name, capacity, calibrated, score)")
            cap = b.capacity
            if isinstance(cap, bool) or not isinstance(cap, int) or cap < 1:
                raise ValueError(f"{b.name}: capacity must be an int >= 1, got {cap!r}")
            names.append(b.name)
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate backend name(s) in chain: {', '.join(dupes)}")
        self.deadline_explicit = deadline_s is not None
        if deadline_s is None:
            deadline_s = DEFAULT_DEADLINE_S
        if not deadline_s > 0:
            raise ValueError(f"deadline_s must be > 0, got {deadline_s!r}")
        self._backends = list(backends)
        self.deadline_s = float(deadline_s)
        self.down_for_s = float(down_for_s)
        self.grace_s = float(grace_s)
        self._clock = clock
        self._down_until: dict[int, float] = {}   # keyed by position: names are unique anyway
        self._lock = threading.Lock()
        self.requested = requested if requested is not None else ",".join(names)

    # -- introspection --
    @property
    def names(self) -> list[str]:
        """Backend names in priority order."""
        return [b.name for b in self._backends]

    @property
    def empty(self) -> bool:
        """True when no validator is configured: validation is off."""
        return not self._backends

    def _is_down(self, i: int) -> bool:
        """Is backend i held off by the breaker right now? Caller holds no lock."""
        with self._lock:
            until = self._down_until.get(i)
            if until is None:
                return False
            if self._clock() >= until:
                del self._down_until[i]            # recovered: the next call tries it again
                return False
            return True

    def _mark_down(self, i: int) -> None:
        """Hold backend i off for down_for_s from now."""
        with self._lock:
            self._down_until[i] = self._clock() + self.down_for_s

    def _deadline_for(self, b: Backend) -> float:
        """The deadline this backend gets on this call.

        No declared deadline -> the chain's. A declared one -> the backend's own, because
        it knows its latency (a CPU cross-encoder needs longer than a hosted reranker);
        but when the chain's deadline was set explicitly it is a latency budget someone
        wrote down, so it caps the backend's: min(own, chain). Without the cap,
        RAG_VALIDATOR_DEADLINE_S=1 would still let a backend declaring 8s hold a bucket
        for 8s."""
        d = getattr(b, "deadline_s", None)
        if not (isinstance(d, (int, float)) and not isinstance(d, bool) and d > 0):
            return self.deadline_s
        return min(float(d), self.deadline_s) if self.deadline_explicit else float(d)

    def pool_width(self, default: int) -> int:
        """How many passages retrieval should hand the validator: the larger of `default`
        and the capacity of the first backend not marked down, or `default` when the chain
        is empty or every backend is down. This is what makes wide extraction conditional
        on a live validator — widening the pool with nobody to judge it only adds noise.

        Never narrower than `default`. A live validator with a small capacity (local on
        cpu judges 6) must not make retrieval thinner than validation-off retrieval
        would be: it judges the first `capacity`, and the rest stay unjudged in fused
        order up to `default`, exactly as they would have with no validator at all.

        This is a forecast made before the call: the lead may still fail, and the chain
        may fall to a backend with a smaller capacity. Pass the same `default` to
        judge(default_width=...) so select() drops any unjudged passage past it; that is
        what keeps a failed widening from reaching the brief."""
        for i, b in enumerate(self._backends):
            if not self._is_down(i):
                return max(int(default), int(b.capacity))
        return default

    # -- the call --
    def judge(self, query: Query, passages: list[Passage], *,
              default_width: int | None = None) -> ValidationResult:
        """Judge `passages` (fused order) with the first backend that answers.

        `default_width` is the width the caller would have retrieved with no validator
        (the `default` it gave pool_width). It is recorded on the result so select() can
        drop unjudged passages the caller only fetched because it widened. None: the
        caller did not widen, and no unjudged passage is dropped.

        Never raises for a backend's failure. Each failure, a hard timeout or an
        exception of any kind inside a backend, is recorded in `attempts`, marks that
        backend down, and falls through. A validator bug must not crash a brief."""
        if default_width is not None and (isinstance(default_width, bool)
                                          or not isinstance(default_width, int)
                                          or default_width < 0):
            raise ValueError(f"default_width must be an int >= 0 or None, got {default_width!r}")
        passages = list(passages)
        res = ValidationResult(verdicts=None, backend_requested=self.requested,
                               backend_used=None, pool_size=len(passages),
                               default_width=default_width)
        if not passages:                          # nothing to judge: no call, nothing failed
            res.verdicts = []
            return res
        if self.empty:                            # validation off: not a fall-back
            return res
        for i, b in enumerate(self._backends):
            if self._is_down(i):
                res.attempts.append({"backend": b.name, "outcome": "skipped_down", "ms": 0.0,
                                     "status": None, "detail": None})
                continue
            batch = passages[:b.capacity]
            t0 = time.perf_counter()
            outcome, status, detail, verdicts = "ok", None, None, None
            try:
                out = _call_with_deadline(b, query, batch, self._deadline_for(b), self.grace_s)
                verdicts = _check_output(b, batch, out)
            except _HardTimeout as e:
                outcome, detail = "timeout", str(e)
            except Exception as e:                # noqa: BLE001 — contained by design
                kind = _unavailable_kind(e)
                outcome = kind or "error"
                status = getattr(e, "status", None) if kind else None
                detail = f"{type(e).__name__}: {e}"[:300]
            ms = round((time.perf_counter() - t0) * 1000.0, 1)
            res.attempts.append({"backend": b.name, "outcome": outcome, "ms": ms,
                                 "status": status, "detail": detail})
            if verdicts is None:
                self._mark_down(i)
                continue
            res.verdicts = verdicts
            res.backend_used = b.name
            res.judged = len(batch)
            res.truncated = len(passages) - len(batch)
            res.provisional = _provisional(b)
            res.fell_back = len(res.attempts) > 1
            return res
        res.fell_back = True                      # somebody was asked and nobody answered
        return res


# ---- selection ----------------------------------------------------------------------
def select(pairs: list, *, floor: int = 2, default_width: int | None = None) -> list[tuple]:
    """Apply verdicts to hits. `pairs` is [(item, Verdict | None), ...] in fused order.

    Returns [(item, verdict, kept), ...] in this order:
      1. passing items (kept="passed"): those with a calibrated score by score, highest
         first, then those without a score in fused order. A score is only compared with
         another score, never with None.
      2. if NOTHING passed and nothing unjudged survives either — every chunk failed —
         the best `floor` judged items (kept="floor"), ranked by calibrated score, or by
         the backend's raw output when it is uncalibrated, else in fused order. Sai's
         decision: a bucket is never emptied by the validator; the top-scoring items come
         back flagged so the reader can see the gate found nothing. When unjudged items
         survive, the bucket is not empty and no rejected item is resurrected ahead of
         them.
      3. items that were not judged (verdict None: truncated, or nobody answered) in fused
         order (kept="unjudged"), but only those whose fused position is below
         `default_width`. They were not rejected, so inside the width the caller would
         have used anyway they stay; past it they exist only because the pool was widened
         for a validator that then did not judge them, and keeping them would let a
         passage nobody looked at outlive a better-ranked one the validator rejected.
         `default_width` None keeps every unjudged item (the caller did not widen).
    Rejected items beyond the floor are dropped. Ties keep fused order: stable and
    deterministic. Nobody answered + default_width=N gives exactly the first N in fused
    order: validation-off behaviour."""
    if floor < 0:
        raise ValueError(f"floor must be >= 0, got {floor!r}")
    if default_width is not None and default_width < 0:
        raise ValueError(f"default_width must be >= 0 or None, got {default_width!r}")
    rows = [(i, item, v) for i, (item, v) in enumerate(pairs)]

    def ranked(group: list) -> list:
        """Scored first by score desc (ties by fused position), then unscored in fused order."""
        scored = sorted((r for r in group if r[2].score is not None), key=lambda r: (-r[2].score, r[0]))
        return scored + [r for r in group if r[2].score is None]

    def best_first(group: list) -> list:
        """Floor order: calibrated score, else raw backend output, else fused position.
        `raw` is only compared with `raw` from the same backend (one result, one backend)."""
        def key(r):
            """Sort key: (tier, descending value, fused position)."""
            v = r[2]
            if v.score is not None:
                return (0, -v.score, r[0])
            if v.raw is not None:
                return (1, -v.raw, r[0])
            return (2, 0.0, r[0])
        return sorted(group, key=key)

    judged = [r for r in rows if r[2] is not None]
    passing = [r for r in judged if r[2].value]
    unjudged = [(item, None, "unjudged") for i, item, v in rows
                if v is None and (default_width is None or i < default_width)]
    out = [(item, v, "passed") for _, item, v in ranked(passing)]
    if judged and not passing and not unjudged:
        out += [(item, v, "floor") for _, item, v in best_first(judged)[:floor]]
    return out + unjudged


# ---- CLI: probe the configured backends -------------------------------------------
PROBE_QUERY = Query(text="How should a challenger brand position itself against an "
                         "entrenched category leader?")
PROBE_PASSAGES = [
    Passage("probe_relevant", "Challenger brands win by picking a fight the leader cannot "
            "answer without undermining itself: redefine the category's terms, dramatise one "
            "sharp point of difference, and make the leader's scale look like complacency."),
    Passage("probe_irrelevant", "Feed the sourdough starter twice a day with equal weights of "
            "flour and water, and keep it somewhere warm until it doubles in size."),
]


def _load_env_file() -> None:
    """Load engine/.env into os.environ without overriding what is set, as rag.py does,
    so `python3 judge.py check` sees the same keys the pipeline does. CLI only: importing
    this module never touches the environment."""
    env = HERE.parent / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:]
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def _ranked_relevant_higher(rel: Verdict, irr: Verdict) -> bool | None:
    """Did the backend prefer the relevant probe? By score when both have one, else by
    raw output, else by the pass/fail decision; None when it cannot be told (both passed
    or both failed with nothing to rank them by)."""
    if rel.score is not None and irr.score is not None:
        return rel.score > irr.score
    if rel.raw is not None and irr.raw is not None:
        return rel.raw > irr.raw
    if rel.value != irr.value:
        return rel.value
    return None


def check(names: list[str] | None = None, *, deadline_s: float | None = None,
          verbose: bool = True) -> dict:
    """Build and call every named backend once and report which are alive.

    The only honest test of a fallback chain is to call every link: nothing exercises the
    backstop in normal operation, so it rots silently until the day the lead is down (see
    parse_brief.check_chain). Each backend is built on its own here, so one that is not
    configured is reported rather than aborting the report. Hits the network.

    `deadline_s` (else RAG_VALIDATOR_DEADLINE_S) caps every backend's own deadline, as
    in the pipeline; neither set, each backend keeps its own."""
    if names is None:
        names = _parse_names(os.environ.get("RAG_VALIDATOR"))
    deadline = deadline_s if deadline_s is not None else _deadline_from_env(os.environ)
    rows, alive = [], 0
    for n in names:
        row: dict = {"backend": n, "configured": False, "alive": False, "outcome": None,
                     "ms": None, "status": None, "relevant_higher": None, "detail": None}
        try:
            b = build_backend(n)
            row["configured"] = True
        except (BackendNotConfigured, ValueError) as e:
            row["detail"] = str(e)[:300]
            rows.append(row)
            if verbose:
                print(f"  MISS {n:<10} not configured — {row['detail']}", file=sys.stderr)
            continue
        except Exception as e:                    # noqa: BLE001 — a constructor bug is still a row
            row["detail"] = f"{type(e).__name__}: {e}"[:300]
            rows.append(row)
            if verbose:
                print(f"  MISS {n:<10} constructor failed — {row['detail']}", file=sys.stderr)
            continue
        try:
            res = Chain([b], deadline_s=deadline, down_for_s=0.0).judge(PROBE_QUERY, PROBE_PASSAGES)
        except Exception as e:                    # noqa: BLE001 — e.g. a malformed capacity
            row["detail"] = f"{type(e).__name__}: {e}"[:300]
            rows.append(row)
            if verbose:
                print(f"  DEAD {n:<10} {row['detail']}", file=sys.stderr)
            continue
        a = res.attempts[0]
        row.update(outcome=a["outcome"], ms=a["ms"], status=a["status"], detail=a["detail"])
        if res.verdicts is not None and len(res.verdicts) == 2:
            row["alive"] = True
            alive += 1
            row["relevant_higher"] = _ranked_relevant_higher(*res.verdicts)
        elif res.verdicts is not None:           # capacity 1: alive, but only one probe judged
            row["alive"] = True
            alive += 1
        rows.append(row)
        if verbose:
            if row["alive"]:
                rh = {True: "yes", False: "NO — ranked the irrelevant probe higher",
                      None: "undecided"}[row["relevant_higher"]]
                print(f"  {'OK  ' if row['relevant_higher'] else 'WARN'} {n:<10} "
                      f"{row['ms']:>7.1f}ms  relevant ranked higher: {rh}", file=sys.stderr)
            else:
                st = f" (status {row['status']})" if row["status"] else ""
                print(f"  DEAD {n:<10} {row['outcome']}{st} {row['ms']:.1f}ms\n"
                      f"       {(row['detail'] or '')[:180]}", file=sys.stderr)
    ok = not ((len(rows) > 1 and alive <= 1) or (rows and alive == 0))
    if verbose:
        print(f"[i] validators: {alive}/{len(rows)} alive", file=sys.stderr)
        if not rows:
            print("[i] RAG_VALIDATOR is unset or 'none': validation is off.", file=sys.stderr)
        elif not ok:
            print("[!] no working fallback — if the lead validator fails, briefs keep the "
                  "fused order unvalidated.", file=sys.stderr)
    return {"alive": alive, "total": len(rows), "ok": ok, "backends": rows}


def _positive_float(s: str) -> float:
    """argparse type for --deadline: a number > 0, so a typo is a usage error rather
    than every backend reported dead."""
    try:
        v = float(s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{s!r} is not a number") from None
    if not v > 0:
        raise argparse.ArgumentTypeError(f"{s!r} must be > 0")
    return v


def main(argv: list[str] | None = None) -> int:
    """`judge.py check [--backends a,b] [--deadline S]`. Exit 1 when a chain of more than
    one backend has at most one alive, or when every requested backend is dead."""
    ap = argparse.ArgumentParser(prog="judge.py", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="probe every validator backend once")
    c.add_argument("--backends", help="comma list; default RAG_VALIDATOR")
    c.add_argument("--deadline", type=_positive_float,
                   help="deadline cap in seconds for every backend (a backend's own "
                        "shorter deadline still applies); default RAG_VALIDATOR_DEADLINE_S")
    args = ap.parse_args(argv)
    _load_env_file()
    try:
        names = _parse_names(args.backends if args.backends is not None
                             else os.environ.get("RAG_VALIDATOR"))
    except ValueError as e:
        print(f"[!] {e}", file=sys.stderr)
        return 2
    report = check(names, deadline_s=args.deadline)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
