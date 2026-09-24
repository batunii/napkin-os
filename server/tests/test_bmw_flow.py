"""The owner's prompt end to end with fake components, driven the way the
host and the view drive it (the contract suite's own Host / Run)."""

import importlib.util
from pathlib import Path

from conftest import contract_suite

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("contract_test", contract_suite())
ct = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ct)

PROMPT = "BMW is trying to enter the Ev hybrid market in Ireland. Make a campaign clan on that"


def run(server, answer):
    suite = ct.Suite(ct.Client(server.url, None), REPO / "app" / "templates" / "campaign-research", 60)
    doc, data, inp, facts, chain = ct.start_doc(PROMPT)
    host = ct.Host(suite, doc, data, facts, chain)
    r = ct.Run(suite, host, inp)
    r.start(answer)
    assert r.states[-1] == "done"
    assert not ct.intake_invariants(host), ct.intake_invariants(host)
    return host, r


def test_bmw_subject_categories_as_buttons_then_reuse(server):
    asked = []

    def answer(r, q):
        asked.append(q)
        assert q["address"].endswith("#campaign.categories")
        ids = [o["id"] for o in q["options"]]
        assert ids == ["automotive_ev_charging", "automotive_hybrid", "both", "other"], ids
        return (next(o for o in q["options"] if o["id"] == "both"), None)
    host, _ = run(server, answer)
    camp = host.data["campaign"]
    assert camp["brand"]["value"] == {"ref": "brand/bmw", "name": "BMW"} and camp["brand"]["origin"] == "extracted"
    assert camp["markets"]["value"] == ["IE"]
    assert camp["categories"]["origin"] == "confirmed"
    assert len(asked) == 1 and host.facts and host.findings and host.data["report"]["headline"]["cites"]
    calls = len(server.research.calls)
    assert calls == 8  # every lens, one market

    # campaign two: the roster row the person confirmed is proposed; research reuses the layer
    host2, _ = run(server, lambda r, q: (_ for _ in ()).throw(AssertionError(f"asked {q['text']}")))
    cats = host2.data["campaign"]["categories"]
    assert cats["origin"] == "proposed" and cats["value"] == ["automotive.ev_charging", "automotive.hybrid"]
    assert len(server.research.calls) == calls  # nothing re-researched
    assert {f["id"] for f in host.facts} & {f["id"] for f in host2.facts}


def test_typed_vertical_offers_its_leaves(server):
    seen = []

    def answer(r, q):
        seen.append(q)
        if len(seen) == 1:
            return (None, "Automotive")
        assert "Automotive" in q["text"] or "From" in r.messages[list(r.messages)[-1]]["text"]
        leaves = [o["value"][0] for o in q["options"] if "value" in o]
        assert "automotive.hybrid" in leaves and "automotive.ev_charging" in leaves and len(leaves) == 8
        assert all(o["origin"] == "stated" for o in q["options"] if "value" in o)
        return (next(o for o in q["options"] if o.get("value") == ["automotive.hybrid"]), None)
    host, _ = run(server, answer)
    assert host.data["campaign"]["categories"]["value"] == ["automotive.hybrid"]
