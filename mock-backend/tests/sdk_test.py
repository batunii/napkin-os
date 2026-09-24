#!/usr/bin/env python3
"""The official SDKs talk to the model port with only the environment changed.

    # Anthropic wire (the client built exactly as the middleware builds it):
    ANTHROPIC_BASE_URL=http://127.0.0.1:8797 ANTHROPIC_API_KEY=dummy \
        uv run --no-project --with anthropic python mock-backend/tests/sdk_test.py anthropic

    # OpenAI-compatible wire (the NIM shape):
    OPENAI_BASE_URL=http://127.0.0.1:8797/v1 OPENAI_API_KEY=dummy \
        uv run --no-project --with openai python mock-backend/tests/sdk_test.py openai

Unset the base URL and set a real key and the same script runs against the
real API. Makes two tiny haiku calls (live; a fraction of a cent).
"""

import json
import os
import sys
import time

SCHEMA = {
    "type": "object",
    "properties": {"city": {"type": "string"}, "country": {"type": "string"}},
    "required": ["city", "country"],
    "additionalProperties": False,
}
MODEL = os.environ.get("SDK_TEST_MODEL", "claude-haiku-4-5")


def anthropic_wire():
    import anthropic
    print(f"anthropic {anthropic.__version__}; ANTHROPIC_BASE_URL="
          f"{os.environ.get('ANTHROPIC_BASE_URL', '(unset: real API)')}")
    client = anthropic.Anthropic()
    t0 = time.monotonic()
    msg = client.messages.create(
        model=MODEL, max_tokens=256, system="Answer with facts only.",
        messages=[{"role": "user", "content": "The capital of France, and its country."}],
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}})
    assert msg.type == "message" and msg.role == "assistant", msg
    obj = json.loads(msg.content[0].text)
    assert set(obj) == {"city", "country"}, obj
    print(f"PASS  structured output via SDK in {time.monotonic() - t0:.1f}s: {obj}  usage={msg.usage}")
    for name, kwargs, exc in [("unknown model -> NotFoundError", {"model": "claude-no-such-model"},
                               anthropic.NotFoundError),
                              ("empty messages -> BadRequestError", {"messages": []}, anthropic.BadRequestError)]:
        base = {"model": MODEL, "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]}
        base.update(kwargs)
        try:
            client.messages.create(**base)
        except exc as e:
            print(f"PASS  {name}: {e.status_code}")
        else:
            sys.exit(f"FAIL  {name}: no error")


def openai_wire():
    import openai
    print(f"openai {openai.__version__}; OPENAI_BASE_URL={os.environ.get('OPENAI_BASE_URL', '(unset)')}")
    client = openai.OpenAI()
    t0 = time.monotonic()
    r = client.chat.completions.create(
        model=MODEL, max_tokens=256,
        messages=[{"role": "system", "content": "Answer with facts only."},
                  {"role": "user", "content": "The capital of France, and its country."}],
        response_format={"type": "json_schema", "json_schema": {"name": "capital", "schema": SCHEMA,
                                                                "strict": True}})
    obj = json.loads(r.choices[0].message.content)
    assert set(obj) == {"city", "country"} and r.choices[0].finish_reason == "stop", r
    print(f"PASS  structured output via SDK in {time.monotonic() - t0:.1f}s: {obj}  usage={r.usage}")
    try:
        client.chat.completions.create(model="no-such-model", messages=[{"role": "user", "content": "hi"}])
    except openai.NotFoundError as e:
        print(f"PASS  unknown model -> NotFoundError: {e.status_code}")
    else:
        sys.exit("FAIL  unknown model: no error")


if __name__ == "__main__":
    wire = sys.argv[1] if len(sys.argv) > 1 else "anthropic"
    anthropic_wire() if wire == "anthropic" else openai_wire()
    print("SDK test passed")
