"""Shared test setup for the engine's rag/ suite.

Every test here is offline: model calls are faked at _json_call / _call_link / subprocess
level. Since 2026-09-25 parse_brief.run() refuses to start when the chain is Claude-only
and there is no route to Claude (no key on transport api, no `claude` on transport cli),
so a developer whose engine/.env pins BRIEF_PROVIDER=anthropic without a key would see
every run() test fail with NoClaudeAvailable. The placeholder key below satisfies that
check; no test sends it anywhere (a test that needs the API path fakes the client).
"""
from __future__ import annotations
import os
import pytest


@pytest.fixture(autouse=True)
def _placeholder_claude_key(monkeypatch):
    """A placeholder ANTHROPIC_API_KEY for run()'s availability check, only when none is set."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-placeholder-never-sent")
    yield


def pytest_configure(config):
    """BRIEF_TESTS_OFFLINE=1 (the CI job sets it): every outbound socket connection raises,
    so a test that quietly reaches a hosted endpoint fails instead of passing on the
    network (audit critic-G12: one 'offline' test made a hosted embedding call)."""
    if os.environ.get("BRIEF_TESTS_OFFLINE", "").strip() not in ("1", "true", "yes"):
        return
    import socket

    def refuse(self, *a, **k):
        """Fail fast: this suite must not touch the network."""
        raise OSError("network disabled: BRIEF_TESTS_OFFLINE=1")
    socket.socket.connect = refuse          # type: ignore[assignment]
    socket.socket.connect_ex = refuse       # type: ignore[assignment]
