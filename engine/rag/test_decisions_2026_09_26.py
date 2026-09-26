"""Sai's decisions of 2026-09-26 pm (project_plan decisions_sai_2026_09_26.five_decisions):
provenance marks go to review.md and the brief object, never the client page; the
audience rule is the IPA one; the invented examples are replaced by sourced ones.
Offline. Run: cd engine/rag && python3 -m pytest -q test_decisions_2026_09_26.py
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
F = {f["id"]: f for f in SCHEMA["fields"]}
BRIEF = "Acme Bank is relaunching its app. Under-30s see it as their parents' bank. We must grow sign-ups by 20 percent."


def _run(monkeypatch, golden_fields):
    """run() with every stage stubbed and the given golden fields."""
    monkeypatch.setattr(pb, "capture_toon", lambda segs: {"fields": {"business_problem": {"value": "p", "status": "fact"}},
                                                          "how_to_win": {}, "open_questions": []})
    monkeypatch.setattr(pb, "how_to_win_toon", lambda segs: {})
    monkeypatch.setattr(pb, "extract_golden_brief", lambda text: {"fields": golden_fields})
    monkeypatch.setattr(pb, "score_betterbriefs", lambda text, fields=None: {"mode": "llm", "dimensions": []})
    return pb.run(None, golden=True, raw_text=BRIEF)


# ---------- decision 2: marks in review and the brief object, not on the client page ----------

def test_provenance_is_recorded_and_low_confidence_assumptions_become_questions(monkeypatch):
    """Every golden field gets a kind and a mark; an inferred value under the floor raises
    an open question; a generated value is 'proposed'."""
    out = _run(monkeypatch, {
        "background": {"value": "Acme relaunched its app", "source": "client_stated", "confidence": 0.95},
        "audience": {"value": "under-30s who bank with their parents' bank", "source": "inferred", "confidence": 0.5},
        "competitor_context": {"value": "RivalBank", "source": "inferred", "confidence": 0.8},
        "insight": {"value": "They stay because leaving feels like a verdict on their parents, because …",
                    "source": "inferred", "method": "gen:insight", "confidence": 0.9},
        "smp": {"value": None, "source": "missing"}})
    prov = out["loop2_golden"]["provenance"]
    assert prov["background"]["kind"] == "client_stated" and prov["background"]["mark"] == "client-stated"
    assert prov["audience"]["kind"] == "inferred" and prov["audience"]["mark"] == "our assumption, to confirm"
    assert prov["insight"]["kind"] == "generated" and prov["insight"]["mark"].startswith("proposed")
    assert prov["smp"]["kind"] == "missing"
    qs = [q for q in out["loop2_brief"]["open_questions"] if isinstance(q, dict) and q.get("blocks_field") == "audience"]
    assert len(qs) == 1 and "our assumption at confidence 0.50" in qs[0]["question"]
    assert not any(isinstance(q, dict) and q.get("blocks_field") == "competitor_context"
                   for q in out["loop2_brief"]["open_questions"])          # 0.8 is above the floor


def test_marks_reach_review_md_but_never_the_client_brief(monkeypatch):
    """review.md lists the provenance of every field; the client brief shows none of it."""
    out = _run(monkeypatch, {
        "background": {"value": "Acme relaunched its app", "source": "client_stated", "confidence": 0.95},
        "audience": {"value": "under-30s", "source": "inferred", "confidence": 0.7},
        "insight": {"value": "An insight, because it holds", "source": "inferred", "method": "gen:insight", "confidence": 0.9}})
    review = pb.render_markdown(out)
    assert "Provenance of every field" in review
    assert "audience: our assumption, to confirm (confidence 0.70)" in review
    assert "insight: proposed (written by the tool)" in review
    client = pb.render_client_brief(out)
    for mark in ("our assumption", "proposed", "client-stated", "Provenance"):
        assert mark not in client, mark
    assert "under-30s" in client and "An insight, because it holds" in client


def test_mark_provenance_is_idempotent_and_survives_a_missing_schema(monkeypatch):
    """Calling it twice adds one question; no schema file still marks (floor 0.6)."""
    out = {"loop2_golden": {"fields": {"audience": {"value": "x", "source": "inferred", "confidence": 0.2}}},
           "loop2_brief": {"open_questions": []}}
    pb._mark_provenance(out); pb._mark_provenance(out)
    assert len(out["loop2_brief"]["open_questions"]) == 1
    monkeypatch.setattr(pb, "HERE", Path("/nonexistent"))
    out = {"loop2_golden": {"fields": {"audience": {"value": "x", "source": "inferred", "confidence": 0.2}}},
           "loop2_brief": {"open_questions": []}}
    pb._mark_provenance(out)
    assert out["loop2_golden"]["provenance"]["audience"]["kind"] == "inferred" and len(out["loop2_brief"]["open_questions"]) == 1


# ---------- decision 3: the audience rule ----------

def test_audience_rule_is_the_ipa_one():
    """No 'one real human', no Conor; is_vivid + says_who_not; the example is a BBH group portrait."""
    a = F["audience"]
    assert "one real human" not in a["prompt"] and "Conor" not in json.dumps(a)
    assert "NOT for" in a["prompt"] and "Never invent a named person" in a["prompt"]
    assert [c["id"] for c in a["rubric"]] == ["within_limit", "is_vivid", "says_who_not"]
    assert "invented individual" in next(c for c in a["rubric"] if c["id"] == "is_vivid")["test"]
    assert a["good_example"].startswith("Busy (working) mothers") and len(a["good_examples"]) == 2
    assert "NatWest" not in a["good_examples"][1] and "owners/directors" in a["good_examples"][1]


def test_extractor_prompt_carries_the_new_audience_rule():
    """The golden extraction prompt is built from the schema, so it now asks for a group
    from the brief's data and forbids an invented person."""
    system = pb._build_golden_system()
    assert "Never invent a named person" in system and "Describe one real human" not in system
    assert "Busy (working) mothers" in system


def test_critic_judges_is_vivid_and_says_who_not():
    """The critic's audience checks are the new ones, pending as llm checks."""
    brief = gc.from_brief_object({"loop1_capture": {"fields": {}}, "loop2_brief": {"open_questions": []},
                                  "loop2_golden": {"fields": {"audience": {"value": "under-30s", "source": "inferred"}}}})
    v = gc.validate(SCHEMA, brief)
    aud = {c["id"]: c for c in next(fr for fr in v["fields"] if fr["id"] == "audience")["checks"]}
    assert set(aud) == {"within_limit", "is_vivid", "says_who_not"}
    assert aud["is_vivid"]["method"] == "llm" and aud["is_vivid"]["status"] == "review"


# ---------- decision 4: sourced examples ----------

def test_examples_are_sourced_and_the_lager_is_gone():
    """SMP, RTB and insight examples are the cited ones; no invented lager, Conor or carbs."""
    blob = json.dumps([[f.get(k) for k in ("prompt", "good_example", "good_examples", "bad_example", "bad_reason")]
                       for f in SCHEMA["fields"]])                        # the examples themselves, not the source notes
    for gone in ("lager that earns", "3g carbs", "Conor, 29", "blind taste test", "brewed with passion",
                 "drinking less mainstream lager", "choosing a drink is choosing a tribe"):
        assert gone not in blob, gone
    smp = F["smp"]
    assert smp["good_example"] == "A smart choice because you don't pay for what you don't need."
    assert len(smp["good_examples"]) == 3 and smp["bad_example"] == "Good drinks, fun times, real people."
    assert "three main ideas" in smp["bad_reason"] and len(smp["contrast_examples"]) == 3
    assert "Nicam" in F["reasons_to_believe"]["good_example"]
    assert "Skoda" in F["insight"]["good_example"] and "because" in F["insight"]["good_example"]
    for fid in ("smp", "audience", "reasons_to_believe", "insight"):
        assert F[fid].get("_example_sources"), fid


def test_generator_and_judge_show_several_shapes_and_the_contrast(monkeypatch):
    """The SMP writer sees three good examples; the judge prompt carries the
    proposition-vs-copy contrast; the critic prompt for the SMP carries it too."""
    system = pb._gen_field_system(F["smp"])
    for ex in F["smp"]["good_examples"]:
        assert ex in system
    assert "STYLE REFERENCES" in system
    seen = {}
    def fake(user, system=None, **k):
        seen["user"] = user
        return {"ranking": [0], "results": {"0": {c["id"]: {"pass": True} for c in F["smp"]["rubric"] if c["method"] == "llm"}}}
    monkeypatch.setattr(pb, "_json_call", fake)
    pb._judge_and_gate(F["smp"], [{"value": "Only Acme treats under-30s as adults"}])
    assert "PROPOSITION vs COPY" in seen["user"] and "Granada" in seen["user"] and "A party waiting to happen" in seen["user"]
    brief = gc.from_brief_object({"loop1_capture": {"fields": {}}, "loop2_brief": {"open_questions": []},
                                  "loop2_golden": {"fields": {"smp": {"value": "Only Acme treats under-30s as adults",
                                                                      "source": "inferred"},
                                                              "competitor_context": {"value": "RivalBank", "source": "client_stated"}}}})
    v = gc.validate(SCHEMA, brief)
    prompts = {b["field"]: b["prompt"] for b in gc.critic_prompts_batched(SCHEMA, brief, v)}
    assert "PROPOSITION vs COPY" in prompts["smp"] and "See What Develops" in prompts["smp"]
    assert "PROPOSITION vs COPY" not in prompts.get("competitor_context", "")
