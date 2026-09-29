"""The compact judge reply (phase C change 2, tested 2026-09-29, off by default): the judge
answers each draft with the numbers of the tests it passes and a short reason per failed
test, instead of a {"pass": bool} object per test. The strict verdict rules hold in both shapes: a test with no
clear verdict leaves the draft unjudged. Offline: every model call is faked.
Run: cd engine/rag && python3 -m pytest -q test_judge_format.py
"""
from __future__ import annotations
import json
import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import parse_brief as pb  # noqa: E402

SCHEMA = json.loads((HERE.parent / "golden-brief" / "golden_brief.schema.json").read_text())
SMP = next(f for f in SCHEMA["fields"] if f["id"] == "smp")
TERR = {"own": "o", "avoid": "a", "rival": "RivalBank"}
SMP_TESTS = [c["id"] for c in SMP["rubric"] if c["method"] == "llm"] + ["own_territory", "brand_only"]


def _judge(monkeypatch, reply: dict, seen: dict | None = None):
    """A judge that returns `reply` and keeps the prompt it was sent in `seen`."""
    def fake(user, system=None, **k):
        """Test stub: stands in for the judge call."""
        if seen is not None:
            seen.update(user=user, system=system)
        return reply
    monkeypatch.setattr(pb, "_json_call", fake)


def test_compact_numbers_the_tests_and_asks_for_pass_lists(monkeypatch):
    """With BRIEF_JUDGE_FORMAT=compact the tests go out numbered and the judge is asked for
    pass lists."""
    monkeypatch.setenv("BRIEF_JUDGE_FORMAT", "compact")
    seen: dict = {}
    _judge(monkeypatch, {"results": {"0": {"pass": list(range(1, 9)), "fail": {}}}, "ranking": [0]}, seen)
    pb._judge_and_gate(SMP, [{"value": "Only Acme treats under-30s as adults"}], territory=TERR)
    for n, tid in enumerate(SMP_TESTS, 1):
        assert f"- {n}. {tid}:" in seen["user"]
    assert '"pass": [numbers of the tests it passes]' in seen["system"]


def test_a_compact_reply_gives_the_same_verdicts_and_reasons(monkeypatch):
    """Passes by number, a failure with its reason under the test's name, territory included."""
    monkeypatch.setenv("BRIEF_JUDGE_FORMAT", "compact")
    fails = {"5": "nobody minds", "8": "RivalBank could say it"}          # reason_to_care, brand_only
    _judge(monkeypatch, {"results": {"0": {"pass": [1, 2, 3, 4, 6, 7], "fail": fails}}, "ranking": [0]})
    (_c, ok, got), = pb._judge_and_gate(SMP, [{"value": "Only Acme treats under-30s as adults"}], territory=TERR)
    assert SMP_TESTS[4] == "reason_to_care" and SMP_TESTS[7] == "brand_only"
    assert ok is False                                                   # two soft failures
    assert got == ["reason_to_care: nobody minds", "walks onto the competitor's ground: RivalBank could say it"]


def test_a_test_in_neither_list_or_in_both_leaves_the_draft_unjudged(monkeypatch):
    """No clear verdict is never a pass (audit F5), in the compact shape too."""
    monkeypatch.setenv("BRIEF_JUDGE_FORMAT", "compact")
    for r in ({"pass": [1, 2, 3, 4, 5], "fail": {}},                     # test 6 missing
              {"pass": [1, 2, 3, 4, 5, 6], "fail": {"6": "x"}}):         # test 6 in both
        _judge(monkeypatch, {"results": {"0": r}, "ranking": [0]})
        (_c, ok, got), = pb._judge_and_gate(SMP, [{"value": "Only Acme treats under-30s as adults"}])
        assert ok is False and got[-1] == f"{pb.UNJUDGED}: no verdict for {SMP_TESTS[5]}"


def test_a_number_that_was_not_sent_is_ignored(monkeypatch):
    """An extra number changes nothing when every test sent has its verdict."""
    monkeypatch.setenv("BRIEF_JUDGE_FORMAT", "compact")
    _judge(monkeypatch, {"results": {"0": {"pass": [1, 2, 3, 4, 5, 6, 9], "fail": {}}}, "ranking": [0]})
    (_c, ok, got), = pb._judge_and_gate(SMP, [{"value": "Only Acme treats under-30s as adults"}])
    assert ok is True and got == []


def test_a_full_shape_reply_is_still_read(monkeypatch):
    """A judge that answers in the old shape anyway is read as before."""
    monkeypatch.setenv("BRIEF_JUDGE_FORMAT", "compact")
    res = {t: {"pass": t != "ownable", "why": "a rival's line" if t == "ownable" else ""} for t in SMP_TESTS[:6]}
    _judge(monkeypatch, {"results": {"0": res}, "ranking": [0]})
    (_c, ok, got), = pb._judge_and_gate(SMP, [{"value": "Only Acme treats under-30s as adults"}])
    assert ok is True and got == ["ownable: a rival's line"]


def test_full_is_the_default_and_keeps_the_old_prompt(monkeypatch):
    """By default (and with BRIEF_JUDGE_FORMAT=full) the tests go out unnumbered and the
    judge is asked for one object per test."""
    monkeypatch.delenv("BRIEF_JUDGE_FORMAT", raising=False)
    seen: dict = {}
    _judge(monkeypatch, {"results": {"0": {t: {"pass": True} for t in SMP_TESTS[:6]}}, "ranking": [0]}, seen)
    (_c, ok, _f), = pb._judge_and_gate(SMP, [{"value": "Only Acme treats under-30s as adults"}])
    assert ok is True
    assert "- single_minded:" in seen["user"] and "- 1. " not in seen["user"]
    assert '{"pass": true|false' in seen["system"]


def test_the_dump_records_each_judge_call(monkeypatch, tmp_path):
    """BRIEF_JUDGE_DUMP appends one line per judge call with what the replay needs."""
    monkeypatch.setenv("BRIEF_JUDGE_FORMAT", "compact")
    out = tmp_path / "judge.jsonl"
    monkeypatch.setenv("BRIEF_JUDGE_DUMP", str(out))
    _judge(monkeypatch, {"results": {"0": {"pass": list(range(1, 7)), "fail": {}}}, "ranking": [0]})
    pb._judge_and_gate(SMP, [{"value": "a"}], ctx="C")
    rec = json.loads(out.read_text().splitlines()[0])
    assert rec["field"]["id"] == "smp" and rec["values"] == ["a"] and rec["ctx"] == "C"
