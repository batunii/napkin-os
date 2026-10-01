"""MOCK_RESEARCH_BACKEND=search-jev with the agent, the page fetch and jev replaced: the excerpts are verbatim page
text, the best passages across pages win under the unit budget, a unit with nothing readable falls back to the
ordinary agent, jev's cost reaches the ledger, and the default backend is untouched. Nothing here spends."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from types import SimpleNamespace

import common
import research_port as rp
import search_jev as sj

PAGE_A = ("Navigation Home About Contact Menu. " * 20 + "In 2025 the Irish tea market was worth EUR 85 million, "
          "and Barry's held a 38% share of value. " + "Cookie notice and footer text repeated. " * 30)
PAGE_B = ("Unrelated filler about gardening and weather. " * 40 + "Tea drinkers in Ireland number 2.1 million adults. "
          + "More filler text about nothing in particular. " * 30)


class FakeJev:
    """Scores a passage by whether it holds a digit: what the real question asks, crudely."""
    def __init__(self):
        self.requests = 0

    def system_one(self, state, questions, model):
        self.requests += 1
        nouls = {k: SimpleNamespace(noul=0.9 if any(ch.isdigit() for ch in q["instructions"]["passage"]) else 0.1)
                 for k, q in questions.items()}
        return SimpleNamespace(nouls=nouls, usage=SimpleNamespace(input_tokens=1000))


class SearchJev(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {k: os.environ.get(k) for k in ("MOCK_DATA", "MOCK_RESEARCH_BACKEND")}
        os.environ.update(MOCK_DATA=self.tmp.name, MOCK_RESEARCH_BACKEND="search-jev")
        self.cfg = common.Config()
        self.rs = rp.Research(self.cfg, common.Slots(2))
        self.orig = (sj.run_claude, sj.fetch, sj._client, sj.UNIT_CHARS)
        self.candidates = [{"url": "https://www.example.ie/a", "title": "Tea market", "publisher": "Example"},
                           {"url": "https://www.example.ie/b", "title": "Tea drinkers", "publisher": "Example"}]
        sj.run_claude = lambda cfg, call, timeout, root=None: {
            "structured_output": {"candidates": self.candidates, "queries": ["irish tea market size"]},
            "total_cost_usd": 0.02}
        self.texts = {"https://www.example.ie/a": PAGE_A, "https://www.example.ie/b": PAGE_B}
        sj.fetch = lambda url: {"text": self.texts[url], "date": "2025-06-01"} if url in self.texts else None
        sj._client = FakeJev()
        self.req = {"query": "How big is the tea market?", "lens": "market_structure", "market": "IE", "max_sources": 6}

    def tearDown(self):
        sj.run_claude, sj.fetch, sj._client, sj.UNIT_CHARS = self.orig
        for k, v in self.saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        self.tmp.cleanup()

    def test_excerpts_are_verbatim_page_text_and_hold_the_figures(self):
        sj.UNIT_CHARS = 1500
        out = self.rs.research(self.req, fresh=True)
        self.assertEqual(out["trace"]["backend"], "search-jev")
        quotes = {s["url"]: [e["quote"] for e in s["excerpts"]] for s in out["sources"]}
        for url, qs in quotes.items():
            for q in qs:
                self.assertIn(q, self.texts[url])
        joined = " ".join(q for qs in quotes.values() for q in qs)
        self.assertIn("EUR 85 million", joined)
        self.assertIn("2.1 million adults", joined)

    def test_the_unit_budget_caps_what_is_kept(self):
        sj.UNIT_CHARS = 600
        out = self.rs.research(self.req, fresh=True)
        kept = sum(len(e["quote"]) for s in out["sources"] for e in s["excerpts"])
        self.assertLessEqual(kept, 600 + sj.WIN)          # the window that crosses the budget is kept whole
        self.assertTrue(all(len(e["quote"]) <= sj.MAX_QUOTE_CHARS for s in out["sources"] for e in s["excerpts"]))

    def test_nothing_readable_falls_back_to_the_ordinary_agent(self):
        self.texts = {}
        called = []
        self.rs._run_agent = lambda req: (called.append(req["lens"]),
                                          {"output": {"sources": [], "queries": []}, "cost_usd": 0.1})[1]
        self.rs.research(self.req, fresh=True)
        self.assertEqual(called, ["market_structure"])

    def test_jev_cost_reaches_the_ledger(self):
        self.rs.research(self.req, fresh=True)
        rows = [json.loads(l) for l in open(self.tmp.name + "/metrics.jsonl")]
        jev = [r for r in rows if r.get("alias") == "jev"]
        self.assertEqual(len(jev), 1)
        self.assertAlmostEqual(jev[0]["cost_usd"], sj._client.requests * 1000 * sj.JEV_USD_PER_TOKEN)

    def test_the_cache_key_is_its_own(self):
        self.assertNotEqual(rp.cache_key(self.req, "search-jev"), rp.cache_key(self.req, "sonnet"))

    def test_windows_hold_any_short_quote_whole(self):
        text = " ".join(f"word{i}" for i in range(2000))
        ws = sj.windows(text)
        q = " ".join(f"word{i}" for i in range(700, 730))          # ~240 characters
        self.assertTrue(any(q in w[2] for w in ws))


class DefaultUntouched(unittest.TestCase):
    def test_the_default_backend_is_still_the_agent(self):
        saved = os.environ.pop("MOCK_RESEARCH_BACKEND", None)
        try:
            self.assertEqual(common.Config().research_backend, "claude-code")
        finally:
            if saved is not None:
                os.environ["MOCK_RESEARCH_BACKEND"] = saved


if __name__ == "__main__":
    unittest.main()
