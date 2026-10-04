"""The model port on every wire (peripherals.md §1), with fake transports."""

import base64
import json
import re
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from napkin.config import Settings
from napkin.model import AnthropicWire, ModelError, ModelPort, OpenAIWire, Usage, build_wire

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["answer", "items", "note"],
          "properties": {"answer": {"type": "string", "enum": ["yes", "no"]},
                         "items": {"type": "array", "items": {"type": "string"}, "maxItems": 2},
                         "note": {"anyOf": [{"type": "string"}, {"type": "null"}]}}}
GOOD = {"answer": "yes", "items": ["a"], "note": None}
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 64).decode()


# ---------------------------------------------------------------------------- Anthropic wire

class FakeSDK:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.messages = self

    def with_options(self, **kw):
        self.opts = kw
        return self

    def create(self, **kw):
        self.calls.append(kw)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        text, stop, usage = r
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=stop, usage=usage)


def U(i=10, o=5, cc=0, cr=0):
    return SimpleNamespace(input_tokens=i, output_tokens=o, cache_creation_input_tokens=cc, cache_read_input_tokens=cr)


def port(client_or_wire, **kw):
    return ModelPort(client_or_wire, "claude-opus-5", 30, vision_model="vision-x", **kw)


def test_anthropic_structured_call_with_images_effort_and_usage():
    sdk = FakeSDK([(json.dumps(GOOD), "end_turn", U(10, 5, 3, 2))])
    u = Usage()
    out = port(sdk).call("transcribe", "sys", {"x": 1}, SCHEMA, usage=u, attribution="t", effort="high",
                         images=[{"media_type": "image/png", "data": PNG}], model="vision-x",
                         headers={"X-Napkin-Handler": "h@1.0", "X-Napkin-Job": "job_1"})
    assert out == GOOD and u.as_dict() == {"input_tokens": 15, "output_tokens": 5}
    kw = sdk.calls[0]
    assert kw["model"] == "vision-x" and kw["system"] == "sys" and sdk.opts["max_retries"] == 1
    assert kw["extra_headers"] == {"X-Napkin-Handler": "h@1.0", "X-Napkin-Job": "job_1"}
    content = kw["messages"][0]["content"]
    assert content[0] == {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PNG}}
    assert content[1]["text"].startswith("Task: transcribe\n\n<input>")
    fmt = kw["output_config"]
    assert fmt["effort"] == "high" and fmt["format"]["type"] == "json_schema"
    assert "maxItems" not in json.dumps(fmt["format"]["schema"])  # stripped on the wire, enforced locally
    for banned in ("temperature", "top_p", "top_k", "tools", "stream", "thinking", "stop_sequences"):
        assert banned not in kw


def test_anthropic_invalid_output_is_retried_once_with_the_error_fed_back():
    bad = {"answer": "maybe", "items": ["a", "b", "c"], "note": None}
    sdk = FakeSDK([(json.dumps(bad), "end_turn", U()), (json.dumps(GOOD), "end_turn", U())])
    assert port(sdk).call("p", "s", {}, SCHEMA, usage=Usage(), attribution="t") == GOOD
    turns = sdk.calls[1]["messages"]
    assert [t["role"] for t in turns] == ["user", "assistant", "user"]  # never an assistant prefill
    assert "does not validate" in turns[2]["content"]


@pytest.mark.parametrize("stop,kind", [("max_tokens", "truncated"), ("refusal", "refusal")])
def test_anthropic_truncation_and_refusal_are_not_retried(stop, kind):
    sdk = FakeSDK([("{", stop, U())])
    with pytest.raises(ModelError) as e:
        port(sdk).call("p", "s", {}, SCHEMA, usage=Usage(), attribution="t")
    assert e.value.kind == kind and len(sdk.calls) == 1


def test_anthropic_status_errors_map_to_kinds():
    import anthropic
    req = httpx.Request("POST", "http://x/v1/messages")
    err = anthropic.RateLimitError("slow down", response=httpx.Response(429, request=req), body=None)
    with pytest.raises(ModelError) as e:
        port(FakeSDK([err])).call("p", "s", {}, SCHEMA, usage=Usage(), attribution="t")
    assert e.value.kind == "rate_limited"
    err = anthropic.APITimeoutError(request=req)
    with pytest.raises(ModelError) as e:
        port(FakeSDK([err])).call("p", "s", {}, SCHEMA, usage=Usage(), attribution="t")
    assert e.value.kind == "timeout"


def test_images_are_checked_before_sending():
    sdk = FakeSDK([])
    with pytest.raises(ModelError) as e:
        port(sdk).call("p", "s", {}, SCHEMA, usage=Usage(), attribution="t",
                       images=[{"media_type": "image/tiff", "data": PNG}])
    assert e.value.kind == "invalid_request" and not sdk.calls


# ---------------------------------------------------------------------------- OpenAI wire

class Endpoint:
    """A fake /v1/chat/completions: answers each request with the next reply."""

    def __init__(self, replies):
        self.replies, self.requests = list(replies), []

    def __call__(self, request):
        self.requests.append(request)
        status, body, headers = self.replies.pop(0)
        return httpx.Response(status, json=body, headers=headers or {})


def ok(content, finish="stop", usage=None, refusal=None):
    msg = {"role": "assistant", "content": content}
    if refusal is not None:
        msg["refusal"] = refusal
    body = {"id": "x", "model": "m", "choices": [{"index": 0, "message": msg, "finish_reason": finish}]}
    if usage is not False:
        body["usage"] = usage or {"prompt_tokens": 7, "completion_tokens": 3}
    return (200, body, None)


def oa(ep, sleeps=None, extra=None):
    return OpenAIWire("http://nim.test/v1", "key-1", extra or {"chat_template_kwargs": {"enable_thinking": False},
                                                              "model": "never-overrides"},
                      transport=httpx.MockTransport(ep), sleep=(sleeps.append if sleeps is not None else lambda s: None))


def test_openai_request_shape_images_and_think_stripped():
    ep = Endpoint([ok("<think>hmm</think>\n" + json.dumps(GOOD))])
    u = Usage()
    out = port(oa(ep)).call("extract", "the system", {"a": 1}, SCHEMA, usage=u, attribution="t", effort="high",
                            images=[{"media_type": "image/png", "data": PNG}],
                            headers={"X-Napkin-Handler": "h@1.0", "X-Napkin-Job": "job_1"})
    assert out == GOOD and u.as_dict() == {"input_tokens": 7, "output_tokens": 3}
    r = ep.requests[0]
    assert str(r.url) == "http://nim.test/v1/chat/completions" and r.headers["authorization"] == "Bearer key-1"
    assert r.headers["x-napkin-handler"] == "h@1.0" and r.headers["x-napkin-job"] == "job_1"
    body = json.loads(r.content)
    assert body["model"] == "claude-opus-5"  # extra_body never overrides a key the port sets
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert [m["role"] for m in body["messages"]] == ["system", "user"] and body["messages"][0]["content"] == "the system"
    parts = body["messages"][1]["content"]
    assert parts[0] == {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG}"}}
    assert parts[1]["type"] == "text" and parts[1]["text"].startswith("Task: extract")
    rf = body["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["name"] == "extract" and rf["json_schema"]["strict"]
    for banned in ("temperature", "top_p", "tools", "stream", "effort", "n", "logprobs"):
        assert banned not in body


def test_openai_400_without_json_schema_support_is_never_retried_without_it():
    ep = Endpoint([(400, {"error": {"message": "response_format json_schema is not supported by this model",
                                    "type": "invalid_request_error"}}, None)])
    with pytest.raises(ModelError) as e:
        port(oa(ep)).call("p", "s", {}, SCHEMA, usage=Usage(), attribution="t")
    assert e.value.kind == "unsupported" and len(ep.requests) == 1


def test_openai_rate_limit_and_overload_retry_once():
    sleeps = []
    ep = Endpoint([(429, {"error": {"message": "slow"}}, {"retry-after": "99"}), ok(json.dumps(GOOD))])
    assert port(oa(ep, sleeps)).call("p", "s", {}, SCHEMA, usage=Usage(), attribution="t") == GOOD
    assert sleeps == [30.0] and len(ep.requests) == 2  # Retry-After honoured, capped at 30 s
    ep = Endpoint([(503, {"error": {"message": "busy"}}, None), (503, {"error": {"message": "busy"}}, None)])
    with pytest.raises(ModelError) as e:
        port(oa(ep)).call("p", "s", {}, SCHEMA, usage=Usage(), attribution="t")
    assert e.value.kind == "overloaded" and len(ep.requests) == 2
    ep = Endpoint([(401, {"error": {"message": "no"}}, None)])
    with pytest.raises(ModelError) as e:
        port(oa(ep)).call("p", "s", {}, SCHEMA, usage=Usage(), attribution="t")
    assert e.value.kind == "auth" and len(ep.requests) == 1


@pytest.mark.parametrize("reply,kind", [(ok("{", finish="length"), "truncated"),
                                        (ok(None, finish="stop", refusal="I can't"), "refusal"),
                                        (ok("{}", finish="content_filter"), "refusal")])
def test_openai_truncation_and_refusal(reply, kind):
    ep = Endpoint([reply])
    with pytest.raises(ModelError) as e:
        port(oa(ep)).call("p", "s", {}, SCHEMA, usage=Usage(), attribution="t")
    assert e.value.kind == kind and len(ep.requests) == 1


def test_openai_invalid_output_retry_turn_and_missing_usage_counts_zero():
    ep = Endpoint([ok('{"answer": "maybe"}', usage=False), ok(json.dumps(GOOD), usage=False)])
    u = Usage()
    assert port(oa(ep)).call("p", "s", {}, SCHEMA, usage=u, attribution="t") == GOOD
    msgs = json.loads(ep.requests[1].content)["messages"]
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert u.as_dict() == {"input_tokens": 0, "output_tokens": 0} and u.calls == 2


def test_wire_selection_by_config():
    assert build_wire(Settings(model_api="anthropic"), client=FakeSDK([])).api == "anthropic"
    w = build_wire(Settings(model_api="openai", model_base_url="http://nim.test/v1", model_api_key="k"))
    assert isinstance(w, OpenAIWire) and w.url == "http://nim.test/v1/chat/completions"
    with pytest.raises(ValueError):
        build_wire(Settings(model_api="openai"))  # the base URL is required
    with pytest.raises(ValueError):
        build_wire(Settings(model_api="gemini"))


# ---------------------------------------------------------------------------- Bedrock wire

def _mantle(handler, **auth):
    """A real AnthropicBedrockMantle client whose HTTP goes to `handler`."""
    import httpx2
    from anthropic import AnthropicBedrockMantle
    return AnthropicBedrockMantle(aws_region="eu-west-1", max_retries=1,
                                  http_client=httpx2.Client(transport=httpx2.MockTransport(handler)), **auth)


def _message(text):
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "anthropic.claude-opus-5",
            "content": [{"type": "text", "text": text}], "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 7, "output_tokens": 3}}


def test_bedrock_sends_the_same_messages_request_to_the_regional_mantle_endpoint():
    seen = []

    def handler(req):
        seen.append(req)
        import httpx2
        return httpx2.Response(200, json=_message(json.dumps(GOOD)))

    wire = AnthropicWire(_mantle(handler, api_key="bedrock-token"), api="bedrock")
    u = Usage()
    p = ModelPort(wire, "anthropic.claude-opus-5", 30)
    assert p.api == "bedrock"
    assert p.call("extract", "sys", {"x": 1}, SCHEMA, usage=u, attribution="t", effort="high") == GOOD
    req = seen[0]
    assert str(req.url) == "https://bedrock-mantle.eu-west-1.api.aws/anthropic/v1/messages"
    body = json.loads(req.content)
    assert body["model"] == "anthropic.claude-opus-5"
    assert body["output_config"]["format"]["type"] == "json_schema" and body["output_config"]["effort"] == "high"
    for banned in ("tools", "stream", "thinking", "temperature"):
        assert banned not in body
    assert u.as_dict() == {"input_tokens": 7, "output_tokens": 3}
    assert len(seen) == 1  # one call, through the client's own transport (never a copy)


def test_bedrock_with_aws_credentials_signs_sigv4_for_bedrock_mantle():
    seen = []

    def handler(req):
        seen.append(req)
        import httpx2
        return httpx2.Response(200, json=_message(json.dumps(GOOD)))

    client = _mantle(handler, aws_access_key="AKIDEXAMPLE", aws_secret_key="secret")
    ModelPort(AnthropicWire(client, api="bedrock"), "anthropic.claude-opus-5", 30).call(
        "p", "s", {}, SCHEMA, usage=Usage(), attribution="t")
    auth = seen[0].headers["authorization"]
    assert auth.startswith("AWS4-HMAC-SHA256") and "/eu-west-1/bedrock-mantle/aws4_request" in auth


def test_bedrock_refusing_structured_output_is_unsupported_and_never_resent_without_it():
    calls = []

    def handler(req):
        calls.append(req)
        import httpx2
        return httpx2.Response(400, json={"type": "error", "error": {
            "type": "invalid_request_error", "message": "output_config.format: Extra inputs are not permitted"}})

    wire = AnthropicWire(_mantle(handler, api_key="t"), api="bedrock")
    with pytest.raises(ModelError) as e:
        ModelPort(wire, "anthropic.claude-opus-5", 30).call("p", "s", {}, SCHEMA, usage=Usage(), attribution="t")
    assert e.value.kind == "unsupported" and len(calls) == 1


def test_bedrock_without_aws_credentials_is_auth_not_a_transient_failure():
    sdk = FakeSDK([RuntimeError("Could not resolve AWS credentials from session")])
    with pytest.raises(ModelError) as e:
        ModelPort(AnthropicWire(sdk, api="bedrock"), "anthropic.claude-opus-5", 30).call(
            "p", "s", {}, SCHEMA, usage=Usage(), attribution="t")
    assert e.value.kind == "auth"


def _runtime(handler, **auth):
    """A real AnthropicBedrock (bedrock-runtime InvokeModel) client whose HTTP goes to `handler`."""
    import httpx2
    from anthropic import AnthropicBedrock
    return AnthropicBedrock(aws_region="eu-west-1", max_retries=1,
                            http_client=httpx2.Client(transport=httpx2.MockTransport(handler)), **auth)


def test_bedrock_runtime_wraps_the_same_request_in_invokemodel():
    seen = []

    def handler(req):
        seen.append(req)
        import httpx2
        return httpx2.Response(200, json=_message(json.dumps(GOOD)))

    wire = AnthropicWire(_runtime(handler, aws_access_key="AKIDEXAMPLE", aws_secret_key="secret"), api="bedrock")
    u = Usage()
    out = ModelPort(wire, "global.anthropic.claude-opus-5-5", 30).call("extract", "sys", {"x": 1}, SCHEMA,
                                                                        usage=u, attribution="t", effort="high")
    assert out == GOOD and len(seen) == 1
    req = seen[0]
    assert str(req.url) == ("https://bedrock-runtime.eu-west-1.amazonaws.com"
                            "/model/global.anthropic.claude-opus-5-5/invoke")
    body = json.loads(req.content)
    assert body["anthropic_version"] == "bedrock-2023-05-31" and "model" not in body  # the model is in the URL
    assert body["output_config"]["format"]["type"] == "json_schema" and body["output_config"]["effort"] == "high"
    for banned in ("tools", "stream", "thinking", "temperature"):
        assert banned not in body
    assert "/eu-west-1/bedrock/aws4_request" in req.headers["authorization"]
    assert u.as_dict() == {"input_tokens": 7, "output_tokens": 3}


def test_bedrock_endpoint_selection_and_default_model(monkeypatch):
    w = build_wire(Settings(model_api="bedrock", model_region="eu-west-1", model_api_key="tok"))
    assert w.api == "bedrock" and str(w.client.base_url).startswith("https://bedrock-runtime.eu-west-1.amazonaws.com")
    w = build_wire(Settings(model_api="bedrock", model_bedrock_endpoint="mantle", model_region="eu-west-1",
                            model_api_key="tok"))
    assert str(w.client.base_url) == "https://bedrock-mantle.eu-west-1.api.aws/anthropic/"
    with pytest.raises(ValueError):
        build_wire(Settings(model_api="bedrock", model_bedrock_endpoint="converse", model_region="eu-west-1"))
    for k in ("NAPKIN_MODEL", "NAPKIN_MODEL_REGION", "NAPKIN_MODEL_BEDROCK_ENDPOINT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NAPKIN_MODEL_API", "bedrock")
    st = Settings.from_env()
    assert st.model == "global.anthropic.claude-opus-5-5" and st.model_bedrock_endpoint == "runtime"
    monkeypatch.setenv("NAPKIN_MODEL_API", "anthropic")
    assert Settings.from_env().model == "claude-opus-5"


def test_the_middleware_names_no_mock():
    """The swap guarantee (peripherals.md §0.1): nothing in server/napkin names a mock."""
    root = Path(__file__).resolve().parents[1] / "napkin"
    hits = [f"{p}:{i}" for p in root.rglob("*.py") for i, line in enumerate(p.read_text().splitlines(), 1)
            if re.search(r"mock|8797|claude -p", line, re.I)]
    assert hits == []
