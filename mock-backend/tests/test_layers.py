"""The layers family: LocalLayers' behaviour (ported from server/tests/test_layers.py)
through the in-process LayersApp, the contract's fixes, and layers_contract.py
against the HTTP server."""

from __future__ import annotations

import json
import secrets
import tempfile
import threading
import unittest
from pathlib import Path

from harness import Server

from common import Request
from layers_port import LayersApp, origin_uri

DEC = {"id": "d_TEST000001", "kind": "pin", "handler": "t@1.0", "action": "t", "rationale": "r", "cites": [],
       "reasoning": {"decided": "x", "because": []}}


def fact(value, as_of="2025-12-31", **kw):
    f = {"layer": "category", "entity": "category/automotive.ev_charging", "key": "market.bev_share", "market": "IE",
         "value": value, "unit": "proportion", "as_of": as_of, "retrieved_at": "2026-09-20", "sources": [],
         "licence": "open"}
    f.update(kw)
    return f


class InProcess(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = LayersApp(str(Path(self.tmp.name) / "layers.sqlite"))

    def tearDown(self):
        self.app.close()
        self.tmp.cleanup()

    def call(self, method, path, body=None, org="org/a", brand=None, key="auto"):
        h = {}
        if org:
            h["X-Napkin-Org"] = org
        if brand:
            h["X-Napkin-Brand"] = brand
        if key == "auto" and method in ("POST", "PUT"):
            key = secrets.token_hex(8)
        if key and key != "auto":
            h["Idempotency-Key"] = key
        r = self.app.handle(Request.build(method, path, h, json.dumps(body).encode() if body is not None else b""))
        return r.status, r.body

    def append(self, f, d=DEC, **kw):
        return self.call("POST", "/v1/layers/facts", {"fact": f, "decision": d}, **kw)

    def source(self, uri, **kw):
        body = {"uri": uri, "tier": "primary", "domain": "x", "licence": "open"}
        body.update(kw.pop("fields", {}))
        return self.call("POST", "/v1/layers/sources", {"source": body}, **kw)[1]["id"]

    def test_tree_seed(self):
        st, b = self.call("GET", "/v1/layers/categories")
        leaves = b["leaves"]
        real = [l for l in leaves if not l["provisional"]]
        self.assertEqual((len(real), len({l["vertical"] for l in leaves})), (108, 18))
        self.assertEqual(b["taxonomy_version"], "planner-research-taxonomy/0.1+derived")
        codes = {l["code"] for l in leaves}
        self.assertTrue({"automotive.ev_charging", "automotive.hybrid"} <= codes)
        self.assertTrue(next(l for l in leaves if l["code"] == "automotive.hybrid")["provisional"])
        st, v = self.call("GET", "/v1/layers/categories/automotive.hybrid/vertical")
        self.assertEqual(v["name"], "Automotive")

    def test_find_maps_typed_names(self):
        find = lambda t: self.call("POST", "/v1/layers/categories/find", {"text": t})[1]["leaves"]  # noqa: E731
        auto = self.call("GET", "/v1/layers/categories/automotive.hybrid/vertical")[1]["leaves"]
        self.assertEqual(find("Automotive"), auto)
        self.assertEqual(find("Ev hybrid"), ["automotive.ev_charging", "automotive.hybrid"])
        self.assertEqual(find("cider"), ["alcohol.cider"])
        self.assertEqual(find("zzz"), [])

    def test_append_is_append_only_with_supersession_and_contest(self):
        src = self.source("https://www.cso.ie/x")
        src2 = self.source("https://www.simi.ie/x")
        self.assertEqual(self.source("https://www.cso.ie/x"), src)
        st, r1 = self.append(fact(0.2, sources=[src]))
        r1 = r1["fact"]
        self.assertEqual((r1["version"], r1["status"], r1["sources"]), (1, "active", [src]))
        st, same = self.append(fact(0.2, sources=[src2]))
        self.assertEqual(same["outcome"], "corroborated")
        self.assertEqual(same["fact"]["id"], r1["id"])
        self.assertEqual(set(same["fact"]["sources"]), {src, src2})
        st, newer = self.append(fact(0.25, as_of="2026-06-30", sources=[src]))
        self.assertEqual((newer["outcome"], newer["fact"]["version"], newer["fact"]["supersedes"]),
                         ("superseded", 2, r1["id"]))
        q = "/v1/layers/facts?layer=category&entity=category/automotive.ev_charging"
        self.assertEqual([r["id"] for r in self.call("GET", q)[1]["facts"]], [newer["fact"]["id"]])
        st, older = self.append(fact(0.3, as_of="2026-01-01", sources=[src2]))
        self.assertEqual((older["outcome"], older["fact"]["status"]), ("contested", "contested"))
        self.assertEqual({r["status"] for r in self.call("GET", q)[1]["facts"]}, {"contested"})
        uri = origin_uri("category", "category/automotive.ev_charging", "market.bev_share", 1)
        st, got = self.call("GET", f"/v1/layers/facts/by-uri?uri={uri}")
        self.assertEqual((got["fact"]["value"], got["fact"]["status"]), (0.2, "superseded"))

    def test_brand_facts_are_scoped(self):
        st, r = self.call("PUT", "/v1/layers/roster/brand/bmw", {"name": "BMW", "categories": ["automotive.ev_charging"],
                                                                  "sources": [], "decision": DEC}, brand="brand/x")
        self.assertEqual(st, 200, r)
        self.assertEqual(self.call("GET", "/v1/layers/roster/brand/bmw", brand="brand/x")[1]["categories"],
                         ["automotive.ev_charging"])
        self.assertEqual(self.call("GET", "/v1/layers/roster/brand/bmw", org="org/b", brand="brand/x")[0], 404)
        self.assertEqual(self.call("POST", "/v1/layers/brands/find", {"text": "bmw"})[1]["brands"],
                         [{"ref": "brand/bmw", "name": "BMW"}])
        self.assertEqual(self.call("POST", "/v1/layers/brands/find", {"text": "bmw"}, org="org/b")[1]["brands"], [])
        self.append(fact(0.2))
        q = "/v1/layers/facts?layer=category&entity=category/automotive.ev_charging"
        self.assertTrue(self.call("GET", q, org="org/b")[1]["facts"], "category facts are shared")

    def test_licence_required_no_default(self):
        f = fact(0.2)
        del f["licence"]
        st, b = self.append(f)
        self.assertEqual((st, b["error"]["type"]), (400, "invalid_input"))
        self.assertIn("licence", b["error"]["message"])
        st, b = self.call("POST", "/v1/layers/sources", {"source": {"uri": "u", "tier": "primary", "domain": "x"}})
        self.assertEqual(st, 400)

    def test_unknown_leaf_refused_but_category_layer_on_a_brand_entity_is_fine(self):
        st, b = self.append(fact(1, entity="category/automotive.nosuch"))
        self.assertEqual((st, b["error"]["type"]), (400, "unknown_leaf"))
        st, b = self.append(fact("2026-03-01", entity="brand/orchard-hill", key="launch.date", unit="date"))
        self.assertEqual(st, 200, b)
        self.assertEqual(b["fact"]["origin"], "fact://category/brand/orchard-hill/launch.date@1")

    def test_confidential_source_links_hidden_from_other_orgs(self):
        pub = self.source("https://pub.example/x")
        conf = self.source("human:reviewer-1", fields={"licence": "client-confidential", "tier": "reviewer-verified"})
        st, b = self.append(fact(0.2, sources=[pub, conf], quotes={conf: "the client said so"}))
        self.assertEqual(set(b["fact"]["sources"]), {pub, conf})
        q = "/v1/layers/facts?layer=category&entity=category/automotive.ev_charging"
        other = self.call("GET", q, org="org/b")[1]["facts"][0]
        self.assertEqual(other["sources"], [pub])
        self.assertNotIn("the client said so", json.dumps(other))
        st, b = self.append(fact(0.2, sources=[conf]), org="org/b")
        self.assertEqual(st, 400, "another org cannot cite it")

    def test_roster_atomic(self):
        # the second category is unknown: nothing may be written, not even the first
        st, b = self.call("PUT", "/v1/layers/roster/brand/bmw", {
            "name": "BMW", "categories": ["automotive.ev_charging", "automotive.nosuch"], "sources": [],
            "decision": {**DEC, "id": "d_ROSTER0001"}}, brand="brand/x")
        self.assertEqual((st, b["error"]["type"]), (400, "unknown_leaf"))
        self.assertEqual(self.call("GET", "/v1/layers/roster/brand/bmw", brand="brand/x")[0], 404)
        self.assertEqual(self.call("GET", "/v1/layers/decisions/d_ROSTER0001", brand="brand/x")[0], 404)
        self.assertEqual(self.call("POST", "/v1/layers/brands/find", {"text": "bmw"})[1]["brands"], [])

    def test_failure_inside_the_transaction_leaves_nothing(self):
        orig = self.app._append

        def boom(db, *a, **kw):
            orig(db, *a, **kw)
            raise RuntimeError("disk on fire")
        self.app._append = boom
        st, b = self.append(fact(0.2), {**DEC, "id": "d_BOOM000001"})
        self.assertEqual((st, b["error"]["type"]), (500, "internal"))
        self.app._append = orig
        self.assertEqual(self.call("GET", "/v1/layers/decisions/d_BOOM000001")[0], 404)
        q = "/v1/layers/facts?layer=category&entity=category/automotive.ev_charging"
        self.assertEqual(self.call("GET", q)[1]["facts"], [])

    def test_idempotency_scoped_per_org(self):
        st, a = self.append(fact(0.2), key="same-key")
        st2, b = self.append(fact(0.9, key="market.other"), key="same-key", org="org/b")
        self.assertEqual((st, st2), (200, 200), "another org's identical key is its own")

    def test_concurrent_supersessions_are_serialised(self):
        results = []

        def go(i):
            results.append(self.append(fact(0.1 + i / 100, as_of=f"2026-0{1 + i % 9}-01"))[0])
        threads = [threading.Thread(target=go, args=(i,)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results, [200] * 16)
        rows = self.app._q("SELECT version FROM facts WHERE key='market.bev_share' ORDER BY version")
        self.assertEqual([r["version"] for r in rows], list(range(1, 17)), "no version collides")


class OverHttp(unittest.TestCase):
    def test_layers_contract(self):
        s = Server(MOCK_FAKES="layers")
        try:
            out = s.run_contract("layers_contract.py")
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            out = s.run_contract("layers_contract.py")          # a re-run against the same store
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            self.assertEqual(s.runs(), [], "the layers never spawn claude")
        finally:
            s.close()

    def test_layers_contract_with_a_token(self):
        s = Server(MOCK_FAKES="layers", MOCK_TOKEN="sekrit")
        try:
            st, b = s.get("/v1/layers/categories", headers={"X-Napkin-Org": "org/a"})
            self.assertEqual((st, b["error"]["type"]), (401, "unauthenticated"))
            out = s.run_contract("layers_contract.py", "--token", "sekrit")
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
