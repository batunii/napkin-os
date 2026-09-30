"""golden_critic.run_critic_sampled (phase A item 2): with samples=1 it is the one-call critic;
with N it keeps each llm check's majority verdict, an even split becomes REVIEW, a sample
that judged nothing is left out, and critic_samples records each sample's health, the
spread and how often every sample agreed. Offline: an injected judge.
Run: cd engine/rag && python3 -m pytest -q test_critic_samples.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent))
import golden_critic as gc  # noqa: E402

SCHEMA = json.loads(gc.SCHEMA_PATH.read_text())
BO = json.loads((HERE.parent / "agent-server" / "fixtures" / "golden_brief_object.json").read_text())


def _judge_all(verdict_for):
    """A judge that answers every pending check with verdict_for(field, check) -> 'pass'|'fail'|None."""
    def judge(prompt):
        """Test stub: stands in for `judge` in _judge_all."""
        gb = gc.from_brief_object(BO)
        v = gc.validate(SCHEMA, gb)
        out = {}
        for b in gc.critic_prompts_batched(SCHEMA, gb, v):
            out[b["field"]] = {cid: {"verdict": verdict_for(b["field"], cid), "reason": "r"}
                               for cid in b["checks"] if verdict_for(b["field"], cid)}
        return out
    return judge


def _fresh():
    """A brief and its code-check validation."""
    gb = gc.from_brief_object(BO)
    return gb, gc.validate(SCHEMA, gb)


def test_one_sample_is_the_one_call_critic():
    """samples=1 gives exactly run_critic_one_call's result."""
    gb, v1 = _fresh(); _, v2 = _fresh()
    j = _judge_all(lambda f, c: "pass")
    a, na = gc.run_critic_sampled(SCHEMA, gb, v1, judge=j, samples=1)
    b, nb = gc.run_critic_one_call(SCHEMA, gb, v2, judge=j)
    assert na == nb and a["health"] == b["health"] and "critic_samples" not in a


def test_majority_wins_and_an_even_split_is_review():
    """Three samples: a check failed twice fails; with one sample judging nothing, a 1-1
    split on a check becomes REVIEW; the spread and unanimity are recorded."""
    calls = {"n": 0}
    first_check = {}
    def verdict(f, c):
        """Test stub: stands in for `verdict` in test_majority_wins_and_an_even_split_is_review."""
        first_check.setdefault("k", (f, c))
        if (f, c) != first_check["k"]:
            return "pass"
        return "fail" if calls["n"] in (0, 1) else "pass"
    base = _judge_all(verdict)
    def judge(prompt):
        """Test stub: stands in for `judge` in test_majority_wins_and_an_even_split_is_review."""
        out = base(prompt); calls["n"] += 1
        return out
    gb, v = _fresh()
    out, n = gc.run_critic_sampled(SCHEMA, gb, v, judge=judge, samples=3)
    f, c = first_check["k"]
    chk = next(x for fr in out["fields"] if fr["id"] == f for x in fr["checks"] if x["id"] == c)
    assert chk["status"] == "fail" and chk["note"].startswith("1 of 3 samples pass")
    cs = out["critic_samples"]
    assert cs["n"] == 3 and cs["judged_samples"] == 3 and len(cs["health_each"]) == 3 and cs["unanimous_share"] < 1

    calls["n"] = 0
    def judge2(prompt):
        """Test stub: stands in for `judge2` in test_majority_wins_and_an_even_split_is_review."""
        calls["n"] += 1
        return {} if calls["n"] == 3 else base(prompt)       # third sample judges nothing
    gb, v = _fresh()
    out, n = gc.run_critic_sampled(SCHEMA, gb, v, judge=judge2, samples=3)
    assert out["critic_samples"]["judged_samples"] == 2
    chk = next(x for fr in out["fields"] if fr["id"] == f for x in fr["checks"] if x["id"] == c)
    assert chk["status"] == "review" and chk["note"].startswith("1 of 2 samples pass")


def test_nothing_judged_is_nothing_judged():
    """Every sample empty: (validation, 0) with judge_model None, never a score."""
    gb, v = _fresh()
    out, n = gc.run_critic_sampled(SCHEMA, gb, v, judge=lambda p: {}, samples=3)
    assert n == 0 and out["judge_model"] is None
