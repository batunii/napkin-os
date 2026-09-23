"""The validation chain: fall-through, breaker, deadline, capacity, containment, select().
Run: cd engine/rag && RAG_STORE=local RAG_INDEX=./_index_v3 python3 -m pytest test_judge.py -q

Every backend here is an in-memory fake. Nothing touches the network.
"""
from __future__ import annotations

import sys
import threading
import time
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import pytest  # noqa: E402
import judge  # noqa: E402
import judge_base as jb  # noqa: E402

Q = jb.Query(text="challenger brand vs entrenched leader")


def ps(n: int) -> list[jb.Passage]:
    """n distinct passages p0..p{n-1}."""
    return [jb.Passage(f"p{i}", f"text {i}") for i in range(n)]


class Fake:
    """Configurable in-memory backend. `fail` is a BackendUnavailable kind, an exception
    instance to raise, or None to answer. Records every call."""

    def __init__(self, name="a", capacity=10, calibrated=True, fail=None, sleep=0.0,
                 scores=None, deadline_s=None, calibration=None):
        """Store the behaviour; `scores` maps passage id -> score (default 0.9 passes)."""
        self.name, self.capacity, self.calibrated = name, capacity, calibrated
        self.fail, self.sleep, self.scores = fail, sleep, scores or {}
        self.calls: list[list[str]] = []
        self._lock = threading.Lock()
        if deadline_s is not None:
            self.deadline_s = deadline_s
        if calibration is not None:
            self.calibration = calibration

    def score(self, query, passages, *, deadline_s):
        """Fail, sleep or answer as configured, one verdict per passage in order."""
        with self._lock:
            self.calls.append([p.id for p in passages])
        if self.sleep:
            time.sleep(self.sleep)
        if isinstance(self.fail, str):
            raise jb.BackendUnavailable(f"{self.name} down", kind=self.fail, status=503)
        if isinstance(self.fail, BaseException):
            raise self.fail
        out = []
        for p in passages:
            s = self.scores.get(p.id, 0.9)
            out.append(jb.Verdict(value=s >= 0.5, score=s if self.calibrated else None,
                                  why=None, backend=self.name, raw=s))
        return out


class FakeClock:
    """A monotonic clock the test advances by hand."""

    def __init__(self):
        """Start at t=1000."""
        self.t = 1000.0

    def __call__(self):
        """Current fake time."""
        return self.t


# ---- order and fall-through --------------------------------------------------------
def test_first_backend_that_answers_wins_and_later_ones_are_not_called():
    """Priority order: the lead answers, so the failsafe is never called and nothing fell back."""
    a, b = Fake("a"), Fake("b")
    r = judge.Chain([a, b]).judge(Q, ps(3))
    assert r.backend_used == "a" and not r.fell_back and len(b.calls) == 0
    assert [v.backend for v in r.verdicts] == ["a"] * 3
    assert r.attempts == [{"backend": "a", "outcome": "ok", "ms": r.attempts[0]["ms"],
                           "status": None, "detail": None}]


def test_unavailable_falls_through_and_records_kind_and_status():
    """A run-time failure falls through and the trace says which kind and which HTTP status."""
    a, b = Fake("a", fail="http_error"), Fake("b")
    r = judge.Chain([a, b]).judge(Q, ps(2))
    assert r.backend_used == "b" and r.fell_back
    assert [(x["backend"], x["outcome"], x["status"]) for x in r.attempts] == \
        [("a", "http_error", 503), ("b", "ok", None)]


def test_nobody_answers_gives_none_verdicts_and_fell_back():
    """Every backend down: verdicts None (keep fused order), fell_back True, requested chain kept."""
    r = judge.Chain([Fake("a", fail="retired"), Fake("b", fail="not_entitled")],
                    requested="a,b").judge(Q, ps(2))
    assert r.verdicts is None and r.backend_used is None and r.fell_back
    assert r.aligned() == [None, None]
    assert r.as_dict()["backend_requested"] == "a,b"


def test_empty_chain_is_validation_off_not_a_fallback():
    """No validator configured is today's behaviour, not a failure: no attempts, no fall-back flag."""
    c = judge.Chain([])
    r = c.judge(Q, ps(2))
    assert c.empty and c.names == [] and r.verdicts is None and not r.fell_back and r.attempts == []


def test_empty_passages_make_no_call():
    """Nothing to judge means no backend call at all."""
    a = Fake("a")
    r = judge.Chain([a]).judge(Q, [])
    assert a.calls == [] and r.verdicts == [] and r.attempts == [] and r.pool_size == 0


# ---- breaker -----------------------------------------------------------------------
def test_breaker_marks_down_then_recovers_after_down_for_s():
    """A failure holds the backend off for down_for_s (pool width follows the next one),
    then it is retried."""
    clock = FakeClock()
    a, b = Fake("a", fail="timeout"), Fake("b", capacity=4)
    c = judge.Chain([a, b], down_for_s=60, clock=clock)
    assert c.pool_width(default=2) == 10
    c.judge(Q, ps(2))
    assert len(a.calls) == 1
    assert c.pool_width(default=2) == 4              # a is down: width follows b
    r = c.judge(Q, ps(2))
    assert len(a.calls) == 1                          # skipped, not retried
    assert r.attempts[0]["outcome"] == "skipped_down" and r.fell_back and r.backend_used == "b"
    clock.t += 59.9
    c.judge(Q, ps(2))
    assert len(a.calls) == 1
    clock.t += 0.2                                   # past down_for_s: tried again
    a.fail = None
    r = c.judge(Q, ps(2))
    assert len(a.calls) == 2 and r.backend_used == "a" and not r.fell_back
    assert c.pool_width(default=2) == 10


def test_pool_width_is_never_narrower_than_the_default():
    """A live validator with a small capacity must not thin retrieval below what
    validation-off retrieval would fetch: local on cpu judges 6, retrieval still gets 40."""
    c = judge.Chain([Fake("local", capacity=6)])
    assert c.pool_width(default=40) == 40
    assert judge.Chain([Fake("jev", capacity=50)]).pool_width(default=40) == 50


def test_pool_width_is_default_when_empty_or_all_down():
    """With no live validator the caller's default width is used: no widening for nobody."""
    clock = FakeClock()
    assert judge.Chain([]).pool_width(default=7) == 7
    c = judge.Chain([Fake("a", fail="error")], clock=clock)
    c.judge(Q, ps(1))
    assert c.pool_width(default=7) == 7


# ---- deadline ----------------------------------------------------------------------
def test_chain_enforces_deadline_on_a_backend_that_ignores_it():
    """A backend that sleeps past its deadline is abandoned by the chain and recorded as a timeout."""
    slow, b = Fake("slow", sleep=1.0), Fake("b")
    c = judge.Chain([slow, b], deadline_s=0.05, grace_s=0.02)
    t0 = time.perf_counter()
    r = c.judge(Q, ps(2))
    assert time.perf_counter() - t0 < 0.5
    assert r.attempts[0]["outcome"] == "timeout" and r.backend_used == "b"


def test_backend_declared_deadline_overrides_chain_default():
    """A backend's own shorter deadline_s is the one enforced, not the chain's longer one."""
    slow = Fake("slow", sleep=0.6, deadline_s=0.03)
    t0 = time.perf_counter()
    r = judge.Chain([slow], deadline_s=5.0, grace_s=0.02).judge(Q, ps(1))
    assert time.perf_counter() - t0 < 0.35 and r.attempts[0]["outcome"] == "timeout"


def test_explicit_chain_deadline_caps_a_backends_longer_own_deadline():
    """An explicitly set chain deadline caps a backend's own longer one (local declares 8s);
    with no explicit deadline the backend's own still applies."""
    slow = Fake("local", sleep=0.5, deadline_s=8.0)
    t0 = time.perf_counter()
    r = judge.Chain([slow], deadline_s=0.05, grace_s=0.02).judge(Q, ps(1))
    assert time.perf_counter() - t0 < 0.3 and r.attempts[0]["outcome"] == "timeout"
    r = judge.Chain([Fake("local", sleep=0.1, deadline_s=8.0)], grace_s=0.02).judge(Q, ps(1))
    assert r.attempts[0]["outcome"] == "ok"             # nobody set one: its own 8s applies


def test_capped_deadline_is_what_score_receives():
    """score() is handed min(own, explicit chain deadline), so a backend that honours its
    deadline raises its own detailed timeout instead of being abandoned."""
    seen = []

    class B(Fake):
        """Records the deadline it was given."""
        def score(self, query, passages, *, deadline_s):
            """Capture deadline_s, then answer normally."""
            seen.append(deadline_s)
            return super().score(query, passages, deadline_s=deadline_s)

    judge.Chain([B("b", deadline_s=8.0)], deadline_s=1.0).judge(Q, ps(1))
    judge.Chain([B("b", deadline_s=0.5)], deadline_s=1.0).judge(Q, ps(1))
    judge.Chain([B("b", deadline_s=8.0)]).judge(Q, ps(1))
    assert seen == [1.0, 0.5, 8.0]


def test_deadline_is_passed_to_score():
    """score() receives the deadline the chain resolved for it."""
    seen = {}

    class B(Fake):
        """Records the deadline it was given."""
        def score(self, query, passages, *, deadline_s):
            """Capture deadline_s, then answer normally."""
            seen["d"] = deadline_s
            return super().score(query, passages, deadline_s=deadline_s)

    judge.Chain([B("b")], deadline_s=1.25).judge(Q, ps(1))
    assert seen["d"] == 1.25


# ---- capacity ----------------------------------------------------------------------
def test_capacity_truncates_to_the_first_n_in_fused_order():
    """Past capacity, only the first N (fused order) are judged and the rest are counted as truncated."""
    a = Fake("a", capacity=3)
    r = judge.Chain([a]).judge(Q, ps(5))
    assert a.calls == [["p0", "p1", "p2"]]
    assert (r.judged, r.truncated, r.pool_size) == (3, 2, 5)
    assert r.aligned()[3:] == [None, None] and len(r.aligned()) == 5


def test_truncation_is_counted_against_the_backend_actually_used():
    """Each backend gets its own capacity's worth; truncation reflects the one that answered."""
    a, b = Fake("a", capacity=5, fail="rate_limited"), Fake("b", capacity=2)
    r = judge.Chain([a, b]).judge(Q, ps(5))
    assert a.calls == [["p0", "p1", "p2", "p3", "p4"]] and b.calls == [["p0", "p1"]]
    assert (r.judged, r.truncated) == (2, 3)


def test_bad_capacity_or_non_backend_is_refused_at_construction():
    """Malformed backends fail at start-up, not on the first brief."""
    with pytest.raises(ValueError):
        judge.Chain([Fake("a", capacity=0)])
    with pytest.raises(TypeError):
        judge.Chain([object()])
    with pytest.raises(ValueError):
        judge.Chain([Fake("a"), Fake("a")])


# ---- containment and output checks -------------------------------------------------
@pytest.mark.parametrize("exc", [TypeError("bug"), KeyError("k"), ZeroDivisionError(), SystemExit(3)])
def test_an_exception_inside_a_backend_is_contained(exc):
    """A validator bug (any exception, even SystemExit) is recorded as 'error' and falls through."""
    a, b = Fake("a", fail=exc), Fake("b")
    r = judge.Chain([a, b]).judge(Q, ps(2))
    assert r.attempts[0]["outcome"] == "error" and r.backend_used == "b"


def test_short_output_is_bad_response_and_falls_through():
    """A short verdict list would misattach verdicts, so it is refused as bad_response."""
    class Short(Fake):
        """Drops the last verdict."""
        def score(self, query, passages, *, deadline_s):
            """Answer, then lose one verdict."""
            return super().score(query, passages, deadline_s=deadline_s)[:-1]

    r = judge.Chain([Short("s"), Fake("b")]).judge(Q, ps(3))
    assert r.attempts[0]["outcome"] == "bad_response" and r.backend_used == "b"


def test_uncalibrated_score_or_score_with_why_is_refused():
    """The chain enforces the score/why invariants itself instead of trusting each backend."""
    class Liar(Fake):
        """Claims to be uncalibrated but fills score."""
        def score(self, query, passages, *, deadline_s):
            """Return a scored verdict per passage."""
            return [jb.Verdict(True, 0.8, None, self.name) for _ in passages]

    class Chatty(Fake):
        """Fills both score and why."""
        def score(self, query, passages, *, deadline_s):
            """Return verdicts carrying both fields."""
            return [jb.Verdict(True, 0.8, "because", self.name) for _ in passages]

    r = judge.Chain([Liar("l", calibrated=False), Chatty("c"), Fake("b")]).judge(Q, ps(1))
    assert [a["outcome"] for a in r.attempts] == ["bad_response", "bad_response", "ok"]


def test_provisional_is_read_from_the_used_backends_calibration():
    """A provisional calibration on the used backend is surfaced in the result."""
    cal = jb.Calibration(a=1, b=0, provisional=True)
    r = judge.Chain([Fake("a", calibration=cal)]).judge(Q, ps(1))
    assert r.provisional
    assert not judge.Chain([Fake("a")]).judge(Q, ps(1)).provisional


# ---- the result's serialisation ----------------------------------------------------
def test_as_contract_matches_the_rag_io_validation_definition():
    """as_contract() validates against rag_io $defs/validation; as_dict() adds trace keys and is JSON."""
    import rag_io
    r = judge.Chain([Fake("a", fail="timeout"), Fake("b", scores={"p1": 0.1})],
                    requested="a,b").judge(Q, ps(3))
    c = r.as_contract()
    assert c == {"backend_requested": "a,b", "backend_used": "b", "pool_size": 3,
                 "passed": 2, "rejected": 1, "fell_back": True}
    assert rag_io.validate(c, "validation") == []
    d = r.as_dict()
    assert {"attempts", "truncated", "provisional", "judged"} <= set(d)
    import json
    json.dumps(d)


# ---- select() ----------------------------------------------------------------------
def V(value, score=None, backend="a"):
    """Shorthand verdict."""
    return jb.Verdict(value=value, score=score, why=None, backend=backend)


def test_select_orders_scored_passes_then_unscored_passes_then_unjudged_and_drops_rejects():
    """Scored passes by score, unscored passes in fused order, then unjudged; rejects dropped."""
    pairs = [("h0", V(True, 0.6)), ("h1", V(False, 0.2)), ("h2", V(True)), ("h3", V(True, 0.9)),
             ("h4", None), ("h5", V(True)), ("h6", None)]
    out = judge.select(pairs)
    assert [(i, k) for i, _, k in out] == [("h3", "passed"), ("h0", "passed"), ("h2", "passed"),
                                           ("h5", "passed"), ("h4", "unjudged"), ("h6", "unjudged")]


def test_select_floor_keeps_top_scoring_when_every_chunk_fails():
    """Every chunk judged and failed: the top-scoring `floor` come back flagged."""
    pairs = [("h0", V(False, 0.1)), ("h1", V(False, 0.4)), ("h2", V(False, 0.3))]
    out = judge.select(pairs, floor=2)
    assert [(i, k) for i, _, k in out] == [("h1", "floor"), ("h2", "floor")]


def test_select_no_floor_when_unjudged_items_survive():
    """Some judged chunks failed but an unjudged one survives: the bucket is not empty, so
    no rejected chunk is resurrected ahead of one nobody rejected (Sai's rule is 'when
    every chunk fails')."""
    pairs = [("h0", V(False, 0.1)), ("h1", V(False, 0.4)), ("h2", V(False, 0.3)), ("h3", None)]
    assert [(i, k) for i, _, k in judge.select(pairs, floor=2)] == [("h3", "unjudged")]


def test_select_uncalibrated_floor_ranks_by_raw_output():
    """No calibrated score: the floor is the top by the backend's raw output, not fused order."""
    raw = lambda x: jb.Verdict(False, None, None, "a", raw=x)
    pairs = [("h0", raw(-5.0)), ("h1", raw(-0.5)), ("h2", raw(-2.0))]
    assert [(i, k) for i, _, k in judge.select(pairs, floor=2)] == [("h1", "floor"), ("h2", "floor")]


def test_select_floor_uses_fused_order_without_scores():
    """An uncalibrated floor falls back to fused order; floor=0 allows an empty result."""
    pairs = [("h0", V(False)), ("h1", V(False)), ("h2", V(False))]
    assert [(i, k) for i, _, k in judge.select(pairs, floor=2)] == [("h0", "floor"), ("h1", "floor")]
    assert judge.select(pairs, floor=0) == []


def test_select_no_floor_when_something_passes_and_nothing_judged():
    """The floor applies only when something was judged and nothing passed."""
    pairs = [("h0", V(False, 0.1)), ("h1", V(True, 0.7))]
    assert [(i, k) for i, _, k in judge.select(pairs)] == [("h1", "passed")]
    assert [(i, k) for i, _, k in judge.select([("x", None), ("y", None)])] == \
        [("x", "unjudged"), ("y", "unjudged")]
    assert judge.select([]) == []


def test_select_ties_are_stable():
    """Equal scores keep fused order: select() is deterministic."""
    pairs = [(f"h{i}", V(True, 0.5)) for i in range(5)]
    assert [i for i, _, _ in judge.select(pairs)] == [f"h{i}" for i in range(5)]


def test_select_feeds_from_a_result():
    """aligned() pads truncated passages with None so select() marks them unjudged."""
    r = judge.Chain([Fake("a", capacity=2, scores={"p0": 0.2, "p1": 0.8})]).judge(Q, ps(3))
    hits = ["h0", "h1", "h2"]
    assert [(i, k) for i, _, k in judge.select(list(zip(hits, r.aligned())))] == \
        [("h1", "passed"), ("h2", "unjudged")]


def test_widened_pool_that_falls_through_to_a_smaller_backend_is_not_mostly_unjudged():
    """jev(50) looks live so the pool widens to 50, jev times out, local(20) judges p0..p19
    and rejects them all. Only the floor comes back: p20..p49 were fetched only for jev and
    nobody judged them, so they must not outlive the passages local looked at."""
    jev = Fake("jev", capacity=50, fail="timeout")
    local = Fake("local", capacity=20, scores={f"p{i}": 0.1 for i in range(50)})
    c = judge.Chain([jev, local])
    width = c.pool_width(default=12)
    assert width == 50
    r = c.judge(Q, ps(width), default_width=12)
    hits = [f"h{i}" for i in range(width)]
    assert [(i, k) for i, _, k in r.select(hits)] == [("h0", "floor"), ("h1", "floor")]
    # without the cap, 30 unjudged would survive (and so no floor): the defect guarded against
    assert len(judge.select(list(zip(hits, r.aligned())))) == 30


def test_widened_pool_with_nobody_answering_is_the_default_width_in_fused_order():
    """A lone dead backend of capacity 50: the result is exactly validation-off behaviour,
    the first `default` passages in fused order, not all 50 unjudged."""
    c = judge.Chain([Fake("jev", capacity=50, fail="http_error")])
    width = c.pool_width(default=12)
    r = c.judge(Q, ps(width), default_width=12)
    out = r.select([f"h{i}" for i in range(width)])
    assert [(i, k) for i, _, k in out] == [(f"h{i}", "unjudged") for i in range(12)]
    assert r.as_dict()["default_width"] == 12


def test_truncated_tail_longer_than_the_judged_prefix_is_cut_at_the_default_width():
    """A small backend (capacity 5) judges p0..p4 of a 50-wide pool. Unjudged p5..p11 are
    inside the default width and stay; p12+ are dropped whether or not anything passed."""
    hits = [f"h{i}" for i in range(50)]
    rej = judge.Chain([Fake("a", capacity=5, scores={f"p{i}": 0.1 for i in range(50)})])
    out = rej.judge(Q, ps(50), default_width=12).select(hits)
    assert [(i, k) for i, _, k in out] == [(f"h{i}", "unjudged") for i in range(5, 12)]
    ok = judge.Chain([Fake("a", capacity=5, scores={"p3": 0.8, "p0": 0.1})])
    out = ok.judge(Q, ps(50), default_width=12).select(hits)
    assert [i for i, _, k in out if k == "unjudged"] == [f"h{i}" for i in range(5, 12)]
    assert "h0" not in [i for i, _, _ in out]            # rejected, and no floor: something passed


def test_select_default_width_cap_and_argument_checks():
    """default_width bounds unjudged items by fused position only; judged items are never
    cut by it. Bad widths and a hits/pool mismatch are refused."""
    pairs = [("h0", None), ("h1", V(True, 0.9)), ("h2", None), ("h3", V(True, 0.8)), ("h4", None)]
    assert [(i, k) for i, _, k in judge.select(pairs, default_width=2)] == \
        [("h1", "passed"), ("h3", "passed"), ("h0", "unjudged")]
    assert [i for i, _, _ in judge.select(pairs, default_width=0)] == ["h1", "h3"]
    with pytest.raises(ValueError):
        judge.select(pairs, default_width=-1)
    with pytest.raises(ValueError):
        judge.Chain([Fake("a")]).judge(Q, ps(2), default_width=True)
    with pytest.raises(ValueError):
        judge.Chain([Fake("a")]).judge(Q, ps(2), default_width=2).select(["h0"])


def test_breaker_recovery_widens_again_but_a_repeat_failure_is_still_capped():
    """Once down_for_s passes pool_width reports the lead's capacity again; if the lead is
    still dead the widened pool is still cut back to the default width."""
    clock = FakeClock()
    c = judge.Chain([Fake("jev", capacity=50, fail="timeout"), Fake("local", capacity=20)],
                    down_for_s=60, clock=clock)
    for _ in range(2):
        width = c.pool_width(default=12)
        assert width == 50
        r = c.judge(Q, ps(width), default_width=12)
        out = r.select([f"h{i}" for i in range(width)])
        assert [k for _, _, k in out] == ["passed"] * 20 and r.backend_used == "local"
        assert c.pool_width(default=12) == 20
        clock.t += 61


# ---- environment -------------------------------------------------------------------
@pytest.mark.parametrize("env", [{}, {"RAG_VALIDATOR": ""}, {"RAG_VALIDATOR": "none"},
                                 {"RAG_VALIDATOR": " None "}])
def test_unset_or_none_is_an_empty_chain(env):
    """RAG_VALIDATOR unset, blank or none: validation off, default deadline."""
    c = judge.chain_from_env(env)
    assert c.empty and c.deadline_s == 3.0


@pytest.mark.parametrize("raw", ["jev,jev", "jev, nemotron ,JEV", "nosuch", "jev,none"])
def test_dupes_unknown_and_mixed_none_are_value_errors(raw):
    """Duplicates, unknown names and 'none' mixed with names are configuration errors."""
    with pytest.raises(ValueError):
        judge.chain_from_env({"RAG_VALIDATOR": raw})


def test_build_backend_unknown_name_lists_known():
    """The error for a typo lists the names that would have worked."""
    with pytest.raises(ValueError) as e:
        judge.build_backend("nosuch")
    assert "jev" in str(e.value) and "local" in str(e.value)


@pytest.fixture
def fake_registry(monkeypatch):
    """Register in-memory backend modules so chain_from_env can build them."""
    mod = types.ModuleType("judge_fake_mod")

    class Good(Fake):
        """A backend that builds."""
        def __init__(self, **kw):
            """Build as 'good'."""
            super().__init__("good", **kw)

    class Missing:
        """A backend whose key is absent."""
        def __init__(self, **kw):
            """Refuse to build, as a backend with no key must."""
            raise jb.BackendNotConfigured("set FAKE_API_KEY")

    class OwnDeadline(Fake):
        """A backend that declares its own long deadline, like judge_local's 8s."""
        def __init__(self, **kw):
            """Build as 'own' with deadline_s=8.0."""
            super().__init__("own", deadline_s=8.0, **kw)

    mod.Good, mod.Missing, mod.OwnDeadline = Good, Missing, OwnDeadline
    monkeypatch.setitem(sys.modules, "judge_fake_mod", mod)
    monkeypatch.setattr(judge, "REGISTRY", {"good": "judge_fake_mod:Good",
                                            "own": "judge_fake_mod:OwnDeadline",
                                            "missing": "judge_fake_mod:Missing",
                                            "noclass": "judge_fake_mod:Nope",
                                            "nomodule": "judge_no_such_module_xyz:X"})
    return mod


def test_env_builds_chain_in_order_with_deadline(fake_registry):
    """RAG_VALIDATOR and RAG_VALIDATOR_DEADLINE_S produce the chain they describe."""
    c = judge.chain_from_env({"RAG_VALIDATOR": "good", "RAG_VALIDATOR_DEADLINE_S": "0.5"})
    assert c.names == ["good"] and c.deadline_s == 0.5 and c.requested == "good"


def test_env_deadline_caps_a_backends_own_and_unset_leaves_it(fake_registry):
    """RAG_VALIDATOR_DEADLINE_S reaches a backend that declares its own deadline (as a cap);
    unset, the backend keeps its own and the chain records that nobody set one."""
    c = judge.chain_from_env({"RAG_VALIDATOR": "own", "RAG_VALIDATOR_DEADLINE_S": "1"})
    assert c.deadline_explicit and c._deadline_for(c._backends[0]) == 1.0
    c = judge.chain_from_env({"RAG_VALIDATOR": "own"})
    assert not c.deadline_explicit and c.deadline_s == 3.0
    assert c._deadline_for(c._backends[0]) == 8.0


def test_not_configured_is_a_hard_error_naming_rag_validator(fake_registry):
    """A requested backend that cannot build is a hard error pointing at RAG_VALIDATOR."""
    with pytest.raises(jb.BackendNotConfigured) as e:
        judge.chain_from_env({"RAG_VALIDATOR": "good,missing"})
    assert "RAG_VALIDATOR" in str(e.value) and "FAKE_API_KEY" in str(e.value)


def test_import_failure_is_not_configured_naming_the_module(fake_registry):
    """A missing module or class is BackendNotConfigured naming what is missing."""
    with pytest.raises(jb.BackendNotConfigured) as e:
        judge.build_backend("nomodule")
    assert "judge_no_such_module_xyz" in str(e.value)
    with pytest.raises(jb.BackendNotConfigured):
        judge.build_backend("noclass")


@pytest.mark.parametrize("bad", ["abc", "0", "-1"])
def test_bad_deadline_is_refused(bad):
    """A deadline that does not parse or is not positive is refused, not silently defaulted."""
    with pytest.raises(ValueError):
        judge.chain_from_env({"RAG_VALIDATOR_DEADLINE_S": bad})


def test_registry_names_the_agreed_classes():
    """The registry matches the names the backend modules were built against."""
    assert judge.REGISTRY == {"jev": "judge_jev:JevBackend",
                              "nemotron": "judge_nemotron:NemotronBackend",
                              "local": "judge_local:LocalCrossEncoderBackend",
                              "llm": "judge_llm:LLMJudgeBackend"}


# ---- concurrency -------------------------------------------------------------------
def test_concurrent_judge_calls_are_consistent():
    """Eight threads judging at once with a flaky lead: no escapes, every result self-consistent."""
    class Flaky(Fake):
        """Fails every third call."""
        def score(self, query, passages, *, deadline_s):
            """Fail on calls 3, 6, 9, ..., otherwise answer."""
            with self._lock:
                self.calls.append([p.id for p in passages])
                n = len(self.calls)
            if n % 3 == 0:
                raise jb.BackendUnavailable("flaky", kind="http_error")
            time.sleep(0.001)
            return [jb.Verdict(True, 0.9, None, self.name) for _ in passages]

    c = judge.Chain([Flaky("a"), Fake("b")], down_for_s=0.0)
    results, errors = [], []

    def worker():
        """Judge 25 times and keep every result, or the exception that escaped."""
        try:
            for _ in range(25):
                results.append(c.judge(Q, ps(4)))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and len(results) == 200
    for r in results:
        assert r.backend_used in ("a", "b") and len(r.verdicts) == 4
        assert all(v.backend == r.backend_used for v in r.verdicts)
        assert r.fell_back == (r.backend_used == "b")


def test_cli_check_reports_alive_and_ranking_without_network(monkeypatch, capsys):
    """check() reports configured/alive/ranking per backend and fails when there is no working fallback."""
    good = Fake("good", scores={"probe_relevant": 0.9, "probe_irrelevant": 0.1})
    built = {"good": good, "dead": Fake("dead", fail="http_error")}

    def fake_build(name, **kw):
        """Hand back a prepared fake, or refuse like an unconfigured backend."""
        if name not in built:
            raise jb.BackendNotConfigured("no key")
        return built[name]

    monkeypatch.setattr(judge, "build_backend", fake_build)
    rep = judge.check(["good", "dead", "absent"], deadline_s=1.0)
    rows = {r["backend"]: r for r in rep["backends"]}
    assert rows["good"]["alive"] and rows["good"]["relevant_higher"] is True
    assert not rows["dead"]["alive"] and rows["dead"]["outcome"] == "http_error"
    assert not rows["absent"]["configured"]
    assert rep["alive"] == 1 and not rep["ok"]               # >1 requested, <=1 alive
    monkeypatch.setattr(judge, "_load_env_file", lambda: None)
    assert judge.main(["check", "--backends", "jev,jev"]) == 2


def test_cli_check_deadline_overrides_a_backends_own_deadline(monkeypatch):
    """check(deadline_s=...) is a real cap: a backend declaring 8s that takes 0.5s is
    reported as a timeout under a 0.05s check deadline, not as 'ok'."""
    slow = Fake("local", sleep=0.5, deadline_s=8.0)
    monkeypatch.setattr(judge, "build_backend", lambda name, **kw: slow)
    monkeypatch.delenv("RAG_VALIDATOR_DEADLINE_S", raising=False)
    t0 = time.perf_counter()
    rep = judge.check(["local"], deadline_s=0.05, verbose=False)
    assert time.perf_counter() - t0 < 0.4
    assert rep["backends"][0]["outcome"] == "timeout" and not rep["backends"][0]["alive"]
    monkeypatch.setenv("RAG_VALIDATOR_DEADLINE_S", "0.05")
    assert judge.check(["local"], verbose=False)["backends"][0]["outcome"] == "timeout"


@pytest.mark.parametrize("bad", ["0", "-2", "abc"])
def test_cli_rejects_a_non_positive_deadline(monkeypatch, bad):
    """--deadline 0 is a usage error (exit 2), not every backend reported dead."""
    monkeypatch.setattr(judge, "_load_env_file", lambda: None)
    with pytest.raises(SystemExit) as e:
        judge.main(["check", "--backends", "jev", "--deadline", bad])
    assert e.value.code == 2
