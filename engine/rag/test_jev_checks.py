"""jev as a checker (ADR 0011, Sai 2026-09-26): RTB / desired-response figures fail a
draft at p(unsupported) >= 0.9; scorecard verdicts are checked and disputes flagged;
the category comes from upstream research, else jev at p >= 0.85; synthesis sentences
their cited sources do not support are marked. Every check degrades to 'not run' when
jev cannot answer. Offline: a fake jev backend.
Run: cd engine/rag && python3 -m pytest -q test_jev_checks.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent))
import parse_brief as pb  # noqa: E402
import jev_checks as jc  # noqa: E402

FIELD = {f["id"]: f for f in json.loads((HERE.parent / "golden-brief" / "golden_brief.schema.json").read_text())["fields"]}
BRIEF = "Acme sells 2 million packs a year. Three in four buyers repurchase. Every asset carries the disclaimer."


class FakeJev:
    """Answers noul questions with `noul(name, q)` and choice questions with `choice(name, q)`."""
    def __init__(self, noul=None, choice=None, fail=False):
        """Test stub: stands in for `__init__` in FakeJev."""
        self.noul, self.choice, self.fail, self.calls = noul, choice, fail, []

    def ask(self, state, questions, **k):
        """Test stub: stands in for `ask` in FakeJev."""
        self.calls.append((state, questions))
        if self.fail:
            raise RuntimeError("jev down")
        nouls = {n: NS(noul=self.noul(n, q)) for n, q in questions.items() if q["type"] == "noul"}
        choices = {}
        for n, q in questions.items():
            if q["type"] == "choice":
                c, p = self.choice(n, q)
                choices[n] = NS(choice=c, probabilities={c: p, "other": 1 - p})
        return NS(nouls=nouls, choices=choices)


@pytest.fixture
def jev(monkeypatch):
    """Install a fake jev for jev_checks; returns a setter."""
    monkeypatch.setenv("BRIEF_JEV_CHECKS", "1")
    def use(fake):
        """Test stub: stands in for `use` in jev."""
        monkeypatch.setattr(jc, "_backend", fake)
        return fake
    return use


def test_figures_asked_only_for_items_with_figures(jev):
    """Items without a number are not sent; the state is the brief."""
    f = jev(FakeJev(noul=lambda n, q: 0.02 if "73%" in q["instructions"]["item"] else 0.97))
    ps = jc.figures_supported(BRIEF, ["2 million packs a year", "Loved by families", "73% repurchase"])
    assert ps[0] == 0.97 and ps[1] is None and ps[2] == 0.02
    assert f.calls[0][0] == {"client_brief": BRIEF} and len(f.calls[0][1]) == 2


def test_rtb_draft_with_an_invented_figure_fails_the_gate(jev, monkeypatch):
    """The judge passes both RTB drafts; jev finds 73% unsupported in draft 0, so draft 0
    fails with a jev reason and draft 1 wins."""
    jev(FakeJev(noul=lambda n, q: 0.03 if "73%" in q["instructions"]["item"] else 0.96))
    tests = [r["id"] for r in FIELD["reasons_to_believe"]["rubric"] if r.get("method") == "llm"]
    ok = {t: True for t in tests}
    monkeypatch.setattr(pb, "_json_call", lambda *a, **k: {"ranking": [0, 1], "why": "w",
                                                           "results": {"0": dict(ok), "1": dict(ok)}})
    cands = [{"value": ["73% of buyers repurchase"]}, {"value": ["Three in four buyers repurchase"]}]
    out = pb._judge_and_gate(FIELD["reasons_to_believe"], cands, allowed_text=BRIEF)
    by = {json.dumps(c["value"]): (passed, fails) for c, passed, fails in out}
    assert by['["73% of buyers repurchase"]'][0] is False
    assert any(f.startswith("jev: a figure") for f in by['["73% of buyers repurchase"]'][1])
    assert by['["Three in four buyers repurchase"]'][0] is True


def test_figure_check_skips_other_fields_and_a_down_jev(jev):
    """No jev call for a hero field; a failing jev leaves the gate to the code checks."""
    f = jev(FakeJev(noul=lambda n, q: 0.0))
    assert pb._jev_figure_failures(FIELD["smp"], [{"value": "73% better"}], BRIEF) == {}
    assert f.calls == []
    jev(FakeJev(fail=True))
    assert pb._jev_figure_failures(FIELD["reasons_to_believe"], [{"value": ["73%"]}], BRIEF) == {}


def test_scorecard_is_checked_not_overruled(jev):
    """jev's verdict is written on each row; a confident disagreement is a dispute; the
    scorecard's own verdict stands."""
    jev(FakeJev(choice=lambda n, q: ("missing", 0.95) if n == "budget_interlock" else ("pass", 0.6)))
    dims = [{"dimension": "budget_interlock", "verdict": "pass"}, {"dimension": "language", "verdict": "vague"}]
    out = pb._jev_scorecard(BRIEF, dims)
    assert out == {"agrees": 0, "checked": 2, "disputes": ["budget_interlock"]}
    assert dims[0]["verdict"] == "pass" and dims[0]["jev"] == {"choice": "missing", "p": 0.95}


def test_facets_upstream_first_then_jev(jev):
    """Upstream category wins without asking jev; otherwise jev's choice at >= 0.85; a
    low or 'other' choice, or no jev, leaves retrieval unfiltered with a note."""
    f = jev(FakeJev(choice=lambda n, q: ("automotive", 0.97)))
    up = pb.brief_facets(BRIEF, {"category": "retail", "brand": "Acme", "competitors": ["Rival", "Other Co"]})
    assert up["category"] == "retail" and up["category_source"] == "upstream" and f.calls == []
    assert up["brand"] == "Acme" and up["competitors"] == ["Rival", "Other Co"]
    got = pb.brief_facets(BRIEF)
    assert got["category"] == "automotive" and got["category_source"] == "jev" and got["category_p"] == 0.97
    jev(FakeJev(choice=lambda n, q: ("automotive", 0.6)))
    assert pb.brief_facets(BRIEF)["category"] is None
    jev(FakeJev(choice=lambda n, q: ("other", 0.99)))
    assert "no category filter" in pb.brief_facets(BRIEF)["category_note"]
    jev(FakeJev(fail=True))
    assert pb.brief_facets(BRIEF)["category_note"] == "jev unavailable: no category filter"


def test_facets_reach_the_retrieval_pairs(monkeypatch):
    """Category, brand and competitors go into build_multi's pairs, where the filter and
    keyword slots read them."""
    pb._load_retriever()
    import brief_context
    seen = {}
    def fake_multi(pairs, queries, **k):
        """Test stub: stands in for `build_multi` in test_facets_reach_the_retrieval_pairs."""
        seen.update(pairs)
        return NS(fields={}, trace={}, validation_degraded=[], notes=[])
    monkeypatch.setattr(brief_context, "build_multi", fake_multi)
    gist = {"problem": "p", "objective": "o", "audience": "a", "key_message": ""}
    try:
        pb._loops_via_mix(gist, {}, facets={"category": "automotive", "brand": "BMW", "competitors": ["Audi", "Mercedes"]})
    except Exception:
        pass                                  # the fake result is thin; only the pairs matter
    assert seen["category"] == "automotive" and seen["brand"] == "BMW" and seen["competitors"] == "Audi, Mercedes"
    q, kw, filters, _ = brief_context.plan(seen)
    assert filters.get("category") == "automotive" and "Audi" in kw and "BMW" in kw


def test_synthesis_sentence_its_source_does_not_support_is_marked(jev):
    """Only cited sentences are asked; a low support probability appends the mark."""
    jev(FakeJev(noul=lambda n, q: 0.05 if "doubles" in q["instructions"]["sentence"] else 0.9))
    loop = {"evidence": [{"citation": "ipa_0003 › Insight", "text": "Sales rose 10% after the relaunch."}]}
    para = ("Relaunches work when the promise is clear (ipa_0003 › Insight). "
            "A relaunch doubles sales within a year (ipa_0003 › Insight). No cite here.")
    out = pb._jev_mark_unsupported(para, loop)
    assert out.count(pb._UNSUPPORTED_MARK) == 1 and "year (ipa_0003 › Insight)." + pb._UNSUPPORTED_MARK in out
    assert loop["unsupported"] == 1


def test_everything_is_off_without_jev(monkeypatch):
    """BRIEF_JEV_CHECKS=0: no backend, every check returns 'not run'."""
    monkeypatch.setenv("BRIEF_JEV_CHECKS", "0")
    monkeypatch.setattr(jc, "_backend", None)
    assert jc.figures_supported(BRIEF, ["73%"]) is None and jc.choose_category(BRIEF) is None
    assert pb._jev_scorecard(BRIEF, [{"dimension": "language", "verdict": "pass"}]) is None


def test_open_questions_answered_asks_one_noul_per_question(jev):
    """JL-13: each open question goes to jev with the brief as the state; a down jev is None."""
    f = jev(FakeJev(noul=lambda n, q: 0.95 if "packs" in q["instructions"]["open_question"] else 0.1))
    assert jc.questions_answered(BRIEF, ["How many packs a year?", "What is the budget?"]) == [0.95, 0.1]
    q = f.calls[0][1]["q000"]
    assert q["type"] == "noul" and "OPEN_QUESTION" in q["instructions"]["question"]
    assert jc.questions_answered(BRIEF, []) == []
    jev(FakeJev(fail=True))
    assert jc.questions_answered(BRIEF, ["x?"]) is None


def test_a_long_brief_is_read_whole_in_overlapping_pieces():
    """2026-10-01 (never leave anything from the input): a brief longer than STATE_CHARS is
    cut into overlapping pieces that together hold every character; a short one is one
    piece, exactly the text."""
    para = "Samaritans answers calls day and night. " * 40 + "\n\n"
    text = para * 400                                         # ~650k characters
    pieces = jc.brief_chunks(text)
    assert len(pieces) > 1 and all(len(p) <= jc.STATE_CHARS for p in pieces)
    covered, pos = 0, 0
    for p in pieces:                                          # each piece starts inside the last
        start = text.index(p, max(0, pos - jc.CHUNK_OVERLAP - 1))
        assert start <= covered
        covered, pos = start + len(p), start + len(p)
    assert covered == len(text)
    assert jc.brief_chunks(BRIEF) == [BRIEF]


def test_a_figure_late_in_a_long_brief_is_found(jev):
    """The figure sits after STATE_CHARS: one piece holds it, and the item takes that piece's
    probability (before, jev saw only the first 60,000 characters and failed it)."""
    long_brief = "Intro paragraph about the tender.\n" * 3000 + "About 350,000 calls are answered a year.\n"
    f = jev(FakeJev(noul=lambda n, q: 0.0))
    seen = []
    def noul(n, q):
        """Supported only in the state that holds the figure."""
        return 0.97 if "350,000" in seen[-1] else 0.05
    f.noul = noul
    real = f.ask
    def ask(state, questions, **k):
        """Records the state before answering."""
        seen.append(state["client_brief"])
        return real(state, questions, **k)
    f.ask = ask
    assert len(long_brief) > jc.STATE_CHARS
    assert jc.figures_supported(long_brief, ["350,000 calls a year"]) == [0.97]
    assert len(f.calls) == len(jc.brief_chunks(long_brief)) > 1


def test_research_facts_have_their_own_slot_in_every_state(jev):
    """With research, every state carries the facts beside a piece of the brief and the
    question names them; without research the state and the question are as before."""
    research = ["[F:f-1 v1] brand calls: 350000 count (brand research)"]
    f = jev(FakeJev(noul=lambda n, q: 0.96))
    assert jc.figures_supported(BRIEF, ["350,000 calls a year [F:f-1]"], research=research) == [0.96]
    state, qs = f.calls[0]
    assert state == {"client_brief": BRIEF, "verified_research_facts": research[0]}
    assert "verified research facts" in qs["f000"]["instructions"]["question"]
    f.calls.clear()
    jc.figures_supported(BRIEF, ["2 million packs"])
    assert f.calls[0][0] == {"client_brief": BRIEF} and f.calls[0][1]["f000"]["instructions"]["question"] == jc.FIGURE_Q


def test_many_research_facts_are_all_shown(jev):
    """More fact lines than fit in one slot go in further states; none is cut off."""
    research = [f"[F:f-{i} v1] brand fact {i}: {i} count " + "x" * 400 for i in range(80)]
    f = jev(FakeJev(noul=lambda n, q: 0.9))
    jc.figures_supported(BRIEF, ["2 million packs"], research=research)
    shown = "\n".join(st["verified_research_facts"] for st, _ in f.calls)
    assert all(r in shown for r in research) and len(f.calls) > 1
    assert all(len(st["verified_research_facts"]) <= jc.RESEARCH_CHARS for st, _ in f.calls)


def test_conflicts_answers_and_claims_take_the_whole_brief(jev):
    """fact_conflicts and questions_answered take the highest probability over the pieces;
    claims_supported keeps support found in any piece over not_in_brief elsewhere."""
    long_brief = "Filler line.\n" * 6000 + "The budget is EUR 140,000.\n"
    def by_piece(n, q):
        """High only for the piece that holds the budget."""
        return 0.95 if "140,000" in f.calls[-1][0]["client_brief"] else 0.05
    f = jev(FakeJev(noul=by_piece))
    assert jc.questions_answered(long_brief, ["What is the budget?"]) == [0.95]
    assert jc.fact_conflicts(long_brief, ["[F:f-1] budget: 90000 EUR"]) == [0.95]
    f2 = jev(FakeJev(choice=lambda n, q: ("supported", 0.9) if "140,000" in f2.calls[-1][0]["client_brief"]
                     else ("not_in_brief", 0.97)))
    assert jc.claims_supported(long_brief, ["The budget is EUR 140,000"]) == [("supported", 0.9)]


def test_the_gate_gives_jev_the_brief_and_the_research_apart(monkeypatch):
    """_judge_and_gate passes the whole brief text and the run's fact lines to the figure
    check, not the brief with the facts pasted on its end."""
    import jev_checks
    seen = {}
    def fake(text, items, research=None):
        """Records what the gate sent."""
        seen.update(text=text, research=research)
        return [0.97] * len(items)
    monkeypatch.setattr(jev_checks, "figures_supported", fake)
    facts = {"f-1": {"id": "f-1", "version": 1, "entity": "brand", "key": "calls", "value": 350000, "unit": "count"}}
    pb._jev_figure_failures(FIELD["reasons_to_believe"], [{"value": ["350,000 calls [F:f-1]"]}],
                            BRIEF + "\n[F:f-1 v1] ...", brief_text=BRIEF,
                            research=[pb._facts_mod.line(f) for f in facts.values()])
    assert seen["text"] == BRIEF and seen["research"][0].startswith("[F:f-1 v1]")
