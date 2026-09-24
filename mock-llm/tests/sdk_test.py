#!/usr/bin/env python3
"""The official `anthropic` SDK talks to the mock with only the env changed.

    ANTHROPIC_BASE_URL=http://127.0.0.1:8791 ANTHROPIC_API_KEY=dummy \
        uv run --with anthropic python tests/sdk_test.py

The client is constructed exactly as the middleware constructs it —
`anthropic.Anthropic()`, no arguments — so the only thing that differs between
this run and production is the environment. Unset ANTHROPIC_BASE_URL and set a
real key and the same script runs against the real API.
"""

import json
import os
import sys
import time

import anthropic

SCHEMA = {
    "type": "object",
    "properties": {"city": {"type": "string"}, "country": {"type": "string"}},
    "required": ["city", "country"],
    "additionalProperties": False,
}

print(f"anthropic {anthropic.__version__}; ANTHROPIC_BASE_URL={os.environ.get('ANTHROPIC_BASE_URL', '(unset: real API)')}")
client = anthropic.Anthropic()

t0 = time.monotonic()
msg = client.messages.create(
    model="claude-haiku-4-5",
    max_tokens=256,
    system="Answer with facts only.",
    messages=[{"role": "user", "content": "The capital of France, and its country."}],
    output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
)
dt = time.monotonic() - t0
assert msg.type == "message" and msg.role == "assistant", msg
assert msg.content[0].type == "text", msg.content
obj = json.loads(msg.content[0].text)
assert set(obj) == {"city", "country"} and all(isinstance(v, str) for v in obj.values()), obj
print(f"PASS  structured output via SDK in {dt:.1f}s: {obj}  stop_reason={msg.stop_reason}  usage={msg.usage}")

for name, kwargs, exc in [
    ("unknown model -> NotFoundError", {"model": "claude-no-such-model"}, anthropic.NotFoundError),
    ("empty messages -> BadRequestError", {"messages": []}, anthropic.BadRequestError),
]:
    base = {"model": "claude-haiku-4-5", "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]}
    base.update(kwargs)
    try:
        client.messages.create(**base)
    except exc as e:
        print(f"PASS  {name}: {e.status_code} {e.body['error']['type']}")
    else:
        print(f"FAIL  {name}: no error")
        sys.exit(1)
print("SDK test passed")
