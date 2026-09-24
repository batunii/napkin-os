"""Offline tests: the translation layer, and the server against a fake `claude`.

    cd mock-llm && python3 -m unittest tests.test_offline -v

No network, no Claude Code calls. The live checks are contract_test.py and
sdk_test.py.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from mock_llm.translate import (ApiError, MODEL_MAP, build_call, envelope_error,  # noqa: E402
                                parse_body, to_response, usage_of)

FAKE = str(HERE / "fake_claude.py")
MSG = [{"role": "user", "content": "hi"}]


def req(**kw):
    body = {"model": "claude-haiku-4-5", "max_tokens": 16, "messages": MSG}
    body.update(kw)
    return body


class TranslateTests(unittest.TestCase):
    def err(self, body) -> ApiError:
        with self.assertRaises(ApiError) as cm:
            build_call(body)
        return cm.exception

    def test_model_map(self):
        self.assertEqual(MODEL_MAP["claude-opus-5"], "opus")
        self.assertEqual(MODEL_MAP["claude-sonnet-5"], "sonnet")
        self.assertEqual(MODEL_MAP["claude-haiku-4-5"], "haiku")
        self.assertEqual(MODEL_MAP["claude-fable-5-1"], "fable")
        e = self.err(req(model="claude-opus-4-8"))
        self.assertEqual((e.status, e.type), (404, "not_found_error"))

    def test_refusals(self):
        cases = {
            "tools": req(tools=[{"name": "x", "input_schema": {"type": "object"}}]),
            "stream": req(stream=True),
            "no max_tokens": {"model": "claude-haiku-4-5", "messages": MSG},
            "empty messages": req(messages=[]),
            "extra field": req(bogus=1),
            "prefill": req(messages=MSG + [{"role": "assistant", "content": "ok"}]),
            "image": req(messages=[{"role": "user", "content": [{"type": "image", "source": {}}]}]),
            "bad format": req(output_config={"format": {"type": "text"}}),
        }
        for name, body in cases.items():
            with self.subTest(name):
                e = self.err(body)
                self.assertEqual((e.status, e.type), (400, "invalid_request_error"), e.message)

    def test_non_utf8_is_400(self):
        with self.assertRaises(ApiError) as cm:
            parse_body(b'{"model": "\xff\xfe"}')
        self.assertEqual(cm.exception.status, 400)

    def test_system_string_or_blocks_cache_control_dropped(self):
        self.assertEqual(build_call(req(system="be brief")).system, "be brief")
        call = build_call(req(system=[{"type": "text", "text": "A", "cache_control": {"type": "ephemeral"}},
                                      {"type": "text", "text": "B"}]))
        self.assertEqual(call.system, "A\n\nB")

    def test_ignored_params_accepted(self):
        call = build_call(req(temperature=0.2, stop_sequences=["x"], thinking={"type": "adaptive"},
                              output_config={"effort": "low"}))
        self.assertEqual(call.effort, "low")
        self.assertEqual(call.ignored, ["stop_sequences", "temperature", "thinking"])

    def test_flatten_multi_turn(self):
        call = build_call(req(messages=[{"role": "user", "content": "one"},
                                        {"role": "assistant", "content": [{"type": "text", "text": "two"}]},
                                        {"role": "user", "content": "three"}]))
        self.assertIn("<user>\none\n</user>", call.prompt)
        self.assertIn("<assistant>\ntwo\n</assistant>", call.prompt)
        self.assertTrue(call.prompt.rstrip().endswith("<user>\nthree\n</user>"))
        self.assertEqual(build_call(req()).prompt, "hi")

    def test_usage_zeros_never_estimates(self):
        self.assertEqual(usage_of({}), {"input_tokens": 0, "output_tokens": 0,
                                        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0})
        u = usage_of({"usage": {"input_tokens": 3, "cache_read_input_tokens": 100, "output_tokens": 2}})
        self.assertEqual(u["cache_read_input_tokens"], 0)
        self.assertEqual(u["input_tokens"], 103)

    def test_response_shape_and_structured(self):
        call = build_call(req(output_config={"format": {"type": "json_schema", "schema": {"type": "object"}}}))
        r = to_response({"is_error": False, "subtype": "success", "result": "ignored",
                         "structured_output": {"a": 1}}, call, "claude-haiku-4-5")
        self.assertEqual(r["type"], "message")
        self.assertEqual(r["stop_reason"], "end_turn")
        self.assertIsNone(r["stop_sequence"])
        self.assertEqual(json.loads(r["content"][0]["text"]), {"a": 1})
        self.assertTrue(r["id"].startswith("msg_"))

    def test_envelope_errors(self):
        e = envelope_error({"is_error": True, "result": "Not logged in · Please run /login"})
        self.assertEqual((e.status, e.type), (401, "authentication_error"))
        self.assertIn("Not logged in", e.message)
        e = envelope_error({"is_error": True, "result": "overloaded", "api_error_status": 529})
        self.assertEqual((e.status, e.type), (529, "overloaded_error"))
        self.assertIsNone(envelope_error({"is_error": False, "subtype": "success"}))


def free_port() -> int:
    while True:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            p = s.getsockname()[1]
        if p not in (8080, 8790):
            return p


class Server:
    """The real mock server in a subprocess, pointed at the fake claude."""

    def __init__(self, **env):
        self.port = free_port()
        self.tmp = tempfile.TemporaryDirectory(prefix="mock-llm-test-")
        self.log_dir = Path(self.tmp.name) / "log"
        self.log_dir.mkdir()
        self.scratch = Path(self.tmp.name) / "scratch"   # server cwd and TMPDIR
        self.scratch.mkdir()
        full = {k: v for k, v in os.environ.items() if not k.startswith(("MOCK_LLM_", "FAKE_CLAUDE_"))}
        full.update(MOCK_LLM_PORT=str(self.port), MOCK_LLM_CLAUDE_BIN=FAKE,
                    FAKE_CLAUDE_LOG=str(self.log_dir), TMPDIR=str(self.scratch),
                    PYTHONPATH=str(ROOT))
        full.update({k: str(v) for k, v in env.items()})
        self.proc = subprocess.Popen([sys.executable, "-m", "mock_llm"], cwd=self.scratch, env=full,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.base = f"http://127.0.0.1:{self.port}"
        for _ in range(100):
            try:
                urllib.request.urlopen(self.base + "/health", timeout=1).read()
                return
            except OSError:
                time.sleep(0.05)
        self.close()
        raise RuntimeError("mock server did not start")

    def post(self, body, raw: bytes | None = None, timeout=30):
        data = raw if raw is not None else json.dumps(body).encode()
        r = urllib.request.Request(self.base + "/v1/messages", data=data, method="POST",
                                   headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def health(self):
        return json.loads(urllib.request.urlopen(self.base + "/health", timeout=5).read())

    def runs(self):
        return [json.loads(p.read_text()) for p in self.log_dir.glob("*.json")]

    def close(self):
        self.proc.kill()
        self.proc.wait()
        self.tmp.cleanup()


class ServerTests(unittest.TestCase):
    def test_round_trip_and_privacy_of_system(self):
        s = Server()
        try:
            status, r = s.post(req(system=[{"type": "text", "text": "SYS-SENTINEL",
                                            "cache_control": {"type": "ephemeral"}}]))
            self.assertEqual(status, 200, r)
            echoed = json.loads(r["content"][0]["text"])
            self.assertEqual(echoed, {"system": "SYS-SENTINEL", "prompt": "hi", "model": "haiku"})
            self.assertEqual(r["usage"], {"input_tokens": 23, "output_tokens": 3,
                                          "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0})
            (run,) = s.runs()
            argv = " ".join(run["argv"])
            self.assertNotIn("SYS-SENTINEL", argv, "system text must not be on argv")
            self.assertNotIn("--resume", argv)
            self.assertIn("--no-session-persistence", argv)
            self.assertEqual(run["cwd_entries"], [], "claude runs in an empty directory")
        finally:
            s.close()

    def test_structured_output_text_is_json(self):
        s = Server()
        try:
            schema = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"],
                      "additionalProperties": False}
            status, r = s.post(req(output_config={"format": {"type": "json_schema", "schema": schema}}))
            self.assertEqual(status, 200, r)
            self.assertEqual(json.loads(r["content"][0]["text"]), {"a": "x"})
            (run,) = s.runs()
            self.assertEqual(json.loads(run["argv"][run["argv"].index("--json-schema") + 1]), schema)
        finally:
            s.close()

    def test_concurrency_cap(self):
        s = Server(MOCK_LLM_CONCURRENCY=3, FAKE_CLAUDE_SLEEP=0.4)
        try:
            results = []
            threads = [threading.Thread(target=lambda: results.append(s.post(req())[0])) for _ in range(20)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(results, [200] * 20)
            self.assertEqual(s.health()["inflight_peak"], 3)
            runs = s.runs()
            self.assertEqual(len(runs), 20)
            # Independent check from the subprocesses' own clocks.
            events = sorted([(r["start"], 1) for r in runs] + [(r["end"], -1) for r in runs])
            live = peak = 0
            for _, d in events:
                live += d
                peak = max(peak, live)
            self.assertLessEqual(peak, 3)
        finally:
            s.close()

    def test_timeout_is_504(self):
        s = Server(MOCK_LLM_TIMEOUT=0.5, FAKE_CLAUDE_SLEEP=10)
        try:
            t0 = time.monotonic()
            status, r = s.post(req())
            self.assertEqual((status, r["error"]["type"]), (504, "api_error"))
            self.assertLess(time.monotonic() - t0, 5)
        finally:
            s.close()

    def test_unauthenticated_cli_is_401_with_its_message(self):
        s = Server(FAKE_CLAUDE_MODE="unauth")
        try:
            status, r = s.post(req())
            self.assertEqual((status, r["type"], r["error"]["type"]), (401, "error", "authentication_error"))
            self.assertIn("Not logged in", r["error"]["message"])
        finally:
            s.close()

    def test_cli_garbage_is_500(self):
        s = Server(FAKE_CLAUDE_MODE="garbage")
        try:
            status, r = s.post(req())
            self.assertEqual((status, r["error"]["type"]), (500, "api_error"))
        finally:
            s.close()

    def test_http_level_refusals(self):
        s = Server(MOCK_LLM_MAX_BODY_BYTES=1000)
        try:
            status, r = s.post(None, raw=b'{"model": "\xff"}')
            self.assertEqual((status, r["error"]["type"]), (400, "invalid_request_error"))
            status, r = s.post(req(messages=[{"role": "user", "content": "x" * 2000}]))
            self.assertEqual((status, r["error"]["type"]), (413, "request_too_large"))
            status, r = s.post(req(model="nope"))
            self.assertEqual((status, r["error"]["type"]), (404, "not_found_error"))
            status, r = s.post(req(tools=[]))
            self.assertEqual(status, 400)
            self.assertEqual(s.runs(), [], "no refusal may spawn claude")
        finally:
            s.close()

    def _files_containing(self, root: Path, needle: bytes):
        hits = []
        for p in root.rglob("*"):
            if p.is_file():
                try:
                    if needle in p.read_bytes():
                        hits.append(p)
                except OSError:
                    pass
        return hits

    def test_no_request_body_on_disk_unless_dump_var_set(self):
        needle = b"CONFIDENTIAL-SENTINEL-7f3a"
        body = req(messages=[{"role": "user", "content": needle.decode()}])
        s = Server()
        try:
            self.assertEqual(s.post(body)[0], 200)
            # Everything the server could write to: its cwd and TMPDIR (scratch).
            # The fake's own log (outside scratch) is test instrumentation.
            self.assertEqual(self._files_containing(s.scratch, needle), [])
        finally:
            s.close()
        # Control: with the var set, the same check does find the body.
        with tempfile.TemporaryDirectory() as dump:
            s = Server(MOCK_LLM_DUMP_REQUEST_BODIES_TO=dump)
            try:
                self.assertEqual(s.post(body)[0], 200)
                self.assertTrue(self._files_containing(Path(dump), needle))
            finally:
                s.close()


if __name__ == "__main__":
    unittest.main()
