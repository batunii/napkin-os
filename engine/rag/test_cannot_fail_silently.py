"""Batch 1 of the 2026-09-24 audit fix plan — "cannot fail silently" (ADR 0006).

Before this batch several errors passed as success: a gate that could not fail, a judge
that passed when it broke, a crash that quietly stripped the strategy, a non-Claude model
answering under a Claude label. Each test here pins one of those doors shut. Offline:
every model call is faked at _json_call / _call_link level.
Run: cd engine/rag && python3 -m pytest -q test_cannot_fail_silently.py
"""
from __future__ import annotations
import json
import re
import sys
import threading
import types
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import pytest  # noqa: E402
import parse_brief as pb  # noqa: E402
import golden_critic as gc  # noqa: E402
import toon_lite  # noqa: E402

GOLDEN_SCHEMA = json.loads((HERE.parent / "golden-brief" / "golden_brief.schema.json").read_text())
FIELD = {f["id"]: f for f in GOLDEN_SCHEMA["fields"]}
BRIEF = ("Acme Bank is relaunching its app. Under-30s see it as their parents' bank. "
         "We must grow sign-ups by 20 percent. Every asset must carry the disclaimer. "
         "Our proof: 2 million customers already use the app every week.")


def _pass_all(user: str) -> dict:
    """A judge reply that passes every candidate on every test named in the prompt."""
    n = len(re.findall(r"^\[\d+\] ", user, flags=re.M))
    tests = re.findall(r"^- ([a-z0-9_]+):", user.split("TESTS", 1)[-1], flags=re.M)
    return {"ranking": list(range(n)), "why": "w",
            "results": {str(i): {t: {"pass": True} for t in tests} for i in range(n)}}


def _fake_models(monkeypatch, calls=None, conf=0.9, territory="ok", gen_raises=None, refine=None):
    """fill_derivable_fields' model calls, answered by kind. `conf` is the confidence every
    draft reports; territory=None makes the territory map fail; gen_raises names a field
    whose generation raises; refine=None returns the default sharpen reply."""
    calls = calls if calls is not None else []
    label = {f["id"]: f["label"] for f in GOLDEN_SCHEMA["fields"]}

    def fake(user, system=None, **kw):
        """Answer by what the prompt asks for; record the kind and the prompt."""
        system = system or ""
        if system.startswith("You map strategic white space"):
            kind, out = "territory", ({"rival": "R", "own": "O", "avoid": "A"} if territory else None)
        elif '"candidates"' in system:
            kind, out = "gen_batch", {"candidates": [{"value": f"draft {i}, because it holds", "confidence": conf}
                                                     for i in range(4)]}
        elif "REFINE MODE" in system:
            kind, out = "refine", ({"value": "refined line, because it holds", "confidence": conf}
                                   if refine is None else refine)
        elif "judging candidate" in system:
            kind, out = "judge_batch", _pass_all(user)
        elif '"think"' in system:
            kind, out = "gen", {"value": {"think": "a", "feel": "b", "do": "open the app"}, "confidence": conf}
        else:
            kind, out = "gen", {"value": ["2 million weekly users", "Free for under-30s"], "confidence": conf}
        if gen_raises and kind == "gen" and f"'{label[gen_raises]}'" in user:
            raise ValueError("boom")
        calls.append((kind, user, system))
        return out
    monkeypatch.setattr(pb, "_json_call", fake)
    monkeypatch.setattr(pb, "resolve_provider", lambda: "fake")
    monkeypatch.setenv("BRIEF_PARALLEL", "0")
    return calls


def _fill(monkeypatch, extra=None, brief_text=BRIEF):
    """Run fill_derivable_fields on a golden brief with the client facts plus `extra`."""
    gf = {"audience": {"value": "under-30s", "source": "client_stated"},
          "background": {"value": "app relaunch", "source": "client_stated"},
          "competitor_context": {"value": "RivalBank", "source": "client_stated"}, **(extra or {})}
    fills, qs = pb.fill_derivable_fields(gf, {"loops": {}}, GOLDEN_SCHEMA, brief_text=brief_text)
    return gf, fills, qs


# ---------- change 1: one gate that can actually fail (F1 / G1 / C1 / J8) ----------

def test_single_llm_test_field_fails_on_its_llm_verdict(monkeypatch):
    """The RTB (one llm test, supports_smp) and the desired response (ladders) FAIL when
    their judge says fail. Before: `len(soft) < 2` let every one-test field through."""
    monkeypatch.setattr(pb, "_json_call", lambda *a, **k: {"ranking": [0], "results": {
        "0": {"supports_smp": {"pass": False, "why": "unrelated"}, "ladders": {"pass": False, "why": "x"}}}})
    (_c, ok, fails), = pb._judge_and_gate(FIELD["reasons_to_believe"], [{"value": ["a", "b"]}])
    assert ok is False and fails == ["supports_smp: unrelated"]
    (_c, ok, _f), = pb._judge_and_gate(FIELD["desired_response"],
                                       [{"value": {"think": "a", "feel": "b", "do": "buy"}}])
    assert ok is False


def test_pass_rule_tolerates_one_soft_failure_only_with_three_or_more_tests():
    """Sai, 2026-09-25: insight and SMP (3 llm tests) keep one-fail tolerance; fewer tests, none."""
    assert pb._pass_rule([], ["a: x"], 3) is True
    assert pb._pass_rule([], ["a: x", "b: y"], 3) is False
    assert pb._pass_rule([], ["a: x"], 2) is False
    assert pb._pass_rule([], ["a: x"], 1) is False
    assert pb._pass_rule(["within_limit: 31/30"], [], 3) is False


def test_the_old_gates_are_gone():
    """One gate implementation: the per-candidate rubric gate, the ranking-only hero judge,
    the separate territory gate and the BRIEF_BATCH_GATES switch no longer exist."""
    for name in ("_rubric_gate", "_judge_hero_candidates", "_smp_territory_gate"):
        assert not hasattr(pb, name), name
    src = Path(pb.__file__).read_text()
    assert "BRIEF_BATCH_GATES" not in src


def test_non_hero_field_goes_through_the_batched_judge(monkeypatch):
    """An RTB is judged by _judge_and_gate (the 'judging candidate' prompt), never a rubric gate."""
    calls = _fake_models(monkeypatch)
    _gf, fills, _qs = _fill(monkeypatch)
    rtb_judges = [u for k, u, s in calls if k == "judge_batch" and "Reasons to believe" in u]
    assert "reasons_to_believe" in fills and rtb_judges and "supports_smp" in rtb_judges[0]


def test_list_and_tfd_fields_are_not_judged_as_hero_lines(monkeypatch):
    """The judge's framing follows the field's shape: an RTB list is judged as a set and
    never failed for being a list; a think/feel/do set likewise; a hero line keeps the
    purity / single-mindedness framing (live check 2026-09-25)."""
    seen = {}
    def judge(user, system=None, **k):
        """Test stub: stands in for `judge` in test_list_and_tfd_fields_are_not_judged_as_hero_lines."""
        seen[k.get("_field") or system[:60]] = system
        return _pass_all(user)
    monkeypatch.setattr(pb, "_json_call", judge)
    pb._judge_and_gate(FIELD["reasons_to_believe"], [{"value": ["a", "b", "c", "d"]}])
    pb._judge_and_gate(FIELD["desired_response"], [{"value": {"think": "t", "feel": "f", "do": "d"}}])
    pb._judge_and_gate(FIELD["insight"], [{"value": "one"}, {"value": "two"}])
    systems = list(seen.values())
    rtb = next(s for s in systems if "'Reasons to believe'" in s)
    tfd = next(s for s in systems if "'Desired response" in s)
    hero = next(s for s in systems if "'The insight'" in s)
    assert "LIST of items by design" in rtb and "never fail it for having several items" in rtb
    assert "one strategic choice" not in rtb and "one strategic choice" not in tfd
    assert "think / feel / do set by design" in tfd and "one strategic choice" in hero


def test_territory_test_is_positively_worded_and_fails_on_false(monkeypatch):
    """`brand_only` (pass = only this brand can say it) replaces the double-negative
    `not_rival_line`; a false verdict fails the line with the judge's reason."""
    seen = {}
    def judge(user, **k):
        """Test stub: stands in for `judge` in test_territory_test_is_positively_worded_and_fails_on_false."""
        seen["user"] = user
        return {"ranking": [0], "results": {"0": {"a": {"pass": True}, "own_territory": {"pass": True},
                                                  "brand_only": {"pass": False, "why": "any bank could"}}}}
    monkeypatch.setattr(pb, "_json_call", judge)
    field = {"id": "smp", "label": "SMP", "rubric": [{"id": "a", "method": "llm", "test": "A?"}]}
    (_c, ok, fails), = pb._judge_and_gate(field, [{"value": "v"}], territory={"own": "o", "avoid": "a", "rival": "R"})
    assert ok is False and "any bank could" in fails[0]
    assert "brand_only" in seen["user"] and "NOT run" not in seen["user"]


# ---------- change 2: a broken judge is not a pass (F5 / J2 / J12 / J10) ----------

@pytest.mark.parametrize("reply", [
    None, {}, {"results": {}}, {"results": {"cand_0": {"a": {"pass": True}}}},
    {"results": {"0": {"a": {"pass": "maybe"}}}}, {"results": {"0": {"smp.a": {"pass": True}}}},
    {"results": {"0": {"a": {"why": "no verdict here"}}}}, {"results": {"0": "pass"}},
])
def test_batch_judge_fails_closed_on_malformed_verdicts(monkeypatch, reply):
    """Every malformed reply shape leaves every candidate UNJUDGED (ok False), order kept.
    Before: 7 of 8 of these passed every candidate."""
    monkeypatch.setattr(pb, "_json_call", lambda *a, **k: reply)
    field = {"id": "insight", "label": "Insight", "rubric": [{"id": "a", "method": "llm", "test": "A?"}]}
    out = pb._judge_and_gate(field, [{"value": "first"}, {"value": "second"}])
    assert [c["value"] for c, _ok, _f in out] == ["first", "second"]
    assert all(ok is False for _c, ok, _f in out)
    assert all(f[0].startswith(pb.UNJUDGED) for _c, _ok, f in out)


def test_verdict_reads_bools_and_words_in_any_case():
    """Accepted verdict shapes: {pass: bool}, {verdict: pass|fail}, bare bool, true/false/pass/fail strings."""
    r = {"a": {"pass": True}, "b": {"verdict": "FAIL"}, "c": False, "d": "Pass", "e": "false",
         "f": "unsure", "g": {"pass": None}, "h": 1}
    assert [pb._verdict(r, t) for t in "abcdefgh"] == [True, False, False, True, False, None, None, None]


def test_one_based_judge_keys_are_realigned(monkeypatch):
    """Keys 1..n are shifted to 0..n-1, so each candidate gets ITS verdict (and nobody
    wins with the lost one). Before: candidate 0 got no verdict and passed."""
    monkeypatch.setattr(pb, "_json_call", lambda *a, **k: {"ranking": [1, 2], "results": {
        "1": {"a": {"pass": False, "why": "first is weak"}}, "2": {"a": {"pass": False, "why": "second is weak"}}}})
    field = {"id": "insight", "label": "Insight", "rubric": [{"id": "a", "method": "llm", "test": "A?"}]}
    out = pb._judge_and_gate(field, [{"value": "first"}, {"value": "second"}])
    assert [(c["value"], ok, f) for c, ok, f in out] == [("first", False, ["a: first is weak"]),
                                                          ("second", False, ["a: second is weak"])]


def test_territory_failure_skips_territory_tests_and_asks(monkeypatch):
    """No competitor could be mapped: the SMP judge prompt carries no territory tests and
    the brief asks which competitor the proposition must beat (no placeholder rival)."""
    calls = _fake_models(monkeypatch, territory=None)
    assert pb._smp_territory("brief", "ctx") is None
    _gf, fills, qs = _fill(monkeypatch)
    assert "smp" in fills
    assert any("Which competitor" in q["question"] and q["blocks_field"] == "smp" for q in qs)
    assert not any("own_territory" in u for k, u, s in calls if k == "judge_batch")


def test_unjudged_field_becomes_missing_with_a_question_and_no_rescue_calls(monkeypatch):
    """Judge down: the field is missing, flagged unjudged, with an open question that says
    the judge was unavailable — and no repair/rescue calls are wasted on it."""
    calls = _fake_models(monkeypatch)
    real = pb._json_call
    monkeypatch.setattr(pb, "_json_call", lambda u, system=None, **k:
                        None if "judging candidate" in (system or "") else real(u, system=system, **k))
    gf, fills, qs = _fill(monkeypatch)
    assert fills == {} and gf["insight"]["source"] == "missing" and gf["insight"].get("unjudged") is True
    assert any("judge was unavailable" in q["question"] for q in qs)
    assert not any(k == "refine" for k, _u, _s in calls)


# ---------- change 3: confidence must be a real number (F6 / G3) ----------

@pytest.mark.parametrize("conf", [None, 0.0, "high", 1.7, True])
def test_confidence_floor_rejects_missing_and_nonnumeric(monkeypatch, conf):
    """A missing, zero, word-valued, out-of-range or boolean confidence is BELOW the floor:
    the field is missing with a reason, and nothing raises. Before: None and 0.0 passed at
    the floor and 'high' crashed the whole run."""
    _fake_models(monkeypatch, conf=conf)
    gf, fills, qs = _fill(monkeypatch)
    assert fills == {} and all(gf[f]["source"] == "missing" for f in pb.GEN_ZONE3_ORDER)
    assert "confidence" in gf["insight"]["reason"] and any(q["blocks_field"] == "insight" for q in qs)


def test_conf_helper():
    """_conf: numbers and numeric strings in [0, 1] only."""
    assert pb._conf(0.72) == 0.72 and pb._conf("0.9") == 0.9 and pb._conf(1) == 1.0
    assert all(pb._conf(x) is None for x in (None, "high", 1.7, -0.1, True, [0.9]))


def test_crash_in_one_field_becomes_missing_plus_question(monkeypatch):
    """A ValueError while generating the RTB leaves THAT field missing with an open
    question; the other fields are filled and fill_derivable_fields does not raise."""
    _fake_models(monkeypatch, gen_raises="reasons_to_believe")
    gf, fills, qs = _fill(monkeypatch)
    assert {"insight", "smp", "desired_response"} <= set(fills) and "reasons_to_believe" not in fills
    assert "generation error" in gf["reasons_to_believe"]["reason"]
    assert any(q["blocks_field"] == "reasons_to_believe" for q in qs)


def test_refine_failure_keeps_the_chosen_draft(monkeypatch):
    """_refine_field returns None on a malformed reply; the fill keeps the winning draft (G7)."""
    _fake_models(monkeypatch, refine={"no": "value"})
    assert pb._refine_field(FIELD["insight"], "x") is None
    _gf, fills, _qs = _fill(monkeypatch)
    assert fills["insight"]["value"].startswith("draft 0")


# ---------- change 4: cut-off and refused replies are caught (F8 / BW3 / F7 / G4) ----------

def _chain(monkeypatch, links):
    """A fake model chain of `links` [(provider, model)]; returns the list of attempts.
    Routing is off: these tests are about walking a chain, and a routed call walks its
    job's own models instead (ADR 0011, covered in test_model_routes.py)."""
    attempts = []
    monkeypatch.setenv("BRIEF_ROUTES", "0")
    monkeypatch.setattr(pb, "_model_chain", lambda model=None: list(links))
    return attempts


def test_truncated_reply_is_retried_with_more_room_then_next_link(monkeypatch):
    """stop_reason max_tokens: one retry on the same link with 1.5x the cap; a second
    truncation moves to the next link. The partial text is never parsed."""
    attempts = _chain(monkeypatch, [("anthropic", "m1"), ("anthropic", "m2")])
    def link(provider, m, user, max_tokens=None, **k):
        """Test stub: stands in for `link` in test_truncated_reply_is_retried_with_more_room_then_next_link."""
        attempts.append((m, max_tokens))
        if m == "m1":
            raise pb._Truncated("cut")
        return '{"ok": 1}'
    monkeypatch.setattr(pb, "_call_link", link)
    assert pb._json_call("q", max_tokens=1000) == {"ok": 1}
    assert attempts == [("m1", 1000), ("m1", 1500), ("m2", 1000)]


def test_refusal_is_loud_and_goes_to_the_next_link(monkeypatch, capsys):
    """A refusal is printed with '[!]' and counted; the chain moves on."""
    _chain(monkeypatch, [("anthropic", "m1"), ("anthropic", "m2")])
    def link(provider, m, user, **k):
        """Test stub: stands in for `link` in test_refusal_is_loud_and_goes_to_the_next_link."""
        if m == "m1":
            raise pb._Refused("anthropic:m1: the model declined")
        return '{"ok": 1}'
    monkeypatch.setattr(pb, "_call_link", link)
    assert pb._json_call("q") == {"ok": 1}
    assert "[!] anthropic:m1: the model declined" in capsys.readouterr().err


def test_api_stop_reasons_raise(monkeypatch):
    """_chat_anthropic raises _Truncated on max_tokens and _Refused on refusal, and records
    cache tokens separately."""
    class Msgs:
        """Test stub class: stands in for `Msgs` in test_api_stop_reasons_raise."""
        stop = "max_tokens"
        def create(self, **kw):
            """Test stub: stands in for `create` in test_api_stop_reasons_raise."""
            class T:
                """Test stub class: stands in for `T` in test_api_stop_reasons_raise."""
                type, text = "text", '{"partial": '
            class U:
                """Test stub class: stands in for `U` in test_api_stop_reasons_raise."""
                input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens = 100, 50, 40, 0
            class R:
                """Test stub class: stands in for `R` in test_api_stop_reasons_raise."""
                content, usage, stop_reason = [T()], U(), Msgs.stop
            return R()
    class Client:
        """Test stub class: stands in for `Client` in test_api_stop_reasons_raise."""
        def __init__(self, **kw):
            """Test stub: stands in for `__init__` in test_api_stop_reasons_raise."""
            self.messages = Msgs()
    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=Client))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    pb._stats_reset()
    with pytest.raises(pb._Truncated):
        pb._chat_anthropic("hi", max_tokens=500)
    assert pb._LLM_STATS["cache_read_tokens"] == 40 and pb._LLM_STATS["prompt_tokens"] == 100
    Msgs.stop = "refusal"
    with pytest.raises(pb._Refused):
        pb._chat_anthropic("hi")
    assert pb._LLM_STATS["truncations"] == 1 and pb._LLM_STATS["refusals"] == 1


def test_loads_lenient_whole_does_not_salvage_an_inner_object():
    """A truncated 3-draft tournament reads as unusable with whole=True (so it is retried),
    not as one draft; the lenient reader still salvages for callers that want it (G4)."""
    raw = '{"candidates": [{"value": "A line", "confidence": 0.9}, {"value": "B line", "conf'
    assert pb._loads_lenient(raw, whole=True) is None
    assert pb._coerce_candidates(pb._loads_lenient(raw, whole=True)) == []
    assert pb._loads_lenient(raw) == {"value": "A line", "confidence": 0.9}      # the old salvage
    assert pb._loads_lenient('```json\n{"a": 1,}\n```'.strip("`json\n"), whole=True) == {"a": 1}


def test_tournament_and_judge_calls_ask_for_whole_replies(monkeypatch):
    """The batched generation and the batched judge pass whole=True to _json_call."""
    seen = {"gen_batch": [], "judge_batch": [], "other": []}
    def fake(user, system=None, **kw):
        """Test stub: stands in for `fake` in test_tournament_and_judge_calls_ask_for_whole_replies."""
        system = system or ""
        if '"candidates"' in system:
            seen["gen_batch"].append(kw.get("whole"))
            return {"candidates": [{"value": f"d{i}, because", "confidence": 0.9} for i in range(4)]}
        if "judging candidate" in system:
            seen["judge_batch"].append(kw.get("whole"))
            return _pass_all(user)
        seen["other"].append(kw.get("whole"))
        if system.startswith("You map"):
            return {"rival": "R", "own": "O", "avoid": "A"}
        return {"value": "one line, because", "confidence": 0.9}
    monkeypatch.setattr(pb, "_json_call", fake)
    monkeypatch.setattr(pb, "resolve_provider", lambda: "fake")
    monkeypatch.setenv("BRIEF_PARALLEL", "0")
    _fill(monkeypatch)
    assert seen["gen_batch"] and all(w is True for w in seen["gen_batch"])
    assert seen["judge_batch"] and all(w is True for w in seen["judge_batch"])
    assert not any(seen["other"])                       # single drafts, refine, territory: lenient


def test_golden_extraction_reaches_second_link(monkeypatch):
    """Link 1 answers valid JSON without `fields`: link 2 is tried (one chain walk, an
    accept). Before: three tries on link 1, link 2 never."""
    attempts = _chain(monkeypatch, [("anthropic", "m1"), ("nim", "m2")])
    def link(provider, m, user, **k):
        """Test stub: stands in for `link` in test_golden_extraction_reaches_second_link."""
        attempts.append(m)
        return '{"note": "nothing"}' if m == "m1" else '{"fields": {"insight": {"value": "x", "source": "inferred"}}}'
    monkeypatch.setattr(pb, "_call_link", link)
    out = pb.extract_golden_brief("brief")
    assert out["fields"]["insight"]["value"] == "x" and attempts == ["m1", "m1", "m2"]


def test_json_call_retries_the_same_link_when_accept_rejects_a_parseable_reply(monkeypatch):
    """accept() rejects a clean reply: one more try on the SAME link before failing over (G8)."""
    attempts = _chain(monkeypatch, [("anthropic", "m1"), ("anthropic", "m2")])
    replies = iter(['{"n": 1}', '{"n": 2}'])
    def link(provider, m, user, **k):
        """Test stub: stands in for `link` in test_json_call_retries_the_same_link_when_accept_rejects_a_parseable_reply."""
        attempts.append(m)
        return next(replies)
    monkeypatch.setattr(pb, "_call_link", link)
    assert pb._json_call("q", retries=1, accept=lambda o: o.get("n") == 2) == {"n": 2}
    assert attempts == ["m1", "m1"]


# ---------- change 5: "the client said it" must be provable (F4b / JL-10 / CC11) ----------

def test_quote_in_brief_is_verbatim_punctuation_free_and_ellipsis_aware():
    """Fragments split on '...' must each be in the brief; case and punctuation are ignored."""
    assert pb._quote_in_brief("under-30s see it as their parents bank", BRIEF)
    assert pb._quote_in_brief("Acme Bank is relaunching ... grow sign-ups by 20 percent", BRIEF)
    assert not pb._quote_in_brief("Under-30s think the app is boring", BRIEF)
    assert not pb._quote_in_brief(None, BRIEF) and not pb._quote_in_brief("", BRIEF)


def test_client_stated_strategy_must_match_its_quote(monkeypatch):
    """An insight labelled client_stated whose value does not match its quote (or whose
    quote is not in the brief) is generated and gated like any other; a genuine client
    line that breaks a code rule (6 RTB items) is kept as written with an open question."""
    _fake_models(monkeypatch)
    paraphrase = {"insight": {"value": "The job isn't proving speed; it is making the bank feel like theirs, "
                                       "because under-30s want a bank that treats them as adults",
                              "source": "client_stated",
                              "source_quote": "Under-30s see it as their parents' bank."}}
    gf, fills, _qs = _fill(monkeypatch, paraphrase)
    assert fills["insight"]["method"] == "gen:insight" and "paraphrase" in str(gf["insight"].get("reason", "")) \
        or fills["insight"]["method"] == "gen:insight"
    fake_quote = {"insight": {"value": "Under-30s think the app is boring because it is old",
                              "source": "client_stated", "source_quote": "Under-30s think the app is boring"}}
    gf, fills, _qs = _fill(monkeypatch, fake_quote)
    assert fills["insight"]["method"] == "gen:insight"
    genuine = {"reasons_to_believe": {"value": ["2 million customers already use the app every week"] + [f"p{i}" for i in range(5)],
                                      "source": "client_stated",
                                      "source_quote": "2 million customers already use the app every week"}}
    gf, fills, qs = _fill(monkeypatch, genuine)
    assert "reasons_to_believe" not in fills and gf["reasons_to_believe"]["source"] == "client_stated"
    assert any(q["blocks_field"] == "reasons_to_believe" and "breaks our rules" in q["question"] for q in qs)


# ---------- change 6: retrieval can never crash the brief (N1 / RAG-2 / C3) ----------

class _UpRetriever:
    """A store that answers the availability check and then fails every request."""
    @staticmethod
    def index_available(index_dir=None):
        """Test stub: stands in for `index_available` in _UpRetriever."""
        return True

    @staticmethod
    def index_label(index_dir=None):
        """Test stub: stands in for `index_label` in _UpRetriever."""
        return "qdrant:up"

    @staticmethod
    def retrieve(*a, **k):
        """Test stub: stands in for `retrieve` in _UpRetriever."""
        raise RuntimeError("store died mid-run")


def test_store_failure_mid_run_falls_back_to_digests(monkeypatch, capsys):
    """Mix raises after the availability check: digests, with the reason recorded and
    announced — never the loops path against the same store, never an exception."""
    monkeypatch.setattr(pb, "_load_retriever", lambda: _UpRetriever)
    monkeypatch.setattr(pb, "_retrieval_scopes", lambda fields: ["global"])
    monkeypatch.setattr(pb, "_loops_via_mix", lambda *a, **k: (_ for _ in ()).throw(TimeoutError("mix stalled")))
    monkeypatch.setattr(pb, "_loops37_from_digests", lambda loop2, fields, synthesize=True:
                        {"enabled": True, "index": "digests:packs_dist", "loops": {}})
    monkeypatch.delenv("RAG_PATH", raising=False)
    out = pb.loops_3_7({}, {"business_problem": {"value": "p"}}, synthesize=False)
    assert out["enabled"] and out["fallback"]["to"] == "digests"
    assert "mix stalled" in out["fallback"]["reason"]
    assert "fall back to the pack digests" in capsys.readouterr().err


def test_no_evidence_is_a_recorded_fallback_not_an_empty_rag_run(monkeypatch):
    """Retrieval that returns nothing falls back to the digests with that reason."""
    monkeypatch.setattr(pb, "_load_retriever", lambda: _UpRetriever)
    monkeypatch.setattr(pb, "_retrieval_scopes", lambda fields: ["global"])
    from mix_queries import LOOP37_SPECS
    monkeypatch.setattr(pb, "_loops_via_mix", lambda *a, **k:
                        ({k_: {"title": t, "query": "q", "evidence": []} for k_, t, _q in LOOP37_SPECS}, {}))
    monkeypatch.setattr(pb, "_loops37_from_digests", lambda loop2, fields, synthesize=True: {"enabled": True, "loops": {}})
    out = pb.loops_3_7({}, {}, synthesize=False)
    assert out["fallback"] == {"to": "digests", "reason": "retrieval returned no evidence"}


def test_retrieval_failure_never_escapes_run(monkeypatch):
    """Even loops_3_7 raising (a bug) is contained: the brief finishes with a disabled stub."""
    monkeypatch.setattr(pb, "capture_toon", lambda segs: {"fields": {"business_problem": {"value": "p", "status": "fact"}},
                                                          "how_to_win": {}, "open_questions": []})
    monkeypatch.setattr(pb, "how_to_win_toon", lambda segs: {})
    monkeypatch.setattr(pb, "extract_golden_brief", lambda text: {"fields": {"background": {"value": "b", "source": "client_stated"}}})
    monkeypatch.setattr(pb, "score_betterbriefs", lambda text, fields=None: {"mode": "llm"})
    def boom(*a, **k):
        """Test stub: stands in for `boom` in test_retrieval_failure_never_escapes_run."""
        raise RuntimeError("retrieval bug")
    monkeypatch.setattr(pb, "loops_3_7", boom)
    out = pb.run(None, loops37=True, golden=True, raw_text=BRIEF)
    assert out["loops3_7"]["enabled"] is False and "retrieval bug" in out["loops3_7"]["reason"]
    assert "loop2_golden" in out


# ---------- change 7: Claude only, and say who answered (N2 / C6 / J4 / D4 / F12 / CC14) ----------

def test_anthropic_pin_has_no_silent_nonclaude_links(monkeypatch):
    """BRIEF_PROVIDER=anthropic with an NVIDIA key: the chain is Claude-only unless
    BRIEF_ALLOW_NONCLAUDE=1."""
    monkeypatch.setenv("BRIEF_PROVIDER", "anthropic")
    monkeypatch.setenv("NVIDIA_API_KEY", "nv")
    monkeypatch.delenv("BRIEF_MODEL_CHAIN", raising=False)
    monkeypatch.delenv("BRIEF_ALLOW_NONCLAUDE", raising=False)
    monkeypatch.setattr(pb, "_LINK_COOLDOWN", {})
    assert all(p == "anthropic" for p, _m in pb._model_chain())
    monkeypatch.setenv("BRIEF_ALLOW_NONCLAUDE", "1")
    assert any(p == "nim" for p, _m in pb._model_chain())


def test_run_raises_a_clear_error_with_no_route_to_claude(monkeypatch):
    """Claude-only chain, transport api, no key, no CLI: run() raises NoClaudeAvailable
    before any call — never a brief written by another model."""
    monkeypatch.setenv("BRIEF_PROVIDER", "anthropic")
    monkeypatch.setenv("BRIEF_CLAUDE_TRANSPORT", "api")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("BRIEF_ALLOW_NONCLAUDE", raising=False)
    monkeypatch.setattr(pb.shutil, "which", lambda name: None)
    with pytest.raises(pb.NoClaudeAvailable, match="ANTHROPIC_API_KEY"):
        pb.run(None, raw_text=BRIEF)
    monkeypatch.setenv("BRIEF_CLAUDE_TRANSPORT", "cli")
    with pytest.raises(pb.NoClaudeAvailable, match="Claude Code"):
        pb.run(None, raw_text=BRIEF)
    monkeypatch.setattr(pb.shutil, "which", lambda name: "/usr/local/bin/claude")
    pb._require_claude("anthropic")                    # cli + claude installed: fine


def _stub_capture(monkeypatch, answered=()):
    """Stub every stage of run(); the capture records `answered` links as if they answered."""
    def cap(segs):
        """Test stub: stands in for `cap` in _stub_capture."""
        for label in answered:
            pb._stats_answered(label)
        return {"fields": {"business_problem": {"value": "p", "status": "fact"}}, "how_to_win": {}, "open_questions": []}
    monkeypatch.setattr(pb, "capture_toon", cap)
    monkeypatch.setattr(pb, "how_to_win_toon", lambda segs: {})
    monkeypatch.setattr(pb, "score_betterbriefs", lambda text, fields=None: {"mode": "llm"})


def test_extraction_mode_names_the_answering_model(monkeypatch):
    """meta.extraction_mode is the link that answered most calls, not model_for(); the
    chain walked is kept in meta.model_chain."""
    monkeypatch.setenv("BRIEF_PROVIDER", "anthropic")
    monkeypatch.setenv("BRIEF_MODEL_CHAIN", "anthropic:claude-opus-5-5")
    _stub_capture(monkeypatch, answered=["anthropic:claude-opus-5-5"] * 3)
    out = pb.run(None, raw_text=BRIEF)
    assert out["meta"]["extraction_mode"] == "anthropic:claude-opus-5-5"
    assert out["meta"]["model_chain"] == ["anthropic:claude-opus-5-5"]
    assert "fallback_links" not in out["meta"]
    assert out["meta"]["llm_stats"]["answered_by"] == {"anthropic:claude-opus-5-5": 3}


def test_a_nonclaude_answer_is_named_in_meta_and_on_stderr(monkeypatch, capsys):
    """When a non-Claude link answers under a Claude lead, meta.fallback_links names it and
    stderr says so once."""
    monkeypatch.setenv("BRIEF_PROVIDER", "anthropic")
    monkeypatch.setenv("BRIEF_ALLOW_NONCLAUDE", "1")
    _stub_capture(monkeypatch)
    real_cap = pb.capture_toon
    def cap(segs):
        """Test stub: stands in for `cap` in test_a_nonclaude_answer_is_named_in_meta_and_on_stderr."""
        pb._note_answer("nim", "openai/gpt-oss-20b"); pb._note_answer("nim", "openai/gpt-oss-20b")
        return real_cap(segs)
    monkeypatch.setattr(pb, "capture_toon", cap)
    out = pb.run(None, raw_text=BRIEF)
    assert out["meta"]["fallback_links"] == ["nim:openai/gpt-oss-20b"]
    assert out["meta"]["extraction_mode"] == "nim:openai/gpt-oss-20b"
    assert capsys.readouterr().err.count("[!] a non-Claude link answered") == 1


def test_critic_never_runs_on_another_model(monkeypatch):
    """The critic walks only its pinned model: Sonnet failing means 0 checks judged and
    judge_model None, never the generator scoring its own work."""
    brief = gc.from_brief_object({"loop1_capture": {"fields": {}}, "loop2_brief": {"open_questions": []},
                                  "loop2_golden": {"fields": {"smp": {"value": "Only Acme treats under-30s as adults",
                                                                      "source": "inferred"},
                                                              "competitor_context": {"value": "RivalBank", "source": "client_stated"}}}})
    schema = json.loads(gc.SCHEMA_PATH.read_text())
    v = gc.validate(schema, brief)
    tried = []
    monkeypatch.setenv("BRIEF_MODEL_CHAIN", "anthropic:claude-sonnet-5,anthropic:claude-opus-4-6")
    monkeypatch.delenv("BRIEF_PROVIDER", raising=False)
    def link(provider, m, user, **k):
        """Test stub: stands in for `link` in test_critic_never_runs_on_another_model."""
        tried.append(m)
        if m == "claude-sonnet-5":
            raise RuntimeError("BadRequestError")
        return json.dumps({"smp": {c: {"verdict": "pass"} for c in ("ownable", "not_a_tagline", "derives_from")}})
    monkeypatch.setattr(pb, "_call_link", link)
    v, ran = gc.run_critic_one_call(schema, brief, v)
    assert ran == 0 and v["judge_model"] is None and set(tried) == {"claude-sonnet-5"}
    monkeypatch.setattr(pb, "_call_link", lambda provider, m, user, **k:
                        json.dumps({"smp": {c: {"verdict": "pass"} for c in ("ownable", "not_a_tagline", "derives_from")}}))
    v = gc.validate(schema, brief)
    v, ran = gc.run_critic_one_call(schema, brief, v)
    assert ran == 3 and v["judge_model"] == "anthropic:claude-sonnet-5"


def test_run_critic_one_call_partial_reply_leaves_missing_checks_unjudged():
    """A reply missing one field and one check judges only what it carries; the rest stay
    REVIEW (half credit), and `ran` counts only real verdicts (G6)."""
    brief = gc.from_brief_object({"loop1_capture": {"fields": {}}, "loop2_brief": {"open_questions": []},
                                  "loop2_golden": {"fields": {
                                      "competitor_context": {"value": "RivalBank", "source": "client_stated"},
                                      "smp": {"value": "Only Acme treats under-30s as adults", "source": "inferred"},
                                      "insight": {"value": "Under-30s feel patronised because banks talk down", "source": "inferred"}}}})
    schema = json.loads(gc.SCHEMA_PATH.read_text())
    v = gc.validate(schema, brief)
    reply = {"smp": {"ownable": {"verdict": "fail", "reason": "r"}, "not_a_tagline": {"verdict": "pass"}}}
    v, ran = gc.run_critic_one_call(schema, brief, v, judge=lambda p: reply)
    smp = {c["id"]: c["status"] for c in next(fr for fr in v["fields"] if fr["id"] == "smp")["checks"]}
    ins = {c["id"]: c["status"] for c in next(fr for fr in v["fields"] if fr["id"] == "insight")["checks"]}
    assert ran == 2 and smp["ownable"] == "fail" and smp["not_a_tagline"] == "pass"
    assert smp["derives_from"] == "review" and ins["is_tension"] == "review"


def test_stats_snapshot_is_a_deep_copy_and_scoped_per_run(monkeypatch):
    """meta.llm_stats is not changed by calls made after run() (CC14), and a call inside
    _stats_scope() never lands in another ledger (critic-G11)."""
    _stub_capture(monkeypatch, answered=["anthropic:claude-opus-4-6"])
    monkeypatch.setenv("BRIEF_PROVIDER", "anthropic")
    out = pb.run(None, raw_text=BRIEF)
    pb._stats_call("anthropic:claude-sonnet-5", 10)
    assert "anthropic:claude-sonnet-5" not in out["meta"]["llm_stats"]["by_provider"]
    pb._stats_reset()
    with pb._stats_scope() as led:
        pb._stats_call("x:y", 5)
    assert led["calls"] == 1 and pb._LLM_STATS["calls"] == 0
    done = threading.Event()
    with pb._stats_scope() as led2:
        t = threading.Thread(target=pb._scoped(lambda: (pb._stats_call("t:m", 1), done.set())))
        t.start(); t.join(2)
    assert led2["by_provider"] == {"t:m": 1} and pb._LLM_STATS["calls"] == 0


def test_synthesis_label_is_the_answering_link(monkeypatch):
    """loops3_7.synthesis_mode names the link that wrote the paragraphs."""
    def fake(user, info=None, **k):
        """Test stub: stands in for `fake` in test_synthesis_label_is_the_answering_link."""
        if isinstance(info, dict):
            info["link"] = "anthropic:claude-opus-5-5"
        return {"paragraph": "Grounded paragraph."}
    monkeypatch.setattr(pb, "_json_call", fake)
    loops = {"loop4_insight": {"title": "t", "evidence": [{"citation": "c", "snippet": "s", "framework": "f"}]}}
    mode = pb._synthesize_loops37({"problem": "", "objective": "", "audience": "", "key_message": ""}, "x", loops)
    assert mode == "llm:claude-opus-5-5" and loops["loop4_insight"]["synthesis"] == "Grounded paragraph."


# ---------- change 8: say when a brief is degraded (JL-2 / RAG-11) ----------

def test_unvalidated_loops_are_flagged(monkeypatch, capsys):
    """Loops whose evidence no validator judged are listed in validation_degraded, named
    per loop (validated_by), and announced on stderr."""
    import brief_context as bc
    from mix_queries import LOOP37_SPECS
    monkeypatch.setattr(pb, "_load_retriever", lambda: None)
    hit = types.SimpleNamespace(metadata={"scope": "global"}, header="H", doc_id="ipa_0001", section="Insight",
                                cite="ipa_0001#1", title="T", source="ipa", score=0.9, text="passage")
    per_field = {k: {"backend_used": "jev", "fell_back": False} for k, _t, _q in LOOP37_SPECS}
    per_field["loop4_insight"] = {"backend_used": None, "fell_back": True}
    mc = types.SimpleNamespace(fields={k: [hit] for k, _t, _q in LOOP37_SPECS},
                               trace={"validation": {"per_field": per_field}})
    monkeypatch.setattr(bc, "build_multi", lambda *a, **k: mc)
    gist = {"problem": "p", "objective": "o", "audience": "a", "key_message": ""}
    loops, trace = pb._loops_via_mix(gist, {"business_problem": {"value": "p"}})
    assert trace["validation_degraded"] == ["loop4_insight"]
    assert loops["loop4_insight"]["validated_by"] is None and loops["loop5_proposition"]["validated_by"] == "jev"
    assert "unvalidated" in capsys.readouterr().err


# ---------- change 9: clean text in, clean text out (H6 / H11 / J11) ----------

def test_toon_extra_numeric_cells_join_into_src():
    """'Resolve tension|11|31' under {point|src} is point + src '11 31', not point '…|11'."""
    d = toon_lite.decode("t[1|]{point|src}:\n  Resolve tension|11|31\n")
    assert d["t"] == [{"point": "Resolve tension", "src": "11 31"}]
    d = toon_lite.decode("t[1|]{point|src}:\n  A stray | pipe|4\n")      # free text keeps the old rule
    assert d["t"] == [{"point": "A stray | pipe", "src": 4}]


def test_how_to_win_rows_never_keep_glued_sentence_numbers(monkeypatch):
    """Belt and braces in the reader: a point that still ends in '|n' is cleaned and its
    numbers become evidence refs."""
    segs = [f"Sentence {i}." for i in range(1, 40)]
    monkeypatch.setattr(pb, "_model_chain", lambda model=None: [("fake", "m")])
    monkeypatch.setattr(pb, "_call_link", lambda *a, **k: "winning_themes[1|]{point|src}:\n  Win on trust|11|31\n")
    out = pb.how_to_win_toon(segs)
    assert out["winning_themes"] == [{"point": "Win on trust", "evidence": "Sentence 11. Sentence 31.",
                                      "source_refs": [11, 31]}]
    assert pb._unglue("Resolve it|11|31", 4) == ("Resolve it", "4 11 31")


def test_internal_markers_are_scrubbed_from_client_text():
    """'(sentence 35 says TBC)' and '[5]' never reach the client brief or its open questions."""
    assert pb._scrub_markers("Service impact (sentence 35 says TBC)?") == "Service impact?"
    assert pb._scrub_markers("Prove it [5] now") == "Prove it now"
    brief = {"meta": {"project": "P"}, "loop2_golden": {"fields": {"insight": {"value": "Under-30s feel judged (sentence 3)"}}},
             "loop2_brief": {"open_questions": [{"question": "What budget (sentence 35 says TBC)?", "priority": "high"}]}}
    md = pb.render_client_brief(brief)
    assert "sentence" not in md and "Under-30s feel judged" in md and "What budget?" in md
    l2 = pb.shape_loop2({}, [{"question": "Who signs off [4]?"}, "Deadline (sentences 2-3)?"])
    texts = [q["question"] if isinstance(q, dict) else q for q in l2["open_questions"]]
    assert "Who signs off?" in texts and "Deadline?" in texts and not any("sentence" in t or "[4]" in t for t in texts)


def test_judge_note_names_drafts_not_indexes():
    """A judge's 'candidate 2 is sharpest' becomes the draft's own opening words."""
    cands = [{"value": "Only Acme treats under-30s as adults with money"}, {"value": "Be modern"},
             {"value": "The considered choice for people who plan"}]
    assert pb._name_candidates("Candidate 2 is sharpest; [0] is generic", cands) == \
        '"The considered choice for people who…" is sharpest; "Only Acme treats under-30s as adults…" is generic'


def test_scorecard_verdicts_are_case_insensitive_and_deduped(monkeypatch):
    """'Pass' reads as pass, 'Multiple' as multiple (with its split kept), duplicate rows
    collapse to the first, and evidence not in the brief downgrades a pass to vague."""
    monkeypatch.setattr(pb, "_json_call", lambda *a, **k: {
        "dimensions": [{"dimension": "Language", "verdict": "Pass", "evidence": "grow sign-ups by 20 percent"},
                       {"dimension": "language", "verdict": "missing", "evidence": ""},
                       {"dimension": "audience_vividness", "verdict": "PASS", "evidence": "everyone aged 18-99"}],
        "single_mindedness": {"verdict": "Multiple", "split_into": ["a", "b"]}, "summary": "s"})
    sc = pb.score_betterbriefs(BRIEF, {})
    rows = {d["dimension"]: d for d in sc["dimensions"]}
    assert rows["language"]["verdict"] == "pass" and sum(d["dimension"] == "language" for d in sc["dimensions"]) == 1
    assert rows["audience_vividness"]["verdict"] == "vague" and "not found in brief" in rows["audience_vividness"]["evidence"]
    assert sc["single_mindedness"] == {"verdict": "multiple", "split_into": ["a", "b"]}
    monkeypatch.setattr(pb, "_json_call", lambda *a, **k: {"dimensions": [],
                                                          "single_mindedness": {"verdict": "single", "split_into": ["a"]}})
    assert pb.score_betterbriefs(BRIEF, {})["single_mindedness"] == {"verdict": "single", "split_into": []}


# ---------- render_client_brief (G5) ----------

def test_render_client_brief_full_and_empty_briefs():
    """A fully populated brief renders every section and value; an empty one renders every
    section as 'to be agreed' without a crash or a literal 'None'."""
    full = {"meta": {"project": "Moving People"}, "loop2_brief": {"open_questions": [{"question": "Budget?"}]},
            "loop2_golden": {"fields": {
                "background": {"value": "b"}, "objectives": {"value": {"commercial": "c", "behavioural": "", "attitudinal": "a"}},
                "audience": {"value": "au"}, "competitor_context": {"value": "cc"}, "insight": {"value": "in"},
                "smp": {"value": "sm"}, "reasons_to_believe": {"value": ["r1", {"value": "r2"}]},
                "desired_response": {"value": {"think": "t", "feel": "f", "do": "d"}},
                "tone_world_assets": {"value": "tw"}, "budget_scope": {"value": "bs"}, "mandatories": {"value": "m"}}}}
    md = pb.render_client_brief(full)
    for h in ("Background", "Objectives", "Audience", "Competitor context", "The insight", "Single-minded proposition",
              "Reasons to believe", "Desired response", "Tone & world", "Budget & scope", "Mandatories", "Open questions"):
        assert f"## {h}" in md, h
    assert "- r1" in md and "- r2" in md and "- **Commercial:** c" in md and "Behavioural" not in md
    assert "- **Do:** d" in md and md.startswith("# Moving People — Brief")
    empty = {"meta": {}, "loop2_brief": {}, "loop2_golden": {"fields": {"insight": {"value": None}}}}
    md = pb.render_client_brief(empty)
    assert md.count("_To be agreed") == 11 and "None" not in md and "Open questions" not in md


# ---------- change 10: start the fill as soon as it can start (CC1) ----------

def test_fill_starts_before_the_capture_returns(monkeypatch):
    """With retrieval from the golden extraction, the strategy fill starts while the capture
    is still in flight (the capture waits for it; sequential order would time out), and
    the scorecard is submitted before the capture returns."""
    fill_started, score_started = threading.Event(), threading.Event()
    def cap(segs):
        """Test stub: stands in for `cap` in test_fill_starts_before_the_capture_returns."""
        assert score_started.wait(5) and fill_started.wait(5), "fill or scorecard did not start before the capture finished"
        return {"fields": {"business_problem": {"value": "p", "status": "fact"}}, "how_to_win": {}, "open_questions": []}
    def score(text, fields=None):
        """Test stub: stands in for `score` in test_fill_starts_before_the_capture_returns."""
        score_started.set(); return {"mode": "llm", "dimensions": []}
    def fill(gf, l37, schema, brief_text="", **kw):
        """Test stub: stands in for `fill` in test_fill_starts_before_the_capture_returns."""
        fill_started.set(); return {"insight": {"value": "i"}}, [{"question": "Agree the SMP.", "blocks_field": "smp"}]
    monkeypatch.setattr(pb, "capture_toon", cap)
    monkeypatch.setattr(pb, "how_to_win_toon", lambda segs: {})
    monkeypatch.setattr(pb, "extract_golden_brief", lambda text: {"fields": {"background": {"value": "b", "source": "client_stated"}}})
    monkeypatch.setattr(pb, "loops_3_7", lambda loop2, fields, **kw: {"enabled": True, "loops": {}, "gist": {}, "intent": "x",
                                                                        "synthesis_mode": "none"})
    monkeypatch.setattr(pb, "score_betterbriefs", score)
    monkeypatch.setattr(pb, "fill_derivable_fields", fill)
    monkeypatch.setenv("BRIEF_RETRIEVE_FROM", "golden")
    monkeypatch.delenv("BRIEF_PARALLEL", raising=False)
    out = pb.run(None, loops37=True, golden=True, raw_text=BRIEF)
    assert out["loop2_golden"]["generation_open_questions"][0]["blocks_field"] == "smp"
    assert out["loop2_brief"]["open_questions"][-1]["question"] == "Agree the SMP."
    assert out["betterbriefs_scorecard"]["mode"] == "llm"


def test_heuristic_scorecard_is_rebuilt_from_the_capture(monkeypatch):
    """The t=0 scorecard call has no capture to read: when it falls back to the heuristic,
    run() rebuilds it from the capture so the fields are scored."""
    _stub_capture(monkeypatch)
    monkeypatch.setattr(pb, "score_betterbriefs", lambda text, fields=None: pb.scorecard_heuristic(fields or {}))
    out = pb.run(None, raw_text=BRIEF)
    sc = out["betterbriefs_scorecard"]
    assert sc["mode"] == "heuristic" and any(d["dimension"] == "objectives_quality" for d in sc["dimensions"])


def test_parallel_off_runs_the_same_graph(monkeypatch):
    """BRIEF_PARALLEL=0: same stages, same output shape, one at a time."""
    monkeypatch.setenv("BRIEF_PARALLEL", "0")
    _stub_capture(monkeypatch)
    monkeypatch.setattr(pb, "extract_golden_brief", lambda text: {"fields": {"background": {"value": "b", "source": "client_stated"}}})
    monkeypatch.setattr(pb, "loops_3_7", lambda loop2, fields, **kw: {"enabled": True, "loops": {}, "gist": {}, "intent": "x",
                                                                        "synthesis_mode": "none"})
    monkeypatch.setattr(pb, "fill_derivable_fields", lambda gf, l37, schema, brief_text="", **kw: ({}, []))
    out = pb.run(None, loops37=True, golden=True, raw_text=BRIEF)
    assert list(out)[:4] == ["meta", "loop1_capture", "loop2_brief", "betterbriefs_scorecard"]
    assert out["loops3_7"]["retrieved_from"] == "golden" and "loop2_golden" in out


def test_a_run_where_no_claude_call_answered_is_an_error(monkeypatch):
    """Every Claude link fails mid-run (a usage limit hit partway): the stages fall back to
    heuristics, and run() must raise NoClaudeAvailable instead of returning that brief as
    normal (as_sent run 2026-09-26)."""
    monkeypatch.setenv("BRIEF_PROVIDER", "anthropic")
    monkeypatch.delenv("BRIEF_ALLOW_NONCLAUDE", raising=False)
    def dead(user, system=None, max_tokens=None, schema=None, model=None):
        """Test stub: stands in for `_chat_anthropic` in test_a_run_where_no_claude_call_answered_is_an_error."""
        pb._stats_call(f"anthropic:{model}", 10)
        raise RuntimeError("usage limit reached")
    monkeypatch.setattr(pb, "_chat_anthropic", dead)
    monkeypatch.setenv("BRIEF_CLAUDE_TRANSPORT", "api")
    try:
        pb.run(None, raw_text="Acme sells packs. The brief is short.", golden=True)
    except pb.NoClaudeAvailable as e:
        assert "every Claude call" in str(e)
    else:
        raise AssertionError("a run with no Claude answer returned a brief")


# ---------- 2026-09-28: a field whose every draft fails is kept as a marked draft ----------

def test_failed_drafts_keep_the_best_one_marked_for_review(monkeypatch):
    """The judge fails every RTB draft on supports_smp: the field keeps the best-ranked
    draft, marked review.status=failed_checks with the failed check id, and an open question
    says so (Sai, 2026-09-28; before, the field was emptied and its content lost)."""
    _fake_models(monkeypatch)
    pass_all = _pass_all

    def fail_rtb(user):
        """Pass everything except the RTB's supports_smp."""
        out = pass_all(user)
        for r in out["results"].values():
            if "supports_smp" in r:
                r["supports_smp"] = {"pass": False, "why": "the proof does not back the proposition"}
        return out
    monkeypatch.setattr(sys.modules[__name__], "_pass_all", fail_rtb)
    monkeypatch.setattr(pb, "_numbers_not_in", lambda value, allowed: [])     # no invented figures here
    gf, fills, qs = _fill(monkeypatch)
    rtb = gf["reasons_to_believe"]
    assert rtb["source"] == "inferred" and rtb["value"] and "reasons_to_believe" in fills
    assert rtb["review"]["status"] == "failed_checks" and rtb["review"]["failed"] == ["supports_smp"]
    assert any(q["blocks_field"] == "reasons_to_believe" and q["question"].startswith("Review the")
               for q in qs)


def test_failed_drafts_that_invent_a_figure_still_leave_the_field_open(monkeypatch):
    """Every RTB draft states a figure the brief never gave: nothing is kept (no invented
    fact reaches the page), the field stays open as before."""
    _fake_models(monkeypatch)
    monkeypatch.setattr(pb, "_numbers_not_in", lambda value, allowed: ["73"])
    gf, fills, qs = _fill(monkeypatch)
    assert gf["reasons_to_believe"]["source"] == "missing" and "review" not in gf["reasons_to_believe"]


def test_a_draft_that_invents_a_figure_is_never_kept():
    assert pb._invents(["figures not in the brief: 73 — remove or replace with a stated fact"])
    assert pb._invents(['jev: a figure in "x" is not in the brief (p unsupported 0.97) — remove it'])
    assert not pb._invents(["supports_smp: no", "ownable: generic"])
    assert pb._check_ids(["supports_smp: a", "supports_smp: b", "ownable: c"]) == ["supports_smp", "ownable"]


def test_client_page_tags_a_kept_draft():
    import brief_render
    brief = {"meta": {"project": "P"}, "loop2_brief": {"open_questions": []},
             "loop2_golden": {"fields": {
                 "smp": {"value": "Keep me", "source": "inferred", "method": "gen:smp",
                         "review": {"status": "failed_checks", "failed": ["derives_from"], "why": "derives_from: no"}},
                 "insight": {"value": "Fine", "source": "inferred", "method": "gen:insight"}}}}
    md = brief_render.render_client_brief(brief)
    assert "## Single-minded proposition\n_Draft — to review: it failed derives_from. See open questions._\nKeep me" in md
    assert "## The insight\nFine" in md                                # an ordinary field gets no tag


def test_app_rationale_names_the_kept_drafts():
    sys.path.insert(0, str(HERE.parent / "agent-server"))
    import mapping
    brief = {"meta": {"extraction_mode": "anthropic:claude-opus-4-6"}, "loop1_capture": {},
             "loop2_golden": {"fields": {"smp": {"value": "x", "review": {"status": "failed_checks"}}}}}
    assert "DRAFTS TO REVIEW (failed their checks): smp" in mapping.build_rationale(brief)
