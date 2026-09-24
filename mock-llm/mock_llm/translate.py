"""Pure translation between the Messages API and the Claude Code CLI.

No I/O here: validate a request, flatten it into a CLI call, and map the CLI's
JSON envelope back to a Messages response. Everything is unit-testable without
spawning `claude`.

Transport only. There is no prompt engineering, retrieval, schema or cost logic
in this module (build-plan decision M1): the middleware owns all of that.
"""

from __future__ import annotations

import json
import secrets
import string
from dataclasses import dataclass, field

# API model id -> Claude Code alias. An id not listed here is a 404, never a
# fallback to some default (decision M4's rule about unknown versions).
MODEL_MAP: dict[str, str] = {
    "claude-opus-5": "opus",
    "claude-sonnet-5": "sonnet",
    "claude-haiku-4-5": "haiku",
    "claude-haiku-4-5-20251001": "haiku",
    "claude-fable-5-1": "fable",
}

# Top-level request fields the mock understands. Anything else is a 400, as the
# real API rejects extra inputs; silently accepting a typo teaches the
# middleware a habit that fails in production.
_ACCEPTED_AND_IGNORED = {
    "temperature", "top_p", "top_k", "stop_sequences", "metadata",
    "thinking", "service_tier",
}
_REFUSED = {
    "tools": "the mock does not support `tools`: `claude -p` cannot emulate "
             "tool_use content blocks, and silently dropping them would pass in "
             "development and fail against the real API",
    "tool_choice": "the mock does not support `tool_choice` (it does not support tools)",
    "mcp_servers": "the mock does not support `mcp_servers`",
    "container": "the mock does not support `container`",
    "context_management": "the mock does not support `context_management`",
}
_KNOWN = {"model", "max_tokens", "messages", "system", "stream", "output_config"} \
    | _ACCEPTED_AND_IGNORED | set(_REFUSED)

EFFORT_LEVELS = {"low", "medium", "high", "xhigh", "max"}


class ApiError(Exception):
    """An error returned to the client in Anthropic's shape."""

    def __init__(self, status: int, type_: str, message: str):
        super().__init__(message)
        self.status = status
        self.type = type_
        self.message = message

    def body(self) -> dict:
        return {"type": "error", "error": {"type": self.type, "message": self.message}}


def invalid(message: str) -> ApiError:
    return ApiError(400, "invalid_request_error", message)


@dataclass
class CliCall:
    """Everything needed to run one `claude -p` subprocess."""
    alias: str
    prompt: str                      # sent on stdin, never argv (argv is visible in `ps`)
    system: str | None               # sent through a pipe fd, never argv or disk
    json_schema: dict | None = None
    effort: str | None = None
    ignored: list[str] = field(default_factory=list)


def parse_body(raw: bytes) -> dict:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise invalid(f"request body is not valid UTF-8 (byte {e.start})") from None
    try:
        body = json.loads(text)
    except json.JSONDecodeError as e:
        raise invalid(f"request body is not valid JSON: {e.msg} at line {e.lineno} column {e.colno}") from None
    if not isinstance(body, dict):
        raise invalid("request body must be a JSON object")
    return body


def _text_of_blocks(blocks, where: str) -> str:
    if isinstance(blocks, str):
        return blocks
    if not isinstance(blocks, list):
        raise invalid(f"{where}: content must be a string or a list of content blocks")
    parts = []
    for i, b in enumerate(blocks):
        if not isinstance(b, dict) or "type" not in b:
            raise invalid(f"{where}.{i}: content block must be an object with a `type`")
        t = b["type"]
        if t == "text":
            if not isinstance(b.get("text"), str):
                raise invalid(f"{where}.{i}.text: Field required")
            # cache_control, citations: accepted and dropped. The mock does not cache.
            parts.append(b["text"])
        elif t in ("tool_use", "tool_result", "server_tool_use"):
            raise invalid(f"{where}.{i}: the mock does not support `{t}` blocks (it does not support tools)")
        else:
            raise invalid(f"{where}.{i}: the mock does not support `{t}` content blocks; only `text` "
                          "(images, documents and audio need the real API)")
    return "\n\n".join(parts)


def build_call(body: dict) -> CliCall:
    """Validate a Messages request and turn it into a CLI call. Raises ApiError."""
    unknown = sorted(set(body) - _KNOWN)
    if unknown:
        raise invalid(f"{unknown[0]}: Extra inputs are not permitted")
    for key, why in _REFUSED.items():
        if key in body:
            raise invalid(why)
    if body.get("stream") is True:
        raise invalid("the mock does not support `stream: true`: it would have to fake the SSE "
                      "event sequence, which hides stream-handling bugs until production")
    if "stream" in body and not isinstance(body["stream"], bool):
        raise invalid("stream: Input should be a valid boolean")

    model = body.get("model")
    if not isinstance(model, str) or not model:
        raise invalid("model: Field required")

    mt = body.get("max_tokens")
    if mt is None:
        raise invalid("max_tokens: Field required")
    if isinstance(mt, bool) or not isinstance(mt, int) or mt < 1:
        raise invalid("max_tokens: Input should be an integer greater than or equal to 1")

    msgs = body.get("messages")
    if msgs is None:
        raise invalid("messages: Field required")
    if not isinstance(msgs, list):
        raise invalid("messages: Input should be a valid list")
    if not msgs:
        raise invalid("messages: at least one message is required")

    # Model is checked after shape so a malformed body is a 400 regardless of model,
    # but before any subprocess is spawned.
    if model not in MODEL_MAP:
        raise ApiError(404, "not_found_error", f"model: {model}")

    system = body.get("system")
    if system is not None:
        system = _text_of_blocks(system, "system")

    turns: list[tuple[str, str]] = []
    for i, m in enumerate(msgs):
        if not isinstance(m, dict):
            raise invalid(f"messages.{i}: Input should be an object")
        role = m.get("role")
        if role not in ("user", "assistant"):
            raise invalid(f"messages.{i}.role: Input should be 'user' or 'assistant'")
        if "content" not in m:
            raise invalid(f"messages.{i}.content: Field required")
        text = _text_of_blocks(m["content"], f"messages.{i}.content")
        if not text.strip():
            raise invalid(f"messages.{i}: text content blocks must be non-empty")
        turns.append((role, text))
    if turns[-1][0] != "user":
        raise invalid("the final message must have role `user`: the mock cannot continue an "
                      "assistant prefill (and current models refuse prefill on the real API)")

    schema = None
    effort = None
    oc = body.get("output_config")
    if oc is not None:
        if not isinstance(oc, dict):
            raise invalid("output_config: Input should be an object")
        extra = sorted(set(oc) - {"format", "effort"})
        if extra:
            raise invalid(f"output_config.{extra[0]}: Extra inputs are not permitted")
        fmt = oc.get("format")
        if fmt is not None:
            if not isinstance(fmt, dict) or fmt.get("type") != "json_schema":
                raise invalid("output_config.format.type: the mock supports only `json_schema`")
            schema = fmt.get("schema")
            if not isinstance(schema, dict):
                raise invalid("output_config.format.schema: Field required")
        effort = oc.get("effort")
        if effort is not None and effort not in EFFORT_LEVELS:
            raise invalid(f"output_config.effort: Input should be one of {sorted(EFFORT_LEVELS)}")

    ignored = sorted(k for k in body if k in _ACCEPTED_AND_IGNORED)
    return CliCall(alias=MODEL_MAP[model], prompt=flatten(turns), system=system,
                   json_schema=schema, effort=effort, ignored=ignored)


def flatten(turns: list[tuple[str, str]]) -> str:
    """One user turn goes through verbatim. A multi-turn history is rendered as a
    transcript, since `claude -p` takes a single prompt and we never --resume."""
    if len(turns) == 1:
        return turns[0][1]
    lines = ["The conversation so far is below, oldest first. Write only the assistant's "
             "next reply to the final user turn.", ""]
    for role, text in turns:
        lines += [f"<{role}>", text, f"</{role}>", ""]
    return "\n".join(lines).rstrip() + "\n"


_B62 = string.ascii_letters + string.digits


def message_id() -> str:
    return "msg_" + "".join(secrets.choice(_B62) for _ in range(24))


def usage_of(envelope: dict) -> dict:
    """Usage from the CLI envelope, or zeros. Never an estimate: a plausible
    number would be believed by the cost accounting downstream.

    input_tokens counts every prompt token the CLI reports the model read
    (fresh + CLI-side cache writes + CLI-side cache reads). The cache fields
    are always 0: the mock does not cache and must not claim to."""
    u = envelope.get("usage") if isinstance(envelope.get("usage"), dict) else {}

    def n(k):
        v = u.get(k)
        return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0

    return {
        "input_tokens": n("input_tokens") + n("cache_creation_input_tokens") + n("cache_read_input_tokens"),
        "output_tokens": n("output_tokens"),
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }


_STATUS_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    413: "request_too_large",
    429: "rate_limit_error",
    529: "overloaded_error",
}


def envelope_error(envelope: dict) -> ApiError | None:
    """If the CLI reported a failure, the matching Anthropic error; else None."""
    if not envelope.get("is_error") and str(envelope.get("subtype", "success")) == "success":
        return None
    msg = str(envelope.get("result") or envelope.get("subtype") or "claude reported an error")
    status = envelope.get("api_error_status")
    low = msg.lower()
    if status == 401 or "not logged in" in low or "/login" in low or "invalid api key" in low:
        return ApiError(401, "authentication_error", f"claude CLI: {msg}")
    if isinstance(status, int) and status in _STATUS_TYPES:
        return ApiError(status, _STATUS_TYPES[status], f"claude CLI: {msg}")
    return ApiError(500, "api_error", f"claude CLI: {msg}")


def to_response(envelope: dict, call: CliCall, model: str) -> dict:
    """Map a successful CLI envelope to a Messages response. Raises ApiError."""
    err = envelope_error(envelope)
    if err:
        raise err
    if call.json_schema is not None:
        if "structured_output" not in envelope:
            raise ApiError(500, "api_error", "claude CLI returned no structured_output for a "
                                             "json_schema request")
        text = json.dumps(envelope["structured_output"], ensure_ascii=False)
    else:
        text = envelope.get("result")
        if not isinstance(text, str):
            raise ApiError(500, "api_error", "claude CLI envelope has no `result` text")
    return {
        "id": message_id(),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        # Always end_turn: the CLI does not report truncation (documented divergence).
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": usage_of(envelope),
    }
