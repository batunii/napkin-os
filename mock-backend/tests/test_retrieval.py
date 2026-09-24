"""The retrieval family: pack discovery, quote verification, identity, cache,
S6, against the fake claude. No Claude spend."""

from __future__ import annotations

import hashlib
import importlib.util
import tempfile
import unittest
from pathlib import Path

from harness import ROOT, Server, files_containing

import retrieval_port as rp

REPO = ROOT.parent
DIGESTS = REPO / "engine" / "packs_dist"

AGENCY_DOC = """---
title: Acme Beer Relaunch
award_tier: gold
year: 2019
RETRIEVAL_QUERIES: ignored
---
# Acme Beer Relaunch

## The insight
Midweek drinkers are **cutting back**, not giving up.
They still want the ritual.

## The result
Sales rose 12% in a year.
"""
OTHER_DOC = """---
title: Acme Cider
award_tier: silver
year: 2021
---
## The insight
Cider drinkers want the orchard, not the factory.
"""


def agency_fixture(root: Path) -> Path:
    d = root / "agency" / "acme" / "acme-cases"
    d.mkdir(parents=True)
    (d / "pack.yaml").write_text("tag: acme-cases\nkind: case\nk: 2\nloops: [loop4_insight]\n")
    (d / "acme-beer.md").write_text(AGENCY_DOC)
    (d / "acme-cider.md").write_text(OTHER_DOC)
    return root / "agency"


class Units(unittest.TestCase):
    def test_split_h2_is_the_engines(self):
        spec = importlib.util.spec_from_file_location("engine_rag", REPO / "engine" / "rag" / "rag.py")
        rag = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rag)
        for f in sorted(DIGESTS.glob("*/digest.md")) + [None]:
            text = f.read_text() if f else AGENCY_DOC
            _, body = rp.parse_frontmatter(text)
            self.assertEqual(rp.split_h2(body), rag.split_h2(body), f)

    def test_frontmatter_scalars_typed(self):
        meta, body = rp.parse_frontmatter(AGENCY_DOC)
        self.assertEqual((meta["year"], meta["award_tier"]), (2019, "gold"))
        self.assertTrue(body.startswith("# Acme"))

    def test_locate_is_verbatim_and_whitespace_tolerant(self):
        sec = "Midweek drinkers are **cutting back**, not giving up.\nThey still want the ritual."
        self.assertEqual(rp.locate("are **cutting back**, not giving up. They still", sec),
                         "are **cutting back**, not giving up.\nThey still")
        self.assertEqual(rp.locate("  Midweek   drinkers ", sec), "Midweek drinkers")
        self.assertIsNone(rp.locate("are cutting back", sec), "markdown is part of the text")
        self.assertIsNone(rp.locate("They still want the ritual!", sec))
        self.assertIsNone(rp.locate("   ", sec))

    def test_identity_formula(self):
        text = "Sales rose 12% in a year."
        pid, uri, sha = rp.passage_identity("acme-cases", "acme-beer.md", "The result", text)
        self.assertEqual(sha, hashlib.sha256(text.encode()).hexdigest())
        self.assertEqual(pid, "psg_" + hashlib.sha256(
            "acme-cases\nacme-beer.md\nThe result\nSales rose 12% in a year.".encode()).hexdigest()[:20])
        self.assertEqual(uri, f"passage://acme-cases/acme-beer.md#the-result@{sha[:16]}")
        self.assertEqual(rp.slug("  Craft rules — for a brief!"), "craft-rules-for-a-brief")

    def test_cut_at_a_word(self):
        text, trunc = rp.cut("word " * 1000)
        self.assertTrue(trunc and len(text) <= 4000 and text.endswith("word"))
        self.assertEqual(rp.cut("short"), ("short", False))


class RetrievalServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = tempfile.TemporaryDirectory()
        cls.agency = agency_fixture(Path(cls.fx.name))
        cls.s = Server(MOCK_FAKES="retrieval", MOCK_AGENCY_PACKS_DIR=cls.agency)

    @classmethod
    def tearDownClass(cls):
        cls.s.close()
        cls.fx.cleanup()

    def get(self, path, org="org/dev-agency"):
        return self.s.get(path, headers={"X-Napkin-Org": org} if org else {})

    def post(self, path, body, org="org/dev-agency"):
        return self.s.post(path, body, headers={"X-Napkin-Org": org} if org else {})

    def test_packs_from_packs_dist(self):
        st, r = self.get("/v1/packs")
        self.assertEqual(st, 200, r)
        house = {p["tag"]: p for p in r["packs"] if p["scope"] == "house"}
        self.assertEqual(set(house), {"template", "cannes", "dandad", "ipa", "playbook"})
        self.assertEqual({p["id"] for p in house.values()},
                         {"briefing-template", "cannes", "dandad", "ipa", "playbooks"})
        self.assertTrue(all(p["kind"] == "digest" and p["passages"] == 3 and p["filterable"] == []
                            and p["licence"] == "licensed-internal" for p in house.values()))
        self.assertEqual(house["cannes"]["loops"], ["loop4_insight", "loop6_substantiation"])
        self.assertIsNone(r["embed_model"])
        self.assertEqual(r["backend"], "claude-code-packs")
        self.assertNotIn("acme-cases", [p["tag"] for p in r["packs"]])

    def test_quotes_verified_against_the_file(self):
        st, r = self.post("/v1/retrieve", {"query": "what makes a great brief", "k": 20, "packs": ["cannes"]})
        self.assertEqual(st, 200, r)
        ps = r["passages"]
        text = (DIGESTS / "cannes" / "digest.md").read_text()
        # three sections, first line of each; the duplicate, the whitespace-mangled
        # copy, the invented quote and the bad index are all dropped
        self.assertEqual(len(ps), 3)
        self.assertEqual([p["section"] for p in ps], ["What great looks like", "Craft rules for a brief", "Traps"])
        for p in ps:
            self.assertIn(p["text"], text)
            pid, uri, sha = rp.passage_identity(p["pack"], p["source"], p["section"], p["text"])
            self.assertEqual((p["id"], p["uri"], p["text_sha256"]), (pid, uri, sha))
            self.assertEqual(p["citation"], f"digest.md › {p['section']}")
            self.assertEqual((p["scope"], p["metadata"]), ("house", {"source": "cannes"}))
        self.assertEqual([p["score"] for p in ps], [1.0, 0.95, 0.9])
        self.assertEqual(r["trace"]["packs"].keys(), {"cannes"})

    def test_k_caps(self):
        st, r = self.post("/v1/retrieve", {"query": "traps to avoid", "k": 2})
        self.assertEqual((st, len(r["passages"])), (200, 2))
        self.assertEqual(set(r["trace"]["packs"]), {"template", "cannes", "dandad", "ipa", "playbook"})

    def test_cache_holds_no_query(self):
        q = "QUERY-SENTINEL-19ab midweek drinkers"
        n0 = len(self.s.runs())
        a = self.post("/v1/retrieve", {"query": q, "k": 2, "packs": ["ipa"]})
        b = self.post("/v1/retrieve", {"query": "  " + q.lower() + " ", "k": 2, "packs": ["ipa"]})
        self.assertEqual(a, b)
        self.assertEqual(len(self.s.runs()), n0 + 1, "a normalised repeat is a cache hit")
        self.s.post("/v1/retrieve?fresh=1", {"query": q, "k": 2, "packs": ["ipa"]},
                    headers={"X-Napkin-Org": "org/dev-agency"})
        self.assertEqual(len(self.s.runs()), n0 + 2)
        self.assertEqual(files_containing(self.s.data, b"QUERY-SENTINEL"), [])
        self.assertEqual(files_containing(self.s.data, b"query-sentinel"), [])
        self.assertTrue(list((self.s.data / "cache" / "retrieval").glob("*.json")))
        run = sorted(self.s.runs(), key=lambda x: x["start"])[-1]
        self.assertNotIn("SENTINEL", " ".join(run["argv"]), "the query goes on stdin, never argv")
        self.assertIn("--system-prompt-file", run["argv"])

    def test_agency_pack_where_and_s6(self):
        st, r = self.get("/v1/packs", org="org/acme")
        acme = next(p for p in r["packs"] if p["tag"] == "acme-cases")
        self.assertEqual((acme["scope"], acme["kind"], acme["filterable"], acme["passages"]),
                         ("agency", "case", ["award_tier", "title", "year"], 3))
        st, r = self.post("/v1/retrieve", {"query": "insight", "k": 5, "packs": ["acme-cases"],
                                           "where": {"award_tier": "gold", "year": 2019}}, org="org/acme")
        self.assertEqual(st, 200, r)
        self.assertEqual({p["source"] for p in r["passages"]}, {"acme-beer.md"})
        self.assertTrue(all(p["scope"] == "agency" and p["metadata"]["year"] == 2019 for p in r["passages"]))
        n0 = len(self.s.runs())
        st, r = self.post("/v1/retrieve", {"query": "insight", "k": 5, "packs": ["acme-cases"],
                                           "where": {"year": "2019"}}, org="org/acme")
        self.assertEqual((st, r["passages"]), (200, []), "exact match: the string 2019 is not the number")
        self.assertEqual(len(self.s.runs()), n0, "nothing to search spends nothing")
        st, r = self.post("/v1/retrieve", {"query": "x", "k": 1, "packs": ["acme-cases"]}, org="org/other")
        self.assertEqual((st, r["error"]["type"]), (404, "unknown_pack"))
        st, r = self.post("/v1/retrieve", {"query": "x", "k": 1, "packs": ["cannes", "acme-cases"],
                                           "where": {"year": 2019}}, org="org/acme")
        self.assertEqual((st, r["error"]["type"]), (400, "invalid_input"), "year is not filterable in cannes")

    def test_contract_suite_against_the_fake(self):
        out = self.s.run_contract("retrieval_contract.py", "--org", "org/dev-agency", "--agency-pack", "acme-cases",
                                  "--agency-owner", "org/acme", "--other-org", "org/other")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)


class RetrievalFailures(unittest.TestCase):
    def test_upstream_failed_and_invented_quotes(self):
        s = Server(MOCK_FAKES="retrieval", FAKE_RETRIEVAL="fail")
        try:
            st, r = s.post("/v1/retrieve", {"query": "x", "k": 1}, headers={"X-Napkin-Org": "org/a"})
            self.assertEqual((st, r["error"]["type"]), (502, "upstream_failed"))
            self.assertEqual(list((s.data / "cache" / "retrieval").glob("*.json")) if (s.data / "cache").exists()
                             else [], [], "a failure is never cached")
        finally:
            s.close()
        s = Server(MOCK_FAKES="retrieval", FAKE_RETRIEVAL="invent")
        try:
            st, r = s.post("/v1/retrieve", {"query": "x", "k": 3}, headers={"X-Napkin-Org": "org/a"})
            self.assertEqual((st, r["passages"]), (200, []), "a quote not in the file never reaches a response")
        finally:
            s.close()

    def test_timeout_504(self):
        s = Server(MOCK_FAKES="retrieval", MOCK_TIMEOUT_RETRIEVAL=0.5, FAKE_CLAUDE_SLEEP=5)
        try:
            st, r = s.post("/v1/retrieve", {"query": "x", "k": 1}, headers={"X-Napkin-Org": "org/a"})
            self.assertEqual((st, r["error"]["type"]), (504, "timeout"))
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
