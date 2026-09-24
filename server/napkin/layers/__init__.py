"""The layers port: the knowledge layers behind a Python protocol.

`LayerStore.open(scope)` binds a scope (from auth, never from a request) and
returns a `Layers` whose methods take no scope argument: category facts are
shared, brand facts are read and written under the bound (org, brand).

Implementations: `LocalLayers` (stdlib sqlite3, now); Postgres with RLS
(W2-B1, later) implements the same protocol. Nothing here is SQLite-specific:
values cross the boundary as plain dicts.

Fact rows (the fact envelope, Contract 3 §4, as the layer holds it):
  {id, layer, entity, key, market|None, value, unit, as_of, retrieved_at,
   status: active|contested|superseded, version, supersedes, licence, method,
   decision, sources: [src ids], source_records: [{id, uri, tier, domain, licence, ...}]}
"""

from __future__ import annotations

from typing import Protocol


class Layers(Protocol):
    # category tree
    def leaves(self) -> list[dict]: ...
    def vertical_of(self, leaf: str) -> dict | None: ...
    def find(self, text: str) -> list[str]: ...

    # facts (append-only; supersession inserts and flips status)
    def facts(self, layer: str, entity: str, key: str | None = None, market: str | None = None,
              key_prefix: str | None = None) -> list[dict]: ...
    def append(self, fact: dict, decision: dict) -> dict: ...
    def resolve(self, pin_uri: str) -> dict | None: ...

    # sources
    def add_source(self, source: dict) -> str: ...
    def sources(self, ids: list[str]) -> list[dict]: ...

    # the brand roster
    def roster(self, brand_ref: str) -> dict | None: ...
    def set_roster(self, brand_ref: str, name: str, categories: list[str], decision: dict,
                   sources: list[str], client_org: dict | None = None) -> list[dict]: ...
    def find_brands(self, text: str) -> list[tuple[str, str]]: ...
    def note_brand(self, brand_ref: str, name: str) -> None: ...


class LayerStore(Protocol):
    def open(self, scope: dict) -> Layers: ...


def open_store(spec: str) -> LayerStore:
    """`NAPKIN_LAYERS`: `local:/path/layers.sqlite` (later `postgres://…`)."""
    if spec.startswith("local:"):
        from .local import LocalLayerStore
        return LocalLayerStore(spec[len("local:"):])
    raise ValueError(f"NAPKIN_LAYERS: no layers implementation for {spec.split(':')[0]!r}")


def origin_uri(layer: str, entity: str, key: str, version: int) -> str:
    """fact://<layer>/<entity path>/<key>@<version>; the entity's type segment
    is dropped when it equals the layer (Contract 3 §4)."""
    typ, _, rest = entity.partition("/")
    path = rest if typ == layer else entity
    return f"fact://{layer}/{path}/{key}@{version}"
