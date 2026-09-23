"""Plan step 5 in parse_brief.loops_3_7: dedupe before the cut (loop5 fix), validation-chain
ordering without drops, one synthesis call per loop in parallel, the BRIEF_FULLTEXT arm.
Offline: model calls and the chain are faked.
Run: cd engine/rag && RAG_STORE=local RAG_INDEX=./_index_v3 python3 -m pytest -q test_loops_step5.py
"""
from __future__ import annotations
import sys
import threading
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import parse_brief as pb  # noqa: E402
import brief_context  # noqa: E402
import judge  # noqa: E402
import judge_base as jb  # noqa: E402


def h(src, text="t"):
    """A retrieve()-shaped hit from `src`."""
    return {"source": src, "text": text, "header": "", "citation": f"{src} › s"}


def test_dedupe_by_source_keeps_the_best_ranked_per_source_before_the_cut():
    """Three sections of one playbook used to fill the top k and collapse to one item."""
    pool = [h("smp"), h("smp"), h("smp"), h("bbh"), h("tpl")]
    assert [x["source"] for x in pb._dedupe_by_source(pool)] == ["smp", "bbh", "tpl"]


class Scorer:
    """Fake backend: probability from the passage text, one call recorded per score()."""
    name, capacity, calibrated = "fake", 40, True

    def __init__(self):
        """No calls yet."""
        self.calls = 0

    def score(self, query, passages, *, deadline_s):
        """Score = the number in the passage text / 10; everything below 0.5 fails."""
        self.calls += 1
        return [jb.Verdict(float(p.text.strip()) / 10 >= 0.5, float(p.text.strip()) / 10, None, "fake")
                for p in passages]


def test_chain_order_sorts_by_score_and_never_drops(monkeypatch):
    """With a configured chain, one call orders the hits; failing hits stay, just lower."""
    be = Scorer()
    monkeypatch.setattr(brief_context, "default_chain", lambda: judge.Chain([be]))
    hits = [h("a", "2"), h("b", "9"), h("c", "5")]
    out = pb._chain_order("q", hits)
    assert [x["source"] for x in out] == ["b", "c", "a"] and be.calls == 1


def test_chain_order_defers_to_the_llm_rerank_when_off(monkeypatch):
    """No RAG_VALIDATOR: None, so _rerank_hits uses its existing LLM path."""
    monkeypatch.setattr(brief_context, "default_chain", lambda: judge.Chain([]))
    assert pb._chain_order("q", [h("a"), h("b")]) is None


def test_synthesis_is_one_call_per_loop_in_parallel(monkeypatch):
    """Five loops, five concurrent calls, same output shape; a loop whose call returns
    nothing falls back to the evidence-only summary."""
    seen, lock = [], threading.Lock()
    def fake(user, **kw):
        """Answer per loop; loop7 gets no paragraph."""
        with lock:
            seen.append(threading.current_thread().name)
        return {} if "loop7" in user else {"paragraph": "grounded para"}
    monkeypatch.setattr(pb, "_json_call", fake)
    loops = {k: {"title": t, "evidence": [{"citation": "x › y", "snippet": "s", "framework": "F"}]}
             for k, t, _q in pb.LOOP37_SPECS}
    gist = {"problem": "p", "objective": "o", "audience": "a", "key_message": "k"}
    pb._synthesize_loops37(gist, "general-strategy", loops)
    assert len(seen) == 5
    assert loops["loop3_research"]["synthesis"] == "grounded para"
    assert loops["loop7_qa"]["synthesis"].startswith("Apply, in order of fit")


def test_fulltext_arm_feeds_the_generator_the_evidence_span(monkeypatch):
    """BRIEF_FULLTEXT=1 reads `text` (capped) instead of the 220-char snippet clip."""
    loops = {"loop4_insight": {"evidence": [{"snippet": "short", "text": "L" * 3000,
                                             "category": "ipa_effectiveness_case", "source": "ipa_1"}]}}
    monkeypatch.delenv("BRIEF_FULLTEXT", raising=False)
    assert len(pb._precedent_blocks(loops, "loop4_insight")[0]) == len("- short")
    monkeypatch.setenv("BRIEF_FULLTEXT", "1")
    assert len(pb._precedent_blocks(loops, "loop4_insight")[0]) == len("- ") + pb.FULLTEXT_IPA_CHARS


def test_rag_path_mix_is_the_default_and_keeps_the_generator_shape(monkeypatch):
    """Default RAG_PATH=mix: loops_3_7 evidence comes from build_multi, in the shape
    fill_derivable_fields reads (citation, framework, category=doc_kind, snippet, text)."""
    monkeypatch.delenv("RAG_PATH", raising=False)
    calls = {}
    def fake_multi(pairs, queries, index_dir=None):
        """Record the queries; return one IPA hit for loop4 only."""
        calls["queries"] = queries
        hit = brief_context.Hit(cite="ipa_0003", doc_id="ipa_0003", source="ipa", bucket="exemplars",
                                title="Case", section="Insight", header="H", text="insight text", score=0.9,
                                metadata={"doc_kind": "ipa_effectiveness_case", "scope": "global"})
        return brief_context.MultiContext(fields={"loop4_insight": [hit]}, trace={"calls": {"embed": 1}})
    monkeypatch.setattr(brief_context, "build_multi", fake_multi)
    gist = {"problem": "p", "objective": "o", "audience": "a", "key_message": ""}
    loops, trace = pb._loops_via_mix(gist, {}, None)
    assert set(calls["queries"]) == {k for k, _t, _q in pb.LOOP37_SPECS}
    ev = loops["loop4_insight"]["evidence"][0]
    assert ev["citation"] == "ipa_0003 › Insight" and ev["category"] == "ipa_effectiveness_case"
    assert "insight text" in pb._precedent_blocks(loops, "loop4_insight")[0]    # reaches the generator

