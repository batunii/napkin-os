"""The storage behind the tests' fake layers SERVICE (fake_layers.py).

Formerly `napkin/layers/local.py`: the middleware no longer opens a database
(peripherals.md §10 item 1); the tests serve `napkin.layers/1` over an
in-process transport from this store, so the middleware's own client
(`HttpLayers`) is what the tests exercise.

LocalLayers: the layers protocol on stdlib sqlite3.

Schema (typed value columns — no JSON value column — per the fact-envelope
cautions; `market` '' means market-independent):

  verticals(code PK, name, aliases, ordinal)
  categories(code PK, vertical, name, aliases, regulated, provisional, ordinal)
  sources(id PK, uri UNIQUE, publisher, title, tier, domain, licence, retrieved_at, published_at,
          added_by_org, added_at)
  facts(id PK, layer, scope_org, scope_brand, entity, key, market, version, value_type,
        value_num, value_text, value_bool, unit, as_of, retrieved_at, status, supersedes,
        superseded_by, licence, method, decision_id, written_by_org, created_at)
        UNIQUE(layer, scope_org, scope_brand, entity, key, market, version)
  fact_sources(fact_id, source_id, quote, added_at)  PK(fact_id, source_id)
  decisions(id PK, kind, handler, action, rationale, cites, scope_org, scope_brand, created_at)
  brands(ref, scope_org, name, updated_at)  PK(ref, scope_org)

Category-layer rows are shared (scope_org = scope_brand = ''); brand-layer
rows carry the bound (org, brand) and are only visible under it. Rows are
never updated except `status` / `superseded_by` on supersession or contest.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading

from napkin.util import iso, slug, uid
from napkin.layers import origin_uri
from taxonomy_seed import TREE

SCHEMA = """
CREATE TABLE IF NOT EXISTS verticals(code TEXT PRIMARY KEY, name TEXT NOT NULL, aliases TEXT NOT NULL,
  ordinal INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS categories(code TEXT PRIMARY KEY, vertical TEXT NOT NULL REFERENCES verticals(code),
  name TEXT NOT NULL, aliases TEXT NOT NULL, regulated INTEGER NOT NULL, provisional INTEGER NOT NULL,
  ordinal INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY, uri TEXT NOT NULL UNIQUE, publisher TEXT, title TEXT,
  tier TEXT NOT NULL CHECK (tier IN ('primary','secondary','tertiary','reviewer-verified')),
  domain TEXT NOT NULL, licence TEXT NOT NULL CHECK (licence IN ('open','licensed-internal','client-confidential')),
  retrieved_at TEXT, published_at TEXT, added_by_org TEXT NOT NULL, added_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS facts(id TEXT PRIMARY KEY, layer TEXT NOT NULL CHECK (layer IN ('brand','category')),
  scope_org TEXT NOT NULL, scope_brand TEXT NOT NULL, entity TEXT NOT NULL, key TEXT NOT NULL,
  market TEXT NOT NULL, version INTEGER NOT NULL CHECK (version >= 1),
  value_type TEXT NOT NULL CHECK (value_type IN ('number','text','boolean')),
  value_num REAL, value_text TEXT, value_bool INTEGER, unit TEXT NOT NULL, as_of TEXT NOT NULL,
  retrieved_at TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('active','contested','superseded')),
  supersedes TEXT, superseded_by TEXT, licence TEXT NOT NULL, method TEXT, decision_id TEXT NOT NULL,
  written_by_org TEXT NOT NULL, created_at TEXT NOT NULL,
  UNIQUE(layer, scope_org, scope_brand, entity, key, market, version));
CREATE INDEX IF NOT EXISTS facts_ident ON facts(layer, scope_org, scope_brand, entity, key, market);
CREATE TABLE IF NOT EXISTS fact_sources(fact_id TEXT NOT NULL REFERENCES facts(id),
  source_id TEXT NOT NULL REFERENCES sources(id), quote TEXT, added_at TEXT NOT NULL,
  PRIMARY KEY(fact_id, source_id));
CREATE TABLE IF NOT EXISTS decisions(id TEXT PRIMARY KEY, kind TEXT NOT NULL, handler TEXT, action TEXT,
  rationale TEXT, cites TEXT NOT NULL, scope_org TEXT NOT NULL, scope_brand TEXT NOT NULL,
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS brands(ref TEXT NOT NULL, scope_org TEXT NOT NULL, name TEXT NOT NULL,
  updated_at TEXT NOT NULL, PRIMARY KEY(ref, scope_org));
"""

ENTITY_RE = re.compile(r"^(brand|org|category)/[a-z0-9][a-z0-9._-]*$")
KEY_RE = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)*$")


class LocalLayerStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.executescript(SCHEMA)
            self._seed()

    def _seed(self):
        cur = self._db.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
        if cur:
            return
        self._db.execute("BEGIN")
        n = 0
        for vi, (vcode, vname, valiases, leaves) in enumerate(TREE):
            self._db.execute("INSERT INTO verticals VALUES (?,?,?,?)", (vcode, vname, json.dumps(valiases), vi))
            for lcode, lname, laliases, regulated, provisional in leaves:
                self._db.execute("INSERT INTO categories VALUES (?,?,?,?,?,?,?)",
                                 (f"{vcode}.{lcode}", vcode, lname, json.dumps(laliases), int(regulated),
                                  int(provisional), n))
                n += 1
        self._db.execute("COMMIT")

    def open(self, scope: dict) -> "LocalLayers":
        return LocalLayers(self, {"org": scope["org"], "brand": scope["brand"]})

    def close(self):
        with self._lock:
            self._db.close()


def _value_cols(v):
    if isinstance(v, bool):
        return "boolean", None, None, int(v)
    if isinstance(v, (int, float)):
        return "number", float(v), None, None
    if isinstance(v, str):
        return "text", None, v, None
    raise ValueError("a fact value is a number, a string or a boolean")


def _value_of(row) -> object:
    if row["value_type"] == "boolean":
        return bool(row["value_bool"])
    if row["value_type"] == "number":
        x = row["value_num"]
        return int(x) if float(x).is_integer() and abs(x) < 2**53 else x
    return row["value_text"]


def _same(a, b) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b or a == b and type(a) is type(b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return float(a) == float(b)
    return a == b


class LocalLayers:
    """One scope's view of the store. No method takes a scope."""

    def __init__(self, store: LocalLayerStore, scope: dict):
        self._s = store
        self._org, self._brand = scope["org"], scope["brand"]

    # -- helpers -------------------------------------------------------------
    def _q(self, sql, args=()):
        with self._s._lock:
            return self._s._db.execute(sql, args).fetchall()

    def _scope_cols(self, layer: str):
        return (self._org, self._brand) if layer == "brand" else ("", "")

    def _row(self, r) -> dict:
        srcs = self._q("SELECT s.*, fs.quote FROM fact_sources fs JOIN sources s ON s.id = fs.source_id "
                       "WHERE fs.fact_id = ? ORDER BY fs.added_at, s.id", (r["id"],))
        return {
            "id": r["id"], "layer": r["layer"], "entity": r["entity"], "key": r["key"],
            "market": r["market"] or None, "value": _value_of(r), "unit": r["unit"], "as_of": r["as_of"],
            "retrieved_at": r["retrieved_at"], "status": r["status"], "version": r["version"],
            "supersedes": r["supersedes"], "licence": r["licence"], "method": r["method"],
            "decision": r["decision_id"], "origin": origin_uri(r["layer"], r["entity"], r["key"], r["version"]),
            "sources": [s["id"] for s in srcs],
            "source_records": [{k: s[k] for k in ("id", "uri", "publisher", "title", "tier", "domain", "licence",
                                                  "retrieved_at", "published_at", "quote")} for s in srcs],
        }

    # -- category tree -------------------------------------------------------
    def leaves(self) -> list[dict]:
        rows = self._q("SELECT c.*, v.name AS vname FROM categories c JOIN verticals v ON v.code = c.vertical "
                       "ORDER BY c.ordinal")
        return [{"code": r["code"], "name": r["name"], "vertical": r["vertical"], "vertical_name": r["vname"],
                 "aliases": json.loads(r["aliases"]), "regulated": bool(r["regulated"]),
                 "provisional": bool(r["provisional"])} for r in rows]

    def vertical_of(self, leaf: str) -> dict | None:
        rows = self._q("SELECT v.* FROM categories c JOIN verticals v ON v.code = c.vertical WHERE c.code = ?",
                       (leaf,))
        if not rows:
            return None
        v = rows[0]
        return {"code": v["code"], "name": v["name"], "aliases": json.loads(v["aliases"]),
                "leaves": [r["code"] for r in self._q("SELECT code FROM categories WHERE vertical = ? "
                                                      "ORDER BY ordinal", (v["code"],))]}

    def find(self, text: str) -> list[str]:
        """Leaves a typed name matches: leaf codes, names and aliases first; a
        vertical's name or alias gives all of its leaves."""
        t = " " + re.sub(r"[^a-z0-9.+]+", " ", (text or "").lower()) + " "
        if not t.strip():
            return []

        def hit(word: str) -> bool:
            w = re.sub(r"[^a-z0-9.+]+", " ", word.lower()).strip()
            return bool(w) and f" {w} " in t

        leaves = self.leaves()
        found = [l["code"] for l in leaves
                 if hit(l["code"]) or hit(l["name"]) or any(hit(a) for a in l["aliases"])]
        if found:
            return found
        verts = self._q("SELECT * FROM verticals ORDER BY ordinal")
        for v in verts:
            if hit(v["code"]) or hit(v["name"]) or any(hit(a) for a in json.loads(v["aliases"])):
                found += [l["code"] for l in leaves if l["vertical"] == v["code"]]
        return list(dict.fromkeys(found))

    # -- sources ---------------------------------------------------------------
    def add_source(self, source: dict) -> str:
        uri = source["uri"]
        rows = self._q("SELECT id FROM sources WHERE uri = ?", (uri,))
        if rows:
            return rows[0]["id"]
        sid = source.get("id") or ("src_" + uid("", uri, n=12).lower())
        with self._s._lock:
            self._s._db.execute(
                "INSERT OR IGNORE INTO sources VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (sid, uri, source.get("publisher"), source.get("title"), source["tier"], source.get("domain") or "",
                 source.get("licence", "open"), source.get("retrieved_at"), source.get("published_at"),
                 self._org, iso()))
        return self._q("SELECT id FROM sources WHERE uri = ?", (uri,))[0]["id"]

    def sources(self, ids: list[str]) -> list[dict]:
        if not ids:
            return []
        rows = self._q(f"SELECT * FROM sources WHERE id IN ({','.join('?' * len(ids))})", tuple(ids))
        return [dict(r) for r in rows]

    # -- facts -------------------------------------------------------------------
    def facts(self, layer, entity, key=None, market=None, key_prefix=None) -> list[dict]:
        so, sb = self._scope_cols(layer)
        sql = ("SELECT * FROM facts WHERE layer = ? AND scope_org = ? AND scope_brand = ? AND entity = ? "
               "AND superseded_by IS NULL")
        args = [layer, so, sb, entity]
        if key is not None:
            sql += " AND key = ?"
            args.append(key)
        if key_prefix is not None:
            sql += " AND (key = ? OR key LIKE ?)"
            args += [key_prefix, key_prefix.replace("_", r"\_") + ".%"]
            sql = sql.replace("LIKE ?", "LIKE ? ESCAPE '\\'")
        if market is not None:
            sql += " AND market IN (?, '')"
            args.append(market)
        sql += " ORDER BY key, market, version"
        return [self._row(r) for r in self._q(sql, tuple(args))]

    def append(self, fact: dict, decision: dict) -> dict:
        """Write one fact with the decision that justifies it. Returns the layer
        row now current for that identity:
          no row          -> version 1, active (or contested when fact.status says so)
          same value      -> the existing row; the new sources are linked (corroboration)
          other value     -> a new version: supersedes when its as_of is newer,
                             otherwise both rows become contested (nothing is picked)
        """
        layer, entity, key = fact["layer"], fact["entity"], fact["key"]
        if layer not in ("brand", "category") or not ENTITY_RE.match(entity) or not KEY_RE.match(key):
            raise ValueError("a fact needs a layer, an entity ref and a dotted key")
        market = fact.get("market") or ""
        so, sb = self._scope_cols(layer)
        vt, vn, vtext, vb = _value_cols(fact["value"])
        t = iso()
        with self._s._lock:
            db = self._s._db
            db.execute("BEGIN IMMEDIATE")
            try:
                self._record_decision(db, decision)
                cur = db.execute("SELECT * FROM facts WHERE layer=? AND scope_org=? AND scope_brand=? AND entity=? "
                                 "AND key=? AND market=? AND superseded_by IS NULL ORDER BY version DESC",
                                 (layer, so, sb, entity, key, market)).fetchall()
                same = next((r for r in cur if _same(_value_of(r), fact["value"])), None)
                if same is not None:
                    fid = same["id"]
                else:
                    # Versions count per entity + key across markets, so the origin URI
                    # (which carries no market) names exactly one row.
                    top = db.execute("SELECT MAX(version) FROM facts WHERE layer=? AND scope_org=? AND "
                                     "scope_brand=? AND entity=? AND key=?",
                                     (layer, so, sb, entity, key)).fetchone()[0] or 0
                    version = top + 1
                    status = fact.get("status", "active")
                    supersedes = None
                    active = [r for r in cur if r["status"] == "active"]
                    if active and status != "contested":
                        prev = active[0]
                        if fact["as_of"] > prev["as_of"]:
                            supersedes = prev["id"]
                        else:
                            status = "contested"
                    if status == "contested":
                        for r in cur:
                            db.execute("UPDATE facts SET status='contested' WHERE id=?", (r["id"],))
                    fid = uid("f_", layer, so, sb, entity, key, market, version, fact["value"])
                    db.execute("INSERT INTO facts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               (fid, layer, so, sb, entity, key, market, version, vt, vn, vtext, vb,
                                fact["unit"], fact["as_of"], fact["retrieved_at"], status, supersedes, None,
                                fact.get("licence", "open"), fact.get("method"), decision["id"], self._org, t))
                    if supersedes:
                        db.execute("UPDATE facts SET status='superseded', superseded_by=? WHERE id=?",
                                   (fid, supersedes))
                quotes = fact.get("quotes") or {}
                for sid in fact.get("sources") or []:
                    db.execute("INSERT OR IGNORE INTO fact_sources VALUES (?,?,?,?)", (fid, sid, quotes.get(sid), t))
                db.execute("COMMIT")
            except Exception:
                db.execute("ROLLBACK")
                raise
        return self._row(self._q("SELECT * FROM facts WHERE id = ?", (fid,))[0])

    def _record_decision(self, db, d: dict):
        db.execute("INSERT OR IGNORE INTO decisions VALUES (?,?,?,?,?,?,?,?,?)",
                   (d["id"], d.get("kind", "pin"), d.get("handler"), d.get("action"), d.get("rationale"),
                    json.dumps(d.get("cites") or []), self._org, self._brand, iso()))

    def resolve(self, pin_uri: str) -> dict | None:
        m = re.fullmatch(r"fact://(brand|category)/(.+)/([a-z0-9_]+(?:\.[a-z0-9_]+)*)@(\d+)", pin_uri or "")
        if not m:
            return None
        layer, path, key, version = m.group(1), m.group(2), m.group(3), int(m.group(4))
        entity = path if re.match(r"^(brand|org|category)/", path) else f"{layer}/{path}"
        so, sb = self._scope_cols(layer)
        rows = self._q("SELECT * FROM facts WHERE layer=? AND scope_org=? AND scope_brand=? AND entity=? AND key=? "
                       "AND version=?", (layer, so, sb, entity, key, version))
        return self._row(rows[0]) if rows else None

    # -- roster ------------------------------------------------------------------
    def roster(self, brand_ref: str) -> dict | None:
        rows = [r for r in self.facts("brand", brand_ref, key_prefix="roster") if r["status"] == "active"]
        if not rows:
            return None
        by = {r["key"]: r for r in rows}
        cats = [by[k]["value"] for k in ("roster.categories.primary", "roster.categories.secondary") if k in by]
        name = self._q("SELECT name FROM brands WHERE ref=? AND scope_org=?", (brand_ref, self._org))
        return {"ref": brand_ref, "name": name[0]["name"] if name else None, "categories": cats,
                "client_org": by["roster.client_org"]["value"] if "roster.client_org" in by else None,
                "facts": rows}

    def set_roster(self, brand_ref, name, categories, decision, sources, client_org=None) -> list[dict]:
        day = iso()[:10]
        rows = []
        keys = ["roster.categories.primary", "roster.categories.secondary"]
        for k, v in zip(keys, categories[:2]):
            rows.append(self.append({"layer": "brand", "entity": brand_ref, "key": k, "value": v, "unit": "code",
                                     "as_of": day, "retrieved_at": day, "sources": sources,
                                     "licence": "client-confidential", "method": "report"}, decision))
        if client_org:
            rows.append(self.append({"layer": "brand", "entity": brand_ref, "key": "roster.client_org",
                                     "value": client_org, "unit": "code", "as_of": day, "retrieved_at": day,
                                     "sources": sources, "licence": "client-confidential", "method": "report"},
                                    decision))
        self.note_brand(brand_ref, name)
        return rows

    def note_brand(self, brand_ref: str, name: str):
        if not name:
            return
        with self._s._lock:
            self._s._db.execute("INSERT INTO brands VALUES (?,?,?,?) ON CONFLICT(ref, scope_org) DO UPDATE SET "
                                "name=excluded.name, updated_at=excluded.updated_at",
                                (brand_ref, self._org, name, iso()))

    def find_brands(self, text: str) -> list[tuple[str, str]]:
        t = slug(text)
        if len(t) < 2:
            return []
        rows = self._q("SELECT ref, name FROM brands WHERE scope_org = ? ORDER BY name", (self._org,))
        return [(r["ref"], r["name"]) for r in rows
                if slug(r["name"]) == t or (len(t) >= 3 and (t in slug(r["name"]) or slug(r["name"]) in t))]
