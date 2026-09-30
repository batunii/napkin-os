"""pairwise.judge_pair (phase A item 1): both orders per round, a round the orders disagree
on is a tie, the verdict is the side with more rounds, scores held to 1-10, failed calls
left out, None with no usable round, seeded order. Offline: a fake judge.
Run: cd engine/rag && python3 -m pytest -q test_pairwise.py
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent))
import pairwise  # noqa: E402

pairwise.RETRY_WAITS_S = ()          # no pauses offline


class FakePB:
    """A parse_brief stand-in whose _json_call answers from `pick(first, second)`."""
    def __init__(self, pick, scores=(7, 5)):
        """Test stub: stands in for `__init__` in FakePB."""
        self.pick, self.scores, self.calls = pick, scores, []

    def _json_call(self, user, **k):
        """Test stub: stands in for `_json_call` in FakePB."""
        x = user.split("<brief_x>\n", 1)[1].split("\n</brief_x>", 1)[0]
        y = user.split("<brief_y>\n", 1)[1].split("\n</brief_y>", 1)[0]
        self.calls.append((x, y))
        k.get("info", {})["link"] = "anthropic:claude-sonnet-5"
        better = self.pick(x, y)
        return None if better is None else {"better": better, "scores": {"X": self.scores[0], "Y": self.scores[1]}, "why": "w"}


def test_a_judge_that_always_prefers_first_position_gives_ties():
    """Position bias cancelled: X always wins, so every round's orders disagree -> tie."""
    r = pairwise.judge_pair("brief", "A text", "B text", key="k", pb=FakePB(lambda x, y: "X"))
    assert r["verdict"] == "tie" and r["wins"] == {"A": 0, "B": 0, "tie": 3} and r["consistency"] == 0.0
    assert r["calls"] == 6


def test_a_consistent_preference_wins():
    """The judge prefers A in both orders every round: A wins 3-0, orders agree 100%."""
    fake = FakePB(lambda x, y: "X" if x == "A text" else "Y")
    r = pairwise.judge_pair("brief", "A text", "B text", key="k", pb=fake)
    assert r["verdict"] == "A" and r["wins"]["A"] == 3 and r["consistency"] == 1.0
    assert sorted(fake.calls) == sorted([("A text", "B text"), ("B text", "A text")] * 3)


def test_same_in_both_orders_is_a_tie_that_agrees():
    """'same' both ways: a tie with the orders agreeing."""
    r = pairwise.judge_pair("brief", "A", "B", pb=FakePB(lambda x, y: "same"))
    assert r["verdict"] == "tie" and r["consistency"] == 1.0


def test_scores_are_clamped_and_failed_calls_left_out():
    """Out-of-range scores are held to 1-10; a round with a failed call is dropped; no round -> None."""
    n = {"i": 0}
    def pick(x, y):
        """Test stub: stands in for `pick` in test_scores_are_clamped_and_failed_calls_left_out."""
        n["i"] += 1
        return None if n["i"] <= 2 else ("X" if x == "A" else "Y")
    r = pairwise.judge_pair("brief", "A", "B", samples=3, pb=FakePB(pick, scores=(14, -3)))
    assert r["judged_rounds"] <= 2 and r["spread"]["A"][1] <= 10 and r["spread"]["B"][0] >= 1
    assert pairwise.judge_pair("brief", "A", "B", pb=FakePB(lambda x, y: None)) is None


def test_every_round_asks_both_orders():
    """Each of the rounds asks A-first and B-first once."""
    fake = FakePB(lambda x, y: "same")
    pairwise.judge_pair("brief", "A", "B", samples=2, pb=fake)
    assert sorted(fake.calls) == [("A", "B"), ("A", "B"), ("B", "A"), ("B", "A")]
