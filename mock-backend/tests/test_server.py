"""The process: health, MOCK_FAKES, auth, 404s, startup refusals, the log line."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest

from harness import SERVER, Server, free_port


class Process(unittest.TestCase):
    def test_health_lists_what_it_fakes(self):
        s = Server()
        try:
            st, h = s.get("/healthz")
            self.assertEqual(st, 200)
            self.assertEqual((h["ok"], h["service"]), (True, "napkin-mock-backend"))
            self.assertEqual(set(h["fakes"]), {"model", "research", "retrieval", "layers"})
            self.assertEqual(h["fakes"]["model"]["shapes"], ["anthropic", "openai"])
            self.assertTrue(h["fakes"]["model"]["images"])
            self.assertEqual(h["fakes"]["model"]["models"]["claude-opus-5"], "opus")
            self.assertEqual(h["fakes"]["research"]["api"], "napkin.research/1")
            self.assertEqual(h["fakes"]["retrieval"]["kinds"], ["digest"])
            self.assertEqual(h["fakes"]["retrieval"]["packs_dir"], "engine/packs_dist")
            self.assertEqual(h["fakes"]["layers"]["backend"], "sqlite")
            self.assertEqual((h["concurrency"], h["in_flight"]), (4, 0))
        finally:
            s.close()

    def test_mock_fakes_turns_families_off(self):
        s = Server(MOCK_FAKES="layers,retrieval")
        try:
            self.assertEqual(set(s.get("/healthz")[1]["fakes"]), {"layers", "retrieval"})
            for path in ("/v1/messages", "/v1/chat/completions", "/v1/research"):
                st, b = s.post(path, {"x": 1})
                self.assertEqual((st, b["error"]["type"]), (404, "not_found"), path)
            self.assertEqual(s.get("/v1/models")[0], 404)
            self.assertEqual(s.get("/v1/packs", headers={"X-Napkin-Org": "org/a"})[0], 200)
        finally:
            s.close()

    def test_embeddings_and_unknown_paths_404(self):
        s = Server(MOCK_FAKES="model,layers")
        try:
            st, b = s.post("/v1/embeddings", {"input": "x", "model": "nvidia/nv-embedqa-e5-v5"})
            self.assertEqual(st, 404)
            self.assertIn("embeddings", b["error"]["message"])
            self.assertEqual(s.get("/nope")[0], 404)
            self.assertEqual(s.get("/v1/layers")[0], 404)
            self.assertEqual(s.runs(), [])
        finally:
            s.close()

    def test_token_every_route_in_its_shape(self):
        s = Server(MOCK_TOKEN="sekrit")
        try:
            self.assertEqual(s.get("/healthz")[0], 401)
            self.assertEqual(s.get("/healthz", headers={"Authorization": "Bearer sekrit"})[0], 200)
            msg = {"model": "claude-haiku-4-5", "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]}
            st, b = s.post("/v1/messages", msg)
            self.assertEqual((st, b["type"], b["error"]["type"]), (401, "error", "authentication_error"))
            self.assertEqual(s.post("/v1/messages", msg, headers={"x-api-key": "sekrit"})[0], 200)
            st, b = s.post("/v1/chat/completions", msg)
            self.assertEqual((st, b["error"]["code"]), (401, "invalid_api_key"))
            self.assertEqual(s.post("/v1/chat/completions", msg, headers={"Authorization": "Bearer sekrit"})[0], 200)
            st, b = s.post("/v1/research", {"query": "q MODE:ok", "lens": "media_spend", "market": "IE"})
            self.assertEqual((st, b["error"]["type"]), (401, "unauthenticated"))
            st, b = s.get("/v1/packs", headers={"X-Napkin-Org": "org/a", "Authorization": "Bearer wrong"})
            self.assertEqual((st, b["error"]["type"]), (401, "unauthenticated"))
        finally:
            s.close()

    def test_log_line_is_metadata_only(self):
        s = Server(MOCK_FAKES="layers")
        try:
            s.call("POST", "/v1/layers/categories/find", {"text": "SENTINEL-cider-44"},
                   headers={"X-Napkin-Org": "org/a", "X-Napkin-Handler": "draft_brief@1.0", "X-Napkin-Job": "job_7"})
            log = s.stderr()
            self.assertIn("POST /v1/layers/categories/find -> 200", log)
            self.assertIn("handler=draft_brief@1.0 job=job_7", log)
            self.assertNotIn("SENTINEL", log)
        finally:
            s.close()

    def run_server(self, **env):
        full = {k: v for k, v in os.environ.items() if not k.startswith(("MOCK_", "FAKE_"))}
        full.update({k: str(v) for k, v in env.items()})
        return subprocess.run([sys.executable, str(SERVER)], env=full, capture_output=True, text=True, timeout=20)

    def test_startup_refusals(self):
        out = self.run_server(MOCK_BACKEND_PORT=8795)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("reserved", out.stderr)
        out = self.run_server(MOCK_BACKEND_PORT=free_port(), MOCK_CLAUDE_BIN="/nonexistent/claude")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("not on PATH", out.stderr)
        out = self.run_server(MOCK_BACKEND_PORT=free_port(), MOCK_FAKES="model,vibes")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("vibes", out.stderr)

    def test_layers_alone_needs_no_claude(self):
        s = Server(MOCK_FAKES="layers", MOCK_CLAUDE_BIN="/nonexistent/claude")
        try:
            self.assertEqual(set(s.get("/healthz")[1]["fakes"]), {"layers"})
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
