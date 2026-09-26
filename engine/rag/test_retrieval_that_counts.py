"""Batch 3 of the 2026-09-24 audit fix plan — "retrieval that counts" (ADR 0003, addendum
2026-09-25): the validator's result reaches the evidence, the validator sees the brief and
asks a bucket-specific question, jev gets a deadline it can meet, and dedupe favours the
fields a writer reads. Offline: stores, embeddings and backends are faked.
Run: cd engine/rag && python3 -m pytest -q test_retrieval_that_counts.py
"""
from __future__ import annotations
import sys
import time
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import pytest  # noqa: E402
import brief_context as bc  # noqa: E402
import judge  # noqa: E402
import judge_base as jb  # noqa: E402
import judge_jev as jj  # noqa: E402
import rag  # noqa: E402
from test_brief_validation import FakeBackend, hit  # noqa: E402


def _store(monkeypatch, per_bucket):
    """Fake store and embedding; `per_bucket` maps bucket -> list of cite ids served for
    EVERY field (fresh Hit objects per call, as a real search would return)."""
    monkeypatch.setattr(rag, "open_store", lambda *a, **k: None)
    monkeypatch.setattr(rag, "embed", lambda texts, t="passage": ([[1.0]] * len(texts), "nim:x"))

    def bucket_hits(store, qvec, text, bucket, *a, **k):
        """The bucket's hits, fresh objects."""
        hs = [hit(c, bucket, client=c) for c in per_bucket.get(bucket, [])]
        for h in hs:
            h.text = "x" * 200
        return hs, {}
    monkeypatch.setattr(bc, "_bucket_hits", bucket_hits)


# ---------- RAG-1: the validator's result reaches the evidence ----------

def test_order_mode_reordering_is_served(monkeypatch):
    """A backend that scores e5 highest changes what the fill serves. Before 2026-09-25 the
    validated buckets were a discarded copy and [e1, e2] was served whatever the judge said."""
    _store(monkeypatch, {"exemplars": ["e1", "e2", "e3", "e4", "e5"]})
    monkeypatch.setenv("RAG_VALIDATION_MODE", "order")
    be = FakeBackend(scores={"e1": 0.1, "e2": 0.2, "e3": 0.3, "e4": 0.4, "e5": 0.99})
    mc = bc.build_multi({"problem": "p"}, {"f": "q"}, chain=judge.Chain([be]), per_field=2)
    assert [h.cite for h in mc.fields["f"]] == ["e5", "e4"]
    assert mc.fields["f"][0].relevance["kept"] == "ordered"


def test_validator_on_vs_off_changes_the_order(monkeypatch):
    """Regression guard for RAG-1 (audit critic-G14): same fixed queries, validator on vs
    off must differ in at least one field."""
    _store(monkeypatch, {"exemplars": ["e1", "e2", "e3"]})
    monkeypatch.setenv("RAG_VALIDATION_MODE", "order")
    off = bc.build_multi({"problem": "p"}, {"f": "q"}, chain=judge.Chain([]), per_field=3)
    on = bc.build_multi({"problem": "p"}, {"f": "q"}, per_field=3,
                        chain=judge.Chain([FakeBackend(scores={"e1": 0.1, "e2": 0.5, "e3": 0.9})]))
    assert [h.cite for h in off.fields["f"]] == ["e1", "e2", "e3"]
    assert [h.cite for h in on.fields["f"]] == ["e3", "e2", "e1"]


def test_gate_mode_drops_are_served(monkeypatch):
    """In gate mode a rejected hit is no longer served."""
    _store(monkeypatch, {"exemplars": ["e1", "e2", "e3", "e4", "e5"]})
    monkeypatch.setenv("RAG_VALIDATION_MODE", "gate")
    be = FakeBackend(scores={"e1": 0.01, "e2": 0.02, "e3": 0.9, "e4": 0.9, "e5": 0.9})
    mc = bc.build_multi({"problem": "p"}, {"f": "q"}, chain=judge.Chain([be]), per_field=5)
    assert [h.cite for h in mc.fields["f"]] == ["e3", "e4", "e5"]


def test_admission_exclusion_is_served(monkeypatch):
    """An excluded doc id never reaches the evidence, and the refusal is recorded."""
    _store(monkeypatch, {"exemplars": ["e1", "e2", "e3"]})
    monkeypatch.setenv("RAG_VALIDATION_MODE", "order")
    mc = bc.build_multi({"problem": "p"}, {"f": "q"}, chain=judge.Chain([]), per_field=5,
                        admission={"exclude_doc_ids": ["e1"]})
    assert [h.cite for h in mc.fields["f"]] == ["e2", "e3"]
    assert mc.trace["validation"]["per_field"]["f"]["admission_refused"][0]["cite"] == "e1"


# ---------- JL-7: order mode reports no rejections ----------

def test_order_mode_contract_reports_no_rejections(monkeypatch):
    """The contract's rejected is 0 in order mode (nothing is dropped) and passed counts
    the ordered hits, matching per_bucket; the trace no longer reports phantom rejections."""
    monkeypatch.setenv("RAG_VALIDATION_MODE", "order")
    hb = {"exemplars": [hit("e1", "exemplars"), hit("e2", "exemplars")], "craft": [hit("c1", "craft")]}
    rec = bc._apply_validation(hb, judge.Chain([FakeBackend(scores={"e1": 0.1, "e2": 0.2, "c1": 0.1})]), query="q")
    assert rec["contract"]["rejected"] == 0 and rec["contract"]["passed"] == 3
    assert all(c["rejected"] == 0 for c in rec["per_bucket"].values())


# ---------- JL-5: the validator sees the brief ----------

def test_build_multi_forwards_context_to_the_judge(monkeypatch):
    """`context` reaches chain.judge as Query.context, clipped to CONTEXT_MAX_CHARS."""
    _store(monkeypatch, {"exemplars": ["e1"]})
    seen = []

    class Rec(FakeBackend):
        """Records the Query it was asked."""
        def score(self, query, passages, *, deadline_s):
            seen.append(query)
            return super().score(query, passages, deadline_s=deadline_s)
    bc.build_multi({"problem": "p"}, {"f": "q"}, chain=judge.Chain([Rec()]), context="brief gist " * 1000)
    assert seen and seen[0].text == "q" and seen[0].context.startswith("brief gist")
    assert len(seen[0].context) == bc.CONTEXT_MAX_CHARS


def test_loops_via_mix_passes_the_brief_gist_as_context(monkeypatch):
    """parse_brief hands build_multi the gist plus background and competitors."""
    import types
    import parse_brief as pb
    monkeypatch.setattr(pb, "_load_retriever", lambda: None)
    seen = {}

    def build_multi(pairs, queries, **kw):
        """Records the context."""
        seen.update(kw)
        return types.SimpleNamespace(fields={}, trace={})
    monkeypatch.setattr(bc, "build_multi", build_multi)
    gist = {"problem": "under-30s ignore the app", "objective": "grow sign-ups", "audience": "under-30s", "key_message": ""}
    fields = {"background_context": {"value": "Acme relaunched its app"}, "competitors_market": {"value": "RivalBank"}}
    _loops, trace = pb._loops_via_mix(gist, fields)
    ctx = seen["context"]
    assert "problem: under-30s ignore the app" in ctx and "competitors: RivalBank" in ctx
    assert "background: Acme relaunched its app" in ctx and "key_message" not in ctx
    assert trace["validator_context_chars"] == len(ctx)
    assert any("no category filter" in n for n in trace["notes"])


# ---------- JL-6: one question per bucket ----------

def test_jev_asks_a_bucket_specific_question():
    """Each bucket gets its own Noul question and criteria; unknown buckets keep the generic one."""
    q = jj.build_question("passage", "craft")
    assert q["instructions"]["question"] == "Is PASSAGE a planning method that applies to this brief?"
    assert "method" in q["criteria"]["true"]
    assert jj.build_question("passage", "rules")["instructions"]["question"].startswith("Is PASSAGE a rule")
    assert jj.build_question("passage", "exemplars")["instructions"]["question"].startswith("Is PASSAGE a comparable precedent")
    assert jj.build_question("passage")["instructions"]["question"] == jj.QUESTION
    assert jj.build_question("passage", "other")["criteria"] == jj.CRITERIA


def test_jev_score_sends_the_passages_bucket_question():
    """score() plans each passage's question from Passage.bucket."""
    sent = []

    class Client:
        """Answers every Noul with 0.7 and records the questions."""
        def system_one(self, state, questions, *, model, timeout):
            sent.append(questions)
            import types
            return types.SimpleNamespace(
                nouls={name: types.SimpleNamespace(noul=0.7) for name in questions},
                model=model, usage=types.SimpleNamespace(input_tokens=10))
    b = jj.JevBackend(Client(), env={})
    vs = b.score(jb.Query("q"), [jb.Passage("a", "text a", "craft"), jb.Passage("b", "text b", "exemplars")],
                 deadline_s=5)
    assert [v.score for v in vs] == [0.7, 0.7]
    qs = sent[0]
    assert qs["p000"]["instructions"]["question"].startswith("Is PASSAGE a planning method")
    assert qs["p001"]["instructions"]["question"].startswith("Is PASSAGE a comparable precedent")


def test_apply_validation_hands_the_bucket_to_the_backend():
    """_apply_validation builds Passages with the hit's bucket."""
    seen = []

    class Rec(FakeBackend):
        """Records the passages."""
        def score(self, query, passages, *, deadline_s):
            seen.extend(passages)
            return super().score(query, passages, deadline_s=deadline_s)
    bc._apply_validation({"exemplars": [hit("e1", "exemplars")], "craft": [hit("c1", "craft")]},
                         judge.Chain([Rec()]), query="q")
    assert {p.id: p.bucket for p in seen} == {"e1": "exemplars", "c1": "craft"}


# ---------- JL-1: jev's deadline and warm-up ----------

def test_jev_declares_an_eight_second_deadline_the_chain_honours():
    """jev's own deadline is 8 s (RAG_JEV_DEADLINE_S); without an explicit chain deadline
    the chain uses it, an explicit RAG_VALIDATOR_DEADLINE_S still caps it."""
    class Client:
        """Never called."""
    b = jj.JevBackend(Client(), env={})
    assert b.deadline_s == 8.0
    assert jj.JevBackend(Client(), env={"RAG_JEV_DEADLINE_S": "5"}).deadline_s == 5.0
    with pytest.raises(jb.BackendNotConfigured):
        jj.JevBackend(Client(), env={"RAG_JEV_DEADLINE_S": "0"})
    chain = judge.Chain([b])
    assert chain._deadline_for(b) == 8.0
    assert judge.Chain([b], deadline_s=3.0)._deadline_for(b) == 3.0


def test_a_slow_backend_within_its_own_deadline_is_still_used():
    """A backend that declares 8 s and answers in 0.4 s is used although the chain
    default is 3 s only for backends that declare nothing."""
    class Slow(FakeBackend):
        """Declares its own deadline; answers after a short sleep."""
        deadline_s = 8.0
        def score(self, query, passages, *, deadline_s):
            assert deadline_s == 8.0
            time.sleep(0.05)
            return super().score(query, passages, deadline_s=deadline_s)
    res = judge.Chain([Slow()]).judge(jb.Query("q"), [jb.Passage("a", "t")])
    assert res.backend_used == "fake" and not res.fell_back


def test_warm_validator_calls_each_backends_warm_and_never_raises(monkeypatch):
    """warm_validator() calls warm() where a backend has one; failures return False."""
    class Warmable(FakeBackend):
        """A backend with a warm-up."""
        def __init__(self, ok):
            super().__init__(); self.ok, self.warmed = ok, 0
        def warm(self):
            self.warmed += 1
            if not self.ok:
                raise RuntimeError("cold")
            return True
    good, bad = Warmable(True), Warmable(False)
    bad.name = "bad"
    monkeypatch.setattr(bc, "brief_chain", lambda: judge.Chain([good, bad]))
    assert bc.warm_validator() == {"fake": True, "bad": False} and good.warmed == 1
    monkeypatch.setattr(bc, "brief_chain", lambda: judge.Chain([]))
    assert bc.warm_validator() == {}


def test_brief_chain_defaults_to_jev_and_respects_an_explicit_setting(monkeypatch):
    """Sai, 2026-09-26: jev on for every brief. RAG_VALIDATOR unset -> jev; 'none' -> off;
    an explicit list is honoured. rag_io's default_chain keeps unset-means-off."""
    monkeypatch.setattr(judge, "chain_from_env", lambda env=None: judge.Chain([], requested=env.get("RAG_VALIDATOR")))
    bc.brief_chain.cache_clear()
    monkeypatch.delenv("RAG_VALIDATOR", raising=False)
    assert bc.brief_chain().requested == "jev"
    bc.brief_chain.cache_clear()
    monkeypatch.setenv("RAG_VALIDATOR", "none")
    assert bc.brief_chain().requested == "none"
    bc.brief_chain.cache_clear()
    monkeypatch.setenv("RAG_VALIDATOR", "nemotron,jev")
    assert bc.brief_chain().requested == "nemotron,jev"
    bc.brief_chain.cache_clear()


def test_unconfigured_validator_runs_the_brief_unvalidated_and_says_so(monkeypatch, capsys):
    """jev asked for but not configured: the brief still runs, every loop is flagged
    unvalidated, and stderr says so (never a silent fallback)."""
    import types
    import parse_brief as pb
    import judge_base as jb
    monkeypatch.setattr(pb, "_load_retriever", lambda: None)

    def boom():
        raise jb.BackendNotConfigured("jev: TYPESAFE_API_KEY is not set")
    monkeypatch.setattr(bc, "brief_chain", boom)
    seen = {}

    def build_multi(pairs, queries, **kw):
        seen.update(kw)
        return types.SimpleNamespace(fields={}, trace={})
    monkeypatch.setattr(bc, "build_multi", build_multi)
    loops, trace = pb._loops_via_mix({"problem": "p", "objective": "", "audience": "", "key_message": ""}, {})
    assert seen["chain"].empty and "TYPESAFE_API_KEY" in trace["validator_unconfigured"]
    assert len(trace["validation_degraded"]) == 5 and all(l["validated_by"] is None for l in loops.values())
    assert "UNVALIDATED" in capsys.readouterr().err


def test_jev_score_calls_from_several_threads_run_one_at_a_time():
    """Five concurrent score() calls (build_multi's five fields) never overlap on the
    client: the checkpoint run of 2026-09-26 saw SSL 'bad record MAC' errors on 3 of 5
    fields with 20 requests in flight on one client."""
    import threading
    import types
    in_flight, peak, lock = [0], [0], threading.Lock()

    class Client:
        """Counts overlapping calls; answers every Noul with 0.6."""
        def system_one(self, state, questions, *, model, timeout):
            with lock:
                in_flight[0] += 1; peak[0] = max(peak[0], in_flight[0])
            time.sleep(0.02)
            with lock:
                in_flight[0] -= 1
            return types.SimpleNamespace(nouls={n: types.SimpleNamespace(noul=0.6) for n in questions},
                                         model=model, usage=types.SimpleNamespace(input_tokens=1))
    b = jj.JevBackend(Client(), env={}, concurrency=1)
    out = {}
    def one(i):
        out[i] = b.score(jb.Query(f"q{i}"), [jb.Passage(f"p{i}", "t", "craft")], deadline_s=5)
    ts = [threading.Thread(target=one, args=(i,)) for i in range(5)]
    [t.start() for t in ts]; [t.join(5) for t in ts]
    assert len(out) == 5 and all(v[0].score == 0.6 for v in out.values())
    assert peak[0] == 1


def test_jev_warm_makes_one_tiny_request_and_swallows_failures():
    """JevBackend.warm() sends one request; a failing client gives False, not an exception."""
    calls = []

    class Client:
        """Fails."""
        def system_one(self, state, questions, *, model, timeout):
            calls.append(questions)
            raise RuntimeError("down")
    assert jj.JevBackend(Client(), env={}).warm() is False and len(calls) == 1


# ---------- RAG-7: dedupe favours the fields a writer reads ----------

def test_dedupe_lets_the_writer_fields_claim_shared_hits_first(monkeypatch):
    """A hit both loop3_research and loop4_insight retrieved goes to loop4 (first in
    MIX_DEDUPE_FIRST) although loop3 comes first in the queries; output order is the caller's."""
    _store(monkeypatch, {"exemplars": ["shared", "e2"]})
    queries = {"loop3_research": "q3", "loop4_insight": "q4"}
    mc = bc.build_multi({"problem": "p"}, queries, chain=judge.Chain([]), per_field=5)
    assert list(mc.fields) == ["loop3_research", "loop4_insight"]
    assert [h.cite for h in mc.fields["loop4_insight"]] == ["shared", "e2"]
    assert [h.cite for h in mc.fields["loop3_research"]] == []
    mc = bc.build_multi({"problem": "p"}, queries, chain=judge.Chain([]), per_field=5, dedupe_first=())
    assert [h.cite for h in mc.fields["loop3_research"]] == ["shared", "e2"]
