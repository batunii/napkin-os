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


def test_tree_seed(layers):
    leaves = layers.leaves()
    real = [l for l in leaves if not l["provisional"]]
    assert len(real) == 108 and len({l["vertical"] for l in leaves}) == 18
    codes = {l["code"] for l in leaves}
    assert {"automotive.ev_charging", "automotive.hybrid"} <= codes
    assert next(l for l in leaves if l["code"] == "automotive.hybrid")["provisional"]
    assert layers.vertical_of("automotive.hybrid")["name"] == "Automotive"
    assert layers.vertical_of("nope.nothing") is None  # 404 unknown_leaf -> None


def test_find_maps_typed_names(layers):
    assert layers.find("Automotive") == layers.vertical_of("automotive.hybrid")["leaves"]
    assert layers.find("Ev hybrid") == ["automotive.ev_charging", "automotive.hybrid"]
    assert layers.find("cider") == ["alcohol.cider"]
    assert layers.find("zzz") == []


def test_append_is_append_only_with_supersession_and_contest(layers):
    s1 = layers.add_source(src("https://www.cso.ie/x"))
    s2 = layers.add_source(src("https://www.simi.ie/x", "secondary"))
    assert layers.add_source(src("https://www.cso.ie/x")) == s1
    r1 = layers.append(fact(0.2, sources=[s1]), DEC)
    assert r1["version"] == 1 and r1["status"] == "active" and r1["sources"] == [s1]
    same = layers.append(fact(0.2, sources=[s2]), {**DEC, "id": "d_TEST000002"})  # corroboration
    assert same["id"] == r1["id"] and set(same["sources"]) == {s1, s2}
    newer = layers.append(fact(0.25, as_of="2026-06-30", sources=[s1]), DEC)
    assert newer["version"] == 2 and newer["supersedes"] == r1["id"]
    assert [r["id"] for r in layers.facts("category", "category/automotive.ev_charging")] == [newer["id"]]
    older = layers.append(fact(0.3, as_of="2026-01-01", sources=[s2]), DEC)  # not newer: both contested
    assert older["status"] == "contested"
    assert {r["status"] for r in layers.facts("category", "category/automotive.ev_charging")} == {"contested"}
    assert layers.resolve(origin_uri("category", "category/automotive.ev_charging", "market.bev_share", 1))["value"] == 0.2
    assert layers.resolve("fact://category/automotive.ev_charging/market.nothing@9") is None


def test_brand_facts_are_scoped(store):
    a = store.open({"org": "org/a", "brand": "brand/x"})
    b = store.open({"org": "org/b", "brand": "brand/x"})
    a.set_roster("brand/bmw", "BMW", ["automotive.ev_charging"], DEC, [])
    assert a.roster("brand/bmw")["categories"] == ["automotive.ev_charging"]
    assert b.roster("brand/bmw") is None
    assert a.find_brands("bmw") == [("brand/bmw", "BMW")] and b.find_brands("bmw") == []
    a.append(fact(0.2), DEC)  # category facts are shared
    assert b.facts("category", "category/automotive.ev_charging")


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
    svc.fail_next = [503, 503]
    L2 = st.open({"org": "org/a", "brand": "brand/x"})
    with pytest.raises(LayersError):
        L2.facts("category", "category/automotive.ev_charging")
    with pytest.raises(LayersError):  # licence is required: never a silent declassification
        L.append({k: v for k, v in fact(0.2).items() if k != "licence"}, DEC)


def test_an_unknown_leaf_is_refused_by_a_service_that_enforces_it():
    L = layer_store(FakeLayersService(enforce_leaves=True)).open({"org": "org/a", "brand": "brand/x"})
    with pytest.raises(LayersError, match="unknown_leaf"):
        L.append(fact(0.2, entity="category/drinks.soft_drinks"), DEC)


def test_store_needs_a_url():
    with pytest.raises(ValueError):
        HttpLayerStore("")
