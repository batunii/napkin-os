"""The model family, both wires: translation units, and the server against the
fake claude. Ported from mock-llm/tests/test_offline.py and extended for the
OpenAI wire, images and the alias map. No Claude spend.

    python3 -m unittest discover -s mock-backend/tests -v
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from harness import Server, files_containing

import model_port as mp
from model_port import WireError, build_anthropic, build_openai, envelope_error, parse_body, to_anthropic, \
    to_openai, usage_of
from png_text import png_text

MSG = [{"role": "user", "content": "hi"}]
PNG = base64.b64encode(png_text("NAPKIN 42")).decode()


def areq(**kw):
    body = {"model": "claude-haiku-4-5", "max_tokens": 16, "messages": MSG}
    body.update(kw)
    return body


def oreq(**kw):
    body = {"model": "claude-haiku-4-5", "max_tokens": 16, "messages": MSG}
    body.update(kw)
    return body


class AnthropicTranslate(unittest.TestCase):
    def err(self, body) -> WireError:
        with self.assertRaises(WireError) as cm:
            build_anthropic(body)
        return cm.exception

    def test_model_map_and_aliases(self):
        self.assertEqual(mp.MODELS["claude-opus-5"], "opus")
        self.assertEqual(mp.MODELS["claude-sonnet-5"], "sonnet")
        self.assertEqual(mp.MODELS["claude-haiku-4-5"], "haiku")
        self.assertEqual(mp.MODELS["claude-fable-5-1"], "fable")
        self.assertEqual(mp.MODELS["nvidia/llama-3.3-nemotron-super-49b-v1"], "sonnet")
        self.assertEqual(mp.MODELS["nvidia/llama-3.1-nemotron-nano-vl-8b-v1"], "haiku")
        e = self.err(areq(model="claude-opus-4-8"))
        self.assertEqual((e.status, e.type), (404, "not_found_error"))

    def test_alias_env(self):
        old = os.environ.get("MOCK_MODEL_ALIASES")
        os.environ["MOCK_MODEL_ALIASES"] = '{"my/nim": "opus"}'
        try:
            m = mp.model_map()
            self.assertEqual(m["my/nim"], "opus")
            self.assertNotIn("nvidia/llama-3.3-nemotron-super-49b-v1", m)   # replaces the default list
            self.assertEqual(m["claude-opus-5"], "opus")
        finally:
            if old is None:
                os.environ.pop("MOCK_MODEL_ALIASES")
            else:
                os.environ["MOCK_MODEL_ALIASES"] = old

    def test_refusals(self):
        cases = {
            "tools": areq(tools=[{"name": "x", "input_schema": {"type": "object"}}]),
            "stream": areq(stream=True),
            "no max_tokens": {"model": "claude-haiku-4-5", "messages": MSG},
            "empty messages": areq(messages=[]),
            "extra field": areq(bogus=1),
            "prefill": areq(messages=MSG + [{"role": "assistant", "content": "ok"}]),
            "document": areq(messages=[{"role": "user", "content": [{"type": "document", "source": {}}]}]),
            "audio": areq(messages=[{"role": "user", "content": [{"type": "audio"}]}]),
            "tool_result": areq(messages=[{"role": "user", "content": [{"type": "tool_result"}]}]),
            "url image": areq(messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "url", "url": "https://x/y.png"}}]}]),
            "bad media type": areq(messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/tiff", "data": PNG}}]}]),
            "bad base64": areq(messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "@@@"}}]}]),
            "image in an assistant turn": areq(messages=[
                {"role": "user", "content": "a"},
                {"role": "assistant", "content": [{"type": "image", "source": {
                    "type": "base64", "media_type": "image/png", "data": PNG}}]},
                {"role": "user", "content": "b"}]),
            "bad format": areq(output_config={"format": {"type": "text"}}),
        }
        for name, body in cases.items():
            with self.subTest(name):
                e = self.err(body)
                self.assertEqual((e.status, e.type), (400, "invalid_request_error"), e.message)

    def test_non_utf8_is_400(self):
        with self.assertRaises(WireError) as cm:
            parse_body(b'{"model": "\xff\xfe"}')
        self.assertEqual(cm.exception.status, 400)

    def test_system_blocks_cache_control_dropped(self):
        self.assertEqual(build_anthropic(areq(system="be brief")).system, "be brief")
        call = build_anthropic(areq(system=[{"type": "text", "text": "A", "cache_control": {"type": "ephemeral"}},
                                            {"type": "text", "text": "B"}]))
        self.assertEqual(call.system, "A\n\nB")

    def test_ignored_params_accepted(self):
        call = build_anthropic(areq(temperature=0.2, stop_sequences=["x"], thinking={"type": "adaptive"},
                                    cache_control={"type": "ephemeral"}, output_config={"effort": "low"}))
        self.assertEqual(call.effort, "low")
        self.assertEqual(call.ignored, ["cache_control", "stop_sequences", "temperature", "thinking"])

    def test_images_collected(self):
        call = build_anthropic(areq(messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PNG}},
            {"type": "text", "text": "Task: transcribe"}]}]))
        self.assertEqual(call.images, [("image/png", PNG)])
        self.assertEqual(call.turns, [("user", "Task: transcribe")])

    def test_flatten_multi_turn(self):
        call = build_anthropic(areq(messages=[{"role": "user", "content": "one"},
                                              {"role": "assistant", "content": [{"type": "text", "text": "two"}]},
                                              {"role": "user", "content": "three"}]))
        prompt = call.claude(type("C", (), {"max_turns": 4})()).prompt
        self.assertIn("<user>\none\n</user>", prompt)
        self.assertIn("<assistant>\ntwo\n</assistant>", prompt)
        self.assertTrue(prompt.rstrip().endswith("<user>\nthree\n</user>"))

    def test_usage_never_estimated(self):
        self.assertEqual(usage_of({}), (0, 0))
        self.assertEqual(usage_of({"usage": {"input_tokens": 3, "cache_read_input_tokens": 100,
                                             "output_tokens": 2}}), (103, 2))

    def test_response_shape_and_structured(self):
        call = build_anthropic(areq(output_config={"format": {"type": "json_schema", "schema": {"type": "object"}}}))
        r = to_anthropic({"is_error": False, "subtype": "success", "result": "ignored",
                          "structured_output": {"a": 1}}, call)
        self.assertEqual((r["type"], r["stop_reason"], r["stop_sequence"]), ("message", "end_turn", None))
        self.assertEqual(json.loads(r["content"][0]["text"]), {"a": 1})
        self.assertTrue(r["id"].startswith("msg_"))
        self.assertEqual(r["usage"]["cache_read_input_tokens"], 0)

    def test_envelope_errors(self):
        e = envelope_error({"is_error": True, "result": "Not logged in · Please run /login"})
        self.assertEqual((e.status, e.type), (401, "authentication_error"))
        e = envelope_error({"is_error": True, "result": "overloaded", "api_error_status": 529})
        self.assertEqual((e.status, e.type), (529, "overloaded_error"))
        self.assertEqual(e.openai().status, 503)
        self.assertEqual(e.openai().body["error"]["code"], "overloaded")
        self.assertIsNone(envelope_error({"is_error": False, "subtype": "success"}))


class OpenAITranslate(unittest.TestCase):
    def err(self, body) -> WireError:
        with self.assertRaises(WireError) as cm:
            build_openai(body)
        return cm.exception

    def test_nim_shape(self):
        schema = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"],
                  "additionalProperties": False}
        call = build_openai({
            "model": "nvidia/llama-3.3-nemotron-super-49b-v1", "max_tokens": 16000,
            "messages": [{"role": "system", "content": "SYS"},
                         {"role": "user", "content": [
                             {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG}"}},
                             {"type": "text", "text": "Task: extract"}]}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "extract", "schema": schema,
                                                                        "strict": True}},
            "chat_template_kwargs": {"enable_thinking": False}, "temperature": 0})
        self.assertEqual(call.alias, "sonnet")
        self.assertEqual(call.system, "SYS")
        self.assertEqual(call.schema, schema)
        self.assertEqual(call.images, [("image/png", PNG)])
        self.assertEqual(call.ignored, ["chat_template_kwargs", "temperature"])
        self.assertIsNone(call.effort)

    def test_max_tokens_optional(self):
        self.assertEqual(build_openai({"model": "claude-sonnet-5", "messages": MSG}).alias, "sonnet")

    def test_refusals(self):
        cases = {
            "tools": oreq(tools=[{"type": "function", "function": {"name": "f"}}]),
            "functions": oreq(functions=[{"name": "f"}]),
            "stream": oreq(stream=True),
            "n": oreq(n=2),
            "logprobs": oreq(logprobs=True),
            "response_format text": oreq(response_format={"type": "text"}),
            "response_format json_object": oreq(response_format={"type": "json_object"}),
            "no schema": oreq(response_format={"type": "json_schema", "json_schema": {"name": "x"}}),
            "no messages": {"model": "claude-haiku-4-5"},
            "prefill": oreq(messages=MSG + [{"role": "assistant", "content": "ok"}]),
            "tool message": oreq(messages=MSG + [{"role": "tool", "content": "x", "tool_call_id": "1"}]),
            "late system": oreq(messages=MSG + [{"role": "system", "content": "x"}, {"role": "user", "content": "y"}]),
            "remote image": oreq(messages=[{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "https://x/y.png"}}]}]),
            "audio part": oreq(messages=[{"role": "user", "content": [{"type": "input_audio"}]}]),
            "file part": oreq(messages=[{"role": "user", "content": [{"type": "file"}]}]),
        }
        for name, body in cases.items():
            with self.subTest(name):
                e = self.err(body)
                self.assertEqual(e.status, 400, e.message)
                self.assertEqual(e.openai().body["error"]["type"], "invalid_request_error")

    def test_unknown_model_404_shape(self):
        e = self.err(oreq(model="gpt-4o"))
        body = e.openai().body
        self.assertEqual((e.status, body["error"]["code"]), (404, "model_not_found"))
        self.assertEqual(set(body["error"]), {"message", "type", "param", "code"})

    def test_response_shape(self):
        call = build_openai(oreq())
        r = to_openai({"is_error": False, "subtype": "success", "result": "hello",
                       "usage": {"input_tokens": 4, "output_tokens": 2}}, call)
        self.assertEqual(r["object"], "chat.completion")
        self.assertEqual(r["model"], "claude-haiku-4-5")
        self.assertEqual(r["choices"][0]["message"], {"role": "assistant", "content": "hello", "refusal": None})
        self.assertEqual(r["choices"][0]["finish_reason"], "stop")
        self.assertEqual(r["usage"], {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6})


class ModelServer(unittest.TestCase):
    def test_round_trip_and_privacy_of_system(self):
        s = Server(MOCK_FAKES="model")
        try:
            st, r = s.post("/v1/messages", areq(system=[{"type": "text", "text": "SYS-SENTINEL",
                                                         "cache_control": {"type": "ephemeral"}}]))
            self.assertEqual(st, 200, r)
            self.assertEqual(json.loads(r["content"][0]["text"]),
                             {"system": "SYS-SENTINEL", "prompt": "hi", "model": "haiku", "images": 0})
            self.assertEqual(r["usage"], {"input_tokens": 23, "output_tokens": 3,
                                          "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0})
            (run,) = s.runs()
            argv = " ".join(run["argv"])
            self.assertNotIn("SYS-SENTINEL", argv, "system text must not be on argv")
            self.assertNotIn("--resume", argv)
            for f in ("--no-session-persistence", "--setting-sources", "--strict-mcp-config",
                      "--disable-slash-commands", "--output-format json"):
                self.assertIn(f, argv)
            self.assertEqual(run["argv"][run["argv"].index("--tools") + 1], "")
            self.assertEqual(run["cwd_entries"], [], "claude runs in an empty directory")
        finally:
            s.close()

    def test_child_env_drops_base_url(self):
        s = Server(MOCK_FAKES="model", ANTHROPIC_BASE_URL="http://127.0.0.1:1")
        try:
            self.assertEqual(s.post("/v1/messages", areq())[0], 200)
            self.assertIsNone(s.runs()[0]["env_base_url"])
        finally:
            s.close()

    def test_structured_output_both_wires(self):
        s = Server(MOCK_FAKES="model")
        try:
            schema = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"],
                      "additionalProperties": False}
            st, r = s.post("/v1/messages", areq(output_config={"format": {"type": "json_schema", "schema": schema},
                                                               "effort": "high"}))
            self.assertEqual(st, 200, r)
            self.assertEqual(json.loads(r["content"][0]["text"]), {"a": "x"})
            st, r = s.post("/v1/chat/completions", oreq(response_format={
                "type": "json_schema", "json_schema": {"name": "extract", "schema": schema, "strict": True}}))
            self.assertEqual(st, 200, r)
            self.assertEqual(json.loads(r["choices"][0]["message"]["content"]), {"a": "x"})
            runs = sorted(s.runs(), key=lambda x: x["start"])
            self.assertEqual(json.loads(runs[0]["argv"][runs[0]["argv"].index("--json-schema") + 1]), schema)
            self.assertEqual(runs[0]["argv"][runs[0]["argv"].index("--effort") + 1], "high")
            self.assertNotIn("--effort", runs[1]["argv"])
        finally:
            s.close()

    def test_openai_round_trip_with_nim_id_and_system(self):
        s = Server(MOCK_FAKES="model")
        try:
            st, r = s.post("/v1/chat/completions", {
                "model": "nvidia/llama-3.3-nemotron-super-49b-v1",
                "messages": [{"role": "system", "content": "SYS"}, {"role": "user", "content": "hi"}]})
            self.assertEqual(st, 200, r)
            self.assertEqual(r["model"], "nvidia/llama-3.3-nemotron-super-49b-v1")
            self.assertEqual(json.loads(r["choices"][0]["message"]["content"]),
                             {"system": "SYS", "prompt": "hi", "model": "sonnet", "images": 0})
            self.assertEqual(r["usage"]["prompt_tokens"], 23)
        finally:
            s.close()

    def test_images_go_through_stream_json(self):
        s = Server(MOCK_FAKES="model")
        try:
            st, r = s.post("/v1/messages", areq(messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PNG}},
                {"type": "text", "text": "Task: transcribe"}]}]))
            self.assertEqual(st, 200, r)
            self.assertEqual(json.loads(r["content"][0]["text"])["images"], 1)
            st, r = s.post("/v1/chat/completions", oreq(messages=[{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG}"}},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG}"}},
                {"type": "text", "text": "Task: transcribe"}]}]))
            self.assertEqual(st, 200, r)
            self.assertEqual(json.loads(r["choices"][0]["message"]["content"])["images"], 2)
            for run in s.runs():
                argv = " ".join(run["argv"])
                self.assertIn("--input-format stream-json", argv)
                self.assertIn("--output-format stream-json", argv)
                self.assertNotIn(PNG[:40], argv, "image bytes never on argv")
        finally:
            s.close()

    def test_models_list_both_shapes(self):
        s = Server(MOCK_FAKES="model")
        try:
            st, r = s.get("/v1/models", headers={"anthropic-version": "2023-06-01"})
            self.assertEqual(st, 200)
            self.assertIn("claude-opus-5", [m["id"] for m in r["data"]])
            self.assertEqual(r["data"][0]["type"], "model")
            st, r = s.get("/v1/models")
            self.assertEqual((st, r["object"]), (200, "list"))
            self.assertIn("nvidia/llama-3.3-nemotron-super-49b-v1", [m["id"] for m in r["data"]])
        finally:
            s.close()

    def test_concurrency_cap(self):
        s = Server(MOCK_FAKES="model", MOCK_CONCURRENCY=3, FAKE_CLAUDE_SLEEP=0.4)
        try:
            results = []
            threads = [threading.Thread(target=lambda: results.append(s.post("/v1/messages", areq())[0]))
                       for _ in range(20)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(results, [200] * 20)
            self.assertEqual(s.get("/healthz")[1]["peak"], 3)
            runs = s.runs()
            events = sorted([(r["start"], 1) for r in runs] + [(r["end"], -1) for r in runs])
            live = peak = 0
            for _, d in events:
                live += d
                peak = max(peak, live)
            self.assertLessEqual(peak, 3)
        finally:
            s.close()

    def test_queue_timeout_is_529_and_503(self):
        s = Server(MOCK_FAKES="model", MOCK_CONCURRENCY=1, MOCK_QUEUE_TIMEOUT=0.2, FAKE_CLAUDE_SLEEP=1.5)
        try:
            out = {}
            t = threading.Thread(target=lambda: out.setdefault("first", s.post("/v1/messages", areq())))
            t.start()
            time.sleep(0.5)
            st, r = s.post("/v1/messages", areq())
            self.assertEqual((st, r["error"]["type"]), (529, "overloaded_error"))
            st, r = s.post("/v1/chat/completions", oreq())
            self.assertEqual((st, r["error"]["type"], r["error"]["code"]), (503, "server_error", "overloaded"))
            t.join()
            self.assertEqual(out["first"][0], 200)
        finally:
            s.close()

    def test_timeout_is_504(self):
        s = Server(MOCK_FAKES="model", MOCK_TIMEOUT_MODEL=0.5, FAKE_CLAUDE_SLEEP=10)
        try:
            t0 = time.monotonic()
            st, r = s.post("/v1/messages", areq())
            self.assertEqual((st, r["error"]["type"]), (504, "api_error"))
            st, r = s.post("/v1/chat/completions", oreq())
            self.assertEqual((st, r["error"]["code"]), (504, "timeout"))
            self.assertLess(time.monotonic() - t0, 8)
        finally:
            s.close()

    def test_cli_failures(self):
        s = Server(MOCK_FAKES="model", FAKE_CLAUDE_MODE="unauth")
        try:
            st, r = s.post("/v1/messages", areq())
            self.assertEqual((st, r["type"], r["error"]["type"]), (401, "error", "authentication_error"))
            self.assertIn("Not logged in", r["error"]["message"])
            st, r = s.post("/v1/chat/completions", oreq())
            self.assertEqual((st, r["error"]["code"]), (401, "invalid_api_key"))
        finally:
            s.close()
        s = Server(MOCK_FAKES="model", FAKE_CLAUDE_MODE="garbage")
        try:
            st, r = s.post("/v1/messages", areq())
            self.assertEqual((st, r["error"]["type"]), (500, "api_error"))
        finally:
            s.close()
        s = Server(MOCK_FAKES="model", FAKE_CLAUDE_MODE="struct_missing")
        try:
            st, r = s.post("/v1/messages", areq(output_config={"format": {"type": "json_schema",
                                                                          "schema": {"type": "object"}}}))
            self.assertEqual((st, r["error"]["type"]), (500, "api_error"))
        finally:
            s.close()

    def test_http_level_refusals_spawn_nothing(self):
        s = Server(MOCK_FAKES="model", MOCK_MAX_BODY_BYTES=1000)
        try:
            st, r = s.post("/v1/messages", None, raw=b'{"model": "\xff"}')
            self.assertEqual((st, r["error"]["type"]), (400, "invalid_request_error"))
            st, r = s.post("/v1/messages", areq(messages=[{"role": "user", "content": "x" * 2000}]))
            self.assertEqual((st, r["error"]["type"]), (413, "request_too_large"))
            st, r = s.post("/v1/chat/completions", oreq(messages=[{"role": "user", "content": "x" * 2000}]))
            self.assertEqual((st, r["error"]["code"]), (413, "request_too_large"))
            st, r = s.post("/v1/messages", areq(model="nope"))
            self.assertEqual((st, r["error"]["type"]), (404, "not_found_error"))
            st, r = s.post("/v1/chat/completions", oreq(model="nope"))
            self.assertEqual((st, r["error"]["code"]), (404, "model_not_found"))
            self.assertEqual(s.post("/v1/messages", areq(tools=[]))[0], 400)
            self.assertEqual(s.post("/v1/chat/completions", oreq(stream=True))[0], 400)
            self.assertEqual(s.runs(), [], "no refusal may spawn claude")
        finally:
            s.close()

    def test_no_request_body_on_disk_unless_dump_var_set(self):
        needle = b"CONFIDENTIAL-SENTINEL-7f3a"
        body = areq(messages=[{"role": "user", "content": needle.decode()}])
        s = Server(MOCK_FAKES="model")
        try:
            self.assertEqual(s.post("/v1/messages", body)[0], 200)
            self.assertEqual(s.post("/v1/chat/completions", body)[0], 200)
            self.assertEqual(files_containing(s.scratch, needle), [])
            self.assertEqual(files_containing(s.data, needle), [])
            self.assertNotIn(needle.decode(), s.stderr(), "the log line never carries a body")
        finally:
            s.close()
        with tempfile.TemporaryDirectory() as dump:
            s = Server(MOCK_FAKES="model", MOCK_DUMP_REQUEST_BODIES_TO=dump)
            try:
                self.assertEqual(s.post("/v1/messages", body)[0], 200)
                self.assertTrue(files_containing(Path(dump), needle))
            finally:
                s.close()

    def test_model_contract_offline(self):
        s = Server(MOCK_FAKES="model")
        try:
            for api in ("anthropic", "openai"):
                out = s.run_contract("model_contract.py", "--api", api, "--no-live", "--expect-refusals")
                self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
            self.assertEqual(s.runs(), [], "--no-live spends nothing")
        finally:
            s.close()


if __name__ == "__main__":
    unittest.main()
