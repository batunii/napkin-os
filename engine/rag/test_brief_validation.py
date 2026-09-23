"""Validation wired into brief retrieval (plan steps 3-4): one combined call per brief,
per-bucket floor, exempt reviewer rejections, admission rules, edge ordering.
Run: cd engine/rag && RAG_STORE=local RAG_INDEX=./_index_v3 python3 -m pytest -q test_brief_validation.py
"""
from __future__ import annotations
import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import brief_context as bc  # noqa: E402
import judge  # noqa: E402
import judge_base as jb  # noqa: E402
import rag_io  # noqa: E402


class FakeBackend:
    """Scores passages from a cite -> probability map (0.9 default) and records calls."""
    name, calibrated = "fake", True

    def __init__(self, scores=None, capacity=40, threshold=0.5):
        """`scores` maps cite id -> probability; `threshold` decides value."""
        self.scores, self.capacity, self.threshold, self.calls = scores or {}, capacity, threshold, []

    def score(self, query, passages, *, deadline_s):
        """One verdict per passage, input order; records the call's passage ids."""
        self.calls.append([p.id for p in passages])
        return [jb.Verdict(self.scores.get(p.id, 0.9) >= self.threshold, self.scores.get(p.id, 0.9),
                           None, self.name) for p in passages]


def hit(cite, bucket, **md):
    """A Hit in `bucket` with global/house scope and extra metadata."""
    return bc.Hit(cite=cite, doc_id=cite, source="ipa", bucket=bucket, title=cite, section="s",
                  header="", text="body " + cite, score=0.5,
                  metadata={"scope": "global", "tenant": "house", **md})


def buckets():
    """Three hits in exemplars, two in craft, a rejection and a pitfall in rules."""
    return {"exemplars": [hit("e1", "exemplars"), hit("e2", "exemplars"), hit("e3", "exemplars")],
            "craft": [hit("c1", "craft"), hit("c2", "craft")],
            "rules": [hit("r_rej", "rules", verdict="rejected"), hit("r_pit", "rules")],
            "instructions": []}


def test_off_when_the_chain_is_empty_and_nothing_changes():
    """No validator and no admission rules: no record, hits untouched (today's behaviour)."""
    hb = buckets()
    assert bc._apply_validation(hb, judge.Chain([]), query="q") is None
    assert [h.cite for h in hb["exemplars"]] == ["e1", "e2", "e3"]
    assert all(h.relevance is None for hs in hb.values() for h in hs)


def test_one_combined_call_per_brief_interleaved_by_rank():
    """Every bucket's candidates go in ONE call, round-robin by rank, so a small capacity
    still sees the best of each bucket. Reviewer rejections are not sent."""
    be = FakeBackend()
    bc._apply_validation(buckets(), judge.Chain([be]), query="q")
    assert len(be.calls) == 1
    assert be.calls[0] == ["e1", "c1", "r_pit", "e2", "c2", "e3"]


def test_capacity_truncation_is_fair_across_buckets():
    """Capacity 3 judges the first of every non-empty bucket, not three exemplars."""
    be = FakeBackend(capacity=3)
    rec = bc._apply_validation(buckets(), judge.Chain([be]), query="q")
    assert be.calls[0] == ["e1", "c1", "r_pit"] and rec["truncated"] == 3


def test_sorted_by_score_rejects_dropped_and_floor_per_bucket():
    """Passing hits come back by score (this IS the rerank); a bucket where everything
    failed keeps its top two flagged, and the prompt marks them as weak."""
    be = FakeBackend(scores={"e1": 0.6, "e2": 0.95, "e3": 0.1, "c1": 0.2, "c2": 0.3})
    hb = buckets()
    rec = bc._apply_validation(hb, judge.Chain([be]), query="q")
    assert [h.cite for h in hb["exemplars"]] == ["e2", "e1"]
    assert [(h.cite, h.relevance["kept"]) for h in hb["craft"]] == [("c2", "floor"), ("c1", "floor")]
    assert "weak match" in hb["craft"][0].render()
    assert rec["per_bucket"]["exemplars"] == {"passed": 2, "rejected": 1, "beyond_share": 0}
    assert rec["calls"] == 1


def test_reviewer_rejections_are_exempt_from_the_relevance_gate():
    """A human's never-do-this is kept whatever the validator thinks, and marked exempt."""
    hb = buckets()
    bc._apply_validation(hb, judge.Chain([FakeBackend(scores={"r_pit": 0.0})]), query="q")
    assert hb["rules"][0].cite == "r_rej" and hb["rules"][0].relevance["kept"] == "exempt"


def test_admission_runs_first_and_is_recorded():
    """Excluded doc ids are refused before judging, even with validation off."""
    hb = buckets()
    rec = bc._apply_validation(hb, judge.Chain([]), query="q", admission={"exclude_doc_ids": ["e2"]})
    assert [h.cite for h in hb["exemplars"]] == ["e1", "e3"]
    assert rec == {"admission_refused": [{"cite": "e2", "bucket": "exemplars", "reason": "excluded"}]}


def test_nobody_answering_keeps_fused_order_unjudged():
    """Every backend down: fall back to fused order, all hits kept as unjudged."""
    class Down(FakeBackend):
        """Always unavailable."""
        def score(self, query, passages, *, deadline_s):
            """Raise a run-time failure."""
            raise jb.BackendUnavailable("down", kind="http_error")
    hb = buckets()
    rec = bc._apply_validation(hb, judge.Chain([Down()]), query="q")
    assert [h.cite for h in hb["exemplars"]] == ["e1", "e2", "e3"]
    assert {h.relevance["kept"] for h in hb["exemplars"]} == {"unjudged"} and rec["fell_back"]


def test_edge_order_puts_the_strongest_at_both_ends():
    """[1..6] -> [1,3,5,6,4,2]; short lists are unchanged."""
    assert bc.edge_order([1, 2, 3, 4, 5, 6]) == [1, 3, 5, 6, 4, 2]
    assert bc.edge_order([1, 2]) == [1, 2] and bc.edge_order([]) == []


def test_response_carries_relevance_and_validation_in_contract_shape():
    """rag_io maps each hit's relevance and the run's contract record, and the response
    validates against rag_io v1.1."""
    hb = buckets()
    rec = bc._apply_validation(hb, judge.Chain([FakeBackend(scores={"c1": 0.1, "c2": 0.2})]), query="q")
    blocks = {b: bc.Block(b, hs, budget=1000) for b, hs in hb.items()}
    ctx = bc.BriefContext(blocks=blocks, query="q", keywords=[], filters={}, validation=rec)
    resp = rag_io.response_from(ctx, "r-1")
    assert rag_io.validate(resp, "response") == []
    assert resp["validation"]["backend_used"] == "fake"
    craft = next(b for b in resp["blocks"] if b["bucket"] == "craft")
    assert {h["relevance"]["kept"] for h in craft["hits"]} == {"floor"}
