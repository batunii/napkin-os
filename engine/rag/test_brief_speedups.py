"""The 2026-09-23 speed-ups in parse_brief: TOON capture with sentence citations, how_to_win
in its own call, the stage graph in run(), and the hero fields' batched judge + waves.
Offline: every model call is faked. Barriers prove concurrency — a step that ran after the
other instead of alongside it would leave the barrier waiting and fail the test.
Run: cd engine/rag && python3 -m pytest -q test_brief_speedups.py
"""
from __future__ import annotations
import json
import sys
import threading
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import pytest  # noqa: E402
import parse_brief as pb  # noqa: E402
import toon_lite  # noqa: E402

GOLDEN_SCHEMA = json.loads((HERE.parent / "golden-brief" / "golden_brief.schema.json").read_text())
SEGS = ["Acme Bank is relaunching its app.", "Under-30s see it as their parents' bank.",
        "We must grow sign-ups by 20 percent.", "Every asset must carry the disclaimer."]


# ---------- toon_lite ----------

def test_toon_table_list_nested_and_leniency():
    """Tables (miscounted N, a stray pipe, a quoted cell), lists, nesting, fences, nulls."""
    d = toon_lite.decode("```toon\nfields:\n  budget:\n    value: null\n    status: gap\n"
                         "  objective[1|]{value|status|src|confidence}:\n"
                         "    Grow | share|fact|3|0.9\n    \"a|b\"|assumption|4 9|0.5\n"
                         "open_questions[1]:\n  - Who signs off?\n```")
    assert d["fields"]["budget"] == {"value": None, "status": "gap"}
    assert d["fields"]["objective"] == [
        {"value": "Grow | share", "status": "fact", "src": 3, "confidence": 0.9},
        {"value": "a|b", "status": "assumption", "src": "4 9", "confidence": 0.5}]
    assert d["open_questions"] == ["Who signs off?"]


def test_toon_wrapped_row_is_joined_back():
    """A row wrapped onto an unindented line (seen live) joins the row above, not an error."""
    d = toon_lite.decode("themes[2|]{point|src}:\n  First point|1\n  A long point that wraps,\n"
                         "onto a second line|4,5\nlandmines[1|]{point|src}:\n  Risk|2\n")
    assert d["themes"][1] == {"point": "A long point that wraps, onto a second line", "src": "4,5"}
    assert d["landmines"] == [{"point": "Risk", "src": 2}]


def test_toon_unreadable_raises():
    """Prose is not TOON: the caller must see a failure, not an empty dict."""
    with pytest.raises(toon_lite.ToonError):
        toon_lite.decode("Sure! Here is the capture you asked for")


# ---------- capture + how_to_win ----------

def _one_link(monkeypatch, reply, seen=None):
    """A model chain of one fake link that returns `reply` (and records json_mode)."""
    monkeypatch.setattr(pb, "_model_chain", lambda model=None: [("fake", "m")])
    def call(provider, m, user, **kw):
        """Return the canned reply."""
        if seen is not None:
            seen.append(kw.get("json_mode"))
        return reply
    monkeypatch.setattr(pb, "_call_link", call)


def test_capture_toon_attaches_verbatim_quotes_from_sentence_numbers(monkeypatch):
    """`src: 1 2` becomes the exact sentences; out-of-range numbers are dropped; JSON mode off."""
    seen = []
    _one_link(monkeypatch, "fields:\n  business_problem:\n    value: Under-30s see an old bank\n"
                           "    status: fact\n    src: 1 2 99\n    confidence: 0.9\n"
                           "  objective[1|]{value|status|objective_type|src|confidence}:\n"
                           "    Grow sign-ups 20 percent|fact|commercial|3|0.9\n"
                           "open_questions[1]:\n  - What is the budget?\n", seen)
    out = pb.capture_toon(SEGS)
    bp = out["fields"]["business_problem"]
    assert bp["source_quote"] == SEGS[0] + " " + SEGS[1] and bp["source_refs"] == [1, 2]
    assert out["fields"]["objective"][0]["objective_type"] == "commercial"
    assert out["fields"]["objective"][0]["source_quote"] == SEGS[2]
    assert out["open_questions"] == [{"question": "What is the budget?"}] and seen == [False]


def test_capture_toon_returns_none_on_prose(monkeypatch):
    """An unreadable reply exhausts the chain -> None, so run() falls back to JSON."""
    _one_link(monkeypatch, "I could not parse this brief.")
    assert pb.capture_toon(SEGS) is None


def test_how_to_win_toon_keeps_the_point_evidence_shape(monkeypatch):
    """Rows become {point, evidence (verbatim), source_refs}; missing tables are empty."""
    _one_link(monkeypatch, "likely_landmines[1|]{point|src}:\n  Looking like a bank for parents|2\n")
    out = pb.how_to_win_toon(SEGS)
    assert out["likely_landmines"] == [{"point": "Looking like a bank for parents", "evidence": SEGS[1],
                                        "source_refs": [2]}]
    assert set(out) == set(pb.HOW_TO_WIN_KEYS) and out["winning_themes"] == []


def test_ledger_counts_cited_sentences_as_mapped():
    """Code-attached quotes are exact, so every cited sentence is mapped in the no-loss ledger."""
    fields = {"business_problem": pb._capture_item({"value": "qqq", "src": "1 2"}, SEGS),
              "mandatories": [pb._capture_item({"value": "zzz", "src": "4"}, SEGS)]}
    led = pb.build_ledger(SEGS, None, fields, "t")
    assert [u["segment"] for u in led["unmapped"]] == [SEGS[2]]


# ---------- run(): the stage graph ----------

def test_run_starts_capture_how_to_win_and_golden_together(monkeypatch):
    """The three reads of the raw brief are in flight at once; output shape unchanged."""
    gate = threading.Barrier(3, timeout=5)
    def cap(segs):
        """Fake capture."""
        gate.wait(); return {"fields": {"business_problem": {"value": "p", "status": "fact"}},
                             "how_to_win": {}, "open_questions": []}
    def htw(segs):
        """Fake how-to-win."""
        gate.wait(); return {"winning_themes": [{"point": "w", "evidence": "e"}]}
    def gold(text):
        """Fake golden extraction."""
        gate.wait(); return {"fields": {}}
    monkeypatch.setattr(pb, "capture_toon", cap)
    monkeypatch.setattr(pb, "how_to_win_toon", htw)
    monkeypatch.setattr(pb, "extract_golden_brief", gold)
    monkeypatch.setattr(pb, "score_betterbriefs", lambda text, fields: {"score": 1})
    monkeypatch.delenv("BRIEF_PARALLEL", raising=False)
    monkeypatch.delenv("BRIEF_CAPTURE", raising=False)
    out = pb.run(None, golden=True, raw_text="Acme Bank is relaunching its app. Under-30s ignore it.")
    assert out["meta"]["capture_format"] == "toon"
    assert out["loop1_capture"]["how_to_win"]["winning_themes"][0]["point"] == "w"
    assert out["betterbriefs_scorecard"] == {"score": 1} and "loop2_golden" in out
    assert list(out)[:4] == ["meta", "loop1_capture", "loop2_brief", "betterbriefs_scorecard"]


def test_run_falls_back_to_json_capture(monkeypatch):
    """TOON capture fails -> extract_llm's JSON capture, with its own how_to_win."""
    monkeypatch.setattr(pb, "capture_toon", lambda segs: None)
    monkeypatch.setattr(pb, "how_to_win_toon", lambda segs: {"unused": []})
    monkeypatch.setattr(pb, "extract_llm", lambda text, schema: {
        "fields": {"business_problem": {"value": "p", "status": "fact"}},
        "how_to_win": {"winning_themes": []}, "open_questions": []})
    monkeypatch.setattr(pb, "score_betterbriefs", lambda text, fields: {})
    out = pb.run(None, raw_text="A brief. With two sentences.")
    assert out["meta"]["capture_format"] == "json"
    assert out["loop1_capture"]["how_to_win"] == {"winning_themes": []}


# ---------- hero fields ----------

HERO = {"id": "insight", "label": "Insight", "max_words": 30,
        "rubric": [{"id": "a", "method": "llm", "test": "A?"}, {"id": "b", "method": "llm", "test": "B?"}]}


def _verdicts(monkeypatch, judge):
    """Make the batched judge return `judge`."""
    monkeypatch.setattr(pb, "_json_call", lambda *a, **k: judge)


def test_judge_and_gate_ranks_and_applies_the_pass_rule(monkeypatch):
    """Ranking is honoured; one soft failure passes, two fail; a code failure is final."""
    _verdicts(monkeypatch, {"ranking": [2, 0, 1], "why": "sharpest", "results": {
        "0": {"a": {"pass": False, "why": "x"}, "b": {"pass": True}},
        "1": {"a": {"pass": True}, "b": {"pass": True}},
        "2": {"a": {"pass": False, "why": "x"}, "b": {"pass": False, "why": "y"}}}})
    cands = [{"value": "zero"}, {"value": " ".join(["long"] * 40)}, {"value": "two"}]
    out = pb._judge_and_gate(HERO, cands)
    assert [c["value"] for c, _ok, _f in out] == ["two", "zero", " ".join(["long"] * 40)]
    assert [ok for _c, ok, _f in out] == [False, True, False]      # 2 soft / 1 soft / over limit
    assert out[0][0]["_judge_why"] == "sharpest"


def test_judge_and_gate_territory_fail(monkeypatch):
    """A line that passes the rubric but not the territory tests fails, with the reason."""
    _verdicts(monkeypatch, {"results": {"0": {"a": {"pass": True}, "b": {"pass": True},
                                              "not_rival_line": {"pass": False, "why": "rival says it"}}}})
    (_c, ok, fails), = pb._judge_and_gate(HERO, [{"value": "v"}],
                                          territory={"own": "o", "avoid": "a", "rival": "R"})
    assert not ok and "rival says it" in fails[0]


def test_judge_and_gate_judge_down_keeps_order_code_tests_only(monkeypatch):
    """No verdict: order unchanged, only the code tests decide — as the old path did."""
    _verdicts(monkeypatch, None)
    out = pb._judge_and_gate(HERO, [{"value": "a"}, {"value": "b"}])
    assert [(c["value"], ok) for c, ok, _f in out] == [("a", True), ("b", True)]


def _fake_models(monkeypatch, calls, gate=None):
    """Every generation/judge prompt of fill_derivable_fields, answered by kind."""
    label = {f["id"]: f["label"] for f in GOLDEN_SCHEMA["fields"]}
    def fake(user, system=None, **kw):
        """Answer by what the prompt asks for; record the kind."""
        system = system or ""
        if system.startswith("You map strategic white space"):
            kind, out = "territory", {"rival": "R", "own": "O", "avoid": "A"}
        elif '"candidates"' in system:
            kind, out = "gen_batch", {"candidates": [{"value": f"draft {i}", "confidence": 0.9} for i in range(4)]}
        elif "REFINE MODE" in system:
            kind, out = "refine", {"value": "refined line", "confidence": 0.9}
        elif "judging candidate" in system:
            kind, out = "judge_batch", {"ranking": [0], "why": "w", "results": {}}
        elif "ranking candidate" in system:
            kind, out = "judge_rank", {"ranking": [0], "why": "w"}
        elif "brief-quality judge" in system:
            kind, out = "gate", {}
        elif "enforcing ownable territory" in system:
            kind, out = "terr_gate", {"own_territory": True, "competitor_could_run": False}
        else:
            kind, out = "gen", {"value": "one line", "confidence": 0.9}
            if gate and any(f"'{label[f]}'" in user for f in ("reasons_to_believe", "desired_response")):
                gate.wait()                  # both in flight at once, or this times out
        calls.append(kind)
        return out
    monkeypatch.setattr(pb, "_json_call", fake)
    monkeypatch.setattr(pb, "resolve_provider", lambda: "fake")


def _fill(monkeypatch):
    """Run fill_derivable_fields on an empty strategy zone."""
    gf = {"audience": {"value": "under-30s", "source": "client_stated"},
          "background": {"value": "app relaunch", "source": "client_stated"},
          "competitor_context": {"value": "RivalBank", "source": "client_stated"}}
    return pb.fill_derivable_fields(gf, {"loops": {}}, GOLDEN_SCHEMA, brief_text="b")


def test_hero_fields_batched_calls_and_parallel_waves(monkeypatch):
    """Default: 13 calls (was 17 with every gate passing first time), and reasons_to_believe
    and desired_response are generated at the same time."""
    calls = []
    _fake_models(monkeypatch, calls, gate=threading.Barrier(2, timeout=5))
    for v in ("BRIEF_BATCH_GATES", "BRIEF_PARALLEL", "BRIEF_HERO_CANDIDATES", "BRIEF_SMP_CANDIDATES"):
        monkeypatch.delenv(v, raising=False)
    fills, _qs = _fill(monkeypatch)
    assert set(fills) == set(pb.GEN_ZONE3_ORDER)
    assert fills["insight"]["value"] == "refined line"
    assert len(calls) == 13, calls


def test_hero_fields_per_candidate_path_still_works(monkeypatch):
    """BRIEF_BATCH_GATES=0, BRIEF_PARALLEL=0: the old per-candidate gates, one at a time."""
    calls = []
    _fake_models(monkeypatch, calls)
    monkeypatch.setenv("BRIEF_BATCH_GATES", "0")
    monkeypatch.setenv("BRIEF_PARALLEL", "0")
    fills, _qs = _fill(monkeypatch)
    assert set(fills) == set(pb.GEN_ZONE3_ORDER)
    assert "judge_batch" not in calls and calls.count("terr_gate") == 2 and len(calls) == 17, calls
