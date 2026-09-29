"""serve.py picks the full engine on every path (audit C7/N10, Sai 2026-09-29): with no key
and Claude Code installed it runs the engine on the login, not the mock agent, and it reads
engine/.env before choosing. Run: cd engine/agent-server && python3 -m pytest -q test_serve.py
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("serve", ROOT / "serve.py")
serve = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(serve)


def test_no_key_with_claude_code_runs_the_engine_on_the_login():
    assert serve.choose({}, has_claude=True) == ("engine", "cli", "no Anthropic key, Claude Code installed")


def test_an_nvidia_key_alone_no_longer_decides_it_when_claude_code_is_there():
    """engine/.env holds NVIDIA_API_KEY; the chain is Claude-only, so the login is used."""
    assert serve.choose({"NVIDIA_API_KEY": "x"}, has_claude=True)[:2] == ("engine", "cli")


def test_an_anthropic_key_uses_auto_with_claude_code_and_api_without():
    assert serve.choose({"ANTHROPIC_API_KEY": "k"}, has_claude=True)[:2] == ("engine", "auto")
    assert serve.choose({"ANTHROPIC_API_KEY": "k"}, has_claude=False)[:2] == ("engine", "api")


def test_an_explicit_transport_wins_and_nothing_means_none():
    assert serve.choose({"BRIEF_CLAUDE_TRANSPORT": "cli", "ANTHROPIC_API_KEY": "k"}, True)[:2] == ("engine", None)
    assert serve.choose({"GROQ_API_KEY": "g"}, has_claude=False)[:2] == ("engine", None)
    assert serve.choose({}, has_claude=False) == ("none", None, "")


def test_main_reads_engine_env_before_choosing(monkeypatch, tmp_path):
    """A key kept only in engine/.env is seen (N10): main() loads it, then chooses."""
    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_API_KEY=from-dotenv\n")
    for k in ("ANTHROPIC_API_KEY", "BRIEF_CLAUDE_TRANSPORT"):
        monkeypatch.delenv(k, raising=False)
    sys.path.insert(0, str(ROOT / "engine"))
    import engine_env
    monkeypatch.setattr(engine_env, "ENV_PATH", env_file)
    monkeypatch.setattr(serve.shutil, "which", lambda name: "/usr/local/bin/claude")
    started = []
    monkeypatch.setattr(serve, "_run_engine", lambda transport, why: started.append((transport, why)) or 0)
    monkeypatch.setattr(serve, "_run_mock", lambda: started.append("mock") or 0)
    assert serve.main([]) == 0
    assert started == [("auto", "ANTHROPIC_API_KEY found")]
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
