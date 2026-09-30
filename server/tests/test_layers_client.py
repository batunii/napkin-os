"""The layers port: `HttpLayers` (the `Layers` protocol over napkin.layers/1)
against a fake layers service served in-process."""

import json

import pytest

from napkin.layers import origin_uri
from napkin.layers.http import HttpLayerStore, LayersError

from conftest import layer_store
from fake_layers import FakeLayersService

DEC = {"id": "d_TEST000001", "kind": "pin", "handler": "t@1.0", "action": "t", "rationale": "r", "cites": []}


def fact(value, as_of="2025-12-31", **kw):
    f = {"layer": "category", "entity": "category/automotive.ev_charging", "key": "market.bev_share", "market": "IE",
         "value": value, "unit": "proportion", "as_of": as_of, "retrieved_at": "2026-09-20", "sources": [],
         "licence": "open"}
    f.update(kw)
    return f


def src(uri, tier="primary"):
    return {"uri": uri, "tier": tier, "domain": uri.split("/")[2], "licence": "open"}


def test_protocol_methods_round_trip_through_the_client(layers):
    """Each Layers method is one route; lookups that 404 come back as None.
    (The store's own semantics are the layers service's, tested with it.)"""
    assert any(l["code"] == "alcohol.cider" for l in layers.leaves())
    assert layers.vertical_of("automotive.hybrid")["name"] == "Automotive"
    assert layers.vertical_of("nope.nothing") is None  # 404 unknown_leaf -> None
    assert layers.find("cider") == ["alcohol.cider"]
    s1 = layers.add_source(src("https://www.cso.ie/x"))
    assert layers.add_source(src("https://www.cso.ie/x")) == s1
    r1 = layers.append(fact(0.2, sources=[s1]), DEC)
    assert r1["version"] == 1 and r1["sources"] == [s1] and r1["origin"].endswith("market.bev_share@1")
    assert layers.facts("category", "category/automotive.ev_charging", market="IE")[0]["id"] == r1["id"]
    assert layers.resolve(origin_uri("category", "category/automotive.ev_charging", "market.bev_share", 1))["value"] == 0.2
    assert layers.resolve("fact://category/automotive.ev_charging/market.nothing@9") is None  # 404 -> None
    assert [s["id"] for s in layers.sources([s1, "src_unknown"])] == [s1]
    assert layers.roster("brand/nobody") is None
    layers.set_roster("brand/bmw", "BMW", ["automotive.ev_charging"], DEC, [s1])
    assert layers.roster("brand/bmw")["categories"] == ["automotive.ev_charging"]
    assert layers.find_brands("bmw") == [("brand/bmw", "BMW")]


def test_one_url_cited_twice_with_different_titles_is_one_source(layers):
    """Two lens researchers can cite the same URL, each with the title it saw.
    The second write is not a replay of the first, so it must not reuse its
    idempotency key (that was a 409 idempotency_conflict that failed research);
    the service's one-row-per-URI rule hands back the same id."""
    a = layers.add_source({**src("https://www.cso.ie/x"), "title": "Vehicle licensing 2025"})
    b = layers.add_source({**src("https://www.cso.ie/x"), "title": "Vehicles Licensed for the First Time"})
    assert a == b


def test_scope_travels_in_headers_never_in_bodies(store):
    svc = store.service
    L = store.open({"org": "org/a", "brand": "brand/x"}, {"handler": "h@1.0", "job": "job_1"})
    L.append(fact(0.2), DEC)
    L.leaves()
    for r in svc.requests:
        assert r.headers["x-napkin-org"] == "org/a" and r.headers["x-napkin-brand"] == "brand/x"
        assert r.headers["x-napkin-handler"] == "h@1.0" and r.headers["x-napkin-job"] == "job_1"
        if r.content:
            body = json.loads(r.content)
            assert not {"scope", "org", "org_id", "tenant", "tenant_id", "brand_scope"} & set(body)


def test_writes_carry_a_deterministic_idempotency_key_and_replay(store):
    svc = store.service
    L = store.open({"org": "org/a", "brand": "brand/x"})
    r1 = L.append(fact(0.2), DEC)
    r2 = L.append(fact(0.2), DEC)  # the same step again: same key, same body -> replay
    posts = [r for r in svc.requests if r.method == "POST" and r.url.path == "/v1/layers/facts"]
    assert len(posts) == 2 and posts[0].headers["idempotency-key"] == posts[1].headers["idempotency-key"]
    assert r1 == r2
    # another decision is another key
    L.append(fact(0.2), {**DEC, "id": "d_OTHER00001"})
    keys = {r.headers["idempotency-key"] for r in svc.requests if r.url.path == "/v1/layers/facts"
            and r.method == "POST"}
    assert len(keys) == 2


def test_reads_retry_once_on_503_and_failures_are_loud():
    svc = FakeLayersService()
    st = layer_store(svc)
    L = st.open({"org": "org/a", "brand": "brand/x"})
    svc.fail_next = [503]
    assert L.leaves()  # retried once
    svc.fail_next = [503, 503, 503]  # three attempts in all: a fourth failure would be needed to keep going
    L2 = st.open({"org": "org/a", "brand": "brand/x"})
    with pytest.raises(LayersError):
        L2.facts("category", "category/automotive.ev_charging")
    with pytest.raises(LayersError):  # licence is required: never a silent declassification
        L.append({k: v for k, v in fact(0.2).items() if k != "licence"}, DEC)


def _store_recording_sleeps(svc, sleeps):
    return HttpLayerStore("http://layers.test", transport=svc.transport(), sleep=sleeps.append)


def test_a_short_outage_is_ridden_out_with_pauses_between_attempts():
    """A blip of a second used to beat both attempts, which ran back to back."""
    svc, sleeps = FakeLayersService(), []
    L = _store_recording_sleeps(svc, sleeps).open({"org": "org/a", "brand": "brand/x"})
    svc.fail_next = [503, 503]
    assert L.leaves()                      # the third attempt is served
    assert sleeps == [1.0, 3.0]            # a pause before the second and before the third attempt
    n = len(svc.requests)
    svc.fail_next = [502, 503, 504]
    with pytest.raises(LayersError):       # three failures in a row still give up
        L.facts("category", "category/automotive.ev_charging")
    assert len(svc.requests) == n + 3


def test_a_write_retried_after_a_pause_lands_once():
    svc, sleeps = FakeLayersService(), []
    L = _store_recording_sleeps(svc, sleeps).open({"org": "org/a", "brand": "brand/x"})
    svc.fail_next = [503, 503]
    r = L.append(fact(0.2), DEC)
    assert r["version"] == 1 and len(L.facts("category", "category/automotive.ev_charging", market="IE")) == 1


def test_a_timeout_is_still_tried_only_twice():
    """A timeout has already waited its full time; a third wait of that length would stall a whole job."""
    import httpx
    seen = []

    def slow(request):
        seen.append(request)
        raise httpx.ReadTimeout("slow", request=request)
    L = HttpLayerStore("http://layers.test", transport=httpx.MockTransport(slow), sleep=lambda s: None) \
        .open({"org": "org/a", "brand": "brand/x"})
    with pytest.raises(LayersError, match="timed out"):
        L.leaves()
    assert len(seen) == 2


def test_an_unknown_leaf_is_refused_by_a_service_that_enforces_it():
    L = layer_store(FakeLayersService(enforce_leaves=True)).open({"org": "org/a", "brand": "brand/x"})
    with pytest.raises(LayersError, match="unknown_leaf"):
        L.append(fact(0.2, entity="category/drinks.soft_drinks"), DEC)


def test_store_needs_a_url():
    with pytest.raises(ValueError):
        HttpLayerStore("")
