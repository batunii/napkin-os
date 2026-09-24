from napkin.rules import budget, identify, markets, merge
from napkin.rules.cite import clean_claim
from napkin.rules.confidence import coverage_of, fact_confidence, finding_confidence, merge_coverage
from napkin.rules.figures import quote_supports, unsourced_figures
from napkin.rules.quotes import find_quote, verbatim
from napkin.rules.tiering import registrable, tier_for


# -- tiering ----------------------------------------------------------------
def test_tiering_policy():
    assert tier_for("https://www.cso.ie/en/releasesandpublications/") == "primary"
    assert tier_for("https://www.gov.ie/en/publication/x") == "primary"
    assert tier_for("https://www.seai.ie/grants/electric-vehicle-grants") == "primary"
    assert tier_for("https://alternative-fuels-observatory.ec.europa.eu/x") == "primary"
    assert tier_for("https://www.bmwgroup.com/en/investor-relations/annual-report.html") == "primary"
    assert tier_for("https://www.simi.ie/en/news") == "secondary"
    assert tier_for("https://www.campaignlive.co.uk/article") == "secondary"
    assert tier_for("https://someblog.example.com/post") == "tertiary"
    assert tier_for("not a url") == "tertiary"
    assert registrable("https://news.bbc.co.uk/x") == "bbc.co.uk"


# -- confidence ---------------------------------------------------------------
def test_confidence_is_derived_from_tier_and_corroboration():
    p = {"tier": "primary", "domain": "cso.ie"}
    s = {"tier": "secondary", "domain": "simi.ie"}
    t = {"tier": "tertiary", "domain": "blog.net"}
    assert fact_confidence([p]) == "medium"
    assert fact_confidence([s]) == "low"
    assert fact_confidence([p, s]) == "high"
    assert fact_confidence([s, {"tier": "secondary", "domain": "smmt.co.uk"}]) == "medium"
    # two sources on one domain are not independent
    assert fact_confidence([s, dict(s)]) == "low"
    # tertiary only: capped at medium however many
    assert fact_confidence([t, {"tier": "tertiary", "domain": "a.com"}, {"tier": "tertiary", "domain": "b.com"}]) == "medium"
    assert fact_confidence([{"tier": "reviewer-verified", "domain": "human:x"}]) == "high"
    assert fact_confidence([]) == "low"


def test_finding_confidence():
    hi, med = {"confidence": "high"}, {"confidence": "medium"}
    assert finding_confidence([hi, hi]) == "high"
    assert finding_confidence([hi, med]) == "medium"
    assert finding_confidence([hi]) == "medium"            # a single citation steps down
    assert finding_confidence([hi, dict(hi, stale={"x": 1})]) == "medium"
    assert finding_confidence([{"confidence": "low"}]) == "low"  # floor


def test_coverage():
    assert coverage_of([]) == "empty"
    assert coverage_of([2, 3]) == "filled"
    assert coverage_of([2, 1]) == "thin"
    assert merge_coverage(["filled", "filled"]) == "filled"
    assert merge_coverage(["empty", "empty"]) == "empty"
    assert merge_coverage(["filled", "empty"]) == "thin"


# -- merge / contests -----------------------------------------------------------
def c(entity, key, value, market=None, run="market_structure/IE", sources=("src_a",)):
    return {"entity": entity, "key": key, "value": value, "market": market, "unit": "x", "run": run,
            "sources": list(sources)}


def test_merge_same_identity_same_value_unions_sources():
    out = merge.merge([c("category/a.b", "market.size", 5, "IE", sources=["src_a"]),
                       c("category/a.b", "market.size", 5.0, "IE", "market_structure/IE", ["src_b"])], {}, set())
    assert len(out["pins"]) == 1 and out["pins"][0]["sources"] == ["src_a", "src_b"] and not out["contests"]


def test_merge_other_market_is_two_facts():
    out = merge.merge([c("category/a.b", "market.size", 5, "IE"), c("category/a.b", "market.size", 7, "GB")], {}, set())
    assert len(out["pins"]) == 2 and not out["contests"]


def test_merge_disagreement_is_a_contest_and_nothing_is_picked():
    out = merge.merge([c("category/a.b", "market.year", "2019", None, "market_structure/IE"),
                       c("category/a.b", "market.year", "2020", None, "market_structure/GB")], {}, set())
    assert not out["pins"] and len(out["contests"]) == 1
    assert {v["value"] for v in out["contests"][0]["values"]} == {"2019", "2020"}


def test_merge_against_a_pin():
    pinned = {("category/a.b", "market.size", "IE"): {"id": "f_X", "value": 4}}
    out = merge.merge([c("category/a.b", "market.size", 5, "IE")], pinned, set())
    assert not out["pins"] and out["contests"][0]["pinned"]["id"] == "f_X"
    out = merge.merge([c("category/a.b", "market.size", 4, "IE")], pinned, set())
    assert out["already"] and not out["pins"]
    out = merge.merge([c("category/a.b", "market.size", 5, "IE")], pinned, {"category/a.b:market.size@IE"})
    assert not out["contests"]  # an open contest is not opened twice


# -- quotes and figures -------------------------------------------------------------
def test_quote_verification():
    text = "We’re relaunching  Harbour Tonic\nin Ireland."
    assert find_quote(text, "relaunching  Harbour") is not None
    assert verbatim(text, "We're relaunching Harbour Tonic in Ireland.") == text  # folds quote marks/space
    assert verbatim(text, "relaunching Harbour Soda") is None
    assert verbatim(text, "") is None


def test_quote_supports_numbers():
    assert quote_supports(0.34, "proportion", "34% of buyers")
    assert quote_supports(1_200_000, "eur", "a budget of €1.2 million")
    assert quote_supports(12500, "count", "12,500 new EVs were registered")
    assert not quote_supports(15000, "count", "12,500 new EVs were registered")
    assert quote_supports("2024-03", "date", "launched in March 2024")
    assert quote_supports("a text value", "text", "anything")


def test_unsourced_figures_matches_the_report_rule():
    pins = {"f_A": {"value": 0.34, "unit": "proportion"}, "f_B": {"value": 12500, "unit": "count"}}
    fi = {"fi_A": {"statement": "Up 21% on last year", "status": "proposed"}}
    assert unsourced_figures("34% moderate", ["f_A"], pins, fi) == []
    assert unsourced_figures("0.34 share", ["f_A"], pins, fi) == []
    assert unsourced_figures("12,500 units", ["f_B"], pins, fi) == ["12,500"]  # the pin says 12500
    assert unsourced_figures("up 21%", ["fi_A"], pins, fi) == []
    assert unsourced_figures("BMW i4 sells", ["f_A"], pins, fi, names=["BMW i4"]) == []
    assert unsourced_figures("42 of them", ["f_A"], pins, fi) == ["42"]


def test_clean_claim():
    pins = {"f_A": {"value": 3, "unit": "count"}}
    fi = {"fi_R": {"statement": "x", "status": "rejected"}}
    assert clean_claim("3 cases", ["f_A", "src_x"], pins, fi) == ({"text": "3 cases", "cites": ["f_A"]}, None)
    assert clean_claim("text", ["fi_R"], pins, fi)[0] is None       # a rejected finding is not a cite
    assert clean_claim("text", [], pins, fi)[0] is None
    assert clean_claim("4 cases", ["f_A"], pins, fi)[0] is None


# -- budget, markets ---------------------------------------------------------------------
def test_budget_band_is_computed_never_the_figure():
    assert budget.band(400_000, "EUR") == ("250k_1m", None)
    assert budget.band(40_000, "EUR")[0] == "under_50k"
    assert budget.band(240_000, "GBP")[0] is None   # too near a band edge to convert
    assert budget.band(100_000, "GBP")[0] == "50k_250k"
    assert budget.band(100, "JPY")[0] is None


def test_markets():
    assert markets.normalise("uk") == "GB" and markets.normalise("XX") is None
    assert markets.from_text("Ireland and the UK") == ["IE", "GB"]
    assert markets.from_text("Northern Ireland") == ["GB"]


# -- identify ----------------------------------------------------------------------------
def b(name, basis="none", comparator=False, quote=None):
    return {"ref": identify.brand_ref(name), "name": name, "basis": basis, "comparator": comparator,
            "span": {"material_id": "mat_p1", "locator": "¶1", "quote": quote or name}}


def test_identify_label_wins():
    kind, sub = identify.decide_subject([b("Harbour Tonic", "brand_label", quote="Brand: Harbour Tonic"),
                                         b("Saltmarsh Soda", comparator=True)])
    assert kind == "subject" and sub["name"] == "Harbour Tonic"


def test_identify_only_brand_is_the_subject():
    kind, sub = identify.decide_subject([b("BMW", quote="BMW is trying to enter the Ev hybrid market")])
    assert kind == "subject" and sub["ref"] == "brand/bmw"


def test_identify_never_guesses_between_two():
    kind, opts = identify.decide_subject([b("Harbour Tonic"), b("Saltmarsh Soda")])
    assert kind == "ask"
    assert [o["label"] for o in opts] == ["Harbour Tonic", "Saltmarsh Soda", "None of these"]
    assert "value" not in opts[-1] and all(o["origin"] == "extracted" and o["source"] for o in opts[:2])


def test_identify_claimed_basis_needs_its_cue_and_the_name():
    # a model claiming 'brand_label' without a label in the quote is not believed
    kind, _ = identify.decide_subject([b("A Brand", "brand_label", quote="A Brand"), b("Other")])
    assert kind == "ask"
    kind, _ = identify.decide_subject([b("A Brand", "named_as_ours", quote="for the one we won"), b("Other")])
    assert kind == "ask"
    kind, sub = identify.decide_subject([b("A Brand", "named_as_ours", quote="our client A Brand"), b("Other")])
    assert kind == "subject" and sub["name"] == "A Brand"


def test_identify_only_a_comparator_or_nothing_asks():
    assert identify.decide_subject([b("Rival", comparator=True)])[0] == "ask"
    assert identify.decide_subject([]) == ("ask_text", None)


def test_category_options_are_ranked_leaves_plus_both_and_escape():
    sp = {"material_id": "mat_p1", "locator": "¶1", "quote": "EV"}
    opts = identify.category_options([("automotive.ev_charging", sp), ("automotive.hybrid", sp)], "extracted",
                                     {"automotive.ev_charging": "EV and charging", "automotive.hybrid": "Hybrid"})
    assert [o["id"] for o in opts] == ["automotive_ev_charging", "automotive_hybrid", "both", "other"]
    assert opts[2]["value"] == ["automotive.ev_charging", "automotive.hybrid"]
    assert "value" not in opts[3]


def test_brand_from_text():
    opts, found = identify.brand_options_from_text("harbour tonic", [("brand/harbour-tonic", "Harbour Tonic")])
    assert found and opts[0]["value"]["ref"] == "brand/harbour-tonic" and opts[0]["origin"] == "stated"
    opts, _ = identify.brand_options_from_text("Kestrel", [])
    assert opts[0]["value"] == {"ref": "brand/kestrel", "name": "Kestrel"} and opts[-1]["id"] == "none"
