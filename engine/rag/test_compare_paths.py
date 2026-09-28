"""compare_paths (phase A item 4): B is really the loops path, the sets are cut to one
size, each path appears once in every position across the three calls, scores are held
to 1-5, and the per-brief verdict counts the calls. Offline: a fake parse_brief.
Run: cd engine/rag && python3 -m pytest -q test_compare_paths.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent))
import compare_paths as cp  # noqa: E402

BRIEF = {"brand": "Acme", "category": "fmcg", "problem": "p", "objective": "o", "audience": "a"}


def test_rotations_put_every_path_in_every_position():
    """Across the three calls each path is shown first, second and third exactly once."""
    for pos in range(3):
        assert sorted(r[pos] for r in cp.ROTATIONS) == ["A", "B", "MIX"]


def test_equalise_and_clamp():
    """Sets are cut to the smallest; scores outside 1-5 are held to the scale."""
    sets = cp.equalise({"A": [1, 2, 3, 4], "B": [1, 2], "MIX": [1, 2, 3]})
    assert {k: len(v) for k, v in sets.items()} == {"A": 2, "B": 2, "MIX": 2}
    assert (cp._clamp5(9), cp._clamp5(0), cp._clamp5("x")) == (5, 1, None)


class FakePB:
    """Records prompts; answers with fixed per-path scores, the best always the MIX set."""
    def __init__(self):
        """Test stub: stands in for `__init__` in FakePB."""
        self.prompts = []

    def _json_call(self, user, **k):
        """Test stub: stands in for `_json_call` in FakePB."""
        self.prompts.append(user)
        # the MIX set's items are titled 'mix'; find which label it got
        lab = next(c for c in "XYZ" if f"=== SET {c} ===\n1. mix" in user)
        k.get("info", {})["link"] = "anthropic:claude-sonnet-5"
        return {"scores": {c: (9 if c == lab else 2) for c in "XYZ"}, "best": lab, "why": "w"}

    def loops_3_7(self, loop2, fields):
        """Test stub: stands in for `loops_3_7` in FakePB."""
        self.rag_path = os.environ.get("RAG_PATH")
        return {"loops": {"loop3": {"evidence": [{"citation": "d › s", "framework": "f", "text": "t"}]}}}


def test_judge_is_blind_rotated_and_aggregated():
    """Three calls, no item counts or cites in the prompt, MIX wins every call, scores clamped."""
    item = lambda t: {"cite": "doc_1 › sec", "title": t, "text": "text"}
    sets = {"A": [item("a")], "B": [item("b")], "MIX": [item("mix")]}
    fake = FakePB()
    j = cp.judge(fake, BRIEF, sets)
    assert len(fake.prompts) == 3 and all("items)" not in p and "doc_1" not in p for p in fake.prompts)
    assert j["best"] == "MIX" and j["best_votes"] == {"A": 0, "B": 0, "MIX": 3} and j["agreed"] is True
    assert j["scores"] == {"A": 2.0, "B": 2.0, "MIX": 5.0}


def test_path_b_runs_the_loops_path_and_restores_the_setting(monkeypatch):
    """RAG_PATH is 'loops' during the call and back to what it was afterwards."""
    monkeypatch.setenv("RAG_PATH", "mix")
    fake = FakePB()
    items = cp.path_b(fake, BRIEF)
    assert fake.rag_path == "loops" and os.environ["RAG_PATH"] == "mix" and items[0]["bucket"] == "loop3"
