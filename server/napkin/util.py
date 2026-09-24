"""Ids, time, slugs, errors: the small pieces every module shares."""

from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import json
import os
import re
import unicodedata


class TaskError(Exception):
    """An error the contract names: status + type + a message that never
    carries request content."""

    def __init__(self, status: int, etype: str, message: str):
        super().__init__(message)
        self.status, self.etype, self.message = status, etype, message


def bad(message: str, etype: str = "invalid_input") -> TaskError:
    return TaskError(400, etype, message)


def _digest(*parts) -> bytes:
    h = hashlib.sha256()
    for p in parts:
        h.update(json.dumps(p, sort_keys=True, ensure_ascii=False, default=str).encode())
        h.update(b"\x1f")
    return h.digest()


def uid(prefix: str, *parts, n: int = 12) -> str:
    """Opaque id ^<prefix>[0-9A-Z]{n}$ (base32, uppercase), seeded by parts."""
    return prefix + base64.b32encode(_digest(*parts)).decode()[:n].replace("=", "A")


def lid(prefix: str, *parts, n: int = 10) -> str:
    """Lowercase hex id (source ids, item keys: ^[a-z0-9_]+$)."""
    return prefix + _digest(*parts).hex()[:n]


def rid(prefix: str, n: int = 16) -> str:
    """A random opaque id (jobs)."""
    return prefix + os.urandom(n // 2 + 1).hex()[:n]


def now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def iso(t: _dt.datetime | None = None) -> str:
    return (t or now()).astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today() -> str:
    return now().date().isoformat()


def slug(text: str) -> str:
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")


def item_slug(text: str) -> str:
    return re.sub(r"-", "_", slug(text))


def ulid_like(prefix: str, seq: int, *seed) -> str:
    """<prefix><10 time chars><4 seed chars><seq>: sorts in creation order."""
    ms = int(now().timestamp() * 1000)
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    t = ""
    for _ in range(10):
        t = alphabet[ms % 32] + t
        ms //= 32
    return f"{prefix}{t}{uid('', *seed, n=4)}{seq:03d}"


def canon_sha(obj) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def norm_sha(s: str) -> str:
    s = (s or "").strip().lower()
    return s if s.startswith("sha256:") else "sha256:" + s
