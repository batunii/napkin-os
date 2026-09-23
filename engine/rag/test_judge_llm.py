"""The LLM relevance judge (judge_llm.LLMJudgeBackend).
Run: cd engine/rag && RAG_STORE=local RAG_INDEX=./_index_v3 python3 -m pytest test_judge_llm.py -q

No network and no real model: every test puts a fake `parse_brief` module into
sys.modules, so the backend's lazy import picks it up instead of the real engine.
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
import judge_base as jb  # noqa: E402
import judge_llm as jl  # noqa: E402

Q = jb.Query(text="challenger launch for a hybrid car", context="brief gist: urban 30-45")
PASSAGES = [jb.Passage(id=f"c{i}", text=f"passage number {i} about cars") for i in range(3)]


def _answer(*rows):
    """A well-formed model answer from (index, relevant, why) tuples."""
    return {"verdicts": [{"index": i, "relevant": r, "why": w} for i, r, w in rows]}


GOOD = _answer((2, False, "about pricing"), (0, True, "launch precedent"), (1, True, "same audience"))


def _fake_pb(answer=None, *, provider="groq", chain=(("groq", "llama-3.3-70b-versatile"),),
             json_call=None, key_env=None, calls=None):
    """A stand-in parse_brief module. `answer` is what _json_call returns (after
    offering it to `accept`, as the real one does); `calls` collects each call's args."""
    pb = types.ModuleType("parse_brief")
    pb.MAXTOK_JUDGE = 500
    pb.resolve_provider = lambda: provider
    pb._model_chain = lambda model=None: list(chain)
    if key_env is not None:
        pb._KEY_ENV = key_env

    def _json_call(user, system=None, retries=1, model=None, accept=None, max_tokens=None,
                   schema=None):
        """Record the call, then return `answer` if `accept` takes it, else None."""
        if calls is not None:
            calls.append(dict(user=user, system=system, retries=retries, model=model,
                              accept=accept, max_tokens=max_tokens, schema=schema))
        if answer is None:
            return None
        return answer if accept is None or accept(answer) else None

    pb._json_call = json_call or _json_call
    return pb


@pytest.fixture
def install(monkeypatch):
    """Install a fake parse_brief into sys.modules for the duration of one test."""
    def _install(pb):
        """Put `pb` where importlib will find it and return it."""
        monkeypatch.setitem(sys.modules, "parse_brief", pb)
        return pb
    return _install


# ---- protocol and configuration -------------------------------------------------
def test_satisfies_the_backend_protocol(install):
    """name 'llm', capacity 8, uncalibrated, and a Backend by the runtime protocol."""
    install(_fake_pb(GOOD))
    b = jl.LLMJudgeBackend()
    assert isinstance(b, jb.Backend)
    assert (b.name, b.capacity, b.calibrated) == ("llm", 8, False)
    assert b.chain == ["groq:llama-3.3-70b-versatile"]


def test_import_failure_is_not_configured(monkeypatch):
    """parse_brief that cannot be imported is a configuration error at construction."""
    monkeypatch.setitem(sys.modules, "parse_brief", None)     # import raises ImportError
    with pytest.raises(jb.BackendNotConfigured, match="cannot import parse_brief"):
        jl.LLMJudgeBackend()


def test_no_provider_is_not_configured(install):
    """No provider and an empty chain: BackendNotConfigured naming what to set."""
    install(_fake_pb(GOOD, provider="", chain=()))
    with pytest.raises(jb.BackendNotConfigured, match="set a provider key"):
        jl.LLMJudgeBackend()


def test_provider_without_a_chain_link_is_not_configured(install):
    """ANTHROPIC_API_KEY alone: resolve_provider() answers but the chain is empty, so
    every call would return None. Caught at construction, not at the first brief."""
    install(_fake_pb(GOOD, provider="anthropic", chain=()))
    with pytest.raises(jb.BackendNotConfigured, match="no chain link uses it"):
        jl.LLMJudgeBackend()


def test_pinned_link_without_its_key_is_not_configured(install, monkeypatch):
    """A pinned link survives _model_chain without its key; the per-link key check drops
    it, so a pin alone does not pass for configuration."""
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    install(_fake_pb(GOOD, key_env={"groq": "GROQ_API_KEY", "ollama": None}))
    with pytest.raises(jb.BackendNotConfigured):
        jl.LLMJudgeBackend()
    monkeypatch.setenv("GROQ_API_KEY", "k")
    assert jl.LLMJudgeBackend().chain == ["groq:llama-3.3-70b-versatile"]


# What the real parse_brief exposes: PROVIDERS (what _call_link dispatches, besides
# "anthropic") and _KEY_ENV (which key each needs).
REAL_PROVIDERS = {"groq": ("u", "GROQ_API_KEY", "m"), "cerebras": ("u", "CEREBRAS_API_KEY", "m"),
                  "ollama": ("u", None, "m")}
REAL_KEY_ENV = {"groq": "GROQ_API_KEY", "cerebras": "CEREBRAS_API_KEY",
                "anthropic": "ANTHROPIC_API_KEY", "ollama": None}


def _real_tables(pb):
    """Give a fake parse_brief the provider tables the real one has."""
    pb.PROVIDERS = REAL_PROVIDERS
    pb._KEY_ENV = REAL_KEY_ENV
    return pb


def test_pinned_model_with_no_keys_is_not_configured(install, monkeypatch):
    """model='claude-sonnet-4-5' with no key set: _model_chain places it on provider ''
    (resolve_provider() finds nothing), which _call_link can never serve. That is
    BackendNotConfigured at construction, not an 'error' on every score()."""
    for k in ("GROQ_API_KEY", "CEREBRAS_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    install(_real_tables(_fake_pb(GOOD, provider="", chain=(("", "claude-sonnet-4-5"),))))
    with pytest.raises(jb.BackendNotConfigured, match="no provider"):
        jl.LLMJudgeBackend(model="claude-sonnet-4-5")


def test_mistyped_chain_provider_is_not_configured(install, monkeypatch):
    """BRIEF_MODEL_CHAIN='cerebras:gpt-oss-120b,frobnicate:x' with no keys: cerebras is
    dropped for its key, frobnicate because _call_link does not know it."""
    monkeypatch.delenv("CEREBRAS_API_KEY", raising=False)
    install(_real_tables(_fake_pb(GOOD, provider="", chain=(("cerebras", "gpt-oss-120b"),
                                                           ("frobnicate", "x")))))
    with pytest.raises(jb.BackendNotConfigured) as e:
        jl.LLMJudgeBackend()
    assert "unknown provider 'frobnicate'" in str(e.value)
    assert "CEREBRAS_API_KEY not set" in str(e.value)


def test_unknown_provider_is_dropped_but_a_good_link_is_kept(install, monkeypatch):
    """A typo does not sink a chain that also has a servable link; it is just not listed."""
    monkeypatch.setenv("GROQ_API_KEY", "k")
    install(_real_tables(_fake_pb(GOOD, chain=(("frobnicate", "x"), ("groq", "llama")))))
    assert jl.LLMJudgeBackend().chain == ["groq:llama"]


def test_anthropic_is_servable_with_its_key(install, monkeypatch):
    """anthropic is not in PROVIDERS (_call_link dispatches it by name) but is served."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    install(_real_tables(_fake_pb(GOOD, provider="anthropic",
                                  chain=(("anthropic", "claude-opus-4-6"),))))
    assert jl.LLMJudgeBackend().chain == ["anthropic:claude-opus-4-6"]
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    with pytest.raises(jb.BackendNotConfigured, match="ANTHROPIC_API_KEY not set"):
        jl.LLMJudgeBackend()


def test_key_falls_back_to_the_providers_table(install, monkeypatch):
    """Without _KEY_ENV, the key variable is read from PROVIDERS' own entry."""
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    pb = _fake_pb(GOOD)
    pb.PROVIDERS = REAL_PROVIDERS
    install(pb)
    with pytest.raises(jb.BackendNotConfigured, match="GROQ_API_KEY not set"):
        jl.LLMJudgeBackend()


def test_empty_provider_is_unservable_even_without_tables(install):
    """With no provider tables to check against, a named provider gets the benefit of
    the doubt, but an empty one never does."""
    install(_fake_pb(GOOD, provider="", chain=(("", "some-local-model"),)))
    with pytest.raises(jb.BackendNotConfigured, match="no provider"):
        jl.LLMJudgeBackend(model="some-local-model")


def test_keyless_provider_counts_as_configured(install):
    """ollama needs no key, so a chain of only ollama is configured."""
    install(_fake_pb(GOOD, chain=(("ollama", "llama3.1"),), key_env={"ollama": None}))
    assert jl.LLMJudgeBackend().chain == ["ollama:llama3.1"]


def test_missing_json_call_is_not_configured(install):
    """If the engine's entry point moves, the judge refuses to start."""
    pb = _fake_pb(GOOD)
    del pb._json_call
    install(pb)
    with pytest.raises(jb.BackendNotConfigured, match="_json_call"):
        jl.LLMJudgeBackend()


def test_fallback_to_resolve_provider_when_no_model_chain(install):
    """A parse_brief without _model_chain is judged configured by resolve_provider()."""
    pb = _fake_pb(GOOD)
    del pb._model_chain
    install(pb)
    assert jl.LLMJudgeBackend().chain == ["groq:default"]


# ---- the happy path -------------------------------------------------------------
def test_verdicts_come_back_in_input_order(install):
    """The model may list verdicts in any order; output follows the input passages."""
    install(_fake_pb(GOOD))
    out = jl.LLMJudgeBackend().score(Q, PASSAGES, deadline_s=5)
    assert [v.value for v in out] == [True, True, False]
    assert [v.why for v in out] == ["launch precedent", "same audience", "about pricing"]


def test_a_generative_backend_explains_and_does_not_score(install):
    """score and raw are None, why is filled, backend is 'llm' on every verdict."""
    install(_fake_pb(GOOD))
    for v in jl.LLMJudgeBackend().score(Q, PASSAGES, deadline_s=5):
        assert v.score is None and v.raw is None and v.backend == "llm"
        assert v.why and not (v.score is not None and v.why is not None)


def test_empty_why_becomes_none(install):
    """A blank explanation is recorded as no explanation rather than an empty string."""
    install(_fake_pb(_answer((0, True, "  "), (1, False, "x"), (2, True, ""))))
    out = jl.LLMJudgeBackend().score(Q, PASSAGES, deadline_s=5)
    assert [v.why for v in out] == [None, "x", None]


def test_schema_system_and_model_are_threaded_through(install):
    """One call, with the response schema, the judge system prompt, the pinned model,
    retries=0 and an accept hook."""
    calls: list = []
    install(_fake_pb(GOOD, calls=calls))
    jl.LLMJudgeBackend(model="openai/gpt-oss-120b").score(Q, PASSAGES, deadline_s=5)
    assert len(calls) == 1
    c = calls[0]
    assert c["schema"] is jl.RESPONSE_SCHEMA and c["system"] is jl.SYSTEM
    assert c["model"] == "openai/gpt-oss-120b" and c["retries"] == 0
    assert callable(c["accept"])


def test_response_schema_is_strict_mode_compatible():
    """Strict structured outputs need every property required and none additional."""
    s = jl.RESPONSE_SCHEMA
    item = s["properties"]["verdicts"]["items"]
    assert s["additionalProperties"] is False and s["required"] == ["verdicts"]
    assert item["additionalProperties"] is False
    assert sorted(item["required"]) == sorted(item["properties"]) == ["index", "relevant", "why"]


def test_prompt_shows_cite_ids_context_and_clipped_passages(install):
    """Passages are numbered from 0, carry their cite id, are whitespace-collapsed and
    clipped to clip_chars; the brief context is included."""
    calls: list = []
    install(_fake_pb(_answer((0, True, "a"), (1, False, "b")), calls=calls))
    long = jb.Passage(id="ipa-7", text="word " * 400)
    padded = jb.Passage(id="aaker-4", text="| a" + " " * 5000 + "| b")
    jl.LLMJudgeBackend().score(Q, [long, padded], deadline_s=5)
    user = calls[0]["user"]
    assert "brief gist: urban 30-45" in user and "challenger launch" in user
    assert "[0] (cite ipa-7) " in user and "[1] (cite aaker-4) | a | b" in user
    line0 = next(line for line in user.splitlines() if line.startswith("[0]"))
    assert len(line0) <= len("[0] (cite ipa-7) ") + jl.CLIP_CHARS + 2 and line0.endswith("…")


def test_output_budget_grows_with_passages(install):
    """Eight explanations need more than the 500-token judge budget; one does not."""
    install(_fake_pb(GOOD))
    b = jl.LLMJudgeBackend()
    assert b.max_tokens(1) == 500 and b.max_tokens(8) > 500


def test_no_passages_means_no_call(install):
    """An empty list returns [] without spending a model call."""
    calls: list = []
    install(_fake_pb(GOOD, calls=calls))
    assert jl.LLMJudgeBackend().score(Q, [], deadline_s=5) == []
    assert calls == []


# ---- misaligned answers are never repaired --------------------------------------
@pytest.mark.parametrize("answer", [
    _answer((0, True, "a"), (1, True, "b")),                               # missing index 2
    _answer((0, True, "a"), (1, True, "b"), (1, False, "c")),              # duplicate
    _answer((0, True, "a"), (1, True, "b"), (3, False, "c")),              # out of range
    _answer((0, True, "a"), (1, True, "b"), (-1, False, "c")),             # negative
    _answer((0, True, "a"), (1, True, "b"), (2, False, "c"), (3, True, "d")),  # extra
    _answer((0, True, "a"), (1, True, "b"), (True, False, "c")),           # bool index
    _answer((0, True, "a"), (1, True, "b"), ("2", False, "c")),            # string index
    _answer((0, True, "a"), (1, True, "b"), (2, "yes", "c")),              # non-bool relevant
    _answer((0, True, "a"), (1, True, "b"), (2, False, 7)),                # non-string why
    {"verdicts": "none"},
    {"ranking": [0, 1, 2]},
    [0, 1, 2],
])
def test_misaligned_answer_is_bad_response(install, answer):
    """Anything but exactly one well-typed verdict per index is BackendUnavailable
    (bad_response), whether or not _json_call applied the accept hook."""
    install(_fake_pb(answer))
    with pytest.raises(jb.BackendUnavailable) as e:
        jl.LLMJudgeBackend().score(Q, PASSAGES, deadline_s=5)
    assert e.value.kind == "bad_response"

    def ignores_accept(user, **kw):
        """An older _json_call that returns the object without consulting accept."""
        return answer
    install(_fake_pb(json_call=ignores_accept))
    with pytest.raises(jb.BackendUnavailable) as e:
        jl.LLMJudgeBackend().score(Q, PASSAGES, deadline_s=5)
    assert e.value.kind == "bad_response"


def test_accept_hook_lets_the_chain_move_past_a_bad_link(install):
    """A misaligned first answer is rejected by accept, so the (simulated) chain goes
    on to the next link and its good answer is used."""
    def two_links(user, accept=None, **kw):
        """Offer a bad answer, then a good one, as two chain links would."""
        for obj in (_answer((0, True, "a")), GOOD):
            if accept(obj):
                return obj
        return None
    install(_fake_pb(json_call=two_links))
    assert [v.value for v in jl.LLMJudgeBackend().score(Q, PASSAGES, deadline_s=5)] == [True, True, False]


def test_exhausted_chain_with_no_json_is_error(install):
    """No link answered at all (network, keys, 429s): kind 'error', not bad_response."""
    install(_fake_pb(None))
    with pytest.raises(jb.BackendUnavailable) as e:
        jl.LLMJudgeBackend().score(Q, PASSAGES, deadline_s=5)
    assert e.value.kind == "error"


def test_exception_inside_the_call_is_error(install):
    """An exception escaping _json_call becomes BackendUnavailable(kind='error')."""
    def boom(user, **kw):
        """Fail the way a broken engine import-time state might."""
        raise KeyError("ANTHROPIC_API_KEY")
    install(_fake_pb(json_call=boom))
    with pytest.raises(jb.BackendUnavailable) as e:
        jl.LLMJudgeBackend().score(Q, PASSAGES, deadline_s=5)
    assert e.value.kind == "error" and "KeyError" in str(e.value)


# ---- the deadline ---------------------------------------------------------------
def test_a_hung_call_times_out_within_the_deadline(install):
    """A call that does not return is abandoned at deadline_s with kind 'timeout';
    score() itself returns promptly even though the worker is still running."""
    release = threading.Event()

    def hangs(user, **kw):
        """Block until the test releases it (never within the deadline)."""
        release.wait(10)
        return GOOD
    install(_fake_pb(json_call=hangs))
    b = jl.LLMJudgeBackend()
    t0 = time.monotonic()
    try:
        with pytest.raises(jb.BackendUnavailable) as e:
            b.score(Q, PASSAGES, deadline_s=0.05)
        assert e.value.kind == "timeout"
        assert time.monotonic() - t0 < 1.0
        workers = [t for t in threading.enumerate() if t.name == "judge-llm"]
        assert workers and all(t.daemon for t in workers)   # never holds the process open
    finally:
        release.set()


def test_a_spent_deadline_does_not_call(install):
    """deadline_s <= 0 is an immediate timeout and no model call is made."""
    calls: list = []
    install(_fake_pb(GOOD, calls=calls))
    with pytest.raises(jb.BackendUnavailable) as e:
        jl.LLMJudgeBackend().score(Q, PASSAGES, deadline_s=0)
    assert e.value.kind == "timeout" and calls == []


def test_a_timeout_raised_inside_the_call_is_not_mistaken_for_the_deadline(install):
    """A TimeoutError from inside the worker is the call failing (kind 'error'), not
    score() running out of time."""
    def socket_timeout(user, **kw):
        """Raise the builtin TimeoutError, as a socket read would."""
        raise TimeoutError("read timed out")
    install(_fake_pb(json_call=socket_timeout))
    with pytest.raises(jb.BackendUnavailable) as e:
        jl.LLMJudgeBackend().score(Q, PASSAGES, deadline_s=5)
    assert e.value.kind == "error"


def test_the_backend_declares_its_own_deadline(install):
    """deadline_s defaults to DEFAULT_DEADLINE_S (30 s), not the chain's 3 s reranker
    default, and the chain uses it; an explicit chain deadline still caps it."""
    import judge
    install(_fake_pb(GOOD))
    b = jl.LLMJudgeBackend()
    assert b.deadline_s == jl.DEFAULT_DEADLINE_S == 30.0
    assert judge.Chain([b])._deadline_for(b) == 30.0
    assert judge.Chain([b], deadline_s=5)._deadline_for(b) == 5.0
    own = jl.LLMJudgeBackend(deadline_s=12)
    assert judge.Chain([own])._deadline_for(own) == 12.0


@pytest.mark.parametrize("bad", [0, -1, True, "30", None])
def test_a_bad_deadline_is_not_configured(install, bad):
    """A deadline that is not a positive number is a configuration error."""
    install(_fake_pb(GOOD))
    with pytest.raises(jb.BackendNotConfigured, match="deadline_s"):
        jl.LLMJudgeBackend(deadline_s=bad)


def test_a_slow_answer_inside_its_own_deadline_is_used_by_the_chain(install):
    """A call slower than the chain's 3 s default but within the backend's own deadline
    is answered, not timed out. Uses a scaled-down chain default so the test is fast:
    the chain default is 0.05 s, the call takes 0.2 s, the backend allows 2 s."""
    import judge

    def slow(user, accept=None, **kw):
        """Answer well, but slower than the chain default."""
        time.sleep(0.2)
        return GOOD
    install(_fake_pb(json_call=slow))
    b = jl.LLMJudgeBackend(deadline_s=2)
    orig = judge.DEFAULT_DEADLINE_S
    judge.DEFAULT_DEADLINE_S = 0.05
    try:
        chain = judge.Chain([b])
    finally:
        judge.DEFAULT_DEADLINE_S = orig
    assert chain.deadline_s == 0.05
    res = chain.judge(Q, PASSAGES, default_width=3)
    assert res.backend_used == "llm"


# ---- the lazy import ------------------------------------------------------------
def test_importing_judge_llm_does_not_import_parse_brief():
    """parse_brief is heavy and reads engine/.env; merely importing this module must not
    load it (only constructing the backend does)."""
    import subprocess
    code = ("import sys; sys.path.insert(0, %r); import judge_llm; "
            "print('parse_brief' in sys.modules)") % str(HERE)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False"
