"""The retrieval port — `napkin.retrieval/1` (peripherals.md §3).

  GET  <NAPKIN_RETRIEVAL_URL>/v1/packs      what this caller may search
  POST <NAPKIN_RETRIEVAL_URL>/v1/retrieve   {query, k, packs?, where?, purpose?} -> verbatim passages

Scope travels in headers (`X-Napkin-Org`, `X-Napkin-Brand`) set here from the
scope the capability was bound to — never in a body. The request fields are
closed: nothing here can say "fill this brief", and every string that comes
back is verbatim pack text.

The client checks what it can recompute (§3.2): `text_sha256` is the SHA-256
of `text`, `id` and `uri` follow the formula. A passage that fails is dropped,
never repaired. A non-2xx, a malformed body or a timeout raises
`RetrievalError`; an honest `passages: []` is a valid answer.
"""

from __future__ import annotations

import hashlib
import re
import time

import httpx

RETRY = {429, 502, 503, 504}


class RetrievalError(Exception):
    pass


def section_slug(section: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (section or "").lower()).strip("-")


def passage_id(pack: str, source: str, section: str, text: str) -> str:
    return "psg_" + hashlib.sha256(f"{pack}\n{source}\n{section}\n{text}".encode()).hexdigest()[:20]


def passage_uri(pack: str, source: str, section: str, text_sha256: str) -> str:
    return f"passage://{pack}/{source}#{section_slug(section)}@{text_sha256[:16]}"


def passage_ok(p) -> bool:
    if not isinstance(p, dict):
        return False
    try:
        text, pack, source, section = p["text"], p["pack"], p["source"], p["section"]
        if not (isinstance(text, str) and text and len(text) <= 4000):
            return False
        sha = hashlib.sha256(text.encode()).hexdigest()
        return (p.get("text_sha256") == sha and p.get("id") == passage_id(pack, source, section, text)
                and p.get("uri") == passage_uri(pack, source, section, sha)
                and p.get("scope") in ("house", "agency")
                and p.get("licence") in ("open", "licensed-internal", "client-confidential"))
    except (KeyError, TypeError):
        return False


class RetrievalPort:
    def __init__(self, base_url: str, token: str | None = None, timeout: float = 120.0, transport=None,
                 sleep=time.sleep):
        self.base = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._client = httpx.Client(timeout=timeout, transport=transport)
        self._sleep = sleep

    def _headers(self, scope: dict, attribution: dict) -> dict:
        h = {"X-Napkin-Org": scope["org"], "X-Napkin-Brand": scope["brand"],
             "X-Napkin-Handler": str(attribution.get("handler") or "-"),
             "X-Napkin-Job": str(attribution.get("job") or "-")}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _req(self, method, path, scope, attribution, body=None) -> dict:
        for attempt in (1, 2):
            try:
                r = self._client.request(method, self.base + path, json=body, headers=self._headers(scope, attribution))
            except httpx.TimeoutException as e:
                if attempt == 1:
                    continue
                raise RetrievalError(f"retrieval timed out after {self.timeout:.0f}s") from e
            except httpx.HTTPError as e:
                if attempt == 1:
                    continue
                raise RetrievalError(f"retrieval unreachable ({type(e).__name__})") from e
            if r.status_code in RETRY and attempt == 1:
                if r.status_code == 429:
                    try:
                        self._sleep(min(30.0, float(r.headers.get("retry-after", "1"))))
                    except ValueError:
                        self._sleep(1.0)
                continue
            break
        try:
            out = r.json()
        except ValueError:
            out = None
        if r.status_code != 200:
            etype = out["error"].get("type") if isinstance(out, dict) and isinstance(out.get("error"), dict) else ""
            raise RetrievalError(f"retrieval returned {r.status_code} {etype or ''}".strip())
        if not isinstance(out, dict) or "error" in out:
            raise RetrievalError("retrieval returned a malformed body")
        return out

    def packs(self, scope: dict, attribution: dict) -> list[dict]:
        out = self._req("GET", "/v1/packs", scope, attribution)
        packs = out.get("packs")
        if not isinstance(packs, list):
            raise RetrievalError("retrieval returned no packs list")
        return [p for p in packs if isinstance(p, dict) and isinstance(p.get("tag"), str)]

    def retrieve(self, scope: dict, attribution: dict, query: str, k: int, packs=None, where=None,
                 purpose: str | None = None) -> dict:
        body = {"query": re.sub(r"\s+", " ", query or "").strip()[:2000], "k": max(1, min(20, int(k)))}
        if not body["query"]:
            raise RetrievalError("an empty retrieval query")
        if packs:
            body["packs"] = list(dict.fromkeys(packs))
        if where:
            body["where"] = dict(where)
        if purpose:
            body["purpose"] = purpose
        out = self._req("POST", "/v1/retrieve", scope, attribution, body)
        ps = out.get("passages")
        if not isinstance(ps, list):
            raise RetrievalError("retrieval returned no passages list")
        tr = out.get("trace") if isinstance(out.get("trace"), dict) else {}
        versions = {k: v for k, v in (tr.get("packs") or {}).items() if isinstance(v, str)}
        good = [p for p in ps if passage_ok(p)]
        return {"passages": good, "dropped": len(ps) - len(good),
                "trace": {"backend": str(tr.get("backend") or ""), "packs": versions}}
