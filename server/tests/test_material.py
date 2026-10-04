"""The client's own documents are research sources: tier primary, high confidence on their
own, the client's (brand layer, client-confidential), and a contest when the web disagrees."""

from napkin.doc import build_materials
from napkin.pipeline.research import Researcher
from napkin.rules.confidence import fact_confidence

from fakes import FakeResearch, _h
from test_pipeline import DOC, caps_for, rclan

CAT = "automotive.ev_charging"


def deck(text, name="client-deck.pdf"):
    mats, _ = build_materials({"attachments": [{"name": name, "sha256": "sha256:" + "ab" * 32, "text": text}]}, {})
    return mats


def web_value(lens="media_spend", market="IE"):
    return _h(lens, market, CAT) % 60 + 5


def run(store, mats, research=None):
    caps = caps_for(store, research=research or FakeResearch())
    return caps, Researcher(DOC, "3", rclan(["IE"]), "t@1.0", caps, ["media_spend"], ["IE"], [CAT],
                            materials=mats).run()


def test_a_figure_the_client_and_the_web_disagree_on_is_a_choice_for_the_person(store):
    v = web_value()
    caps, (_, change, _) = run(store, deck(f"Our tracking: the media_spend indicator for IE stood at {v + 7}% in 2025."))
    ct = change["data_patch"]["selection"]["contested"]
    mine = next(c for c in ct if c["key"].endswith("media.indicator@IE"))
    vals = {round(x["value"], 2): x for x in mine["values"]}
    assert set(vals) == {round(v / 100, 2), round((v + 7) / 100, 2)} and mine["status"] == "open"
    client = vals[round((v + 7) / 100, 2)]
    recs = {s["id"]: s for s in change["sources_append"]}
    assert all(recs[s]["uri"].startswith("material:") and recs[s]["licence"] == "client-confidential"
               and recs[s]["tier"] == "primary" for s in client["sources"])
    assert client["pin"]["layer"] == "brand" and client["pin"]["confidence"] == "high"
    assert not [p for p in change["facts_append"] if p["key"] == "media.indicator"]  # nothing is picked


def test_a_figure_they_agree_on_is_one_fact_kept_at_brand_scope(store):
    v = web_value()
    caps, (_, change, _) = run(store, deck(f"Our tracking: the media_spend indicator for IE stood at {v}% in 2025."))
    p = next(p for p in change["facts_append"] if p["key"] == "media.indicator")
    assert len(p["sources"]) == 3 and p["confidence"] == "high"   # two web sources and the client's deck
    assert p["layer"] == "brand" and p["licence"] == "client-confidential"
    assert any(s.startswith("src_") for s in p["quotes"])
    # it never reaches the shared category layer
    rows = caps.layers.facts("category", f"category/{CAT}", key_prefix="media.", market="IE")
    assert not any(r["key"] == "media.indicator" and r["status"] == "active" and r["version"] > 0
                   and any(str(x.get("uri", "")).startswith("material:") for x in r["source_records"]) for r in rows)


def test_the_clients_material_alone_is_high_and_read_when_the_web_fails(store):
    caps, (_, change, _) = run(store, deck("Our tracking: the media_spend indicator for IE stood at 61% in 2025."),
                               research=FakeResearch(fail_on={("media_spend", "IE")}))
    p = next(p for p in change["facts_append"] if p["key"] == "media.indicator")
    assert p["value"] == 0.61 and p["confidence"] == "high" and p["layer"] == "brand"
    assert "research failed" in change["data_patch"]["selection"]["gaps"][0]["note"]  # the web's failure still says so


def test_a_quote_the_document_does_not_hold_is_rejected_as_any_source(store):
    caps, (_, change, _) = run(store, deck("Nothing about media here."),
                               research=FakeResearch(fail_on={("media_spend", "IE")}))
    assert not [p for p in change["facts_append"] if p["key"] == "media.indicator"]


def test_material_confidence():
    assert fact_confidence([{"uri": "material:sha256:x", "tier": "primary", "domain": "material-x"}]) == "high"
    assert fact_confidence([{"uri": "https://cso.ie/x", "tier": "primary", "domain": "cso.ie"}]) == "medium"
