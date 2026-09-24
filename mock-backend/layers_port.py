"""The layers port — `napkin.layers/1` (contract 5, §4), on stdlib sqlite3. No model.

This is `server/napkin/layers/local.py` (LocalLayers) moved behind HTTP, with
the fixes contract 5 makes:

  * `licence` is required on a fact and on a source — no default (a default of
    `open` silently declassified, C2);
  * a category-layer fact on `category/<leaf>` whose leaf the tree does not
    know is `400 unknown_leaf`, and so is a roster category;
  * a `client-confidential` source is visible only to the org that added it
    (sources(ids), a fact's source links, and the ids an append may cite);
  * the roster write — every roster fact, its decision and the brand's name —
    is one transaction, and so is an append (fact + decision + source links +
    status flips: P2 met by one request, §4.6);
  * writes carry `Idempotency-Key`; a replay returns the first response with
    `Idempotent-Replay: true`, the same key with another body is 409.

The append rule for one identity (layer, scope, entity, key, market) is
exactly LocalLayers.append (§4.4): created / corroborated / superseded /
contested. It lives here, inside the write transaction (owner decision O3,
and O4 for the roster key mapping).

Scope comes only from the X-Napkin-Org / X-Napkin-Brand headers. The service
never computes, widens or defaults it.

In-process use (the middleware's tests, §8.5): `LayersApp(path).handle(Request)`
answers exactly as the HTTP routes do, with no socket.
"""

from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import json
import math
import re
import sqlite3
import threading
import unicodedata
from pathlib import Path

from common import REPO, PeripheralError, Request, Response, parse_json_object, refuse_scope_in_body

API = "napkin.layers/1"
TAXONOMY_FILE = REPO / "docs" / "contracts" / "peripherals" / "taxonomy.json"
IDEMPOTENCY_TTL = _dt.timedelta(hours=24)

SCHEMA = """
CREATE TABLE IF NOT EXISTS verticals(code TEXT PRIMARY KEY, name TEXT NOT NULL, aliases TEXT NOT NULL,
  ordinal INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS categories(code TEXT PRIMARY KEY, vertical TEXT NOT NULL REFERENCES verticals(code),
  name TEXT NOT NULL, aliases TEXT NOT NULL, regulated INTEGER NOT NULL, provisional INTEGER NOT NULL,
  ordinal INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
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
  created_at TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS brands(ref TEXT NOT NULL, scope_org TEXT NOT NULL, name TEXT NOT NULL,
  updated_at TEXT NOT NULL, PRIMARY KEY(ref, scope_org));
CREATE TABLE IF NOT EXISTS idempotency(scope_org TEXT NOT NULL, key TEXT NOT NULL, body_sha256 TEXT NOT NULL,
  status INTEGER NOT NULL, response TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(scope_org, key));
"""

ENTITY_RE = re.compile(r"^(brand|org|category)/[a-z0-9][a-z0-9._-]*$")
BRAND_RE = re.compile(r"^brand/[a-z0-9][a-z0-9._-]*$")
ORG_RE = re.compile(r"^org/[a-z0-9][a-z0-9._-]*$")
KEY_RE = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)*$")
LEAF_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
MARKET_RE = re.compile(r"^[A-Z]{2}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PIN_RE = re.compile(r"fact://(brand|category)/(.+)/([a-z0-9_]+(?:\.[a-z0-9_]+)*)@([1-9][0-9]*)")
TIERS = ("primary", "secondary", "tertiary", "reviewer-verified")
LICENCES = ("open", "licensed-internal", "client-confidential")
METHODS = ("observation", "report", "measurement", "synthesis")
FACT_FIELDS = {"layer", "entity", "key", "market", "value", "unit", "as_of", "retrieved_at", "sources", "quotes",
               "licence", "method", "status"}
FACT_REQUIRED = {"layer", "entity", "key", "value", "unit", "as_of", "retrieved_at", "sources", "licence"}
SOURCE_FIELDS = {"uri", "tier", "domain", "licence", "publisher", "title", "retrieved_at", "published_at"}
ROSTER_FIELDS = {"name", "categories", "client_org", "sources", "decision"}
ROSTER_KEYS = ("roster.categories.primary", "roster.categories.secondary")


# ------------------------------------------------------------------ small pieces (server/napkin/util.py)


def _digest(*parts) -> bytes:
    h = hashlib.sha256()
    for p in parts:
        h.update(json.dumps(p, sort_keys=True, ensure_ascii=False, default=str).encode())
        h.update(b"\x1f")
    return h.digest()


def uid(prefix: str, *parts, n: int = 12) -> str:
    """Opaque id ^<prefix>[0-9A-Z]{n}$ seeded by parts — the ids LocalLayers made."""
    return prefix + base64.b32encode(_digest(*parts)).decode()[:n].replace("=", "A")


def iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slug(text: str) -> str:
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")


def origin_uri(layer: str, entity: str, key: str, version: int) -> str:
    """fact://<layer>/<entity path>/<key>@<version>; the entity's type segment is
    dropped when it equals the layer (Contract 3 §4)."""
    typ, _, rest = entity.partition("/")
    path = rest if typ == layer else entity
    return f"fact://{layer}/{path}/{key}@{version}"


def _value_cols(v):
    if isinstance(v, bool):
        return "boolean", None, None, int(v)
    if isinstance(v, (int, float)):
        return "number", float(v), None, None
    return "text", None, v, None


def _value_of(row):
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


def bad(message: str, type_: str = "invalid_input") -> PeripheralError:
    return PeripheralError(400, type_, message)


def _date(v, name: str, nullable: bool = False):
    if v is None and nullable:
        return None
    if not isinstance(v, str) or not DATE_RE.match(v):
        raise bad(f"{name} must be a YYYY-MM-DD date")
    try:
        _dt.date.fromisoformat(v)
    except ValueError:
        raise bad(f"{name} is not a real date") from None
    return v


def _check_decision(d) -> dict:
    if not isinstance(d, dict):
        raise bad("decision must be an object")
    if not isinstance(d.get("id"), str) or not d["id"].startswith("d_") or len(d["id"]) > 128:
        raise bad("decision.id is required (d_…)")
    if not isinstance(d.get("kind"), str) or not d["kind"]:
        raise bad("decision.kind is required")
    for k in ("handler", "action", "rationale"):
        if k in d and d[k] is not None and not isinstance(d[k], str):
            raise bad(f"decision.{k} must be a string")
    if "cites" in d and not (isinstance(d["cites"], list) and all(isinstance(c, str) for c in d["cites"])):
        raise bad("decision.cites must be a list of strings")
    if "reasoning" in d and d["reasoning"] is not None and not isinstance(d["reasoning"], dict):
        raise bad("decision.reasoning must be an object")
    return d


# ------------------------------------------------------------------ the store


class LayersApp:
    def __init__(self, path: str, taxonomy: Path = TAXONOMY_FILE):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            if path != ":memory:":
                self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.executescript(SCHEMA)
            self._seed(taxonomy)
        self.taxonomy_version = self._q("SELECT value FROM meta WHERE key='taxonomy_version'")[0]["value"]

    def close(self):
        with self._lock:
            self._db.close()

    def _seed(self, taxonomy: Path):
        if self._db.execute("SELECT COUNT(*) FROM categories").fetchone()[0]:
            return
        tree = json.loads(taxonomy.read_text(encoding="utf-8"))
        self._db.execute("BEGIN")
        n = 0
        for vi, v in enumerate(tree["verticals"]):
            self._db.execute("INSERT INTO verticals VALUES (?,?,?,?)", (v["code"], v["name"],
                                                                         json.dumps(v["aliases"]), vi))
            for leaf in v["leaves"]:
                self._db.execute("INSERT INTO categories VALUES (?,?,?,?,?,?,?)",
                                 (f"{v['code']}.{leaf['code']}", v["code"], leaf["name"], json.dumps(leaf["aliases"]),
                                  int(leaf["regulated"]), int(leaf["provisional"]), n))
                n += 1
        self._db.execute("INSERT OR REPLACE INTO meta VALUES ('taxonomy_version', ?)", (tree["taxonomy_version"],))
        self._db.execute("COMMIT")

    def health(self) -> dict:
        return {"api": API, "backend": "sqlite", "taxonomy_version": self.taxonomy_version}

    def _q(self, sql, args=()):
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    # -- scope ---------------------------------------------------------------
    @staticmethod
    def _org(req: Request) -> str:
        org = req.header("x-napkin-org")
        if not org:
            raise PeripheralError(400, "missing_scope", "X-Napkin-Org is required")
        if not ORG_RE.match(org):
            raise bad("X-Napkin-Org must be org/<slug>")
        return org

    @staticmethod
    def _brand(req: Request, required: bool) -> str | None:
        brand = req.header("x-napkin-brand")
        if not brand:
            if required:
                raise PeripheralError(400, "missing_scope", "X-Napkin-Brand is required for the brand layer")
            return None
        if not BRAND_RE.match(brand):
            raise bad("X-Napkin-Brand must be brand/<slug>")
        return brand

    @staticmethod
    def _scope_cols(layer: str, org: str, brand: str | None):
        return (org, brand or "") if layer == "brand" else ("", "")

    # -- rows ----------------------------------------------------------------
    def _visible_source(self, s, org: str) -> bool:
        return s["licence"] != "client-confidential" or s["added_by_org"] == org

    def _row(self, db, r, org: str) -> dict:
        srcs = [s for s in db.execute(
            "SELECT s.*, fs.quote FROM fact_sources fs JOIN sources s ON s.id = fs.source_id "
            "WHERE fs.fact_id = ? ORDER BY fs.added_at, s.id", (r["id"],)).fetchall() if self._visible_source(s, org)]
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

    def known_leaf(self, db, code: str) -> bool:
        return db.execute("SELECT 1 FROM categories WHERE code = ?", (code,)).fetchone() is not None

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
        """LocalLayers.find: leaf codes, names and aliases as whole words first;
        else every leaf of a matching vertical."""
        t = " " + re.sub(r"[^a-z0-9.+]+", " ", (text or "").lower()) + " "
        if not t.strip():
            return []

        def hit(word: str) -> bool:
            w = re.sub(r"[^a-z0-9.+]+", " ", word.lower()).strip()
            return bool(w) and f" {w} " in t

        leaves = self.leaves()
        found = [l["code"] for l in leaves if hit(l["code"]) or hit(l["name"]) or any(hit(a) for a in l["aliases"])]
        if found:
            return found
        for v in self._q("SELECT * FROM verticals ORDER BY ordinal"):
            if hit(v["code"]) or hit(v["name"]) or any(hit(a) for a in json.loads(v["aliases"])):
                found += [l["code"] for l in leaves if l["vertical"] == v["code"]]
        return list(dict.fromkeys(found))

    # -- sources ---------------------------------------------------------------
    def _check_source(self, body: dict) -> dict:
        if set(body) != {"source"}:
            raise bad("the body is {source: {...}}")
        s = body["source"]
        if not isinstance(s, dict):
            raise bad("source must be an object")
        extra = sorted(set(s) - SOURCE_FIELDS)
        if extra:
            raise bad(f"source: unknown field(s) {', '.join(extra)}")
        if not isinstance(s.get("uri"), str) or not s["uri"].strip():
            raise bad("source.uri is required")
        if s.get("tier") not in TIERS:
            raise bad(f"source.tier must be one of {', '.join(TIERS)}")
        if not isinstance(s.get("domain"), str):
            raise bad("source.domain is required (a string)")
        if "licence" not in s:
            raise bad("source.licence is required: there is no default (a default would silently declassify)")
        if s["licence"] not in LICENCES:
            raise bad(f"source.licence must be one of {', '.join(LICENCES)}")
        for k in ("publisher", "title"):
            if s.get(k) is not None and not isinstance(s[k], str):
                raise bad(f"source.{k} must be a string or null")
        for k in ("retrieved_at", "published_at"):
            _date(s.get(k), f"source.{k}", nullable=True)
        return s

    def _add_source(self, db, s: dict, org: str) -> tuple[int, dict]:
        row = db.execute("SELECT id FROM sources WHERE uri = ?", (s["uri"],)).fetchone()
        if row:
            return 200, {"id": row["id"], "created": False}
        sid = "src_" + uid("", s["uri"], n=12).lower()
        db.execute("INSERT INTO sources VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (sid, s["uri"], s.get("publisher"), s.get("title"), s["tier"], s["domain"], s["licence"],
                    s.get("retrieved_at"), s.get("published_at"), org, iso()))
        return 200, {"id": sid, "created": True}

    def sources(self, ids: list[str], org: str) -> list[dict]:
        if not ids:
            return []
        rows = {r["id"]: r for r in self._q(f"SELECT * FROM sources WHERE id IN ({','.join('?' * len(ids))})",
                                            tuple(ids))}
        out = []
        for i in dict.fromkeys(ids):
            r = rows.get(i)
            if r is not None and self._visible_source(r, org):
                out.append({k: r[k] for k in ("id", "uri", "publisher", "title", "tier", "domain", "licence",
                                              "retrieved_at", "published_at")})
        return out

    def _check_source_ids(self, db, ids, org: str, where: str) -> list[str]:
        if not isinstance(ids, list) or not all(isinstance(i, str) and i.startswith("src_") for i in ids):
            raise bad(f"{where} must be a list of source ids (src_…)")
        for i in ids:
            r = db.execute("SELECT licence, added_by_org FROM sources WHERE id = ?", (i,)).fetchone()
            if r is None or not self._visible_source(r, org):
                raise bad(f"{where}: a source id is unknown to this org")
        return ids

    # -- facts -------------------------------------------------------------------
    def facts(self, org, brand, layer, entity, key=None, market=None, key_prefix=None) -> list[dict]:
        so, sb = self._scope_cols(layer, org, brand)
        sql = ("SELECT * FROM facts WHERE layer = ? AND scope_org = ? AND scope_brand = ? AND entity = ? "
               "AND superseded_by IS NULL")
        args = [layer, so, sb, entity]
        if key is not None:
            sql += " AND key = ?"
            args.append(key)
        if key_prefix is not None:
            sql += " AND (key = ? OR key LIKE ? ESCAPE '\\')"
            args += [key_prefix, key_prefix.replace("_", r"\_") + ".%"]
        if market is not None:
            sql += " AND market IN (?, '')"
            args.append(market)
        sql += " ORDER BY key, market, version"
        with self._lock:
            return [self._row(self._db, r, org) for r in self._db.execute(sql, tuple(args)).fetchall()]

    def _check_fact(self, db, fact, org: str, brand: str | None) -> dict:
        if not isinstance(fact, dict):
            raise bad("fact must be an object")
        extra = sorted(set(fact) - FACT_FIELDS)
        if extra:
            raise bad(f"fact: unknown field(s) {', '.join(extra)}")
        missing = sorted(FACT_REQUIRED - set(fact))
        if missing:
            why = " (there is no default licence: a default would silently declassify)" if "licence" in missing else ""
            raise bad(f"fact: missing {', '.join(missing)}{why}")
        if fact["layer"] not in ("brand", "category"):
            raise bad("fact.layer must be brand or category")
        if not isinstance(fact["entity"], str) or not ENTITY_RE.match(fact["entity"]):
            raise bad("fact.entity must be an entity ref (brand|org|category)/<slug>")
        if not isinstance(fact["key"], str) or not KEY_RE.match(fact["key"]):
            raise bad("fact.key must be a dotted key [a-z0-9_]+(.[a-z0-9_]+)*")
        m = fact.get("market")
        if m is not None and (not isinstance(m, str) or not MARKET_RE.match(m)):
            raise bad("fact.market must be an ISO 3166-1 alpha-2 code (upper case) or absent")
        v = fact["value"]
        if isinstance(v, bool) or isinstance(v, str):
            pass
        elif isinstance(v, (int, float)):
            if not math.isfinite(v):
                raise bad("fact.value must be a finite number")
        else:
            raise bad("fact.value is a number, a string or a boolean — never an object, a list or null")
        if not isinstance(fact["unit"], str) or not fact["unit"]:
            raise bad("fact.unit is required (a non-empty string)")
        _date(fact["as_of"], "fact.as_of")
        _date(fact["retrieved_at"], "fact.retrieved_at")
        if fact["licence"] not in LICENCES:
            raise bad(f"fact.licence must be one of {', '.join(LICENCES)}")
        if fact.get("method") is not None and fact["method"] not in METHODS:
            raise bad(f"fact.method must be one of {', '.join(METHODS)}")
        if "status" in fact and fact["status"] != "contested":
            raise bad("fact.status, when sent, may only be contested")
        self._check_source_ids(db, fact["sources"], org, "fact.sources")
        quotes = fact.get("quotes")
        if quotes is not None:
            if not isinstance(quotes, dict) or not all(isinstance(q, str) for q in quotes.values()):
                raise bad("fact.quotes must be an object of source id -> quote")
            if set(quotes) - set(fact["sources"]):
                raise bad("fact.quotes keys must be among fact.sources")
        if fact["layer"] == "brand" and not brand:
            raise PeripheralError(400, "missing_scope", "X-Napkin-Brand is required for the brand layer")
        typ, _, rest = fact["entity"].partition("/")
        if fact["layer"] == "category" and typ == "category":
            if not LEAF_RE.match(rest) or not self.known_leaf(db, rest):
                raise bad("the category layer rejects an unknown leaf", "unknown_leaf")
        return fact

    def _record_decision(self, db, d: dict, org: str, brand: str | None):
        db.execute("INSERT OR IGNORE INTO decisions VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (d["id"], d["kind"], d.get("handler"), d.get("action"), d.get("rationale"),
                    json.dumps(d.get("cites") or []), org, brand or "", iso(), json.dumps(d, ensure_ascii=False)))

    def _append(self, db, fact: dict, decision: dict, org: str, brand: str | None) -> tuple[str, str]:
        """LocalLayers.append inside the caller's transaction. Returns (fact id now
        current for the identity, outcome)."""
        layer, entity, key = fact["layer"], fact["entity"], fact["key"]
        market = fact.get("market") or ""
        so, sb = self._scope_cols(layer, org, brand)
        vt, vn, vtext, vb = _value_cols(fact["value"])
        t = iso()
        self._record_decision(db, decision, org, brand)
        cur = db.execute("SELECT * FROM facts WHERE layer=? AND scope_org=? AND scope_brand=? AND entity=? "
                         "AND key=? AND market=? AND superseded_by IS NULL ORDER BY version DESC",
                         (layer, so, sb, entity, key, market)).fetchall()
        same = next((r for r in cur if _same(_value_of(r), fact["value"])), None)
        if same is not None:
            fid, outcome = same["id"], "corroborated"
        else:
            # Versions count per entity + key across markets, so the origin URI
            # (which carries no market) names exactly one row.
            top = db.execute("SELECT MAX(version) FROM facts WHERE layer=? AND scope_org=? AND scope_brand=? "
                             "AND entity=? AND key=?", (layer, so, sb, entity, key)).fetchone()[0] or 0
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
            outcome = "superseded" if supersedes else ("contested" if cur and status == "contested" else "created")
            fid = uid("f_", layer, so, sb, entity, key, market, version, fact["value"])
            db.execute("INSERT INTO facts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (fid, layer, so, sb, entity, key, market, version, vt, vn, vtext, vb, fact["unit"],
                        fact["as_of"], fact["retrieved_at"], status, supersedes, None, fact["licence"],
                        fact.get("method"), decision["id"], org, t))
            if supersedes:
                db.execute("UPDATE facts SET status='superseded', superseded_by=? WHERE id=?", (fid, supersedes))
        quotes = fact.get("quotes") or {}
        for sid in fact.get("sources") or []:
            db.execute("INSERT OR IGNORE INTO fact_sources VALUES (?,?,?,?)", (fid, sid, quotes.get(sid), t))
        return fid, outcome

    def resolve(self, uri: str, org: str, brand_hdr: str | None) -> dict | None:
        m = PIN_RE.fullmatch(uri or "")
        if not m:
            return None
        layer, path, key, version = m.group(1), m.group(2), m.group(3), int(m.group(4))
        if layer == "brand" and not brand_hdr:
            raise PeripheralError(400, "missing_scope", "X-Napkin-Brand is required to resolve a brand-layer pin")
        entity = path if re.match(r"^(brand|org|category)/", path) else f"{layer}/{path}"
        so, sb = self._scope_cols(layer, org, brand_hdr)
        with self._lock:
            r = self._db.execute("SELECT * FROM facts WHERE layer=? AND scope_org=? AND scope_brand=? AND entity=? "
                                 "AND key=? AND version=?", (layer, so, sb, entity, key, version)).fetchone()
            return self._row(self._db, r, org) if r else None

    # -- roster ------------------------------------------------------------------
    def roster(self, ref: str, org: str, brand: str) -> dict | None:
        rows = [r for r in self.facts(org, brand, "brand", ref, key_prefix="roster") if r["status"] == "active"]
        if not rows:
            return None
        by = {r["key"]: r for r in rows}
        cats = [by[k]["value"] for k in ROSTER_KEYS if k in by]
        name = self._q("SELECT name FROM brands WHERE ref=? AND scope_org=?", (ref, org))
        return {"ref": ref, "name": name[0]["name"] if name else None, "categories": cats,
                "client_org": by["roster.client_org"]["value"] if "roster.client_org" in by else None,
                "facts": rows}

    def _check_roster(self, db, body: dict, org: str) -> dict:
        extra = sorted(set(body) - ROSTER_FIELDS)
        if extra:
            raise bad(f"unknown field(s) {', '.join(extra)}")
        for k in ("name", "categories", "sources", "decision"):
            if k not in body:
                raise bad(f"{k} is required")
        if not isinstance(body["name"], str) or not body["name"].strip():
            raise bad("name must be a non-empty string")
        cats = body["categories"]
        if not isinstance(cats, list) or not 1 <= len(cats) <= 2 or len(set(map(str, cats))) != len(cats):
            raise bad("categories must be 1 or 2 distinct leaf codes")
        for c in cats:
            if not isinstance(c, str) or not LEAF_RE.match(c):
                raise bad("categories must be leaf codes <vertical>.<leaf>")
            if not self.known_leaf(db, c):
                raise bad("a roster category is a leaf the tree does not know", "unknown_leaf")
        co = body.get("client_org")
        if co is not None and (not isinstance(co, str) or not ORG_RE.match(co)):
            raise bad("client_org must be org/<slug>")
        self._check_source_ids(db, body["sources"], org, "sources")
        _check_decision(body["decision"])
        return body

    def _set_roster(self, db, ref: str, body: dict, org: str, brand: str) -> list[str]:
        day = iso()[:10]
        base = {"layer": "brand", "entity": ref, "unit": "code", "as_of": day, "retrieved_at": day,
                "sources": body["sources"], "licence": "client-confidential", "method": "report"}
        ids = []
        for k, v in zip(ROSTER_KEYS, body["categories"][:2]):
            ids.append(self._append(db, {**base, "key": k, "value": v}, body["decision"], org, brand)[0])
        if body.get("client_org"):
            ids.append(self._append(db, {**base, "key": "roster.client_org", "value": body["client_org"]},
                                    body["decision"], org, brand)[0])
        self._note_brand(db, ref, body["name"], org)
        return ids

    def _note_brand(self, db, ref: str, name: str, org: str):
        db.execute("INSERT INTO brands VALUES (?,?,?,?) ON CONFLICT(ref, scope_org) DO UPDATE SET "
                   "name=excluded.name, updated_at=excluded.updated_at", (ref, org, name, iso()))

    def find_brands(self, text: str, org: str) -> list[dict]:
        t = slug(text)
        if len(t) < 2:
            return []
        rows = self._q("SELECT ref, name FROM brands WHERE scope_org = ? ORDER BY name", (org,))
        return [{"ref": r["ref"], "name": r["name"]} for r in rows
                if slug(r["name"]) == t or (len(t) >= 3 and (t in slug(r["name"]) or slug(r["name"]) in t))]

    # -- writes: one transaction each, with idempotency ------------------------
    def _write(self, req: Request, org: str, key_required: bool, work) -> Response:
        """Run work(db) -> (status, body) in one IMMEDIATE transaction together
        with the idempotency record; a failure leaves nothing behind."""
        key = req.header("idempotency-key")
        if key is None or key == "":
            if key_required:
                raise bad("Idempotency-Key is required on a write")
            key = None
        elif len(key) > 128:
            raise bad("Idempotency-Key is at most 128 characters")
        body_sha = hashlib.sha256("\n".join([req.method, req.path, req.header("x-napkin-brand") or "",
                                             req.body.decode("utf-8", "replace")]).encode()).hexdigest()
        with self._lock:
            db = self._db
            db.execute("BEGIN IMMEDIATE")
            try:
                if key is not None:
                    cutoff = (_dt.datetime.now(_dt.timezone.utc) - IDEMPOTENCY_TTL).strftime("%Y-%m-%dT%H:%M:%SZ")
                    db.execute("DELETE FROM idempotency WHERE created_at < ?", (cutoff,))
                    prev = db.execute("SELECT * FROM idempotency WHERE scope_org=? AND key=?", (org, key)).fetchone()
                    if prev is not None:
                        db.execute("COMMIT")
                        if prev["body_sha256"] != body_sha:
                            raise PeripheralError(409, "idempotency_conflict",
                                                  "this Idempotency-Key was used with a different body")
                        return Response(prev["status"], json.loads(prev["response"]),
                                        {"Idempotent-Replay": "true"}, note="replay")
                status, out = work(db)
                if key is not None:
                    db.execute("INSERT INTO idempotency VALUES (?,?,?,?,?,?)",
                               (org, key, body_sha, status, json.dumps(out, ensure_ascii=False), iso()))
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
        return Response(status, out)

    # -- routing ---------------------------------------------------------------
    def handle(self, req: Request) -> Response:
        try:
            return self._route(req)
        except PeripheralError as e:
            return e.response()
        except sqlite3.OperationalError as e:
            return PeripheralError(503, "unavailable", f"the layer store is unavailable ({type(e).__name__})").response()
        except Exception as e:  # never a 200 that hides a failure
            return PeripheralError(500, "internal", f"internal error: {type(e).__name__}").response()

    def _route(self, req: Request) -> Response:
        seg = req.segments()
        if seg[:2] != ["v1", "layers"] or len(seg) < 3:
            raise PeripheralError(404, "not_found", f"no route {req.path}")
        seg, m = seg[2:], req.method

        def need(method: str):
            if m != method:
                raise PeripheralError(405, "method_not_allowed", f"use {method}")

        head = seg[0]
        if head == "categories":
            org = self._org(req)
            if len(seg) == 1:
                need("GET")
                return Response(200, {"taxonomy_version": self.taxonomy_version, "leaves": self.leaves()})
            if seg[1:] == ["find"]:
                need("POST")
                body = self._json(req)
                if set(body) != {"text"} or not isinstance(body["text"], str):
                    raise bad("the body is {text: <string>}")
                return Response(200, {"leaves": self.find(body["text"])})
            if len(seg) == 3 and seg[2] == "vertical":
                need("GET")
                v = self.vertical_of(seg[1])
                if v is None:
                    raise PeripheralError(404, "unknown_leaf", "the tree does not know this leaf")
                return Response(200, v)
            raise PeripheralError(404, "not_found", f"no route {req.path}")

        if head == "facts":
            org = self._org(req)
            if len(seg) == 1 and m == "GET":
                layer, entity = req.q("layer"), req.q("entity")
                if layer not in ("brand", "category"):
                    raise bad("layer must be brand or category")
                if not entity or not ENTITY_RE.match(entity):
                    raise bad("entity must be an entity ref")
                brand = self._brand(req, required=(layer == "brand"))
                key, kp, market = req.q("key") or None, req.q("key_prefix") or None, req.q("market") or None
                if key is not None and not KEY_RE.match(key):
                    raise bad("key must be a dotted key")
                if kp is not None and not KEY_RE.match(kp):
                    raise bad("key_prefix must be a dotted key")
                if market is not None and not MARKET_RE.match(market):
                    raise bad("market must be an ISO 3166-1 alpha-2 code (upper case)")
                return Response(200, {"facts": self.facts(org, brand, layer, entity, key, market, kp)})
            if len(seg) == 1:
                need("POST")
                brand = self._brand(req, required=False)
                body = self._json(req)
                extra = sorted(set(body) - {"fact", "decision"})
                if extra or "fact" not in body or "decision" not in body:
                    raise bad("the body is {fact, decision}" + (f"; unknown {', '.join(extra)}" if extra else ""))
                _check_decision(body["decision"])
                with self._lock:
                    fact = self._check_fact(self._db, body["fact"], org, brand)

                    def work(db):
                        fid, outcome = self._append(db, fact, body["decision"], org, brand)
                        row = self._row(db, db.execute("SELECT * FROM facts WHERE id = ?", (fid,)).fetchone(), org)
                        return 200, {"fact": row, "outcome": outcome}
                    return self._write(req, org, True, work)
            if seg[1:] == ["by-uri"]:
                need("GET")
                row = self.resolve(req.q("uri") or "", org, self._brand(req, required=False))
                if row is None:
                    raise PeripheralError(404, "unknown_fact", "no fact at this pin in this scope")
                return Response(200, {"fact": row})
            raise PeripheralError(404, "not_found", f"no route {req.path}")

        if head == "sources" and len(seg) == 1:
            org = self._org(req)
            if m == "GET":
                ids = [i for i in (req.q("ids") or "").split(",") if i]
                return Response(200, {"sources": self.sources(ids, org)})
            need("POST")
            s = self._check_source(self._json(req))
            return self._write(req, org, True, lambda db: self._add_source(db, s, org))

        if head == "roster" and len(seg) >= 2:
            org = self._org(req)
            ref = "/".join(seg[1:])
            if not BRAND_RE.match(ref):
                raise bad("the roster is keyed by a brand ref brand/<slug>")
            brand = self._brand(req, required=True)
            if m == "GET":
                r = self.roster(ref, org, brand)
                if r is None:
                    raise PeripheralError(404, "unknown_brand", "no roster for this brand in this scope")
                return Response(200, r)
            need("PUT")
            body = self._json(req)
            with self._lock:
                self._check_roster(self._db, body, org)

                def work(db):
                    ids = self._set_roster(db, ref, body, org, brand)
                    rows = [self._row(db, db.execute("SELECT * FROM facts WHERE id = ?", (i,)).fetchone(), org)
                            for i in ids]
                    return 200, {"facts": rows}
                return self._write(req, org, True, work)

        if head == "brands" and len(seg) >= 2:
            org = self._org(req)
            if seg[1:] == ["find"]:
                need("POST")
                body = self._json(req)
                if set(body) != {"text"} or not isinstance(body["text"], str):
                    raise bad("the body is {text: <string>}")
                return Response(200, {"brands": self.find_brands(body["text"], org)})
            ref = "/".join(seg[1:])
            if not BRAND_RE.match(ref):
                raise bad("a brand ref is brand/<slug>")
            need("PUT")
            body = self._json(req)
            if set(body) != {"name"} or not isinstance(body["name"], str) or not body["name"].strip():
                raise bad("the body is {name: <non-empty string>}")

            def note(db):
                self._note_brand(db, ref, body["name"], org)
                return 200, {"ref": ref, "name": body["name"]}
            # The key is honoured when sent; not required (a name upsert is naturally repeat-safe).
            return self._write(req, org, False, note)

        if head == "decisions" and len(seg) == 2:
            org = self._org(req)
            need("GET")
            brand = self._brand(req, required=False)
            r = self._q("SELECT * FROM decisions WHERE id = ?", (seg[1],))
            if not r or r[0]["scope_org"] != org or (r[0]["scope_brand"] and r[0]["scope_brand"] != brand):
                raise PeripheralError(404, "unknown_decision", "no decision with this id in this scope")
            return Response(200, {"decision": json.loads(r[0]["body"])})

        raise PeripheralError(404, "not_found", f"no route {req.path}")

    @staticmethod
    def _json(req: Request) -> dict:
        body = parse_json_object(req.body)
        refuse_scope_in_body(body)
        return body


