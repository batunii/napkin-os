from napkin.layers import origin_uri

DEC = {"id": "d_TEST000001", "kind": "pin", "handler": "t@1.0", "action": "t", "rationale": "r", "cites": []}


def fact(value, as_of="2025-12-31", **kw):
    f = {"layer": "category", "entity": "category/automotive.ev_charging", "key": "market.bev_share", "market": "IE",
         "value": value, "unit": "proportion", "as_of": as_of, "retrieved_at": "2026-09-20", "sources": []}
    f.update(kw)
    return f


def test_tree_seed(layers):
    leaves = layers.leaves()
    real = [l for l in leaves if not l["provisional"]]
    assert len(real) == 108 and len({l["vertical"] for l in leaves}) == 18
    codes = {l["code"] for l in leaves}
    assert {"automotive.ev_charging", "automotive.hybrid"} <= codes
    assert next(l for l in leaves if l["code"] == "automotive.hybrid")["provisional"]
    assert layers.vertical_of("automotive.hybrid")["name"] == "Automotive"


def test_find_maps_typed_names(layers):
    assert layers.find("Automotive") == layers.vertical_of("automotive.hybrid")["leaves"]
    assert layers.find("Ev hybrid") == ["automotive.ev_charging", "automotive.hybrid"]
    assert layers.find("cider") == ["alcohol.cider"]
    assert layers.find("zzz") == []


def test_append_is_append_only_with_supersession_and_contest(layers):
    src = layers.add_source({"uri": "https://www.cso.ie/x", "tier": "primary", "domain": "cso.ie"})
    src2 = layers.add_source({"uri": "https://www.simi.ie/x", "tier": "secondary", "domain": "simi.ie"})
    assert layers.add_source({"uri": "https://www.cso.ie/x", "tier": "primary", "domain": "cso.ie"}) == src
    r1 = layers.append(fact(0.2, sources=[src]), DEC)
    assert r1["version"] == 1 and r1["status"] == "active" and r1["sources"] == [src]
    same = layers.append(fact(0.2, sources=[src2]), DEC)          # corroboration: no new version
    assert same["id"] == r1["id"] and set(same["sources"]) == {src, src2}
    newer = layers.append(fact(0.25, as_of="2026-06-30", sources=[src]), DEC)
    assert newer["version"] == 2 and newer["supersedes"] == r1["id"]
    assert [r["id"] for r in layers.facts("category", "category/automotive.ev_charging")] == [newer["id"]]
    older = layers.append(fact(0.3, as_of="2026-01-01", sources=[src2]), DEC)  # not newer: both contested
    assert older["status"] == "contested"
    assert {r["status"] for r in layers.facts("category", "category/automotive.ev_charging")} == {"contested"}
    assert layers.resolve(origin_uri("category", "category/automotive.ev_charging", "market.bev_share", 1))["value"] == 0.2


def test_brand_facts_are_scoped(store):
    a = store.open({"org": "org/a", "brand": "brand/x"})
    b = store.open({"org": "org/b", "brand": "brand/x"})
    a.set_roster("brand/bmw", "BMW", ["automotive.ev_charging"], DEC, [])
    assert a.roster("brand/bmw")["categories"] == ["automotive.ev_charging"]
    assert b.roster("brand/bmw") is None
    assert a.find_brands("bmw") == [("brand/bmw", "BMW")] and b.find_brands("bmw") == []
    # category facts are shared
    a.append(fact(0.2), DEC)
    assert b.facts("category", "category/automotive.ev_charging")
