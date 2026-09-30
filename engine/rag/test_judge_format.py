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


# ---- the missing-verdict re-ask (Sai, 2026-09-29) -----------------------------------------

def _sequence(monkeypatch, replies: list, prompts: list):
    """A judge that gives `replies` in turn (then None) and keeps every prompt it was sent."""
    it = iter(replies)

    def fake(user, system=None, **k):
        """Test stub: stands in for the judge call."""
        prompts.append(user)
        return next(it, None)
    monkeypatch.setattr(pb, "_json_call", fake)


FULL6 = {t: {"pass": True} for t in SMP_TESTS[:6]}


def test_a_missing_verdict_is_asked_for_once_and_fills_in(monkeypatch):
    """The judge skipped 'ownable' on draft 1: one more call asks for exactly that, and the
    draft is then judged instead of left empty."""
    monkeypatch.delenv("BRIEF_JUDGE_FORMAT", raising=False)
    partial = {k: v for k, v in FULL6.items() if k != "ownable"}
    prompts: list = []
    _sequence(monkeypatch, [{"results": {"0": FULL6, "1": partial}, "ranking": [1, 0]},
                            {"results": {"1": {"ownable": {"pass": True}}}}], prompts)
    judged = pb._judge_and_gate(SMP, [{"value": "Line A for Acme"}, {"value": "Line B for Acme"}])
    assert len(prompts) == 2
    assert "- candidate 1: ownable" in prompts[1] and "candidate 0" not in prompts[1]
    assert [ok for _c, ok, _f in judged] == [True, True]
    assert judged[0][0]["value"] == "Line B for Acme"          # the first reply's ranking holds


def test_still_missing_after_the_re_ask_stays_unjudged(monkeypatch):
    """One re-ask only: a verdict still missing leaves the draft unjudged, as before."""
    monkeypatch.delenv("BRIEF_JUDGE_FORMAT", raising=False)
    partial = {k: v for k, v in FULL6.items() if k != "ownable"}
    prompts: list = []
    _sequence(monkeypatch, [{"results": {"0": partial}, "ranking": [0]}, {"results": {"0": {}}}], prompts)
    (_c, ok, fails), = pb._judge_and_gate(SMP, [{"value": "Line A for Acme"}])
    assert len(prompts) == 2 and ok is False
    assert fails[-1] == f"{pb.UNJUDGED}: no verdict for ownable"


def test_a_judge_that_is_down_is_not_asked_again(monkeypatch):
    """No reply at all: no re-ask; every draft unjudged, as before."""
    prompts: list = []
    _sequence(monkeypatch, [None], prompts)
    (_c, ok, fails), = pb._judge_and_gate(SMP, [{"value": "Line A for Acme"}])
    assert len(prompts) == 1 and ok is False and fails[-1] == f"{pb.UNJUDGED}: judge unavailable"


def test_a_complete_reply_makes_no_second_call(monkeypatch):
    """Every verdict readable: one call, as before."""
    monkeypatch.delenv("BRIEF_JUDGE_FORMAT", raising=False)
    prompts: list = []
    _sequence(monkeypatch, [{"results": {"0": FULL6}, "ranking": [0]}], prompts)
    (_c, ok, _f), = pb._judge_and_gate(SMP, [{"value": "Line A for Acme"}])
    assert len(prompts) == 1 and ok is True


def test_the_re_ask_keeps_a_failure_and_its_reason(monkeypatch):
    """A verdict filled by the re-ask counts like any other, reason included."""
    monkeypatch.delenv("BRIEF_JUDGE_FORMAT", raising=False)
    partial = {k: v for k, v in FULL6.items() if k != "derives_from"}
    _sequence(monkeypatch, [{"results": {"0": partial}, "ranking": [0]},
                            {"results": {"0": {"derives_from": {"pass": False, "why": "no line to the insight"}}}}], [])
    (_c, ok, fails), = pb._judge_and_gate(SMP, [{"value": "Line A for Acme"}])
    assert ok is False and fails == ["derives_from: no line to the insight"]    # a hard test


def test_the_re_ask_realigns_a_reply_numbered_from_1(monkeypatch):
    """The first reply numbered drafts from 1; the re-ask answers by the prompt's index."""
    monkeypatch.delenv("BRIEF_JUDGE_FORMAT", raising=False)
    partial = {k: v for k, v in FULL6.items() if k != "ownable"}
    prompts: list = []
    _sequence(monkeypatch, [{"results": {"1": FULL6, "2": partial}, "ranking": [1, 2]},
                            {"results": {"1": {"ownable": {"pass": True}}}}], prompts)
    judged = pb._judge_and_gate(SMP, [{"value": "Line A for Acme"}, {"value": "Line B for Acme"}])
    assert "- candidate 1: ownable" in prompts[1]
    assert [ok for _c, ok, _f in judged] == [True, True]


def test_the_compact_re_ask_names_tests_by_number(monkeypatch):
    """In the compact format the re-ask lists the test numbers and reads a compact reply."""
    monkeypatch.setenv("BRIEF_JUDGE_FORMAT", "compact")
    prompts: list = []
    _sequence(monkeypatch, [{"results": {"0": {"pass": [1, 2, 3, 5, 6], "fail": {}}}, "ranking": [0]},
                            {"results": {"0": {"pass": [4], "fail": {}}}}], prompts)
    (_c, ok, _f), = pb._judge_and_gate(SMP, [{"value": "Line A for Acme"}])
    assert "- candidate 0: 4. ownable" in prompts[1] and ok is True
