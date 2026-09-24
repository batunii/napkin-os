"""Every decision the middleware emits says why, in the spec's shape: the
model writes the evidence where a model call decided, the code checks every
cite and derives the certainty; deterministic decisions are the code's."""

import pytest

from napkin import reasoning as rsn
from napkin.pipeline import extract, report, synthesise
from napkin.pipeline.research import Researcher

from fakes import FakeModel, FakeResearch, reasoning as model_reasoning
from test_pipeline import DOC, caps_for, rclan


def valid(decisions):
    for d in decisions:
        r = d.get("reasoning")
        assert r is not None, f"{d['id']} ({d['action']}) has no reasoning"
        assert not rsn.problems(r), (d["action"], rsn.problems(r))
        assert d["rationale"] == rsn.summary(r)
    return decisions


def cites(r):
    return {c for p in r["because"] for c in p.get("cites", [])}


# -- the helper ----------------------------------------------------------------

def test_from_model_keeps_what_the_cite_check_passes():
    raw = model_reasoning([("CSO says so", ["f_1"]), ("A made-up source", ["f_X"]), ("It is 41%", [])],
                          rejected=[("the blog", "tertiary")], attention="thin")
    r, notes = rsn.from_model(raw, decided="Pinned it.", known={"f_1"}, certainty_=rsn.certainty("high", "tiers"),
                              fallback=[rsn.point("code point", "f_1")], would_change_if="x")
    assert [p["point"] for p in r["because"]] == ["CSO says so"]
    assert len(notes) == 2 and "f_X" in notes[0] and "figure" in notes[1]
    assert r["certainty"] == {"level": "high", "why": "tiers"}  # the code's, whatever the model thinks
    assert r["attention"] == "thin" and r["rejected"] == [{"option": "the blog", "why": "tertiary"}]


def test_from_model_falls_back_to_the_codes_reason_and_says_so():
    raw = model_reasoning([("A made-up source", ["f_X"])], only="one")
    r, _ = rsn.from_model(raw, decided="Pinned it.", known={"f_1"}, certainty_=rsn.certainty("low", "one source"),
                          fallback=[rsn.point("The layer row says 41%", "f_1")], would_change_if="x")
    assert r["because"] == [{"point": "The layer row says 41%", "cites": ["f_1"]}]
    assert "did not survive the cite check" in r["attention"]
    assert r["only_option"] == "one"
    r, _ = rsn.from_model(None, decided="d", known=set(), certainty_=rsn.certainty("low", "w"),
                          fallback=[rsn.point("p")], would_change_if="x", only_option="only")
    assert "did not survive" in r["attention"]


def test_make_refuses_a_broken_shape():
    with pytest.raises(ValueError, match="states a figure and cites nothing"):
        rsn.make("d", [rsn.point("41% of it")], rsn.certainty("low", "w"), "x", only_option="o")
    with pytest.raises(ValueError, match="only_option"):
        rsn.make("d", [rsn.point("p")], rsn.certainty("low", "w"), "x")
    with pytest.raises(ValueError, match="because has no point"):
        rsn.make("d", [], rsn.certainty("low", "w"), "x", only_option="o")


# -- the stages ----------------------------------------------------------------

def test_extract_reasoning_is_the_models_cite_checked_with_derived_certainty(store):
    caps = caps_for(store)
    clan = {"id": DOC, "version": "1", "data": {}, "facts": [], "findings": [], "decision_chain": {}}
    _, change, _ = extract.run_extract(DOC, "1", clan, {"prompt": "Launch in Ireland on a €200k budget."},
                                       "t@1.0", caps)
    [d] = valid(change["decisions"])
    r = d["reasoning"]
    mat = next(iter(change["data_patch"]["materials"]))
    assert [p["point"] for p in r["because"]] == ["The prompt states the markets and the budget"]
    assert cites(r) == {mat}                              # the invented mat_NOTGIVEN point was dropped
    assert r["certainty"]["level"] == "high"              # every value on a verbatim quote, nothing dropped
    assert any("fill" in x["option"] for x in r["rejected"])  # the code adds what it abstained on
    assert r["decided"].startswith("Filled ")


def test_research_reasoning_is_the_codes_from_the_evidence(store):
    caps = caps_for(store)
    _, change, _ = Researcher(DOC, "3", rclan(), "t@1.0", caps, ["market_structure"], ["IE", "GB"],
                              ["automotive.ev_charging"]).run()
    decs = valid(change["decisions"])
    ids = {f["id"] for f in change["facts_append"]} | {s for f in change["facts_append"] for s in f["sources"]}
    merge = next(d for d in decs if d["action"] == "research_merge")
    assert cites(merge["reasoning"]) <= ids
    # certainty is the pins' derived confidence, never averaged
    assert merge["reasoning"]["certainty"]["level"] == min((f["confidence"] for f in change["facts_append"]),
                                                          key=rsn.LEVELS.index)
    assert "tier" in merge["reasoning"]["certainty"]["why"]

    # the contest: each run's value with its fact, nothing picked, a person should look
    [ct] = [d for d in decs if d["kind"] == "contest"]
    r = ct["reasoning"]
    contested = change["data_patch"]["selection"]["contested"][0]
    assert {v["fact_id"] for v in contested["values"]} <= cites(r)
    assert [p["point"].split(" says ")[0] for p in r["because"]] == ["The market_structure/IE run",
                                                                      "The market_structure/GB run"]
    assert any("silently" in x["option"] for x in r["rejected"])
    assert "primary source" in r["would_change_if"] and r["attention"]

    # a run: coverage decides the certainty
    runs = [d for d in decs if d["action"] == "research_run"]
    assert {d["reasoning"]["certainty"]["level"] for d in runs} == {"medium"}  # the flagship year is single-source


def test_a_failed_run_is_low_certainty_and_asks_for_attention(store):
    caps = caps_for(store, research=FakeResearch(fail_on={("media_spend", "IE")}))
    _, change, _ = Researcher(DOC, "3", rclan(["IE"]), "t@1.0", caps, ["media_spend"], ["IE"],
                              ["automotive.ev_charging"]).run()
    run = next(d for d in valid(change["decisions"]) if d["action"] == "research_run")
    assert run["reasoning"]["certainty"]["level"] == "low"
    assert "failed" in run["reasoning"]["attention"]
    assert cites(run["reasoning"]) == {g["id"] for g in change["data_patch"]["selection"]["gaps"]}


def test_finding_certainty_is_the_findings_derived_confidence(store):
    caps = caps_for(store)
    _, rch, _ = Researcher(DOC, "3", rclan(["IE"]), "t@1.0", caps, ["media_spend", "market_structure"], ["IE"],
                           ["automotive.ev_charging"]).run()
    clan = dict(rclan(["IE"]), facts=rch["facts_append"])
    _, change, _ = synthesise.run_synthesis(DOC, "3", clan, {}, "t@1.0", caps)
    decs = valid(change["decisions"])
    by_fi = {f["decision"]: f for f in change["findings_append"]}
    for d in decs:
        fi = by_fi[d["id"]]
        r = d["reasoning"]
        assert r["certainty"]["level"] == fi["confidence"]
        assert cites(r) <= set(fi["cites"])
        assert not any("99%" in p["point"] for p in r["because"])  # the uncited figure was dropped


def test_report_reasoning_steps_down_when_the_cite_rule_dropped_claims(store):
    caps = caps_for(store)
    _, rch, _ = Researcher(DOC, "3", rclan(["IE"]), "t@1.0", caps, ["media_spend"], ["IE"],
                           ["automotive.ev_charging"]).run()
    clan = dict(rclan(["IE"]), facts=rch["facts_append"])
    rpt, _, _, r = report.compose(DOC, clan, "t@1.0", caps)
    assert not rsn.problems(r)
    lead = min((f["confidence"] for f in rch["facts_append"] if f["id"] in rpt["headline"]["cites"]),
               key=rsn.LEVELS.index)
    assert r["certainty"]["level"] == rsn.step_down(lead)  # the fake's made-up 42 was dropped
    assert "dropped" in r["certainty"]["why"] and "dropped" in r["attention"]
    assert cites(r) <= {f["id"] for f in rch["facts_append"]}


def test_select_reasoning_marks_an_inferred_skip(store):
    from test_pipeline import SCOPE  # noqa: F401  (the fixture's scope)
    from napkin.pipeline.campaign import CampaignJob
    from napkin.config import Settings

    def select(p):
        return {"lenses": [{"lens": "category_codes", "run": False, "skip_markets": [],
                            "reason": "Nothing to read for these categories."}],
                "reasoning": model_reasoning([("The category has no codes worth reading", [p["prompt_material_id"]])],
                                             rejected=[("research every lens", "wasted spend")])}
    caps = caps_for(store, model=FakeModel({"select": select}))
    job = CampaignJob("job_t", DOC, "t@1.0", rclan(["IE"]), {"prompt": "BMW in Ireland.", "attachments": []}, caps,
                      Settings())
    job.stage_select()
    d = job.chunks[-1].decisions[0]
    r = valid([d])[0]["reasoning"]
    assert r["certainty"]["level"] == "medium" and "Category codes" in r["attention"]
    assert r["because"][0]["cites"] == [job.materials()[0].id]
