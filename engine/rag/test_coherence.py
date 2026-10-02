"""The coherence pass (2026-10-02): problem -> insight -> proposition -> reasons to believe ->
desired response must hold together. One judge call; at most one targeted rewrite of a field
generated in this run, kept only if it clears its own gate and leaves fewer links broken."""
import json
from pathlib import Path

import parse_brief as pb

SCHEMA = json.loads((Path(pb.__file__).resolve().parent / "golden-brief" / "golden_brief.schema.json").read_text())

CHAIN = {"background": "A polenta brand launching an instant format.",
         "objectives": "Grow trial among young families.",
         "insight": "People fear instant food is fake food.",
         "smp": "Ready in a minute, still real polenta.",
         "reasons_to_believe": ["Cornmeal only, no additives."],
         "desired_response": {"think": "It is real", "feel": "Reassured", "do": "Try it"}}


def _fills(*ids):
    """Entries as fill_derivable_fields builds them, for the generated fields."""
    return {f: {"value": CHAIN[f], "source": "inferred", "method": f"gen:{f}"} for f in ids}


def _run(monkeypatch, verdicts, fills, refine=None, gate=True, regen=None):
    """Run the pass with the judge returning `verdicts` in turn (one dict per call)."""
    golden = {k: ({"value": v} if k not in fills else fills[k]) for k, v in CHAIN.items()}
    val = lambda fid: (golden.get(fid) or {}).get("value")
    replies = list(verdicts)
    calls = {"judge": 0, "refine": 0, "gate": 0}

    def judge(user, system=None, **kw):
        assert system == pb.COHERENCE_SYSTEM and kw.get("route") == "judge"   # Sonnet, never the writers' Opus
        calls["judge"] += 1
        return replies.pop(0)

    def fake_refine(field, value, note=""):
        calls["refine"] += 1
        assert note.startswith("COHERENCE:")
        return refine

    def fake_gate(field, value, ctx, territory=None):
        calls["gate"] += 1
        return gate

    monkeypatch.setattr(pb, "_json_call", judge)
    monkeypatch.setattr(pb, "_refine_field", fake_refine)
    qs = pb._coherence_pass(fills, golden, SCHEMA, val, lambda deps: "ctx", fake_gate, None, lambda e: e, regen=regen)
    return qs, calls, golden


def _links(**broken):
    """A judge reply: every link holds except those named, e.g. smp='why'."""
    rows = []
    for ups, fid in pb.COHERENCE_LINKS:
        name = f"{'+'.join(ups)}->{fid}"
        rows.append({"link": name, "holds": fid not in broken, "why": broken.get(fid, "ok"), "fix": "tie it in"})
    return {"links": rows, "weakest": next(iter(broken), "")}


def test_a_chain_that_holds_costs_one_call_and_is_recorded_on_each_field(monkeypatch):
    fills = _fills("insight", "smp", "reasons_to_believe", "desired_response")
    qs, calls, _g = _run(monkeypatch, [_links()], fills)
    assert calls == {"judge": 1, "refine": 0, "gate": 0} and qs == []
    assert all(fills[f]["coherence"]["holds"] is True for f in fills)


def test_a_broken_link_gets_one_rewrite_kept_when_it_fixes_the_chain(monkeypatch):
    fills = _fills("insight", "smp", "reasons_to_believe", "desired_response")
    qs, calls, golden = _run(monkeypatch, [_links(smp="sells the convenience the client fears"), _links()],
                             fills, refine={"value": "Real polenta, no pot needed."})
    assert calls == {"judge": 2, "refine": 1, "gate": 1} and qs == []
    assert fills["smp"]["value"] == "Real polenta, no pot needed." and golden["smp"]["value"] == fills["smp"]["value"]
    assert fills["smp"]["alternatives"][0] == CHAIN["smp"] and fills["smp"]["alternatives_failed"][0] == ["coherence"]
    assert fills["smp"]["coherence"]["repaired"] is True


def test_a_rewrite_that_does_not_help_is_dropped_and_the_break_is_flagged(monkeypatch):
    fills = _fills("insight", "smp", "reasons_to_believe", "desired_response")
    still = _links(smp="still off")
    qs, calls, golden = _run(monkeypatch, [_links(smp="off"), still], fills, refine={"value": "Another line."})
    assert fills["smp"]["value"] == CHAIN["smp"] and golden["smp"]["value"] == CHAIN["smp"]
    assert fills["smp"]["coherence"]["holds"] is False
    assert [q["blocks_field"] for q in qs] == ["smp"] and qs[0]["priority"] == "medium"


def test_a_rewrite_that_fails_its_own_gate_is_never_used(monkeypatch):
    fills = _fills("insight", "smp", "reasons_to_believe", "desired_response")
    qs, calls, _g = _run(monkeypatch, [_links(smp="off")], fills, refine={"value": "Bad."}, gate=False)
    assert calls == {"judge": 1, "refine": 1, "gate": 1}
    assert fills["smp"]["value"] == CHAIN["smp"] and len(qs) == 1


def test_a_clients_own_line_is_never_rewritten(monkeypatch):
    fills = _fills("reasons_to_believe", "desired_response")          # insight and smp are the client's
    qs, calls, _g = _run(monkeypatch, [_links(smp="off")], fills)
    assert calls["refine"] == 0 and calls["judge"] == 1
    assert [q["blocks_field"] for q in qs] == ["smp"]                  # flagged, not rewritten


def test_both_judges_down_raises_for_the_caller_to_record(monkeypatch):
    """With Claude and jev both unable to answer the pass raises; fill_derivable_fields records it
    on the fields as did_not_run and the brief goes on."""
    import jev_checks
    fills = _fills("insight", "smp")

    def down(*a, **k):
        raise RuntimeError("judge down")
    monkeypatch.setattr(pb, "_json_call", down)
    monkeypatch.setattr(jev_checks, "coherence_links", lambda chain, later: None)
    golden = {k: ({"value": v} if k not in fills else fills[k]) for k, v in CHAIN.items()}
    try:
        pb._coherence_pass(fills, golden, SCHEMA, lambda f: (golden.get(f) or {}).get("value"),
                           lambda d: "", lambda *a, **k: True, None, lambda e: e)
        raised = False
    except RuntimeError:
        raised = True
    assert raised          # the pass itself raises; fill_derivable_fields records it as did_not_run


def test_jev_probabilities_decide_the_links_when_jev_answers(monkeypatch):
    """BRIEF_COHERENCE_JUDGE=jev: jev's p(holds) below COHERENCE_P is a broken link; the weakest is
    the lowest p; no Claude call."""
    import jev_checks
    seen = {}

    def fake_links(chain, later):
        seen["later"] = later
        return {"insight": 0.9, "smp": 0.2, "reasons_to_believe": 0.4, "desired_response": 0.8}
    monkeypatch.setattr(jev_checks, "coherence_links", fake_links)
    monkeypatch.setattr(pb, "_json_call", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no Claude call")))
    monkeypatch.setenv("BRIEF_COHERENCE_JUDGE", "jev")
    val = lambda f: CHAIN.get(f)
    out = pb._coherence_ask(val, pb._coherence_links(val))
    assert out["_by"] == "jev" and out["_weakest"] == "smp"
    assert seen["later"] == ["insight", "smp", "reasons_to_believe", "desired_response"]
    holds = {n.split("->")[1]: v["holds"] for n, v in out.items() if not n.startswith("_")}
    assert holds == {"insight": True, "smp": False, "reasons_to_believe": False, "desired_response": True}


def test_claude_leads_by_default_and_jev_answers_when_claude_cannot(monkeypatch):
    import jev_checks
    monkeypatch.setattr(jev_checks, "coherence_links", lambda chain, later: (_ for _ in ()).throw(AssertionError))
    monkeypatch.setattr(pb, "_json_call", lambda *a, **k: _links())
    val = lambda f: CHAIN.get(f)
    assert pb._coherence_ask(val, pb._coherence_links(val))["_by"] == "claude"     # default: jev never asked

    def down(*a, **k):
        raise RuntimeError("claude down")
    monkeypatch.setattr(pb, "_json_call", down)
    monkeypatch.setattr(jev_checks, "coherence_links", lambda chain, later: {f: 0.9 for f in later})
    assert pb._coherence_ask(val, pb._coherence_links(val))["_by"] == "jev"        # fallback


def test_jev_coherence_asks_one_yes_no_question_per_link_over_the_chain(monkeypatch):
    import jev_checks
    sent = {}

    def fake_ask(state, questions, what):
        sent["state"], sent["q"] = state, questions
        return {k: 0.7 for k in questions}
    monkeypatch.setattr(jev_checks, "_ask", fake_ask)
    got = jev_checks.coherence_links({k: str(v) for k, v in CHAIN.items()}, ["insight", "smp", "reasons_to_believe",
                                                                             "desired_response"])
    assert got == {"insight": 0.7, "smp": 0.7, "reasons_to_believe": 0.7, "desired_response": 0.7}
    assert all(q["type"] == "noul" and "PROPOSITION" in q["instructions"] for q in sent["q"].values())
    assert "brief_chain" in sent["state"] and "INSIGHT:" in sent["state"]["brief_chain"]


def test_broken_proof_reruns_the_proof_writer_and_keeps_it_when_the_link_then_holds(monkeypatch):
    fills = _fills("insight", "smp", "reasons_to_believe", "desired_response")
    asked = []

    def regen(fid, note):
        asked.append((fid, note))
        return {"value": ["Blind taste test: 8 in 10 could not tell."], "source": "inferred", "method": f"gen:{fid}"}, []
    qs, calls, golden = _run(monkeypatch, [_links(reasons_to_believe="proves convenience, not taste"), _links()],
                             fills, regen=regen)
    assert [f for f, _n in asked] == ["reasons_to_believe"] and "proves convenience" in asked[0][1]
    assert calls["refine"] == 0 and calls["judge"] == 2 and qs == []
    rtb = fills["reasons_to_believe"]
    assert rtb["value"] == ["Blind taste test: 8 in 10 could not tell."] and golden["reasons_to_believe"] is rtb
    assert rtb["alternatives"][0] == CHAIN["reasons_to_believe"]
    assert rtb["coherence"]["repair"]["how"] == "regenerate" and rtb["coherence"]["repair"]["outcome"] == "kept"


def test_no_proof_to_write_is_an_evidence_gap_and_the_original_stays(monkeypatch):
    fills = _fills("insight", "smp", "reasons_to_believe", "desired_response")
    qs, calls, golden = _run(monkeypatch, [_links(reasons_to_believe="no proof of taste")], fills,
                             regen=lambda fid, note: (None, []))
    rtb = fills["reasons_to_believe"]
    assert rtb["value"] == CHAIN["reasons_to_believe"] and golden["reasons_to_believe"]["value"] == CHAIN["reasons_to_believe"]
    assert rtb["coherence"]["repair"]["outcome"] == "evidence_gap" and calls["judge"] == 1
    assert [q["blocks_field"] for q in qs] == ["reasons_to_believe"]


def test_a_regenerated_field_that_does_not_fix_the_link_is_dropped(monkeypatch):
    fills = _fills("insight", "smp", "reasons_to_believe", "desired_response")
    qs, _c, golden = _run(monkeypatch, [_links(reasons_to_believe="off"), _links(reasons_to_believe="still off")], fills,
                          regen=lambda fid, note: ({"value": ["other"], "source": "inferred"}, []))
    assert fills["reasons_to_believe"]["value"] == CHAIN["reasons_to_believe"]
    assert golden["reasons_to_believe"]["value"] == CHAIN["reasons_to_believe"]
    assert fills["reasons_to_believe"]["coherence"]["repair"]["outcome"] == "not_better" and len(qs) == 1
