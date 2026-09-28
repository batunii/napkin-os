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
