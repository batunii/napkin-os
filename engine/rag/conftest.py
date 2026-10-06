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
    # jev checks (ADR 0011) off unless a test turns them on with a fake backend: the real
    # backend would read TYPESAFE_API_KEY from engine/.env and try the network.
    monkeypatch.setenv("BRIEF_JEV_CHECKS", "0")
    # The coherence pass (one extra judge call after the fields are filled) is off unless a test
    # turns it on: the call-count and fake-model tests count exact calls.
    monkeypatch.setenv("BRIEF_COHERENCE", "0")
    # REQ-01's fill step (repair then flag; labelled proposals) runs after the first-pass gates and adds
    # calls; the gate tests check the first pass, so it is off unless a test turns it on (test_fill_all.py).
    monkeypatch.setenv("BRIEF_FILL_ALL", "0")
    # The gap-filler (ADR 0019) never searches or writes the real fact store in tests.
    monkeypatch.setenv("BRIEF_GAP_FILL", "0")
    monkeypatch.setenv("BRIEF_FACT_CHECK", "0")      # one more judge call (ADR 0021); its own tests turn it on
    monkeypatch.setenv("BRIEF_SECTIONS", "0")        # seven section writers (ADR 0022); test_sections turns them on
    monkeypatch.delenv("BRIEF_RESEARCH_URL", raising=False)
    import sys as _sys
    jc = _sys.modules.get("jev_checks")
    if jc is not None:
        monkeypatch.setattr(jc, "_backend", None)
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
