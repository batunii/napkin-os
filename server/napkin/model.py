"""The model port — `napkin.model/1` (docs/contracts/peripherals.md §1).

Structured output on every call (W2-C3): the object the model returns is
validated against the declared schema before it reaches any writer; on a
failure the call is retried once with the validation error fed back (the last
turn is always `user`, never a prefill), then fails loudly. There is no prose
salvage and no unconstrained fallback.

Two wire shapes carry it, chosen by configuration (`NAPKIN_MODEL_API`):

  anthropic  the Anthropic Messages API through the official SDK
             (`output_config.format = json_schema`)
  openai     an OpenAI-compatible `/chat/completions` (self-hosted NIM)
             (`response_format = json_schema, strict: true`)

No handler knows which wire is in use. Both wires' failures map to one
`ModelError(kind)` (§1.7).
"""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
import time

import httpx
import jsonschema

from .metrics import emit, record

log = logging.getLogger("napkin.model")

IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGES = 20
PURPOSE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

KINDS = ("auth", "not_found", "invalid_request", "rate_limited", "overloaded", "server", "timeout",
         "truncated", "refusal", "invalid_output", "unsupported")


class ModelError(Exception):
    """A model failure, by kind (peripherals.md §1.7). Handlers see only the kind."""

    def __init__(self, message: str, kind: str = "invalid_output"):
        super().__init__(message)
        self.kind = kind if kind in KINDS else "server"


def strip_unsupported(schema):
    """The providers refuse or ignore string-length and numeric constraints; the
    full schema is still enforced here after the call."""
    if isinstance(schema, dict):
        return {k: strip_unsupported(v) for k, v in schema.items()
                if k not in ("minLength", "maxLength", "minimum", "maximum", "minItems", "maxItems",
                             "uniqueItems", "pattern", "format")}
    if isinstance(schema, list):
        return [strip_unsupported(x) for x in schema]
    return schema


_strip_unsupported = strip_unsupported  # older name


class Usage:
    def __init__(self):
        self.input_tokens = 0
        self.output_tokens = 0
        self.calls = 0
        self._lock = threading.Lock()

    def add(self, input_tokens: int, output_tokens: int):
        with self._lock:
            self.calls += 1
            self.input_tokens += int(input_tokens or 0)
            self.output_tokens += int(output_tokens or 0)

    def as_dict(self):
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens}


class Reply:
    """What a wire returns: the text (or None), how it stopped, and the usage
    the provider reported (None when it reported none: never estimated)."""

    def __init__(self, text, stop: str, usage: tuple[int, int] | None, detail: str = "", breakdown: dict | None = None):
        self.text, self.stop, self.usage, self.detail = text, stop, usage, detail
        self.breakdown = breakdown  # {"fresh", "cache_write", "cache_read"} input tokens, when the wire reports them


def check_images(images) -> list[dict]:
    """[{media_type, data(base64)}] -> the same, validated (§1.6). Raises ModelError."""
    out = []
    for im in images or []:
        mt, data = (im or {}).get("media_type"), (im or {}).get("data")
        if mt not in IMAGE_TYPES or not isinstance(data, str):
            raise ModelError("an image part is not png, jpeg, gif or webp base64", "invalid_request")
        try:
            raw = base64.b64decode(data, validate=True)
        except (ValueError, TypeError) as e:
            raise ModelError("an image part is not valid base64", "invalid_request") from e
        if len(raw) > MAX_IMAGE_BYTES:
            raise ModelError("an image part is over 5 MB decoded", "invalid_request")
        out.append({"media_type": mt, "data": data})
    if len(out) > MAX_IMAGES:
        raise ModelError("more than 20 images in one call", "invalid_request")
    return out


# ---------------------------------------------------------------------------
# Wire A — Anthropic Messages (the official SDK)
# ---------------------------------------------------------------------------

class AnthropicWire:
    api = "anthropic"

    def __init__(self, client):
        self.client = client

    @staticmethod
    def _content(turn):
        if not turn.get("images"):
            return turn["text"]
        parts = [{"type": "image", "source": {"type": "base64", "media_type": im["media_type"], "data": im["data"]}}
                 for im in turn["images"]]
        return parts + [{"type": "text", "text": turn["text"]}]

    def send(self, *, model, system, turns, schema, purpose, max_tokens, effort, timeout, headers=None) -> Reply:
        kwargs = dict(model=model, max_tokens=max_tokens, system=system,
                      messages=[{"role": t["role"], "content": self._content(t)} for t in turns],
                      output_config={"format": {"type": "json_schema", "schema": schema},
                                     **({"effort": effort} if effort else {})})
        if headers:
            kwargs["extra_headers"] = dict(headers)
        try:
            resp = self.client.with_options(timeout=timeout, max_retries=1).messages.create(**kwargs)
        except Exception as e:  # transport / API failure: mapped to a kind, attributable
            raise ModelError(f"{purpose}: the model call failed ({type(e).__name__})", _anthropic_kind(e)) from e
        u = getattr(resp, "usage", None)
        usage, breakdown = None, None
        if u is not None:
            fresh = int(getattr(u, "input_tokens", 0) or 0)
            cw = int(getattr(u, "cache_creation_input_tokens", 0) or 0)
            cr = int(getattr(u, "cache_read_input_tokens", 0) or 0)
            usage = (fresh + cw + cr, int(getattr(u, "output_tokens", 0) or 0))
            breakdown = {"fresh": fresh, "cache_write": cw, "cache_read": cr}
        text = next((b.text for b in getattr(resp, "content", None) or [] if getattr(b, "type", None) == "text"),
                    None)
        stop = getattr(resp, "stop_reason", None)
        if stop == "end_turn":
            return Reply(text, "ok", usage, breakdown=breakdown)
        if stop == "max_tokens":
            return Reply(text, "truncated", usage, breakdown=breakdown)
        if stop == "refusal":
            cat = getattr(getattr(resp, "stop_details", None), "category", None)
            return Reply(text, "refusal", usage, detail=str(cat or ""), breakdown=breakdown)
        return Reply(text, "invalid", usage, detail=f"stop_reason {stop}", breakdown=breakdown)


def _anthropic_kind(e) -> str:
    try:
        import anthropic
    except ImportError:  # pragma: no cover
        anthropic = None
    if anthropic is not None:
        if isinstance(e, anthropic.APITimeoutError):
            return "timeout"
        if isinstance(e, anthropic.APIStatusError):
            return _status_kind(e.status_code, anthropic_wire=True)
        if isinstance(e, anthropic.APIConnectionError):
            return "server"
    return "server"


def _status_kind(status: int, anthropic_wire: bool = False) -> str:
    if status in (401, 403):
        return "auth"
    if status == 404:
        return "not_found"
    if status in (400, 413) or (status == 422 and not anthropic_wire):
        return "invalid_request"
    if status == 429:
        return "rate_limited"
    if status == 504:
        return "timeout"
    if status == 503 or (status == 529 and anthropic_wire):
        return "overloaded"
    if status >= 500:
        return "server"
    return "invalid_request"


# ---------------------------------------------------------------------------
# Wire B — OpenAI-compatible chat completions (NIM)
# ---------------------------------------------------------------------------

_THINK = re.compile(r"^\s*<think>.*?</think>\s*", re.S)
_UNSUPPORTED = re.compile(r"response_format|json_schema|structured output|guided", re.I)
PORT_KEYS = {"model", "max_tokens", "messages", "response_format"}


class OpenAIWire:
    api = "openai"

    def __init__(self, base_url: str, api_key: str | None = None, extra_body: dict | None = None, transport=None,
                 sleep=time.sleep):
        if not base_url:
            raise ValueError("NAPKIN_MODEL_BASE_URL is required for NAPKIN_MODEL_API=openai (the root including /v1)")
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.key = api_key
        self.extra = {k: v for k, v in (extra_body or {}).items() if k not in PORT_KEYS}
        self._client = httpx.Client(transport=transport)
        self._sleep = sleep

    @staticmethod
    def _content(turn):
        if not turn.get("images"):
            return turn["text"]
        parts = [{"type": "image_url", "image_url": {"url": f"data:{im['media_type']};base64,{im['data']}"}}
                 for im in turn["images"]]
        return parts + [{"type": "text", "text": turn["text"]}]

    def send(self, *, model, system, turns, schema, purpose, max_tokens, effort, timeout, headers=None) -> Reply:
        body = dict(self.extra)
        body.update(model=model, max_tokens=max_tokens,
                    messages=[{"role": "system", "content": system}]
                    + [{"role": t["role"], "content": self._content(t)} for t in turns],
                    response_format={"type": "json_schema",
                                     "json_schema": {"name": purpose, "schema": schema, "strict": True}})
        headers = {**(headers or {}), "Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        for attempt in (1, 2):
            try:
                r = self._client.post(self.url, json=body, headers=headers, timeout=timeout)
            except httpx.TimeoutException as e:
                raise ModelError(f"{purpose}: the model call timed out", "timeout") from e
            except httpx.HTTPError as e:
                if attempt == 1:
                    continue
                raise ModelError(f"{purpose}: the model endpoint is unreachable ({type(e).__name__})", "server") from e
            if r.status_code == 200:
                break
            kind = _status_kind(r.status_code)
            msg = _openai_error_message(r)
            if kind == "invalid_request" and r.status_code == 400 and _UNSUPPORTED.search(msg):
                # Structured output is enforced or the call fails: never retried without it (§1.4).
                raise ModelError(f"{purpose}: the endpoint does not support response_format json_schema",
                                 "unsupported")
            if attempt == 1 and kind in ("rate_limited", "overloaded", "server"):
                if kind == "rate_limited":
                    self._sleep(_retry_after(r))
                continue
            raise ModelError(f"{purpose}: the model endpoint returned {r.status_code}", kind)
        try:
            out = r.json()
        except ValueError as e:
            raise ModelError(f"{purpose}: the model endpoint returned a body that is not JSON", "invalid_output") from e
        u = out.get("usage") if isinstance(out, dict) else None
        usage = (int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)) if isinstance(u, dict) \
            else None
        choices = out.get("choices") if isinstance(out, dict) else None
        if not choices or not isinstance(choices[0], dict):
            return Reply(None, "invalid", usage, detail="no choices")
        msg = choices[0].get("message") or {}
        text = msg.get("content")
        if isinstance(text, str):
            text = _THINK.sub("", text, count=1)
        finish = choices[0].get("finish_reason")
        if (msg.get("refusal") or "") or finish == "content_filter":
            return Reply(text, "refusal", usage)
        if finish == "length":
            return Reply(text, "truncated", usage)
        if text is None:
            return Reply(None, "invalid", usage, detail="content is null")
        if finish == "stop":
            return Reply(text, "ok", usage)
        return Reply(text, "invalid", usage, detail=f"finish_reason {finish}")


def _openai_error_message(r) -> str:
    try:
        e = r.json().get("error")
        return str(e.get("message") if isinstance(e, dict) else e or "")
    except (ValueError, AttributeError):
        return r.text[:500]


def _retry_after(r) -> float:
    try:
        return max(0.0, min(30.0, float(r.headers.get("retry-after", "1"))))
    except ValueError:
        return 1.0


# ---------------------------------------------------------------------------
# The port
# ---------------------------------------------------------------------------

class ModelPort:
    """Shared by every request; `Capabilities` gives each job a view that
    records usage and attributes each call to a handler.

    `client` may be an Anthropic SDK client (wrapped in the Anthropic wire) or a
    wire object (`AnthropicWire`, `OpenAIWire`)."""

    def __init__(self, client, model: str, timeout: float, max_tokens: int = 16000, vision_model: str | None = None):
        self.wire = client if hasattr(client, "send") and hasattr(client, "api") else AnthropicWire(client)
        self.model, self.timeout, self.max_tokens = model, timeout, max_tokens
        self.vision_model = vision_model or model

    @property
    def api(self) -> str:
        return self.wire.api

    def call(self, purpose: str, system: str, payload: dict, schema: dict, *, usage: Usage, attribution: str,
             max_tokens: int | None = None, effort: str | None = None, images=None, model: str | None = None,
             headers: dict | None = None) -> dict:
        """`headers`: the attribution headers (`X-Napkin-Handler`, `X-Napkin-Job`,
        peripherals.md §0.2) — metadata only, never auth, never content."""
        if not PURPOSE_RE.match(purpose or ""):
            raise ModelError(f"purpose {purpose!r} is not a slug", "invalid_request")
        images = check_images(images)
        model = model or self.model
        # Compact JSON: indentation was 5-21% of every payload's characters (21% of the report's), paid as input.
        user = f"Task: {purpose}\n\n<input>\n{json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n</input>"
        first = {"role": "user", "text": user, "images": images}
        turns = [first]
        api_schema = strip_unsupported(schema)
        validator = jsonschema.Draft202012Validator(schema)
        last_err = None
        for attempt in (1, 2):
            t0 = time.monotonic()
            try:
                reply = self.wire.send(model=model, system=system, turns=turns, schema=api_schema, purpose=purpose,
                                       max_tokens=max_tokens or self.max_tokens, effort=effort, timeout=self.timeout,
                                       headers=headers)
            except ModelError as e:
                emit("model", purpose=purpose, model=model, wire=self.wire.api, attempt=attempt, stop="error",
                     error=getattr(e, "kind", type(e).__name__), secs=round(time.monotonic() - t0, 2),
                     job=(headers or {}).get("X-Napkin-Job"), handler=(headers or {}).get("X-Napkin-Handler"))
                raise
            if reply.usage is None:
                log.warning("model %s: the response carried no usage; counted as zero [%s]", purpose, attribution)
                usage.add(0, 0)
            else:
                usage.add(*reply.usage)
            log.info("model %s wire=%s attempt=%d stop=%s %.1fs [%s]", purpose, self.wire.api, attempt, reply.stop,
                     time.monotonic() - t0, attribution)
            emit("model", purpose=purpose, model=model, wire=self.wire.api, attempt=attempt, stop=reply.stop,
                 secs=round(time.monotonic() - t0, 2), input_tokens=(reply.usage or (None, None))[0],
                 output_tokens=(reply.usage or (None, None))[1], max_tokens=max_tokens or self.max_tokens,
                 effort=effort, breakdown=reply.breakdown, job=(headers or {}).get("X-Napkin-Job"),
                 handler=(headers or {}).get("X-Napkin-Handler"))
            record("model_calls", purpose=purpose, model=model, attempt=attempt, stop=reply.stop, system=system,
                   user=turns[-1]["text"] if len(turns) == 1 else turns[0]["text"], turns=len(turns), schema=schema,
                   reply=reply.text, usage=reply.usage, breakdown=reply.breakdown, max_tokens=max_tokens or self.max_tokens,
                   effort=effort, job=(headers or {}).get("X-Napkin-Job"))
            if reply.stop == "truncated":
                raise ModelError(f"{purpose}: the response was cut off at max_tokens", "truncated")
            if reply.stop == "refusal":
                if reply.detail:
                    log.info("model %s refusal category=%s [%s]", purpose, reply.detail, attribution)
                raise ModelError(f"{purpose}: the model refused", "refusal")
            text = reply.text
            if reply.stop != "ok" or text is None:
                problem = reply.detail or "no text in the response"
            else:
                try:
                    obj = json.loads(text)
                except json.JSONDecodeError as e:
                    problem = f"the response is not JSON ({e.msg})"
                else:
                    errs = sorted(validator.iter_errors(obj), key=lambda e: list(e.path))
                    if not errs:
                        if attempt == 2:
                            log.warning("model %s needed a second attempt [%s]", purpose, attribution)
                        return obj
                    problem = "; ".join(f"{'/'.join(map(str, e.path)) or '$'}: {e.message}" for e in errs[:5])
            last_err = problem
            if attempt == 1 and text is not None:
                # Retry once with the validation error fed back (no prefill: a new user turn).
                turns = [first, {"role": "assistant", "text": text[:20000], "images": []},
                         {"role": "user", "text": f"That output does not validate against the schema: {problem}. "
                                                  f"Return the corrected JSON only.", "images": []}]
                continue
            break
        raise ModelError(f"{purpose}: no valid structured output ({last_err})", "invalid_output")


def build_wire(settings, client=None, transport=None):
    """The configured wire. `client`: an Anthropic-SDK-shaped client to use
    instead of constructing one (tests)."""
    if settings.model_api == "openai":
        return OpenAIWire(settings.model_base_url, settings.model_api_key, settings.model_extra_body,
                          transport=transport)
    if settings.model_api != "anthropic":
        raise ValueError(f"NAPKIN_MODEL_API must be anthropic or openai, not {settings.model_api!r}")
    if client is None:
        import anthropic
        kw = {}
        if settings.model_base_url:
            kw["base_url"] = settings.model_base_url
        if settings.model_api_key:
            kw["api_key"] = settings.model_api_key
        client = anthropic.Anthropic(**kw)  # unset values: the SDK's own resolution
    return AnthropicWire(client)
