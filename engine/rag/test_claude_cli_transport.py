"""BRIEF_CLAUDE_TRANSPORT=cli: every `anthropic:` link goes through `claude -p` (the logged-in
Claude Code account) instead of the API key. Offline: subprocess.run is faked, so these
tests pin the command line, the parity settings with the API path, and the failure
mapping that lets the model chain fail over.
Run: cd engine/rag && python3 -m pytest -q test_claude_cli_transport.py
"""
from __future__ import annotations
import json
import subprocess
import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import pytest  # noqa: E402
import parse_brief as pb  # noqa: E402


class _Proc:
    """A finished subprocess, shaped like subprocess.CompletedProcess."""
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


def _envelope(result="", structured=None, **extra):
    """The JSON the CLI prints with --output-format json on success."""
    d = {"type": "result", "subtype": "success", "is_error": False, "result": result,
         "stop_reason": "end_turn",
         "usage": {"input_tokens": 120, "cache_read_input_tokens": 30,
                   "cache_creation_input_tokens": 0, "output_tokens": 12}}
    if structured is not None:
        d["structured_output"] = structured
    d.update(extra)
    return json.dumps(d)


@pytest.fixture
def cli(monkeypatch):
    """Route anthropic links to the CLI, pretend `claude` is installed, record each call."""
    calls = []
    replies = []
    monkeypatch.setenv("BRIEF_CLAUDE_TRANSPORT", "cli")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-reach-the-cli")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/claude")

    def fake_run(cmd, input=None, capture_output=None, text=None, timeout=None, env=None, cwd=None):
        """Record the call and return the next queued reply."""
        calls.append({"cmd": cmd, "input": input, "env": env, "timeout": timeout})
        return replies.pop(0) if replies else _Proc(_envelope('{"ok": true}'))

    monkeypatch.setattr(subprocess, "run", fake_run)
    pb._stats_reset()
    return calls, replies


def _flag(cmd, name):
    """The value that follows `name` on a command line, or None."""
    return cmd[cmd.index(name) + 1] if name in cmd else None


def test_transport_switch_defaults_to_api(monkeypatch):
    """Unset or anything but 'cli' keeps the API path, so nothing changes by default."""
    monkeypatch.delenv("BRIEF_CLAUDE_TRANSPORT", raising=False)
    assert pb._claude_transport() == "api"
    monkeypatch.setenv("BRIEF_CLAUDE_TRANSPORT", " CLI ")
    assert pb._claude_transport() == "cli"


def test_api_path_is_untouched_when_transport_is_api(monkeypatch):
    """With the default transport, an anthropic link still calls _chat_anthropic."""
    monkeypatch.delenv("BRIEF_CLAUDE_TRANSPORT", raising=False)
    seen = []
    monkeypatch.setattr(pb, "_chat_anthropic", lambda *a, **k: seen.append(k) or "api")
    monkeypatch.setattr(pb, "_chat_claude_cli", lambda *a, **k: pytest.fail("CLI used on the API transport"))
    assert pb._call_link("anthropic", "claude-opus-4-6", "hi") == "api"
    assert seen and seen[0]["model"] == "claude-opus-4-6"


def test_command_line_is_one_turn_no_tools_no_settings_prompt_on_stdin(cli):
    """The CLI runs one tool-free turn with our system prompt, no session file, no user or
    project settings and no MCP servers; the prompt goes on stdin, not argv."""
    calls, _ = cli
    out = pb._call_link("anthropic", "claude-opus-4-6", "USER PROMPT", system="SYS", max_tokens=500)
    assert out == '{"ok": true}'
    cmd = calls[0]["cmd"]
    assert cmd[:2] == ["claude", "-p"]
    assert _flag(cmd, "--model") == "claude-opus-4-6"
    assert _flag(cmd, "--system-prompt") == "SYS"
    assert _flag(cmd, "--tools") == "" and _flag(cmd, "--max-turns") == "1"
    assert _flag(cmd, "--output-format") == "json" and _flag(cmd, "--setting-sources") == ""
    assert "--no-session-persistence" in cmd and "--strict-mcp-config" in cmd
    assert calls[0]["input"] == "USER PROMPT" and "USER PROMPT" not in cmd


def test_api_key_is_not_inherited_by_the_cli(cli):
    """With a key in the env the CLI would bill that key (possibly out of credit) instead
    of the logged-in account, so the key is removed from the child's environment."""
    calls, _ = cli
    pb._call_link("anthropic", "claude-opus-4-6", "x")
    assert "ANTHROPIC_API_KEY" not in calls[0]["env"]


def test_non_thinking_model_runs_with_thinking_off_and_the_callers_cap(cli):
    """Opus 4.6 does not think on the API path, so the CLI is told not to either, and the
    output cap is the caller's max_tokens (no thinking headroom)."""
    calls, _ = cli
    pb._call_link("anthropic", "claude-opus-4-6", "x", max_tokens=500)
    env, cmd = calls[0]["env"], calls[0]["cmd"]
    assert env["MAX_THINKING_TOKENS"] == "0"
    assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "500"
    assert "--effort" not in cmd


def test_thinking_model_gets_api_default_effort_and_headroom(cli):
    """Opus 5.5 thinks by default: effort is the API default (medium), thinking stays on,
    and the cap carries the same headroom as the API path."""
    calls, _ = cli
    pb._call_link("anthropic", "claude-opus-5-5", "x", max_tokens=500)
    env, cmd = calls[0]["env"], calls[0]["cmd"]
    assert _flag(cmd, "--effort") == "medium"
    assert "MAX_THINKING_TOKENS" not in env or env["MAX_THINKING_TOKENS"] != "0"
    assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == str(500 + pb.THINKING_HEADROOM)
    pb._call_link("anthropic", "claude-sonnet-5", "x", max_tokens=500)
    assert _flag(calls[1]["cmd"], "--effort") == "high"


def test_schema_goes_to_json_schema_and_structured_output_is_returned(cli):
    """A schema call passes --json-schema and returns the CLI's structured output as JSON."""
    calls, replies = cli
    schema = {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}
    replies.append(_Proc(_envelope(result="prose the model wrote", structured={"a": 3})))
    out = pb._call_link("anthropic", "claude-sonnet-5", "x", json_mode=True, schema=schema)
    assert json.loads(_flag(calls[0]["cmd"], "--json-schema")) == schema
    assert json.loads(out) == {"a": 3}


def test_usage_is_recorded_under_the_anthropic_label(cli):
    """Tokens land in the ledger under anthropic:<model>; cache reads and writes are recorded
    apart from the uncached input, because they are priced apart (audit BW13)."""
    pb._call_link("anthropic", "claude-opus-4-6", "x")
    assert pb._LLM_STATS["by_provider"] == {"anthropic:claude-opus-4-6": 1}
    assert pb._LLM_STATS["prompt_tokens"] == 120 and pb._LLM_STATS["completion_tokens"] == 12
    assert pb._LLM_STATS["cache_read_tokens"] == 30 and pb._LLM_STATS["cache_creation_tokens"] == 0


def test_cli_reply_cut_off_at_the_cap_is_not_returned(cli):
    """stop_reason max_tokens raises _Truncated (never half-read); a refusal raises _Refused."""
    _, replies = cli
    replies.append(_Proc(_envelope('{"partial": ', stop_reason="max_tokens")))
    with pytest.raises(pb._Truncated):
        pb._call_link("anthropic", "claude-opus-4-6", "x", max_tokens=500)
    replies.append(_Proc(_envelope("", stop_reason="refusal")))
    with pytest.raises(pb._Refused):
        pb._call_link("anthropic", "claude-opus-4-6", "x")


def test_auto_switch_expires_after_its_ttl(monkeypatch):
    """A switch older than BRIEF_CLI_FALLBACK_TTL is over: the next call re-tries the API
    (before 2026-09-25 one auth hiccup moved every later brief to the CLI until restart)."""
    import time
    monkeypatch.setenv("BRIEF_CLAUDE_TRANSPORT", "auto")
    monkeypatch.setattr(pb, "_CLI_FALLBACK", {"on": True, "since": time.monotonic() - pb.CLI_FALLBACK_TTL_S - 1})
    assert pb._cli_fallback_active() is False and pb.transport_used() == "api"
    monkeypatch.setattr(pb, "_CLI_FALLBACK", {"on": True, "since": time.monotonic()})
    assert pb._cli_fallback_active() is True and pb.transport_used() == "api→cli"


def test_error_envelope_raises_so_the_chain_fails_over(cli):
    """A failed CLI call (non-zero exit or is_error) raises instead of returning text."""
    _, replies = cli
    replies.append(_Proc(_envelope(result="Credit balance is too low", is_error=True), returncode=1))
    with pytest.raises(RuntimeError):
        pb._call_link("anthropic", "claude-opus-4-6", "x")


def test_usage_limit_raises_rate_limited(cli):
    """The account's usage limit maps to _RateLimited, so the link cools down."""
    _, replies = cli
    replies.append(_Proc(_envelope(result="Claude AI usage limit reached", is_error=True), returncode=1))
    with pytest.raises(pb._RateLimited):
        pb._call_link("anthropic", "claude-opus-4-6", "x")


def test_timeout_raises_runtime_error(cli, monkeypatch):
    """A hung CLI is a failed link, not a hung brief."""
    def hang(*a, **k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)
    monkeypatch.setattr(subprocess, "run", hang)
    with pytest.raises(RuntimeError, match="timed out"):
        pb._call_link("anthropic", "claude-opus-4-6", "x")


def test_missing_cli_raises(cli, monkeypatch):
    """No `claude` on PATH is a clear failure, not a silent empty reply."""
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(RuntimeError, match="not on PATH"):
        pb._call_link("anthropic", "claude-opus-4-6", "x")


def test_json_call_fails_over_from_a_failing_cli_link(cli, monkeypatch):
    """End to end through _json_call: a failing first link hands over to the next one."""
    _, replies = cli
    monkeypatch.setenv("BRIEF_MODEL_CHAIN", "anthropic:claude-opus-5-5,anthropic:claude-opus-4-6")
    monkeypatch.delenv("BRIEF_PROVIDER", raising=False)
    replies.append(_Proc("", "boom", returncode=1))
    replies.append(_Proc(_envelope('{"answer": 1}')))
    assert pb._json_call("q", system="JSON only.") == {"answer": 1}


# ---------- auto: API first, Claude Code CLI when the key cannot be used ----------

class BadRequestError(Exception):
    """Named like the SDK's class; _api_account_failure matches on the class name."""


class RateLimitError(Exception):
    """A transient API error: auto must NOT switch transports on this."""


@pytest.fixture
def auto(monkeypatch):
    """Transport `auto` with a key set, a fake API link and a recording CLI link."""
    monkeypatch.setenv("BRIEF_CLAUDE_TRANSPORT", "auto")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(pb, "_CLI_FALLBACK", {"on": False})
    cli_calls, api_calls = [], []
    monkeypatch.setattr(pb, "_chat_claude_cli", lambda user, **k: cli_calls.append(user) or "from-cli")
    return cli_calls, api_calls, monkeypatch


def test_typo_in_transport_reads_as_api(monkeypatch):
    """A misspelt value never silently moves traffic off the API."""
    monkeypatch.setenv("BRIEF_CLAUDE_TRANSPORT", "clii")
    assert pb._claude_transport() == "api"


def test_auto_uses_the_api_while_it_works(auto):
    """With a working key, auto is exactly the API path."""
    cli_calls, api_calls, mp = auto
    mp.setattr(pb, "_chat_anthropic", lambda user, **k: api_calls.append(user) or "from-api")
    assert pb._call_link("anthropic", "claude-opus-4-6", "q") == "from-api"
    assert not cli_calls and pb.transport_used() == "api"


def test_auto_switches_on_no_credit_and_stays_switched(auto):
    """'credit balance is too low' moves this call and every later one to the CLI."""
    cli_calls, api_calls, mp = auto

    def no_credit(user, **k):
        api_calls.append(user)
        raise BadRequestError("Your credit balance is too low to access the Anthropic API.")
    mp.setattr(pb, "_chat_anthropic", no_credit)
    assert pb._call_link("anthropic", "claude-opus-4-6", "q1") == "from-cli"
    assert pb._call_link("anthropic", "claude-opus-4-6", "q2") == "from-cli"
    assert api_calls == ["q1"] and cli_calls == ["q1", "q2"]
    assert pb.transport_used() == "api→cli"


def test_auto_does_not_switch_on_a_transient_error(auto):
    """A rate limit or other request error still fails the link, so the chain handles it."""
    cli_calls, _, mp = auto

    def limited(user, **k):
        raise RateLimitError("429")
    mp.setattr(pb, "_chat_anthropic", limited)
    with pytest.raises(RateLimitError):
        pb._call_link("anthropic", "claude-opus-4-6", "q")
    assert not cli_calls and pb.transport_used() == "api"


def test_auto_without_a_key_goes_straight_to_the_cli(auto):
    """No ANTHROPIC_API_KEY but `claude` installed: auto starts on the CLI."""
    cli_calls, _, mp = auto
    mp.delenv("ANTHROPIC_API_KEY")
    mp.setattr("shutil.which", lambda name: "/usr/local/bin/claude")
    mp.setattr(pb, "_chat_anthropic", lambda *a, **k: pytest.fail("API used without a key"))
    assert pb._call_link("anthropic", "claude-opus-4-6", "q") == "from-cli"
    assert pb.transport_used() == "api→cli"


def test_explicit_api_never_falls_back(monkeypatch):
    """transport=api keeps today's behaviour: a credit failure fails the link."""
    monkeypatch.setenv("BRIEF_CLAUDE_TRANSPORT", "api")
    monkeypatch.setattr(pb, "_CLI_FALLBACK", {"on": False})

    def no_credit(user, **k):
        raise BadRequestError("Your credit balance is too low")
    monkeypatch.setattr(pb, "_chat_anthropic", no_credit)
    monkeypatch.setattr(pb, "_chat_claude_cli", lambda *a, **k: pytest.fail("CLI used on transport=api"))
    with pytest.raises(BadRequestError):
        pb._call_link("anthropic", "claude-opus-4-6", "q")
