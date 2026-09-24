"""The research family against the fake claude: validation, caching, the
502/504 paths and the contract's tidy-ups. Ported from
mock-research/tests/test_server.py. No Claude spend, no network."""

from __future__ import annotations

import time
import unittest

from harness import Server

import research_port as rp


def body(mode, **kw):
    return {"query": f"irish ev market MODE:{mode}", "lens": "market_structure", "market": "ie", **kw}


class Units(unittest.TestCase):
    def test_request_rules(self):
        ok = rp.parse_request(b'{"query": "  a   b ", "lens": "media_spend", "market": "gb"}')
        self.assertEqual(ok, {"query": "a b", "lens": "media_spend", "market": "GB", "max_sources": 8})
        for raw in (b'{"query": "q", "lens": "media_spend", "market": "UK"}',
                    b'{"query": "' + b"x" * 501 + b'", "lens": "media_spend", "market": "IE"}',
                    b'{"query": "q", "lens": "media_spend", "market": "IE", "entity": "BMW"}',
                    b'{"query": "q", "lens": "media_spend", "market": "IE", "category": "automotive"}'):
            with self.subTest(raw[:60]):
                with self.assertRaises(rp.PeripheralError) as cm:
                    rp.parse_request(raw)
                self.assertEqual((cm.exception.status, cm.exception.type), (400, "invalid_input"))

    def test_published_at_never_future(self):
        self.assertIsNone(rp.valid_date("2999-01-01"))
        self.assertIsNone(rp.valid_date("May 2026"))
        self.assertEqual(rp.valid_date("2026-08-01"), "2026-08-01")


class ResearchServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = Server(MOCK_FAKES="research", MOCK_TIMEOUT_RESEARCH=2)

    @classmethod
    def tearDownClass(cls):
        cls.s.close()

    def post(self, b, qs=""):
        return self.s.post("/v1/research" + qs, b, timeout=20)

    def calls(self):
        return len(self.s.runs())

    def test_validation(self):
        st, r = self.post(body("ok", max_sources=5))
        self.assertEqual(st, 200, r)
        s = r["sources"]
        self.assertEqual(len(s), 5)                                    # capped
        urls = [x["url"] for x in s]
        self.assertEqual(len(set(urls)), len(urls))                    # one per URL
        self.assertTrue(all(u.startswith(("http://", "https://")) for u in urls))
        cso = s[0]
        self.assertEqual(cso["url"], "https://www.cso.ie/en/stats")
        self.assertEqual([e["quote"] for e in cso["excerpts"]],
                         ["1,234 new electric cars were licensed.", "A second quote from the same page."])
        self.assertEqual(cso["published_at"], "2026-08-01")
        self.assertEqual(s[1]["publisher"], "www.simi.ie")             # empty publisher -> host
        self.assertNotIn("published_at", s[1])                          # future date dropped
        self.assertNotIn("published_at", s[2])                          # not an ISO date
        today = time.strftime("%Y-%m-%d", time.gmtime())
        self.assertTrue(all(x["retrieved_at"] == today for x in s))
        self.assertEqual(r["trace"], {"backend": "claude-code-websearch", "queries": ["ireland ev registrations"],
                                      "model": "sonnet"})

    def test_cli_flags(self):
        self.post(body("ok", entity="brand/flags"), "?fresh=1")
        run = sorted(self.s.runs(), key=lambda x: x["start"])[-1]
        argv = " ".join(run["argv"])
        for f in ("--tools WebSearch WebFetch", "--allowedTools WebSearch WebFetch", "--permission-mode dontAsk",
                  "--no-session-persistence", "--setting-sources", "--strict-mcp-config", "--json-schema"):
            self.assertIn(f, argv)
        self.assertEqual(run["cwd_entries"], [])

    def test_cache_and_fresh(self):
        b = body("ok", entity="brand/cache-test")
        n0 = self.calls()
        st1, r1 = self.post(b)
        st2, r2 = self.post({**b, "query": "  IRISH ev market   MODE:ok ", "market": "IE"})
        self.assertEqual((st1, st2), (200, 200))
        self.assertEqual(r1, r2)
        self.assertEqual(self.calls(), n0 + 1)          # a normalised repeat is a hit
        self.assertEqual(self.post(b, "?fresh=1")[0], 200)
        self.assertEqual(self.calls(), n0 + 2)          # fresh bypasses

    def test_honest_empty(self):
        st, r = self.post(body("empty"))
        self.assertEqual((st, r["sources"]), (200, []))

    def test_failures_are_502_upstream_failed(self):
        for mode in ("exit", "is_error", "garbage", "nostructured"):
            st, r = self.post(body(mode))
            self.assertEqual((st, r["error"]["type"]), (502, "upstream_failed"), mode)

    def test_timeout_is_504(self):
        t0 = time.monotonic()
        st, r = self.post(body("sleep"))
        self.assertEqual((st, r["error"]["type"]), (504, "timeout"))
        self.assertLess(time.monotonic() - t0, 10)

    def test_failures_not_cached(self):
        n0 = self.calls()
        self.post(body("exit", entity="brand/nocache"))
        self.post(body("exit", entity="brand/nocache"))
        self.assertEqual(self.calls(), n0 + 2)

    def test_errors(self):
        st, r = self.post({**body("ok"), "brand": "x"})
        self.assertEqual((st, r["error"]["type"]), (400, "invalid_input"))
        st, r = self.s.get("/v1/research")
        self.assertEqual((st, r["error"]["type"]), (405, "method_not_allowed"))

    def test_contract_suite_offline_and_against_the_fake(self):
        out = self.s.run_contract("research_contract.py", "--no-live")
        self.assertEqual(out.returncode, 0, out.stdout)
        # The live part too: the fake stands in for the search, so this checks
        # the suite's response checks against the service's output shape.
        req = self.s.tmp.name + "/req.json"
        with open(req, "w") as f:
            f.write('{"query": "irish ev MODE:ok", "lens": "market_structure", "market": "IE", "max_sources": 6}')
        out = self.s.run_contract("research_contract.py", "--request", req, "--cache-check")
        self.assertEqual(out.returncode, 0, out.stdout)


if __name__ == "__main__":
    unittest.main()
