#!/usr/bin/env python3
"""Shape-contract test for a Messages API endpoint: the mock, or the real API.

    python3 tests/contract_test.py --base-url http://127.0.0.1:8791
    python3 tests/contract_test.py --base-url https://api.anthropic.com --api-key "$ANTHROPIC_API_KEY"

Stdlib only. It asserts the same response keys and types from any base URL; it
is the one test that keeps the mock honest. The success cases make real model
calls (tiny prompts, claude-haiku-4-5). Cases the real API accepts but the mock
refuses (tools, stream) run only against the mock: pass --real to skip them
(it is implied when the base URL is api.anthropic.com).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

HAIKU = "claude-haiku-4-5"
SCHEMA = {
    "type": "object",
    "properties": {
        "colour": {"type": "string"},
        "count": {"type": "integer"},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["colour", "count", "tags"],
    "additionalProperties": False,
}


def check_schema(value, schema, path="$"):
    """The subset of JSON Schema the middleware uses: type, properties,
    required, additionalProperties: false, items, enum."""
    t = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}
    if t == "integer":
        assert isinstance(value, int) and not isinstance(value, bool), f"{path}: not an integer"
    elif t == "number":
        assert isinstance(value, (int, float)) and not isinstance(value, bool), f"{path}: not a number"
    elif t:
        assert isinstance(value, types[t]), f"{path}: not {t}"
    if "enum" in schema:
        assert value in schema["enum"], f"{path}: not in enum"
    if t == "object":
        props = schema.get("properties", {})
        for k in schema.get("required", []):
            assert k in value, f"{path}: missing {k}"
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(props)
            assert not extra, f"{path}: extra keys {extra}"
        for k, v in value.items():
            if k in props:
                check_schema(v, props[k], f"{path}.{k}")
    if t == "array" and "items" in schema:
        for i, v in enumerate(value):
            check_schema(v, schema["items"], f"{path}[{i}]")


class Client:
    def __init__(self, base, key, timeout):
        self.base, self.key, self.timeout = base.rstrip("/"), key, timeout

    def post(self, body=None, raw=None):
        data = raw if raw is not None else json.dumps(body).encode()
        r = urllib.request.Request(self.base + "/v1/messages", data=data, method="POST", headers={
            "content-type": "application/json", "x-api-key": self.key,
            "anthropic-version": "2023-06-01"})
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(r, timeout=self.timeout) as resp:
                status, payload = resp.status, resp.read()
        except urllib.error.HTTPError as e:
            status, payload = e.code, e.read()
        except (ConnectionError, urllib.error.URLError) as e:
            # A server may answer 413 and close before the upload finishes.
            return None, {"_transport_error": str(e)}, time.monotonic() - t0
        return status, json.loads(payload), time.monotonic() - t0


def assert_message(r, model):
    assert set(r) >= {"id", "type", "role", "model", "content", "stop_reason", "stop_sequence", "usage"}, set(r)
    assert isinstance(r["id"], str) and r["id"].startswith("msg_"), r["id"]
    assert r["type"] == "message" and r["role"] == "assistant"
    assert r["model"].startswith(model), r["model"]
    assert isinstance(r["content"], list) and r["content"], r["content"]
    texts = [b for b in r["content"] if b.get("type") == "text"]
    assert texts and isinstance(texts[0]["text"], str) and texts[0]["text"], r["content"]
    assert r["stop_reason"] in ("end_turn", "max_tokens", "stop_sequence", "refusal"), r["stop_reason"]
    assert r["stop_sequence"] is None or isinstance(r["stop_sequence"], str)
    u = r["usage"]
    for k in ("input_tokens", "output_tokens"):
        assert isinstance(u.get(k), int) and u[k] >= 0, (k, u)
    for k in ("cache_creation_input_tokens", "cache_read_input_tokens"):
        assert u.get(k) is None or isinstance(u[k], int), (k, u)
    return texts[0]["text"]


def assert_error(status, r, want_status, want_type):
    assert status == want_status, f"status {status} != {want_status}: {r}"
    assert r.get("type") == "error", r
    assert r["error"]["type"] == want_type, r
    assert isinstance(r["error"]["message"], str) and r["error"]["message"], r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--api-key", default="dummy")
    ap.add_argument("--real", action="store_true", help="target is the real API: skip mock-only refusals")
    ap.add_argument("--timeout", type=float, default=300)
    ap.add_argument("--oversize-bytes", type=int, default=33 * 1024 * 1024,
                    help="size of the body for the 413 case (the real API's cap is 32 MB)")
    a = ap.parse_args()
    real = a.real or "api.anthropic.com" in a.base_url
    c = Client(a.base_url, a.api_key, a.timeout)
    msg = [{"role": "user", "content": "Reply with the single word: ok"}]
    results = []

    def case(name, fn):
        try:
            extra = fn()
            results.append((name, True, extra or ""))
        except AssertionError as e:
            results.append((name, False, str(e)[:300]))
        print(f"{'PASS' if results[-1][1] else 'FAIL'}  {name}  {results[-1][2]}", flush=True)

    def plain():
        s, r, dt = c.post({"model": HAIKU, "max_tokens": 64, "messages": msg})
        assert s == 200, (s, r)
        text = assert_message(r, HAIKU)
        if not real:
            assert r["usage"]["cache_read_input_tokens"] == 0, r["usage"]
        return f"{dt:.1f}s  text={text[:40]!r}  usage={r['usage']}"

    def structured():
        s, r, dt = c.post({"model": HAIKU, "max_tokens": 256,
                           "system": [{"type": "text", "text": "Answer briefly.",
                                       "cache_control": {"type": "ephemeral"}}],
                           "messages": [{"role": "user", "content": "Name a colour, a count from 1 to 5, "
                                                                    "and two short tags."}],
                           "output_config": {"format": {"type": "json_schema", "schema": SCHEMA}}})
        assert s == 200, (s, r)
        text = assert_message(r, HAIKU)
        obj = json.loads(text)
        check_schema(obj, SCHEMA)
        return f"{dt:.1f}s  json={obj}"

    def multi_turn():
        s, r, dt = c.post({"model": HAIKU, "max_tokens": 64, "system": "Be terse.",
                           "temperature": 0, "stop_sequences": ["\n\n\n"],
                           "messages": [{"role": "user", "content": "Remember the word: plum."},
                                        {"role": "assistant", "content": "Noted."},
                                        {"role": "user", "content": [{"type": "text",
                                                                      "text": "What word? One word."}]}]})
        assert s == 200, (s, r)
        text = assert_message(r, HAIKU)
        return f"{dt:.1f}s  text={text[:40]!r}"

    def refusal(body, st, ty, raw=None):
        def fn():
            s, r, _ = c.post(body, raw=raw)
            assert_error(s, r, st, ty)
            return r["error"]["message"][:70]
        return fn

    def oversize():
        pad = "x" * a.oversize_bytes
        s, r, _ = c.post({"model": HAIKU, "max_tokens": 16, "messages": [{"role": "user", "content": pad}]})
        if s is None:
            raise AssertionError(f"no HTTP response: {r}")
        assert_error(s, r, 413, "request_too_large")
        return r["error"]["message"][:70]

    case("text response is Messages-shaped", plain)
    case("output_config.format json_schema -> first text block is schema-valid JSON", structured)
    case("system blocks + multi-turn + ignored params", multi_turn)
    case("unknown model -> 404 not_found_error",
         refusal({"model": "claude-no-such-model", "max_tokens": 16, "messages": msg}, 404, "not_found_error"))
    case("missing max_tokens -> 400", refusal({"model": HAIKU, "messages": msg}, 400, "invalid_request_error"))
    case("empty messages -> 400", refusal({"model": HAIKU, "max_tokens": 16, "messages": []},
                                          400, "invalid_request_error"))
    case("non-UTF-8 body -> 400", refusal(None, 400, "invalid_request_error",
                                          raw=b'{"model": "\xff\xfe", "max_tokens": 1, "messages": []}'))
    case(f"body over the cap ({a.oversize_bytes} B) -> 413", oversize)
    if not real:
        case("tools -> 400 (mock only)", refusal(
            {"model": HAIKU, "max_tokens": 16, "messages": msg,
             "tools": [{"name": "t", "description": "d", "input_schema": {"type": "object"}}]},
            400, "invalid_request_error"))
        case("stream: true -> 400 (mock only)", refusal(
            {"model": HAIKU, "max_tokens": 16, "messages": msg, "stream": True}, 400, "invalid_request_error"))

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed against {a.base_url}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
