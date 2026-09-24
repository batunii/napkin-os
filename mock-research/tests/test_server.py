#!/usr/bin/env python3
"""Offline tests for mock-research: the server runs against tests/fake_claude.py,
so validation, caching and the 502/504 paths are exercised without the network.

    python3 mock-research/tests/test_server.py
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent / "server.py"
CONTRACT = HERE.parent / "contract_test.py"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class MockResearchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.log = Path(cls.tmp.name) / "calls.log"
        cls.port = free_port()
        env = {**os.environ, "MOCK_RESEARCH_PORT": str(cls.port),
               "MOCK_RESEARCH_DATA": cls.tmp.name, "MOCK_RESEARCH_TIMEOUT": "2",
               "MOCK_RESEARCH_CLAUDE_BIN": str(HERE / "fake_claude.py"),
               "FAKE_CLAUDE_LOG": str(cls.log)}
        env.pop("MOCK_RESEARCH_NO_CACHE", None)
        cls.proc = subprocess.Popen([sys.executable, str(SERVER)], env=env,
                                    stderr=subprocess.DEVNULL)
        cls.base = f"http://127.0.0.1:{cls.port}"
        for _ in range(50):
            try:
                urllib.request.urlopen(cls.base + "/healthz", timeout=1)
                break
            except OSError:
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.proc.kill()
        cls.proc.wait()
        cls.tmp.cleanup()

    def post(self, body, qs=""):
        req = urllib.request.Request(self.base + "/v1/research" + qs, method="POST",
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def calls(self):
        return len(self.log.read_text().splitlines()) if self.log.exists() else 0

    def body(self, mode, **kw):
        return {"query": f"irish ev market MODE:{mode}", "lens": "market_structure",
                "market": "ie", **kw}

    def test_validation(self):
        st, r = self.post(self.body("ok", max_sources=5))
        self.assertEqual(st, 200)
        s = r["sources"]
        self.assertEqual(len(s), 5)                      # capped
        urls = [x["url"] for x in s]
        self.assertEqual(len(set(urls)), len(urls))      # deduped
        self.assertTrue(all(u.startswith(("http://", "https://")) for u in urls))
        cso = s[0]
        self.assertEqual(cso["url"], "https://www.cso.ie/en/stats")
        self.assertEqual([e["quote"] for e in cso["excerpts"]],
                         ["1,234 new electric cars were licensed.",
                          "A second quote from the same page."])  # merged, stripped, empty dropped
        self.assertEqual(cso["published_at"], "2026-08-01")
        simi = s[1]
        self.assertEqual(simi["publisher"], "www.simi.ie")  # empty publisher -> host
        self.assertNotIn("published_at", simi)              # future date dropped
        self.assertNotIn("published_at", s[2])              # "May 2026" is not an ISO date
        today = time.strftime("%Y-%m-%d", time.gmtime())
        self.assertTrue(all(x["retrieved_at"] == today for x in s))
        self.assertTrue(all(x["id"].startswith("src_") for x in s))
        self.assertEqual(r["trace"]["queries"], ["ireland ev registrations"])
        self.assertEqual(r["trace"]["backend"], "claude-code-websearch")

    def test_cache_and_fresh(self):
        b = self.body("ok", entity="brand/cache-test")
        n0 = self.calls()
        st1, r1 = self.post(b)
        st2, r2 = self.post({**b, "query": "  IRISH ev market   MODE:ok ", "market": "IE"})
        self.assertEqual((st1, st2), (200, 200))
        self.assertEqual(r1, r2)
        self.assertEqual(self.calls(), n0 + 1)          # normalised repeat is a hit
        st3, _ = self.post(b, "?fresh=1")
        self.assertEqual(st3, 200)
        self.assertEqual(self.calls(), n0 + 2)          # fresh bypasses

    def test_honest_empty(self):
        st, r = self.post(self.body("empty"))
        self.assertEqual(st, 200)
        self.assertEqual(r["sources"], [])

    def test_failures_are_502(self):
        for mode in ("exit", "is_error", "garbage", "nostructured"):
            st, r = self.post(self.body(mode))
            self.assertEqual(st, 502, mode)
            self.assertEqual(r["error"]["type"], "upstream_error")

    def test_timeout_is_504(self):
        t0 = time.monotonic()
        st, r = self.post(self.body("sleep"))
        self.assertEqual(st, 504)
        self.assertLess(time.monotonic() - t0, 10)
        self.assertEqual(r["error"]["type"], "timeout")

    def test_failures_not_cached(self):
        n0 = self.calls()
        self.post(self.body("exit", entity="brand/nocache"))
        self.post(self.body("exit", entity="brand/nocache"))
        self.assertEqual(self.calls(), n0 + 2)

    def test_unknown_field_400(self):
        st, _ = self.post({**self.body("ok"), "brand": "x"})
        self.assertEqual(st, 400)

    def test_contract_suite_offline(self):
        out = subprocess.run([sys.executable, str(CONTRACT), "--base-url", self.base,
                              "--no-live"], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
