"""Shared by the contract suites: an HTTP client and a pass/fail tally.
Standard library only. Names no implementation."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request


class Result:
    def __init__(self):
        self.passes = 0
        self.failures: list[str] = []
        self.skipped: list[str] = []

    def check(self, cond: bool, what: str) -> bool:
        if cond:
            self.passes += 1
            print(f"  ok    {what}", flush=True)
        else:
            self.failures.append(what)
            print(f"  FAIL  {what}", flush=True)
        return bool(cond)

    def skip(self, what: str) -> None:
        self.skipped.append(what)
        print(f"  skip  {what}", flush=True)

    def section(self, title: str) -> None:
        print(title, flush=True)

    def report(self, label: str) -> int:
        print(f"\n{label}: {self.passes} passed, {len(self.failures)} failed, {len(self.skipped)} skipped")
        for f in self.failures:
            print(f"  - {f}")
        return 1 if self.failures else 0


class Client:
    def __init__(self, base: str, token: str | None = None, timeout: float = 60):
        self.base = base.rstrip("/")
        self.token = token
        self.timeout = timeout

    def call(self, method: str, path: str, body=None, raw: bytes | None = None, headers: dict | None = None,
             timeout: float | None = None, auth: bool = True):
        """(status, parsed JSON or None, response headers, seconds)."""
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        h = {"Content-Type": "application/json"}
        if self.token and auth:
            h["Authorization"] = f"Bearer {self.token}"
        h.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                status, text, rh = r.status, r.read(), dict(r.headers.items())
        except urllib.error.HTTPError as e:
            with e:
                status, text, rh = e.code, e.read(), dict(e.headers.items())
        except (ConnectionError, urllib.error.URLError) as e:
            return None, {"_transport_error": str(e)}, {}, time.monotonic() - t0
        try:
            payload = json.loads(text) if text else None
        except json.JSONDecodeError:
            payload = None
        return status, payload, {k.lower(): v for k, v in rh.items()}, time.monotonic() - t0


def error_type(payload) -> str | None:
    """The `type` of a peripheral error body {"error": {"type", "message"}}."""
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        return payload["error"].get("type")
    return None


def is_error_shape(payload) -> bool:
    return (isinstance(payload, dict) and set(payload) == {"error"} and isinstance(payload["error"], dict)
            and isinstance(payload["error"].get("type"), str) and isinstance(payload["error"].get("message"), str))
