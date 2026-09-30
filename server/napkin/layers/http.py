"""HttpLayers: the `Layers` protocol over `napkin.layers/1` (peripherals.md §4).

The knowledge layers are a peripheral. This client implements the same
protocol handlers already use, so no handler changes: every method is one
route. Scope travels in headers set in ONE place (`_headers`), from the scope
the store was opened with — never from a body, never from the caller.

Writes carry a deterministic `Idempotency-Key` (§4.5), so a retried step
sends the same key. Reads are retried once on a connection error, 429, 502,
503 or 504; writes likewise, with the same key. A lookup's 404 becomes
`None`; any other failure raises `LayersError` — never an empty answer.
"""

from __future__ import annotations

import hashlib
import json
import time
from urllib.parse import quote

import httpx


class LayersError(Exception):
    pass


RETRY = {429, 502, 503, 504}
# Seconds waited before the second and before the third attempt at a call that failed to connect or was
# answered 502/503/504. Writes are safe to repeat: each carries a deterministic Idempotency-Key.
PAUSES = (1.0, 3.0)


def _canon(v) -> str:
    return json.dumps(v, sort_keys=True, ensure_ascii=False)


def idem(*parts) -> str:
    return hashlib.sha256("\n".join(str(p) for p in parts).encode()).hexdigest()


class HttpLayerStore:
    """`open(scope)` binds a scope and returns an `HttpLayers`."""

    def __init__(self, base_url: str, token: str | None = None, timeout: float = 30.0, transport=None,
                 sleep=time.sleep):
        if not base_url:
            raise ValueError("NAPKIN_LAYERS_URL is required")
        self.base = base_url.rstrip("/")
        self.token = token
        self._client = httpx.Client(timeout=timeout, transport=transport)
        self._sleep = sleep

    def open(self, scope: dict, attribution: dict | None = None) -> "HttpLayers":
        return HttpLayers(self, {"org": scope["org"], "brand": scope["brand"]}, attribution)


class HttpLayers:
    """One scope's view of the layers service. No method takes a scope."""

    def __init__(self, store: HttpLayerStore, scope: dict, attribution: dict | None = None):
        self._s, self._scope = store, scope
        self._attr = attribution if attribution is not None else {"handler": "-", "job": "-"}
        self._leaves = None

    # -- transport -----------------------------------------------------------
    def _headers(self, key: str | None = None) -> dict:
        h = {"X-Napkin-Org": self._scope["org"], "X-Napkin-Brand": self._scope["brand"],
             "X-Napkin-Handler": str(self._attr.get("handler") or "-"), "X-Napkin-Job": str(self._attr.get("job") or "-")}
        if self._s.token:
            h["Authorization"] = f"Bearer {self._s.token}"
        if key:
            h["Idempotency-Key"] = key
        return h

    def _req(self, method: str, path: str, *, params=None, body=None, key=None, missing: set | None = None):
        url = self._s.base + path
        for attempt in (1, 2, 3):
            try:
                r = self._s._client.request(method, url, params=params, json=body, headers=self._headers(key))
            except httpx.TimeoutException as e:
                if attempt == 1:
                    continue  # a timeout has already waited its full time: one more try, no pause, never a third
                raise LayersError(f"layers {method} {path.split('?')[0]} timed out") from e
            except httpx.HTTPError as e:
                if attempt < 3:
                    self._s._sleep(PAUSES[attempt - 1])
                    continue
                raise LayersError(f"layers unreachable ({type(e).__name__})") from e
            if r.status_code in RETRY and attempt < 3:
                if r.status_code == 429:
                    try:
                        self._s._sleep(min(30.0, float(r.headers.get("retry-after", "1"))))
                    except ValueError:
                        self._s._sleep(1.0)
                else:
                    self._s._sleep(PAUSES[attempt - 1])
                continue
            break
        etype = ""
        try:
            out = r.json()
        except ValueError:
            out = None
        if r.status_code != 200:
            if isinstance(out, dict) and isinstance(out.get("error"), dict):
                etype = str(out["error"].get("type") or "")
            if r.status_code == 404 and missing and etype in missing:
                return None
            raise LayersError(f"layers {method} {path.split('?')[0]} returned {r.status_code} {etype}".strip())
        if not isinstance(out, dict) or "error" in out:
            raise LayersError(f"layers {method} {path.split('?')[0]} returned a malformed body")
        return out

    # -- category tree -------------------------------------------------------
    def leaves(self) -> list[dict]:
        if self._leaves is None:
            out = self._req("GET", "/v1/layers/categories")
            self._leaves = list(out.get("leaves") or [])
        return [dict(x) for x in self._leaves]

    def vertical_of(self, leaf: str) -> dict | None:
        return self._req("GET", f"/v1/layers/categories/{quote(leaf, safe='')}/vertical", missing={"unknown_leaf"})

    def find(self, text: str) -> list[str]:
        return list(self._req("POST", "/v1/layers/categories/find", body={"text": text or ""}).get("leaves") or [])

    # -- facts ---------------------------------------------------------------
    def facts(self, layer: str, entity: str, key: str | None = None, market: str | None = None,
              key_prefix: str | None = None) -> list[dict]:
        params = {k: v for k, v in (("layer", layer), ("entity", entity), ("key", key), ("market", market),
                                     ("key_prefix", key_prefix)) if v is not None}
        return list(self._req("GET", "/v1/layers/facts", params=params).get("facts") or [])

    def append(self, fact: dict, decision: dict) -> dict:
        f = {k: v for k, v in fact.items() if v is not None or k == "market"}
        if f.get("market") is None:
            f.pop("market", None)
        key = idem(decision.get("id"), f["layer"] + f["entity"] + f["key"] + (f.get("market") or ""),
                   _canon(f["value"]))
        out = self._req("POST", "/v1/layers/facts", body={"fact": f, "decision": decision}, key=key)
        if not isinstance(out.get("fact"), dict):
            raise LayersError("layers append returned no fact")
        return out["fact"]

    def resolve(self, pin_uri: str) -> dict | None:
        out = self._req("GET", "/v1/layers/facts/by-uri", params={"uri": pin_uri}, missing={"unknown_fact"})
        return out.get("fact") if out else None

    # -- sources -------------------------------------------------------------
    def add_source(self, source: dict) -> str:
        src = {k: v for k, v in source.items() if v is not None and k != "id"}
        # Keyed by the whole body, not the URI: two researchers citing one URL
        # with different titles are two requests, and the service's own rule
        # (one row per URI, `created: false`) makes the second a no-op. A
        # URI-only key made the second a 409 idempotency_conflict.
        out = self._req("POST", "/v1/layers/sources", body={"source": src},
                        key=idem("source", json.dumps(src, sort_keys=True)))
        if not isinstance(out.get("id"), str):
            raise LayersError("layers add_source returned no id")
        return out["id"]

    def sources(self, ids: list[str]) -> list[dict]:
        if not ids:
            return []
        return list(self._req("GET", "/v1/layers/sources", params={"ids": ",".join(ids)}).get("sources") or [])

    # -- the brand roster ----------------------------------------------------
    def roster(self, brand_ref: str) -> dict | None:
        return self._req("GET", f"/v1/layers/roster/{brand_ref}", missing={"unknown_brand"})

    def set_roster(self, brand_ref: str, name: str, categories: list[str], decision: dict, sources: list[str],
                   client_org=None) -> list[dict]:
        body = {"name": name, "categories": list(categories)[:2], "sources": list(sources), "decision": decision}
        if isinstance(client_org, dict):
            client_org = client_org.get("ref")
        if isinstance(client_org, str) and client_org:
            body["client_org"] = client_org
        out = self._req("PUT", f"/v1/layers/roster/{brand_ref}", body=body,
                        key=idem(decision.get("id"), brand_ref))
        return list(out.get("facts") or [])

    def find_brands(self, text: str) -> list[tuple[str, str]]:
        out = self._req("POST", "/v1/layers/brands/find", body={"text": text or ""})
        return [(b["ref"], b["name"]) for b in out.get("brands") or [] if isinstance(b, dict)]

    def note_brand(self, brand_ref: str, name: str) -> None:
        if not name:
            return
        self._req("PUT", f"/v1/layers/brands/{brand_ref}", body={"name": name}, key=idem("brand", brand_ref, name))
