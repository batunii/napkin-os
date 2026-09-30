"""The stages with fake components: what the middleware decides."""

import json
import threading
from types import SimpleNamespace

import pytest

from napkin.capabilities import Capabilities
from napkin.model import ModelError, ModelPort
from napkin.pipeline import extract, report, synthesise
from napkin.pipeline.research import Researcher

from fakes import FakeModel, FakeResearch

SCOPE = {"org": "org/test-agency", "brand": "brand/test"}
DOC = "11111111-2222-4333-8444-555555555555"


def caps_for(store, model=None, research=None):
    return Capabilities(handler="t@1.0", scope=SCOPE, model_port=ModelPort(model or FakeModel(), "claude-opus-5", 30),
                        research_port=research or FakeResearch(), layer_store=store,
                        research_semaphore=threading.Semaphore(4))


def stated(v, gate):
    return {"value": v, "origin": "stated", "gate": gate, "by": "human:u", "decision": "d_TESTSTATED1"}


def rclan(markets=("IE", "GB")):
    return {"id": DOC, "version": "3", "facts": [], "findings": [], "decision_chain": {"decisions": []},
            "data": {"campaign": {"brand": stated({"ref": "brand/bmw", "name": "BMW"}, "created"),
                                  "categories": stated(["automotive.ev_charging"], "research"),
                                  "markets": stated(list(markets), "research")}}}


def test_research_writes_layers_first_then_pins_contests_and_reuses(store):
    caps = caps_for(store)
    result, change, hits = Researcher(DOC, "3", rclan(), "t@1.0", caps, ["market_structure", "media_spend"],
                                      ["IE", "GB"], ["automotive.ev_charging"]).run()
    pins = change["facts_append"]
    assert pins and all(p["origin"].startswith("fact://category/automotive.ev_charging/") for p in pins)
    # D1: every pin is a layer row, written with the merge decision
    merge = next(d for d in change["decisions"] if d["action"] == "research_merge")
    for p in pins:
        row = caps.layers.resolve(p["origin"])
        assert row and row["id"] == p["id"] and row["decision"] == merge["id"] and p["decision"] == merge["id"]
        assert p["confidence"] == "high"  # primary + independent secondary
        assert set(p["sources"]) == set(row["sources"]) and len(p["sources"]) == 2
    # the market-independent year the two markets disagree on is a contest; neither value is pinned
    ct = change["data_patch"]["selection"]["contested"]
    assert len(ct) == 1 and ct[0]["key"] == "category/automotive.ev_charging:market.flagship_year"
    assert {v["value"] for v in ct[0]["values"]} == {"2019", "2020"}
    assert not {v["fact_id"] for v in ct[0]["values"]} & {p["id"] for p in pins}
    assert any(d["kind"] == "contest" for d in change["decisions"])
    # sources carry their tier in the result
    assert {s["tier"] for s in result["sources"].values()} == {"primary", "secondary", "tertiary"}
    # a second campaign reuses the layer's facts: no research call
    r2 = FakeResearch()
    caps2 = caps_for(store, research=r2)
    result2, change2, _ = Researcher("22222222-2222-4333-8444-555555555555", "1", rclan(), "t@1.0", caps2,
                                     ["market_structure", "media_spend"], ["IE", "GB"], ["automotive.ev_charging"]).run()
    assert r2.calls == [] and result2["reused"] > 0
    assert {p["id"] for p in change2["facts_append"]} == {p["id"] for p in pins}


def _stored_fact(caps, market, key="market.flagship_year", value="2019"):
    """A fresh, active row in the category layer, as an earlier campaign would have left it."""
    import datetime
    src = caps.layers.add_source({"uri": "https://www.cso.ie/x", "tier": "primary", "domain": "cso.ie", "licence": "open"})
    caps.layers.append({"layer": "category", "entity": "category/automotive.ev_charging", "key": key, "market": market,
                        "value": value, "unit": "text", "as_of": "2025-12-31",
                        "retrieved_at": datetime.date.today().isoformat(), "sources": [src], "licence": "open"},
                       {"id": "d_TESTSTORED1", "kind": "pin", "handler": "t@1.0", "action": "t", "rationale": "r",
                        "cites": []})


def test_a_fact_about_no_market_does_not_stand_in_for_a_markets_research(store):
    """An earlier Ireland job left a fact that names no market. It is returned for every market, but it
    says nothing about Germany, so Germany's topic must still go to the web."""
    caps = caps_for(store)
    _stored_fact(caps, None)
    r = FakeResearch()
    result, _, _ = Researcher(DOC, "3", rclan(["DE"]), "t@1.0", caps_for(store, research=r), ["market_structure"],
                              ["DE"], ["automotive.ev_charging"]).run()
    assert r.calls and result["reused"] == 0


def test_a_fresh_fact_about_the_market_still_stands_in_for_its_research(store):
    caps = caps_for(store)
    _stored_fact(caps, "DE")
    r = FakeResearch()
    result, _, _ = Researcher(DOC, "3", rclan(["DE"]), "t@1.0", caps_for(store, research=r), ["market_structure"],
                              ["DE"], ["automotive.ev_charging"]).run()
    assert r.calls == [] and result["reused"] == 1


class _RecordingResearch(FakeResearch):
    def __init__(self):
        super().__init__()
        self.queries = []

    def search(self, query, *a, **kw):
        self.queries.append(query)
        return super().search(query, *a, **kw)


def _run_with_competitors(store, names, brand=None):
    clan = rclan(["IE"])
    if names:
        clan["data"]["campaign"]["competitor_set"] = stated(
            [{"ref": f"brand/c{i}", "name": n} for i, n in enumerate(names)], "research")
    if brand:
        clan["data"]["campaign"]["brand"] = stated({"ref": "brand/long", "name": brand}, "created")
    r = _RecordingResearch()
    Researcher(DOC, "3", clan, "t@1.0", caps_for(store, research=r), ["market_structure"], ["IE"],
               ["automotive.ev_charging"]).run()
    return r.queries


def test_a_long_competitor_list_still_gives_a_question_the_port_accepts(store):
    """The research port refuses a question over 500 characters (a 400, so the topic returns nothing)."""
    q = _run_with_competitors(store, [f"Competitor Brand Number {i}" for i in range(40)])
    assert len(q) == 1 and len(q[0]) <= 500
    assert "Competitor Brand Number 0" in q[0] and "Competitor Brand Number 39" not in q[0]  # whole names, in order
    assert not q[0].rstrip(".").endswith(",")


def test_a_short_competitor_list_leaves_the_question_as_it_was(store):
    q = _run_with_competitors(store, ["Tesla", "Polestar"])
    assert q[0].endswith("Brand: BMW. Comparators: Tesla, Polestar.")


def test_a_very_long_brand_name_still_gives_an_accepted_question(store):
    q = _run_with_competitors(store, ["Tesla"], brand="B" * 600)
    assert len(q[0]) <= 500


def test_each_entitys_stored_facts_are_fetched_once_per_job_not_once_per_topic(store):
    """6 topics (3 lenses x 2 markets) used to make 6 x 2 database reads; the reads are the same for every topic."""
    caps = caps_for(store)
    _stored_fact(caps, "IE")
    before = len(store.service.requests)
    result, _, _ = Researcher(DOC, "3", rclan(["IE", "GB"]), "t@1.0", caps_for(store, research=FakeResearch()),
                              ["market_structure", "media_spend", "consumer_culture"], ["IE", "GB"],
                              ["automotive.ev_charging"]).run()
    gets = [r for r in store.service.requests[before:] if r.method == "GET" and r.url.path.endswith("/facts")]
    assert len(gets) == 2                    # the category and the brand, once each
    assert result["reused"] >= 1             # and the stored IE fact is still found for its own topic


def test_the_fixed_measures_are_well_formed_and_include_every_asked_for_name():
    import re
    from napkin.measures import MEASURES, SYNONYM
    from napkin.pipeline.research import LENS_QUESTIONS, UNITS
    for lens, (_, wanted) in LENS_QUESTIONS.items():
        keys = [m["key"] for m in MEASURES[lens]]
        assert len(keys) >= 3 and len(keys) == len(set(keys)), lens
        assert set(wanted) <= set(keys), (lens, set(wanted) - set(keys))
        assert all(re.fullmatch(r"[a-z0-9_]+", k) and m["unit"] in UNITS and m["means"]
                   for k, m in zip(keys, MEASURES[lens])), lens
    assert all(SYNONYM[(l, s)] in [m["key"] for m in MEASURES[l]] and s not in [m["key"] for m in MEASURES[l]]
               for (l, s) in SYNONYM)


def _spy_extraction(seen, key_suffix=None, not_found=()):
    """An extraction that records what it was given and returns one fact under `key_suffix` (or none)."""
    def respond(p):
        seen.append(p)
        if key_suffix is None:
            return {"facts": [], "not_found": list(not_found)}
        import re as _re
        s = p["sources"][0]
        pct = int(_re.search(r"(\d+)%", s["excerpts"][0]).group(1))  # the number the fake source states
        return {"facts": [{"about": "category", "category": "automotive.ev_charging", "competitor": None,
                           "key_suffix": key_suffix, "value_number": pct / 100, "value_text": None, "value_boolean": None,
                           "unit": "proportion", "as_of": None, "market_specific": True,
                           "evidence": [{"source_id": s["source_id"], "quote": s["excerpts"][0]}]}],
                "not_found": list(not_found)}
    return FakeModel({"extract_facts": respond})


def test_the_extraction_is_given_the_lens_measures_to_name_its_facts_by(store):
    seen = []
    Researcher(DOC, "3", rclan(["IE"]), "t@1.0", caps_for(store, model=_spy_extraction(seen)), ["market_structure"],
               ["IE"], ["automotive.ev_charging"]).run()
    ms = {m["key"]: m for m in seen[0]["measures"]}
    assert {"size_eur", "share"} <= set(ms) and all(m["means"] and m["unit"] for m in ms.values())
    assert "size_eur" in seen[0]["wanted"]  # what was asked for is unchanged


def test_a_name_the_model_invents_is_rewritten_to_the_fixed_name_before_facts_merge(store):
    from napkin.measures import MEASURES
    invented = next(m for m in MEASURES["market_structure"] if m["key"] == "share")["synonyms"][0]
    _, change, _ = Researcher(DOC, "3", rclan(["IE"]), "t@1.0", caps_for(store, model=_spy_extraction([], invented)),
                              ["market_structure"], ["IE"], ["automotive.ev_charging"]).run()
    assert [f["key"] for f in change["facts_append"]] == ["market.share"]


def test_a_fact_under_a_qualified_asked_for_name_is_not_reported_as_a_gap(store):
    _, change, _ = Researcher(DOC, "3", rclan(["IE"]), "t@1.0",
                              caps_for(store, model=_spy_extraction([], "share.acme", not_found=["share", "size_eur"])),
                              ["market_structure"], ["IE"], ["automotive.ev_charging"]).run()
    gap_keys = [g["key"] for g in change["data_patch"]["selection"]["gaps"]]
    assert any(k.endswith(":market.size_eur") for k in gap_keys)
    assert not any(k.endswith(":market.share") for k in gap_keys)


def test_research_carries_its_evidence_into_the_document(store):
    """Every pin keeps the verbatim quote each source gave, and every source a pin or a
    contest value cites arrives as a record in sources_append: a citation leads somewhere."""
    caps = caps_for(store)
    _, change, _ = Researcher(DOC, "3", rclan(), "t@1.0", caps, ["market_structure", "media_spend"],
                              ["IE", "GB"], ["automotive.ev_charging"]).run()
    pins, recs = change["facts_append"], {s["id"]: s for s in change["sources_append"]}
    for p in pins:
        assert set(p["quotes"]) == set(p["sources"]), p["id"]
        assert all(q.strip() for q in p["quotes"].values())
    ct = change["data_patch"]["selection"]["contested"][0]
    cited = {s for p in pins for s in p["sources"]} | {s for v in ct["values"] for s in v["sources"]}
    assert cited == set(recs)
    for s in recs.values():
        assert s["uri"].startswith("http") and s["tier"] and s["title"] and "url" not in s
    assert all(set(v["quotes"]) == set(v["sources"]) for v in ct["values"])
    # A second campaign reuses the layer's rows: its pins still quote, and its
    # sources still resolve to where they were read.
    _, change2, _ = Researcher("22222222-2222-4333-8444-555555555555", "1", rclan(), "t@1.0",
                               caps_for(store, research=FakeResearch()), ["market_structure", "media_spend"],
                               ["IE", "GB"], ["automotive.ev_charging"]).run()
    recs2 = {s["id"]: s for s in change2["sources_append"]}
    for p in change2["facts_append"]:
        assert set(p["sources"]) <= set(recs2) and set(p["quotes"]) == set(p["sources"])
    assert all(s["uri"].startswith("http") for s in recs2.values())


def test_a_failed_unit_is_a_gap_with_its_error(store):
    caps = caps_for(store, research=FakeResearch(fail_on={("media_spend", "IE")}))
    _, change, _ = Researcher(DOC, "3", rclan(["IE"]), "t@1.0", caps, ["media_spend"], ["IE"],
                              ["automotive.ev_charging"]).run()
    sel = change["data_patch"]["selection"]
    assert sel["coverage"] == {"media_spend": "empty"}
    assert "research failed" in sel["gaps"][0]["note"] and not change["facts_append"]


def test_a_fact_whose_quote_is_not_in_the_source_is_rejected(store):
    def lying(p):
        s = p["sources"][0]
        return {"facts": [
            {"about": "category", "category": "automotive.ev_charging", "competitor": None, "key_suffix": "made_up",
             "value_number": 0.5, "value_text": None, "value_boolean": None, "unit": "proportion", "as_of": None,
             "market_specific": True, "evidence": [{"source_id": s["source_id"], "quote": "Half of all cars are EVs."}]},
            {"about": "category", "category": "automotive.ev_charging", "competitor": None, "key_suffix": "wrong_number",
             "value_number": 0.77, "value_text": None, "value_boolean": None, "unit": "proportion", "as_of": None,
             "market_specific": True, "evidence": [{"source_id": s["source_id"], "quote": s["excerpts"][0]}]}],
            "not_found": []}
    caps = caps_for(store, model=FakeModel({"extract_facts": lying}))
    _, change, _ = Researcher(DOC, "3", rclan(["IE"]), "t@1.0", caps, ["media_spend"], ["IE"],
                              ["automotive.ev_charging"]).run()
    assert change["facts_append"] == []
    assert change["data_patch"]["selection"]["gaps"][0]["note"].startswith("the sources held nothing")


def test_model_output_is_validated_retried_once_then_fails(store):
    bad = FakeModel({"select": lambda p: {"lenses": "not a list"}})
    port = ModelPort(bad, "claude-opus-5", 30)
    from napkin.model import Usage
    with pytest.raises(ModelError, match="no valid structured output"):
        port.call("select", "s", {}, {"type": "object", "required": ["lenses"], "additionalProperties": False,
                                      "properties": {"lenses": {"type": "array"}}}, usage=Usage(), attribution="t")
    assert [c[0] for c in bad.calls] == ["select", "select"]
    retry = bad.calls[1][2]["messages"]
    assert retry[-1]["role"] == "user" and "does not validate" in retry[-1]["content"]
    # the schema reaches the API through output_config, stripped of what it does not enforce
    assert bad.calls[0][2]["output_config"]["format"]["type"] == "json_schema"


def test_extract_drops_a_value_whose_quote_is_not_in_the_material(store):
    def extract_lies(p):
        out = FakeModel.r_extract(p)
        mid = p["materials"][0]["material_id"]
        out["problem"] = {"quote": "Sales are collapsing everywhere.", "material_id": mid}
        out["markets"] = [{"code": "UK", "quote": "Ireland", "material_id": mid}]
        out["in_market"] = {"from": "2027-03-01", "to": "2027-05-31", "quote": "next spring", "material_id": mid}
        return out
    caps = caps_for(store, model=FakeModel({"extract": extract_lies}))
    clan = {"id": DOC, "version": "1", "data": {}, "facts": [], "findings": [], "decision_chain": {}}
    result, change, _ = extract.run_extract(DOC, "1", clan, {"prompt": "Launch in Ireland next spring."}, "t@1.0", caps)
    camp = change["data_patch"]["campaign"]
    assert "problem" not in camp and "in_market" not in camp
    assert camp["markets"]["value"] == ["GB"]  # UK is normalised; the quote was verified
    assert "problem" in result["abstained"]


def test_synthesis_drops_a_statement_with_an_unsourced_figure(store):
    caps = caps_for(store)
    _, rch, _ = Researcher(DOC, "3", rclan(["IE"]), "t@1.0", caps, ["media_spend"], ["IE"],
                           ["automotive.ev_charging"]).run()
    clan = dict(rclan(["IE"]), facts=rch["facts_append"])
    result, change, _ = synthesise.run_synthesis(DOC, "3", clan, {}, "t@1.0", caps)
    assert change["findings_append"] and all("99" not in f["statement"] for f in change["findings_append"])
    assert result["dropped"] and all(f["status"] == "proposed" for f in change["findings_append"])


def test_report_claims_are_checked_in_code(store):
    caps = caps_for(store)
    _, rch, _ = Researcher(DOC, "3", rclan(["IE"]), "t@1.0", caps, ["media_spend"], ["IE"],
                           ["automotive.ev_charging"]).run()
    clan = dict(rclan(["IE"]), facts=rch["facts_append"])
    rpt, cites, _, why = report.compose(DOC, clan, "t@1.0", caps)
    texts = [b["text"] for s in rpt["sections"] for b in s["blocks"] if b["kind"] == "claim"]
    assert texts and not any("42" in t for t in texts)                 # the made-up figure was dropped
    assert all(s["cites"] for s in rpt["summary"])                     # the uncited line was dropped
    assert rpt["headline"]["cites"] and set(cites) <= {f["id"] for f in rch["facts_append"]}


def _verify_req(clan, **inp):
    base = {"finding": "", "by": "human:ana", "decision_id": "d_01JBVERIFY01"}
    return SimpleNamespace(doc=DOC, base="3", clan=clan, handler="verify_finding@1.0", inp={**base, **inp})


def test_verifying_a_finding_writes_it_to_the_layer_as_reviewed(store):
    from napkin.handlers import verify_finding
    from napkin.util import TaskError
    caps = caps_for(store)
    _, rch, _ = Researcher(DOC, "3", rclan(["IE"]), "t@1.0", caps, ["media_spend"], ["IE"],
                           ["automotive.ev_charging"]).run()
    clan = dict(rclan(["IE"]), facts=rch["facts_append"])
    _, sch, _ = synthesise.run_synthesis(DOC, "3", clan, {}, "t@1.0", caps)
    fi = sch["findings_append"][0]
    clan = dict(clan, findings=sch["findings_append"])

    result, change, _ = verify_finding.run(_verify_req(clan, finding=fi["id"]), caps)
    assert change is None  # the host records it; this only writes the layer
    pin = result["pin"]
    assert pin["method"] == "synthesis" and pin["decision"] == "d_01JBVERIFY01" and pin["value"] == fi["statement"]
    assert pin["sources"][0] == result["source"] and pin["sources"][1:] == fi["cites"]
    row = caps.layers.resolve(pin["origin"])
    assert row and row["id"] == pin["id"] and row["method"] == "synthesis"
    human = caps.layers.sources([result["source"]])[0]
    assert human["uri"] == "human:ana" and human["tier"] == "reviewer-verified"
    # the strictest licence of what it cites
    cited = [p for p in rch["facts_append"] if p["id"] in fi["cites"]]
    assert pin["licence"] == max((p["licence"] for p in cited), key=["open", "licensed-internal",
                                                                      "client-confidential"].index)

    # Only a proposed finding, only a person, only a d_ id the host chose.
    done = dict(clan, findings=[dict(fi, status="verified")])
    for req, status in ((_verify_req(done, finding=fi["id"]), 409),
                        (_verify_req(clan, finding="fi_NOPE0001"), 404),
                        (_verify_req(clan, finding=fi["id"], by="process:x"), 400),
                        (_verify_req(clan, finding=fi["id"], decision_id="d_x"), 400)):
        with pytest.raises(TaskError) as e:
            verify_finding.run(req, caps)
        assert e.value.status == status


def test_the_agent_lays_the_report_out_and_the_rule_holds_it_to_the_record(store):
    caps = caps_for(store)
    _, rch, _ = Researcher(DOC, "3", rclan(["IE"]), "t@1.0", caps, ["media_spend", "market_structure"], ["IE"],
                           ["automotive.ev_charging"]).run()
    clan = dict(rclan(["IE"]), facts=rch["facts_append"])
    rpt, _, _, _ = report.compose(DOC, clan, "t@1.0", caps)
    html = rpt["layout"]
    assert rpt["layout_by"] == "agent"
    for gone in ("<script", "onclick", "onerror", "<img", "<a ", "javascript:", "evil", "no evidence at all",
                 "73%", "f_NOTREAL01"):
        assert gone not in html, gone
    assert 'class="cl-nums"' in html and "<clan-chart" in html and "<clan-sources></clan-sources>" in html
    assert "Grounded." in html


def test_a_layout_that_loses_its_evidence_is_built_from_the_report(store):
    from fakes import FakeModel
    caps = caps_for(store, model=FakeModel({"layout": lambda p: {"html": "<p>Nothing but words.</p>"}}))
    _, rch, _ = Researcher(DOC, "3", rclan(["IE"]), "t@1.0", caps, ["media_spend"], ["IE"],
                           ["automotive.ev_charging"]).run()
    clan = dict(rclan(["IE"]), facts=rch["facts_append"])
    rpt, _, _, _ = report.compose(DOC, clan, "t@1.0", caps)
    assert rpt["layout_by"] == "built"
    assert rpt["headline"]["text"] in rpt["layout"].replace("&#x27;", "'")
    assert "<clan-field" in rpt["layout"] and "<clan-sources></clan-sources>" in rpt["layout"]


def _audience_model(pin_ids):
    from fakes import FakeModel, reasoning
    def synth(p):
        ids = [x["id"] for x in p["pins"]][:2]
        g = reasoning([("The pins say so", ids[:1])], only="one reading")
        return {"findings": [{"lens": "media_spend", "statement": "Spend reads differently by market.",
                              "cites": ids, "markets": [], "grounds": g}],
                "audience": {"definition": "Busy households who plan meals ahead", "fact_ids": ids[:1],
                             "behaviours": [], "attitudes": [], "grounds": g}}
    return FakeModel({"synthesise": synth})


def test_a_redo_replaces_the_proposed_audience_and_never_the_rejected_finding(store):
    caps = caps_for(store)
    _, rch, _ = Researcher(DOC, "3", rclan(["IE"]), "t@1.0", caps, ["media_spend"], ["IE"],
                           ["automotive.ev_charging"]).run()
    pins = rch["facts_append"]
    ids = [p["id"] for p in pins][:2]
    rejected = {"id": "fi_01OLDREJ", "statement": "Promotion-hunting defines the shopper", "cites": sorted(ids),
                "method": "synthesis", "status": "rejected", "lens": "media_spend",
                "rejection": {"reason": "one survey", "by": "human:u", "at": "2026-09-28T10:00:00Z", "decision": "d_01REJECT01"}}
    old = {"value": {"definition": "Promotion-hunters", "synthesis_finding_ids": ["fi_01OLDREJ"]}, "origin": "proposed",
           "gate": "brief", "fact_ids": ids[:1], "decision": "d_01OLDAUD1"}
    clan = dict(rclan(["IE"]), facts=pins, findings=[rejected])
    clan["data"]["campaign"]["audience"] = old
    model = _audience_model(ids)
    caps2 = caps_for(store, model=model)
    result, change, _ = synthesise.run_synthesis(DOC, "4", clan, {}, "t@1.0", caps2, redo_audience=True)
    # the model was told what a person ruled out
    sent = [c for c in model.calls if c[0] == "synthesise"][-1][1]
    assert sent["rejected"] == [{"statement": "Promotion-hunting defines the shopper", "reason": "one survey"}]
    # only the audience is redone: no new findings to check, no finding decisions
    assert change["findings_append"] == []
    assert [d["action"] for d in change["decisions"]] == ["propose_audience"]
    aud = change["data_patch"]["campaign"]["audience"]
    assert aud["origin"] == "proposed" and aud["value"]["definition"] == "Busy households who plan meals ahead"
    assert aud["value"]["synthesis_finding_ids"] is None, "the rejected finding is dropped from the merge patch"
    assert change["read"]["campaign.audience"] == old
    assert result["audience"] is True
    # an audience a person confirmed is never replaced
    clan["data"]["campaign"]["audience"] = dict(old, origin="confirmed")
    _, change2, _ = synthesise.run_synthesis(DOC, "4", clan, {}, "t@1.0", caps2, redo_audience=True)
    assert "campaign" not in change2["data_patch"]


def test_synthesis_never_proposes_a_rejected_finding_again(store):
    caps = caps_for(store)
    _, rch, _ = Researcher(DOC, "3", rclan(["IE"]), "t@1.0", caps, ["media_spend"], ["IE"],
                           ["automotive.ev_charging"]).run()
    clan = dict(rclan(["IE"]), facts=rch["facts_append"])
    _, first, _ = synthesise.run_synthesis(DOC, "3", clan, {}, "t@1.0", caps)
    fi = dict(first["findings_append"][0], status="rejected",
              rejection={"reason": "no", "by": "human:u", "at": "2026-09-28T10:00:00Z", "decision": "d_01REJECT01"})
    _, again, _ = synthesise.run_synthesis(DOC, "4", dict(clan, findings=[fi]), {}, "t@1.0", caps)
    assert fi["id"] not in {f["id"] for f in again["findings_append"]}


def test_a_person_corrects_a_fact_and_the_layer_keeps_both(store):
    from napkin.handlers import correct_fact
    from napkin.util import TaskError
    caps = caps_for(store)
    _, rch, _ = Researcher(DOC, "3", rclan(["IE"]), "t@1.0", caps, ["media_spend"], ["IE"],
                           ["automotive.ev_charging"]).run()
    old = next(p for p in rch["facts_append"] if p["unit"] == "proportion")
    clan = dict(rclan(["IE"]), facts=rch["facts_append"])
    req = SimpleNamespace(doc=DOC, base="3", clan=clan, handler="correct_fact@1.0",
                          inp={"fact": old["id"], "value": "31%", "note": "The 2026 panel says 31",
                               "source_uri": "https://example.org/panel", "by": "human:ana",
                               "decision_id": "d_01JBCORRECT1"})
    result, change, _ = correct_fact.run(req, caps)
    assert change is None
    pin = result["pin"]
    assert pin["value"] == 0.31 and pin["key"] == old["key"] and pin.get("market") == old.get("market")
    assert pin["decision"] == "d_01JBCORRECT1" and pin["id"] != old["id"]
    assert result["source_record"]["uri"] == "https://example.org/panel"
    assert result["source_record"]["tier"] == "reviewer-verified"
    assert caps.layers.resolve(pin["origin"])["value"] == 0.31
    # nothing to correct, and a note is required
    for inp, status in (({"value": old["value"]}, 409), ({"note": ""}, 400), ({"value": "lots"}, 400)):
        with pytest.raises(TaskError) as e:
            correct_fact.run(SimpleNamespace(**{**req.__dict__, "inp": {**req.inp, **inp}}), caps)
        assert e.value.status == status


def test_a_corrected_value_is_read_in_the_facts_unit():
    from napkin.handlers.correct_fact import parse_value
    assert parse_value("23%", "proportion") == 0.23
    assert parse_value("0.4", "proportion") == 0.4
    assert parse_value("€4.2m", "eur") == 4200000
    assert parse_value("1,500", "count") == 1500
    assert parse_value("Donegal Catch", "text") == "Donegal Catch"
    assert parse_value("yes", "boolean") is True
