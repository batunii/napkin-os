"""The model port: structured output on every call (W2-C3).

One helper, `ModelPort.structured()`, over the official SDK:
`client.messages.create(..., output_config={"format": {"type": "json_schema",
"schema": ...}})`. The first text block is the JSON. The returned object is
validated against the declared schema before it reaches any writer; on a
failure the call is retried once with the validation error fed back, then
fails loudly. There is no prose salvage.

The client is `anthropic.Anthropic()` — the SDK reads `ANTHROPIC_BASE_URL`
and credentials from the environment, so which Messages endpoint answers is
configuration only.
"""

from __future__ import annotations

import json
import logging
import threading
import time

import jsonschema

log = logging.getLogger("napkin.model")


class ModelError(Exception):
    pass


def _strip_unsupported(schema):
    """The API's structured outputs ignore/refuse string-length and numeric
    constraints; the full schema is still enforced here after the call."""
    if isinstance(schema, dict):
        return {k: _strip_unsupported(v) for k, v in schema.items()
                if k not in ("minLength", "maxLength", "minimum", "maximum", "minItems", "maxItems",
                             "uniqueItems", "pattern", "format")}
    if isinstance(schema, list):
        return [_strip_unsupported(x) for x in schema]
    return schema


class Usage:
    def __init__(self):
        self.input_tokens = 0
        self.output_tokens = 0
        self.calls = 0
        self._lock = threading.Lock()

    def add(self, u):
        with self._lock:
            self.calls += 1
            self.input_tokens += int(getattr(u, "input_tokens", 0) or 0)
            self.output_tokens += int(getattr(u, "output_tokens", 0) or 0)

    def as_dict(self):
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens}


class ModelPort:
    """Shared by every request; `bind()` gives a per-job view that records
    usage and attributes each call to a handler."""

    def __init__(self, client, model: str, timeout: float, max_tokens: int = 16000):
        self.client, self.model, self.timeout, self.max_tokens = client, model, timeout, max_tokens

    def call(self, purpose: str, system: str, payload: dict, schema: dict, *, usage: Usage, attribution: str,
             max_tokens: int | None = None, effort: str | None = None) -> dict:
        user = f"Task: {purpose}\n\n<input>\n{json.dumps(payload, ensure_ascii=False, indent=1)}\n</input>"
        messages = [{"role": "user", "content": user}]
        api_schema = _strip_unsupported(schema)
        validator = jsonschema.Draft202012Validator(schema)
        last_err = None
        for attempt in (1, 2):
            t0 = time.monotonic()
            kwargs = dict(model=self.model, max_tokens=max_tokens or self.max_tokens, system=system,
                          messages=messages,
                          output_config={"format": {"type": "json_schema", "schema": api_schema},
                                         **({"effort": effort} if effort else {})})
            try:
                resp = self.client.with_options(timeout=self.timeout, max_retries=1).messages.create(**kwargs)
            except Exception as e:  # transport / API failure: explicit, attributable
                raise ModelError(f"{purpose}: the model call failed ({type(e).__name__})") from e
            usage.add(getattr(resp, "usage", None))
            text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), None)
            stop = getattr(resp, "stop_reason", None)
            log.info("model %s attempt=%d stop=%s %.1fs [%s]", purpose, attempt, stop, time.monotonic() - t0,
                     attribution)
            problem = None
            if stop == "refusal":
                problem = "the model refused"
            elif stop == "max_tokens":
                problem = "the response was cut off at max_tokens"
            elif text is None:
                problem = "no text block in the response"
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
            if attempt == 1 and text is not None and stop not in ("refusal",):
                # Retry once with the validation error fed back (no prefill: a new user turn).
                messages = [{"role": "user", "content": user},
                            {"role": "assistant", "content": text[:20000]},
                            {"role": "user", "content": f"That output does not validate against the schema: "
                                                        f"{problem}. Return the corrected JSON only."}]
                continue
            break
        raise ModelError(f"{purpose}: no valid structured output ({last_err})")
