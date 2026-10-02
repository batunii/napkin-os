"""The research port: `POST <NAPKIN_RESEARCH_URL>/v1/research`.

Request  {query, lens, market, entity?, category?, max_sources?}
Response {sources: [{id, url, publisher, title, retrieved_at, published_at?,
                     excerpts: [{quote}]}], trace: {backend, queries}}

The port returns sources and verbatim excerpts only — no facts, no
confidence, no tiers; the middleware does that. A non-2xx, a malformed body
or a timeout raises ResearchError (the unit becomes a gap), never an empty
success. An honest `sources: []` is a valid answer.

No scope headers: the query is a public-web question and the org is not sent
(peripherals.md §2.1, O8). Attribution headers only.
"""

from __future__ import annotations

import re

import httpx


class ResearchError(Exception):
    pass


class ResearchPort:
    def __init__(self, base_url: str, timeout: float, transport=None, token: str | None = None):
        self.token = token
        self.base = base_url.rstrip("/")
        if self.base.endswith("/v1/research"):
            self.base = self.base[: -len("/v1/research")]
        self.timeout = timeout
        self._client = httpx.Client(timeout=timeout, transport=transport)

    def search(self, query: str, lens: str, market: str, entity: str | None = None,
               category: str | None = None, max_sources: int = 6, attribution: dict | None = None) -> dict:
        body = {"query": query, "lens": lens, "market": market, "max_sources": max_sources}
        if entity:
            body["entity"] = entity
        if category:
            body["category"] = category
        attribution = attribution or {}
        headers = {"X-Napkin-Handler": str(attribution.get("handler") or "-"),
                   "X-Napkin-Job": str(attribution.get("job") or "-")}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            r = self._client.post(self.base + "/v1/research", json=body, headers=headers)
        except httpx.TimeoutException as e:
            raise ResearchError(f"research timed out after {self.timeout:.0f}s") from e
        except httpx.HTTPError as e:
            raise ResearchError(f"research unreachable ({type(e).__name__})") from e
        if r.status_code != 200:
            raise ResearchError(f"research returned {r.status_code}")
        try:
            out = r.json()
        except ValueError as e:
            raise ResearchError("research returned a body that is not JSON") from e
        srcs = out.get("sources") if isinstance(out, dict) else None
        if not isinstance(srcs, list):
            raise ResearchError("research returned no sources list")
        clean = []
        for s in srcs:
            if not isinstance(s, dict) or not isinstance(s.get("url"), str) or \
                    not re.match(r"https?://", s["url"]):
                continue
            ex = [e["quote"] for e in (s.get("excerpts") or []) if isinstance(e, dict)
                  and isinstance(e.get("quote"), str) and e["quote"].strip()]
            if not ex:
                continue
            clean.append({"id": str(s.get("id") or ""), "url": s["url"], "publisher": s.get("publisher") or "",
                          "title": s.get("title") or "", "retrieved_at": str(s.get("retrieved_at") or "")[:10],
                          "published_at": (str(s["published_at"])[:10] if s.get("published_at") else None),
                          "excerpts": ex})
        tr = out.get("trace") if isinstance(out.get("trace"), dict) else {}
        cost = tr.get("cost_usd")
        return {"sources": clean, "trace": {"backend": str(tr.get("backend") or ""),
                                            "queries": [q for q in tr.get("queries") or [] if isinstance(q, str)],
                                            # what the service says the search cost, and whether it was a cache hit
                                            # (the run log's; neither steers anything)
                                            "cost_usd": float(cost) if isinstance(cost, (int, float)) else None,
                                            "cached": bool(tr.get("cached")) if "cached" in tr else None}}
