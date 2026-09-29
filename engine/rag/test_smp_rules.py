"""The SMP rules from the 2026-09-24 literature research (R1), accepted by Sai 2026-09-26
(ADR 0007): the definition, the checks and their tolerance, what is soft, what is a flag.
Offline: every model call is faked.
Run: cd engine/rag && python3 -m pytest -q test_smp_rules.py
"""
from __future__ import annotations
import json
import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import parse_brief as pb  # noqa: E402
import golden_critic as gc  # noqa: E402

SCHEMA = json.loads((HERE.parent / "golden-brief" / "golden_brief.schema.json").read_text())
SMP = next(f for f in SCHEMA["fields"] if f["id"] == "smp")
LLM = {c["id"]: c for c in SMP["rubric"] if c["method"] == "llm"}


def _judge(monkeypatch, verdicts: dict, why: dict | None = None):
    """A batched judge that answers the given {test_id: bool} for candidate 0, True otherwise."""
    def fake(user, system=None, **k):
        """Test stub: stands in for `fake` in _judge."""
        import re
        tests = re.findall(r"^- (?:\d+\. )?([a-z0-9_]+):", user.split("TESTS", 1)[-1], flags=re.M)
        res = {t: {"pass": verdicts.get(t, True), "why": (why or {}).get(t, "")} for t in tests}
        return {"ranking": [0], "results": {"0": res}}
    monkeypatch.setattr(pb, "_json_call", fake)


def test_schema_carries_the_sourced_smp_checks():
    """The SMP's checks are the R1 checklist: one sentence and the word limit by code; one
    strategic choice, follows from the insight, not written as copy, not a category
    generic, a reason to care, room for many ads by the judge. The comma regex is gone."""
    assert [c["id"] for c in SMP["rubric"]] == ["single_sentence", "within_limit", "single_minded", "derives_from",
                                                "not_a_tagline", "ownable", "reason_to_care", "room_for_many_ads"]
    assert LLM["single_minded"]["tolerance"] == "hard" and LLM["derives_from"]["tolerance"] == "hard"
    assert all("tolerance" not in LLM[t] for t in ("not_a_tagline", "ownable", "reason_to_care", "room_for_many_ads"))
    assert "headline" in LLM["not_a_tagline"]["test"] and "pun" in LLM["not_a_tagline"]["test"]
    assert "pre-empted" in LLM["ownable"]["test"] and "category generic" in LLM["ownable"]["test"]
    assert "problem and the insight" in LLM["derives_from"]["test"] and "benefit" not in LLM["derives_from"]["test"]
    assert "take away" in SMP["prompt"] and "reason to care" in SMP["prompt"] and "ownable" not in SMP["prompt"]
    assert "audience" in SMP["depends_on"] and SMP["max_words"] == 20


def test_hard_llm_tests_fail_the_draft_outright(monkeypatch):
    """A failed single_minded or derives_from is final, whatever the one-fail tolerance."""
    _judge(monkeypatch, {"derives_from": False}, {"derives_from": "no line to the insight"})
    (_c, ok, fails), = pb._judge_and_gate(SMP, [{"value": "Only Acme treats under-30s as adults"}])
    assert ok is False and fails == ["derives_from: no line to the insight"]
    _judge(monkeypatch, {"single_minded": False})
    (_c, ok, _f), = pb._judge_and_gate(SMP, [{"value": "Fast and cheap and friendly"}])
    assert ok is False


def test_one_soft_failure_is_tolerated_two_are_not(monkeypatch):
    """The SMP has six llm tests: one soft failure passes (with the reason kept), two fail."""
    _judge(monkeypatch, {"reason_to_care": False}, {"reason_to_care": "nobody minds"})
    (_c, ok, fails), = pb._judge_and_gate(SMP, [{"value": "Only Acme treats under-30s as adults"}])
    assert ok is True and fails == ["reason_to_care: nobody minds"]
    _judge(monkeypatch, {"reason_to_care": False, "room_for_many_ads": False})
    (_c, ok, _f), = pb._judge_and_gate(SMP, [{"value": "Only Acme treats under-30s as adults"}])
    assert ok is False


def test_territory_failure_is_soft_not_fatal(monkeypatch):
    """A line on the rival's ground counts as one soft failure (D6: differentiation is
    contested), so it passes alone and fails with a second soft failure."""
    _judge(monkeypatch, {"brand_only": False}, {"brand_only": "RivalBank could say it"})
    terr = {"own": "o", "avoid": "a", "rival": "RivalBank"}
    (_c, ok, fails), = pb._judge_and_gate(SMP, [{"value": "The bank that gets you"}], territory=terr)
    assert ok is True and fails == ["walks onto the competitor's ground: RivalBank could say it"]
    _judge(monkeypatch, {"brand_only": False, "ownable": False})
    (_c, ok, _f), = pb._judge_and_gate(SMP, [{"value": "The bank that gets you"}], territory=terr)
    assert ok is False


def test_masterbrand_echo_is_a_flag_not_a_fail(monkeypatch):
    """An SMP that echoes the brand's standing line is flagged for a human, not blocked
    (Levi's and Forte Posthouse restated theirs on purpose)."""
    brand = "Vision: The bank that treats young people as adults with money"
    assert pb._rubric_hard(SMP, "The bank that treats young people as adults with money", brand) == []
    flags = pb._rubric_flags(SMP, "The bank that treats young people as adults with money", brand)
    assert flags and "standing vision" in flags[0]
    _judge(monkeypatch, {})
    (c, ok, _f), = pb._judge_and_gate(SMP, [{"value": "The bank that treats young people as adults with money"}], brand)
    assert ok is True and c["_flags"] == flags


def test_gate_notes_and_flags_are_stored_on_the_entry(monkeypatch):
    """The fill keeps the tolerated reason and the flag on the SMP entry, so a judge's
    verdicts can be audited later."""
    def fake(user, system=None, **k):
        """Test stub: stands in for `fake` in test_gate_notes_and_flags_are_stored_on_the_entry."""
        import re
        system = system or ""
        if system.startswith("You map strategic white space"):
            return {"rival": "R", "own": "O", "avoid": "A"}
        if '"candidates"' in system:
            return {"candidates": [{"value": "Vision line for the bank, because it holds", "confidence": 0.9}] * 4}
        if "REFINE MODE" in system:
            return {"value": "Vision line for the bank, because it holds", "confidence": 0.9}
        if "judging candidate" in system:
            n = len(re.findall(r"^\[\d+\] ", user, flags=re.M))
            tests = re.findall(r"^- (?:\d+\. )?([a-z0-9_]+):", user.split("TESTS", 1)[-1], flags=re.M)
            res = {t: {"pass": t != "reason_to_care", "why": "meh" if t == "reason_to_care" else ""} for t in tests}
            return {"ranking": list(range(n)), "results": {str(i): res for i in range(n)}}
        if '"think"' in system:
            return {"value": {"think": "a", "feel": "b", "do": "open the app"}, "confidence": 0.9}
        return {"value": ["one reason"], "confidence": 0.9}
    monkeypatch.setattr(pb, "_json_call", fake)
    monkeypatch.setattr(pb, "resolve_provider", lambda: "fake")
    monkeypatch.setenv("BRIEF_PARALLEL", "0")
    gf = {"audience": {"value": "under-30s", "source": "client_stated"},
          "background": {"value": "app relaunch", "source": "client_stated"},
          "competitor_context": {"value": "RivalBank", "source": "client_stated"}}
    fills, _qs = pb.fill_derivable_fields(gf, {"loops": {}}, SCHEMA,
                                          brief_text="Brand vision: Vision line for the bank")
    assert fills["smp"]["gate_notes"] == ["reason_to_care: meh"]
    assert any("standing vision" in f for f in fills["smp"]["flags"])


def test_critic_leaves_ownable_for_review_without_competitor_context():
    """No competitor context in the client brief: ownable is REVIEW, not a FAIL charged
    to the SMP; the dependency says the same."""
    brief = gc.from_brief_object({"loop1_capture": {"fields": {}}, "loop2_brief": {"open_questions": []},
                                  "loop2_golden": {"fields": {"smp": {"value": "Only Acme treats under-30s as adults",
                                                                      "source": "inferred"}}}})
    v = gc.validate(SCHEMA, brief)
    smp = {c["id"]: c for c in next(fr for fr in v["fields"] if fr["id"] == "smp")["checks"]}
    assert smp["ownable"]["status"] == "review" and smp["ownable"]["note"] == "needs competitor_context"
    assert smp["single_minded"]["status"] == "review" and smp["single_minded"]["method"] == "llm"
    dep = next(d for d in v["dependencies"] if d["id"] == "ownable_needs_competitors")
    assert dep["status"] == "review"


def test_judge_prompt_no_longer_bans_and_and_names_copy_devices(monkeypatch):
    """The hero framing drops 'never an and' (Levi's and Corona contradict it) and names
    the copy devices instead of 'restated taglines'."""
    seen = {}
    def fake(user, system=None, **k):
        """Test stub: stands in for `fake` in test_judge_prompt_no_longer_bans_and_and_names_copy_devices."""
        seen["system"] = system
        return {"ranking": [0, 1], "results": {"0": {t: {"pass": True} for t in LLM}, "1": {t: {"pass": True} for t in LLM}}}
    monkeypatch.setattr(pb, "_json_call", fake)
    pb._judge_and_gate(SMP, [{"value": "a"}, {"value": "b"}])
    s = seen["system"]
    assert "'and'" not in s and "restated taglines" not in s
    assert "pun or double meaning" in s and "headline-able is not a fault" in s


def test_angle_seeds_are_the_sourced_proposition_types():
    """The tournament seeds are the proposition types the sources name, not social judgement."""
    joined = " ".join(pb.SMP_ANGLE_SEEDS)
    for word in ("killer fact", "promise", "emotional benefit", "big idea"):
        assert word in joined
    assert "judged" not in joined and "settles for" not in joined


def test_sentence_counter_ignores_titles_and_abbreviations():
    """'Dr. Oetker …' is one sentence, so is 'Acme Ltd. every day' and 'J. K. Rowling'; real
    sentence breaks still count (bug found on the 2026-09-26 checkpoint run)."""
    assert len(gc._sentences("Dr. Oetker no-boil mămăligă removes the guilt of choosing convenience.")) == 1
    assert len(gc._sentences("We work with Acme Ltd. every day. Really.")) == 2
    assert len(gc._sentences("J. K. Rowling wrote it. Twice!")) == 2
    assert len(gc._sentences("Prices from 2.5 euro a pack.")) == 1
    assert pb._rubric_hard(SMP, "Dr. Oetker keeps the making in your hands.") == []
