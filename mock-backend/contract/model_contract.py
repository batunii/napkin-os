#!/usr/bin/env python3
"""Contract suite for the model port — napkin.model/1 (contract 5, §1, §8.1).

    python3 model_contract.py --base-url http://127.0.0.1:8797 --api anthropic [--model claude-haiku-4-5]
    python3 model_contract.py --base-url http://127.0.0.1:8797/v1 --api openai --model nvidia/llama-3.3-nemotron-super-49b-v1
    python3 model_contract.py --base-url https://api.anthropic.com --api anthropic --api-key "$ANTHROPIC_API_KEY"
    python3 model_contract.py --base-url https://<nim host>/v1 --api openai --api-key "$NIM_KEY" --model <id>

Names no implementation. `--base-url` is what the middleware's
NAPKIN_MODEL_BASE_URL would be: the API root for anthropic (the suite
appends /v1/messages), the root including /v1 for openai (the suite appends
/chat/completions; a root without /v1 is accepted and /v1 added).

  --no-live            skip the checks that make model calls (the rest spend nothing)
  --no-image           skip the image transcription check (an endpoint without vision)
  --expect-refusals    the target refuses tools, stream, a prefill and a document block
                       with a 400 (the mock does; the real APIs accept most of these)

Standard library only.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from suite import Client, Result  # noqa: E402
from png_text import png_text  # noqa: E402

STRICT = {
    "type": "object", "additionalProperties": False, "required": ["colour", "count", "tags", "size", "note"],
    "properties": {
        "colour": {"type": "string", "enum": ["red", "green", "blue"]},
        "count": {"type": "integer"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "size": {"type": "object", "additionalProperties": False, "required": ["w", "h"],
                 "properties": {"w": {"type": "number"}, "h": {"type": "number"}}},
        "note": {"type": ["string", "null"]},
    },
}
TRANSCRIBE = {"type": "object", "additionalProperties": False, "required": ["text", "visuals"],
              "properties": {"text": {"type": "string"}, "visuals": {"type": "array", "items": {"type": "string"}}}}


def validate(value, schema, path="$") -> list[str]:
    """The subset of JSON Schema middleware schemas use: type (incl. lists and
    null), enum, properties, required, additionalProperties: false, items."""
    errs = []
    t = schema.get("type")
    types = t if isinstance(t, list) else ([t] if t else [])

    def is_type(v, name):
        return {"object": isinstance(v, dict), "array": isinstance(v, list), "string": isinstance(v, str),
                "boolean": isinstance(v, bool), "null": v is None,
                "integer": isinstance(v, int) and not isinstance(v, bool),
                "number": isinstance(v, (int, float)) and not isinstance(v, bool)}[name]

    if types and not any(is_type(value, n) for n in types):
        return [f"{path}: not {t}"]
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path}: not in enum")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        errs += [f"{path}: missing {k}" for k in schema.get("required", []) if k not in value]
        if schema.get("additionalProperties") is False:
            errs += [f"{path}: extra {k}" for k in value if k not in props]
        for k, v in value.items():
            if k in props:
                errs += validate(v, props[k], f"{path}.{k}")
    if isinstance(value, list) and "items" in schema:
        for i, v in enumerate(value):
            errs += validate(v, schema["items"], f"{path}[{i}]")
    return errs


class Wire:
    def __init__(self, api: str, base: str, key: str, timeout: float):
        self.api = api
        base = base.rstrip("/")
        if api == "anthropic":
            self.path = "/v1/messages"
            self.client = Client(base, None, timeout)
            self.headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        else:
            if not base.endswith("/v1"):
                base += "/v1"
            self.path = "/chat/completions"
            self.client = Client(base, None, timeout)
            self.headers = {"Authorization": f"Bearer {key}"}

    def post(self, body=None, raw=None):
        st, payload, _, dt = self.client.call("POST", self.path, body, raw=raw, headers=self.headers)
        return st, payload, dt

    # -- request builders in the wire's shape ---------------------------------
    def body(self, model, text, system=None, schema=None, history=None, image=None, max_tokens=512):
        if self.api == "anthropic":
            content = text
            if image:
                content = [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": image}},
                           {"type": "text", "text": text}]
            msgs = list(history or []) + [{"role": "user", "content": content}]
            b = {"model": model, "max_tokens": max_tokens, "messages": msgs}
            if system:
                b["system"] = system
            if schema:
                b["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
            return b
        content = text
        if image:
            content = [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image}"}},
                       {"type": "text", "text": text}]
        msgs = ([{"role": "system", "content": system}] if system else []) + list(history or []) + \
            [{"role": "user", "content": content}]
        b = {"model": model, "max_tokens": max_tokens, "messages": msgs}
        if schema:
            b["response_format"] = {"type": "json_schema", "json_schema": {"name": "contract", "schema": schema,
                                                                          "strict": True}}
        return b

    def text_of(self, r) -> str:
        if self.api == "anthropic":
            return next(b["text"] for b in r["content"] if b.get("type") == "text")
        return r["choices"][0]["message"]["content"]

    def shape_ok(self, r, model) -> list[str]:
        errs = []
        if not isinstance(r, dict):
            return ["body is not an object"]
        if "error" in r:
            errs.append("a 200 carries an error key")
        if r.get("model") != model:
            errs.append(f"model {r.get('model')!r} does not echo {model!r}")
        if self.api == "anthropic":
            if not (str(r.get("id", "")).startswith("msg_") and r.get("type") == "message"
                    and r.get("role") == "assistant"):
                errs.append("id/type/role")
            content = r.get("content")
            if not (isinstance(content, list) and any(b.get("type") == "text" and isinstance(b.get("text"), str)
                                                     for b in content)):
                errs.append("no text block")
            if r.get("stop_reason") not in ("end_turn", "max_tokens", "stop_sequence", "refusal"):
                errs.append(f"stop_reason {r.get('stop_reason')!r}")
            u = r.get("usage") or {}
            for k in ("input_tokens", "output_tokens"):
                if not (isinstance(u.get(k), int) and u[k] >= 0):
                    errs.append(f"usage.{k}")
        else:
            if r.get("object") != "chat.completion":
                errs.append("object")
            ch = r.get("choices")
            if not (isinstance(ch, list) and ch and isinstance(ch[0].get("message"), dict)
                    and isinstance(ch[0]["message"].get("content"), str)):
                errs.append("choices[0].message.content")
            elif ch[0].get("finish_reason") not in ("stop", "length", "content_filter"):
                errs.append(f"finish_reason {ch[0].get('finish_reason')!r}")
            u = r.get("usage") or {}
            for k in ("prompt_tokens", "completion_tokens"):
                if not (isinstance(u.get(k), int) and u[k] >= 0):
                    errs.append(f"usage.{k}")
        return errs

    def error_ok(self, r) -> bool:
        if self.api == "anthropic":
            return (isinstance(r, dict) and r.get("type") == "error" and isinstance(r.get("error"), dict)
                    and isinstance(r["error"].get("type"), str) and isinstance(r["error"].get("message"), str))
        return (isinstance(r, dict) and isinstance(r.get("error"), dict)
                and {"message", "type", "param", "code"} <= set(r["error"]))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--api", choices=["anthropic", "openai"], required=True)
    ap.add_argument("--model", default="claude-haiku-4-5")
    ap.add_argument("--api-key", "--token", dest="key", default="dummy")
    ap.add_argument("--no-live", action="store_true")
    ap.add_argument("--no-image", action="store_true")
    ap.add_argument("--expect-refusals", action="store_true")
    ap.add_argument("--timeout", type=float, default=300)
    a = ap.parse_args()
    w = Wire(a.api, a.base_url, a.key, a.timeout)
    r = Result()
    m = a.model

    r.section(f"{a.api} wire: requests the port never sends well-formed")
    st, body, _ = w.post({"model": "no-such-model-id", "max_tokens": 16, "messages": [{"role": "user",
                                                                                       "content": "hi"}]})
    r.check(st == 404 and w.error_ok(body), f"unknown model -> {st} in the wire's error shape")
    if a.api == "anthropic":
        st, body, _ = w.post({"model": m, "messages": [{"role": "user", "content": "hi"}]})
        r.check(st == 400 and w.error_ok(body), f"missing max_tokens -> {st}")
    st, body, _ = w.post({"model": m, "max_tokens": 16})
    r.check(st == 400 and w.error_ok(body), f"missing messages -> {st}")
    st, body, _ = w.post(raw=b"{not json")
    r.check(st == 400 and w.error_ok(body), f"body not JSON -> {st}")

    if a.expect_refusals:
        r.section("refusals (--expect-refusals)")
        user = [{"role": "user", "content": "hi"}]
        if a.api == "anthropic":
            cases = {
                "tools": {"model": m, "max_tokens": 16, "messages": user,
                          "tools": [{"name": "t", "description": "d", "input_schema": {"type": "object"}}]},
                "stream: true": {"model": m, "max_tokens": 16, "messages": user, "stream": True},
                "assistant prefill": {"model": m, "max_tokens": 16,
                                      "messages": user + [{"role": "assistant", "content": "{"}]},
                "document block": {"model": m, "max_tokens": 16, "messages": [{"role": "user", "content": [
                    {"type": "document", "source": {"type": "text", "media_type": "text/plain", "data": "x"}},
                    {"type": "text", "text": "hi"}]}]},
            }
        else:
            cases = {
                "tools": {"model": m, "messages": user, "tools": [{"type": "function", "function": {"name": "t"}}]},
                "stream: true": {"model": m, "messages": user, "stream": True},
                "assistant prefill": {"model": m, "messages": user + [{"role": "assistant", "content": "{"}]},
                "file part": {"model": m, "messages": [{"role": "user", "content": [
                    {"type": "file", "file": {"file_data": "x"}}, {"type": "text", "text": "hi"}]}]},
                "response_format json_object": {"model": m, "messages": user,
                                                "response_format": {"type": "json_object"}},
            }
        for name, b in cases.items():
            st, body, _ = w.post(b)
            r.check(st == 400 and w.error_ok(body), f"{name} -> {st}")

    if a.no_live:
        return r.report(f"model contract ({a.api}) against {a.base_url} [no live]")

    r.section("live calls")
    st, body, dt = w.post(w.body(m, "Reply with the single word: ok", max_tokens=64))
    if r.check(st == 200, f"plain text call -> {st} in {dt:.1f}s" + ("" if st == 200 else f" {body}")):
        errs = w.shape_ok(body, m)
        r.check(not errs, "the wire's response shape, usage integers >= 0, model echoed" +
                (f" ({'; '.join(errs)})" if errs else ""))

    st, body, dt = w.post(w.body(m, "Pick a colour from red, green or blue, a count from 1 to 5, two short tags, "
                                    "a size with width and height in cm, and a note or null.",
                                 system="Answer briefly.", schema=STRICT))
    if r.check(st == 200, f"structured call (strict schema: nested object, enum, array, nullable) -> {st} "
                          f"in {dt:.1f}s"):
        try:
            obj = json.loads(w.text_of(body))
            errs = validate(obj, STRICT)
        except (ValueError, KeyError, StopIteration, TypeError) as e:
            obj, errs = None, [f"not JSON: {type(e).__name__}"]
        r.check(not errs, f"the text parses and validates: {obj}" + (f" ({'; '.join(errs)})" if errs else ""))

    hist = [{"role": "user", "content": "Task: extract\n\nGive the city."},
            {"role": "assistant", "content": '{"city": 7}'}]
    st, body, dt = w.post(w.body(m, "That output does not validate against the schema: $.city: not string. Return "
                                    "the corrected JSON only.", history=hist,
                                 schema={"type": "object", "additionalProperties": False, "required": ["city"],
                                         "properties": {"city": {"type": "string"}}}))
    ok = st == 200
    if ok:
        try:
            ok = isinstance(json.loads(w.text_of(body)).get("city"), str)
        except (ValueError, KeyError, StopIteration, AttributeError, TypeError):
            ok = False
    r.check(ok, f"the retry shape (user, assistant, user) is accepted and answered -> {st} in {dt:.1f}s")

    if a.no_image:
        r.skip("image transcription (--no-image)")
    else:
        img = base64.b64encode(png_text("NAPKIN 42")).decode()
        st, body, dt = w.post(w.body(m, "Task: transcribe\n\nTranscribe every word and number in the image, "
                                        "verbatim, in reading order.", image=img, schema=TRANSCRIBE))
        text = ""
        if st == 200:
            try:
                text = json.loads(w.text_of(body)).get("text", "")
            except (ValueError, KeyError, StopIteration, AttributeError, TypeError):
                text = ""
        r.check(st == 200 and "42" in text, f"image transcription reads '42' -> {st} {text!r} in {dt:.1f}s")

    return r.report(f"model contract ({a.api}) against {a.base_url}")


if __name__ == "__main__":
    sys.exit(main())
