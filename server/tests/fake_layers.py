"""A fake layers SERVICE for tests: `napkin.layers/1` (peripherals.md §4) served
in-process through an `httpx.MockTransport`, so the middleware's real client
(`HttpLayers`) is exercised end to end with no network and no mock process.

It applies the contract's rules a client can trip over: scope from headers
only (missing -> 400 missing_scope, scope in the body -> 400), an
`Idempotency-Key` on every write (replay -> the first response with
`Idempotent-Replay: true`, same key + another body -> 409), `licence`
required on facts and sources, and — with `enforce_leaves=True` — an unknown
category leaf refused (§4.4). Off by default: the napkin.middleware/1 contract
suite's fixtures research `drinks.soft_drinks` / `drinks.mixers`, which are not
leaves of the tree, so a layers service applying §4.4 refuses those appends
(a defect reported against the suite's fixtures, not edited out of it).
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from urllib.parse import unquote

import httpx

from layers_backing import LocalLayerStore

SCOPE_KEYS = ("scope", "org", "org_id", "tenant", "tenant_id", "brand_scope")


def _err(status, etype, msg="refused"):
    return httpx.Response(status, json={"error": {"type": etype, "message": msg}})


class FakeLayersService:
    def __init__(self, path: str = ":memory:", enforce_leaves: bool = False):
        self.store = LocalLayerStore(path)
        self.enforce_leaves = enforce_leaves
        self.requests: list[httpx.Request] = []
        self._idem: dict[str, tuple[str, int, dict]] = {}
        self._lock = threading.Lock()
        self.fail_next: list[int] = []  # statuses to answer before serving (retry tests)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_next:
            return _err(self.fail_next.pop(0), "unavailable")
        org, brand = request.headers.get("x-napkin-org"), request.headers.get("x-napkin-brand")
        if not org:
            return _err(400, "missing_scope")
        body = None
        if request.content:
            try:
                body = json.loads(request.content)
            except ValueError:
                return _err(400, "invalid_input")
            if isinstance(body, dict) and any(k in body for k in SCOPE_KEYS):
                return _err(400, "invalid_input", "scope in the body")
        write = request.method in ("POST", "PUT") and not request.url.path.endswith("/find")
        key = request.headers.get("idempotency-key")
        if write:
            if not key:
                return _err(400, "invalid_input", "a write needs an Idempotency-Key")
            sha = hashlib.sha256(request.content).hexdigest()
            with self._lock:
                hit = self._idem.get(key)
            if hit:
                if hit[0] != sha:
                    return _err(409, "idempotency_conflict")
                return httpx.Response(hit[1], json=hit[2], headers={"Idempotent-Replay": "true"})
        try:
            status, out = self.route(request, body, org, brand)
        except ValueError as e:
            status, out = 400, {"error": {"type": "invalid_input", "message": str(e)[:100]}}
        if write and status == 200:
            with self._lock:
                self._idem[key] = (hashlib.sha256(request.content).hexdigest(), status, out)
        return httpx.Response(status, json=out)

    # ------------------------------------------------------------------------------------------
    def route(self, request, body, org, brand):
        L = self.store.open({"org": org, "brand": brand or ""})
        path, m, q = request.url.path, request.method, request.url.params
        need_brand = lambda: not brand
        if path == "/v1/layers/categories" and m == "GET":
            return 200, {"taxonomy_version": "0.1", "leaves": L.leaves()}
        if (x := re.fullmatch(r"/v1/layers/categories/([^/]+)/vertical", path)) and m == "GET":
            v = L.vertical_of(unquote(x.group(1)))
            return (200, v) if v else (404, {"error": {"type": "unknown_leaf", "message": "no such leaf"}})
        if path == "/v1/layers/categories/find" and m == "POST":
            return 200, {"leaves": L.find(body.get("text", ""))}
        if path == "/v1/layers/facts" and m == "GET":
            if q.get("layer") == "brand" and need_brand():
                return 400, {"error": {"type": "missing_scope", "message": "brand"}}
            return 200, {"facts": L.facts(q["layer"], q["entity"], key=q.get("key"), market=q.get("market"),
                                          key_prefix=q.get("key_prefix"))}
        if path == "/v1/layers/facts" and m == "POST":
            f, d = body.get("fact") or {}, body.get("decision") or {}
            if f.get("layer") == "brand" and need_brand():
                return 400, {"error": {"type": "missing_scope", "message": "brand"}}
            if "licence" not in f:
                raise ValueError("licence is required")
            if isinstance(f.get("value"), (dict, list)) or not d.get("id") or not d.get("kind"):
                raise ValueError("bad fact or decision")
            if self.enforce_leaves and f.get("layer") == "category" and \
                    str(f.get("entity", "")).startswith("category/"):
                leaf = f["entity"].split("/", 1)[1]
                if leaf not in {l["code"] for l in L.leaves()}:
                    return 400, {"error": {"type": "unknown_leaf", "message": "unknown leaf"}}
            before = {r["id"] for r in L.facts(f["layer"], f["entity"], key=f["key"])}
            row = L.append(f, d)
            outcome = "corroborated" if row["id"] in before else (
                "superseded" if row.get("supersedes") else "contested" if row["status"] == "contested" else "created")
            return 200, {"fact": row, "outcome": outcome}
        if path == "/v1/layers/facts/by-uri" and m == "GET":
            row = L.resolve(q.get("uri", ""))
            return (200, {"fact": row}) if row else (404, {"error": {"type": "unknown_fact", "message": "no"}})
        if path == "/v1/layers/sources" and m == "POST":
            s = body.get("source") or {}
            if "licence" not in s or not s.get("uri") or not s.get("tier"):
                raise ValueError("a source needs uri, tier and licence")
            existed = bool(L._q("SELECT id FROM sources WHERE uri = ?", (s["uri"],)))
            return 200, {"id": L.add_source(s), "created": not existed}
        if path == "/v1/layers/sources" and m == "GET":
            ids = [i for i in (q.get("ids") or "").split(",") if i]
            got = {r["id"]: r for r in L.sources(ids)}
            return 200, {"sources": [got[i] for i in ids if i in got
                                     and (got[i]["licence"] != "client-confidential" or got[i]["added_by_org"] == org)]}
        if (x := re.fullmatch(r"/v1/layers/roster/(.+)", path)):
            if need_brand():
                return 400, {"error": {"type": "missing_scope", "message": "brand"}}
            ref = x.group(1)
            if m == "GET":
                r = L.roster(ref)
                return (200, r) if r else (404, {"error": {"type": "unknown_brand", "message": "no"}})
            if m == "PUT":
                rows = L.set_roster(ref, body.get("name"), body.get("categories") or [], body["decision"],
                                    body.get("sources") or [], body.get("client_org"))
                return 200, {"facts": rows}
        if path == "/v1/layers/brands/find" and m == "POST":
            return 200, {"brands": [{"ref": r, "name": n} for r, n in L.find_brands(body.get("text", ""))]}
        if (x := re.fullmatch(r"/v1/layers/brands/(.+)", path)) and m == "PUT":
            L.note_brand(x.group(1), body.get("name"))
            return 200, {"ref": x.group(1), "name": body.get("name")}
        return 404, {"error": {"type": "not_found", "message": "no such route"}}
