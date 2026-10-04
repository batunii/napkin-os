"""Writing category research into napkin_category (layers-service schema).

Everything goes through the database's own checks: sources, captures and
excerpts are plain inserts (append-only, hashed and dated there), facts go
through layers.append_fact, cells through layers.open_cells and
layers.confirm_cell. The connection is the app role; scope is set per
transaction with SET LOCAL, never passed in a body.
"""

from __future__ import annotations

import hashlib

import psycopg
from psycopg.types.json import Jsonb

from .pagecheck import Capture


def _h(*parts) -> str:
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()


class Store:
    def __init__(self, dsn: str, org: str):
        self.dsn, self.org = dsn, org

    def connect(self) -> psycopg.Connection:
        conn = psycopg.connect(self.dsn)
        self.scope(conn)
        return conn

    def scope(self, conn):
        conn.execute("SELECT set_config('napkin.org', %s, true)", (self.org,))

    # -- evidence ----------------------------------------------------------------
    def write_capture(self, conn, url: str, publisher: str | None, tier: str, domain: str, cap: Capture) -> list[str]:
        """The source (once per URL), this reading of it and its passages; the excerpt ids."""
        sid = "src_" + _h(url)[:20]
        conn.execute("""INSERT INTO layers.sources (id, uri, domain, publisher, tier, licence, added_by_org)
                        VALUES (%s, %s, %s, %s, %s, 'open', %s) ON CONFLICT (uri) DO NOTHING""",
                     (sid, url, domain, publisher, tier, self.org))
        sid = conn.execute("SELECT id FROM layers.sources WHERE uri = %s", (url,)).fetchone()[0]
        cid = "cap_" + _h(url, cap.retrieved_at, cap.content_sha256)[:20]
        conn.execute("""INSERT INTO layers.captures (id, source_id, retrieved_at, published_at, published_basis,
                                                     title, content_sha256)
                        VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING""",
                     (cid, sid, cap.retrieved_at, cap.published_at, cap.published_basis, cap.title,
                      cap.content_sha256))
        ids = []
        for i, text in enumerate(cap.excerpts):
            eid = "exc_" + _h(cid, i)[:20]
            conn.execute("""INSERT INTO layers.excerpts (id, capture_id, ordinal, text) VALUES (%s, %s, %s, %s)
                            ON CONFLICT (id) DO NOTHING""", (eid, cid, i, text))
            ids.append(eid)
        return ids

    # -- facts -------------------------------------------------------------------
    def append(self, conn, fact: dict, evidence: list[dict], decision: dict) -> dict:
        return conn.execute("SELECT layers.append_fact(%s, %s, %s)",
                            (Jsonb(fact), Jsonb(evidence), Jsonb(decision))).fetchone()[0]

    def current_facts(self, conn, leaf: str, key_prefix: str, market: str) -> list[dict]:
        """Facts a cell for (leaf, lens, market) may cite: this leaf, this lens, an applicable market."""
        rows = conn.execute(
            """SELECT f.id, f.key, f.qualifier, f.market, f.status, f.unit, f.period_start, f.period_end, f.date_basis,
                      f.period_basis,
                      coalesce(f.value_num::text, f.value_text, f.value_bool::text) AS value,
                      (SELECT count(DISTINCT s.publisher) FROM layers.evidence e
                         JOIN layers.excerpts x ON x.id = e.excerpt_id
                         JOIN layers.captures c ON c.id = x.capture_id
                         JOIN layers.sources s ON s.id = c.source_id WHERE e.fact_id = f.id) AS publishers,
                      (SELECT min(e.quote) FROM layers.evidence e WHERE e.fact_id = f.id) AS quote
                 FROM layers.facts f
                WHERE f.entity IN (%s, %s, 'category/all')
                  AND f.key LIKE %s AND f.status IN ('active', 'contested', 'history', 'undated')
                  AND f.market IN (SELECT code FROM layers.applicable_markets(%s))
                ORDER BY f.key, f.period_end NULLS LAST, f.entity""",
            ("category/" + leaf, "category/" + leaf.split(".", 1)[0], key_prefix + ".%", market)).fetchall()
        cols = ["id", "key", "qualifier", "market", "status", "unit", "period_start", "period_end", "date_basis",
                "period_basis",
                "value",
                "publishers", "quote"]
        out = []
        for r in rows:
            d = dict(zip(cols, r))
            d["period_start"] = d["period_start"].isoformat() if d["period_start"] else None
            d["period_end"] = d["period_end"].isoformat() if d["period_end"] else None
            out.append(d)
        return out

    def measures(self, conn) -> list[dict]:
        cols = ["key", "lens", "units", "cardinality", "qualifier", "definition", "search_group", "status"]
        return [dict(zip(cols, r)) for r in conn.execute(
            f"SELECT {', '.join(cols)} FROM layers.measures WHERE status <> 'retired' ORDER BY lens, search_group, key")]

    def propose(self, conn, pid, lens, key, entity, market, value, unit, excerpt_id, quote):
        """A measure the list lacks, for a planner to add or refuse; never a fact."""
        conn.execute("""INSERT INTO layers.measure_proposals (id, lens, proposed, entity, market, value, unit,
                                                               excerpt_id, quote, proposed_by_org)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING""",
                     (pid, lens, key, entity, market, value[:500], unit, excerpt_id, quote, self.org))

    def promote(self, conn, leaves: list[str], run: str) -> dict:
        """Figures seen under two or more leaves move up to their vertical or to all categories."""
        return conn.execute("SELECT layers.promote_shared(%s, %s)", (leaves, run)).fetchone()[0]

    def apply_corrections(self, conn, corrections: dict) -> int:
        """Retract what the reviewed corrections file says is wrong."""
        n = 0
        for c in corrections.get("retract", []):
            out = conn.execute("SELECT layers.retract_fact(%s, %s, %s)", (c["fact"], c["reason"], Jsonb({
                "id": "d_" + _h("correction", c["fact"], c["reason"])[:24], "kind": "classify",
                "handler": "corrections@1", "action": "retract"}))).fetchone()[0]
            n += out["outcome"] == "retracted"
        return n

    # -- cells -------------------------------------------------------------------
    def open_cells(self, conn, leaf: str, market: str) -> int:
        return conn.execute("SELECT layers.open_cells(%s, %s)", (leaf, market)).fetchone()[0]

    def cell_state(self, conn, leaf: str, lens: str, market: str) -> tuple[str, str | None]:
        row = conn.execute("""SELECT cs.state, v.summary FROM layers.cell_status cs
                                LEFT JOIN layers.cell_versions v ON v.cell_id = cs.cell AND v.version = cs.version
                               WHERE cs.leaf = %s AND cs.lens = %s AND cs.market = %s""",
                           (leaf, lens, market)).fetchone()
        return (row[0], row[1]) if row else ("closed", None)

    def confirm(self, conn, payload: dict) -> dict:
        return conn.execute("SELECT layers.confirm_cell(%s)", (Jsonb(payload),)).fetchone()[0]

    def version_publishers(self, conn, version_id: str) -> tuple[int, bool]:
        """(distinct publishers, any primary source) behind a cell version."""
        row = conn.execute("""SELECT count(DISTINCT s.publisher), coalesce(bool_or(s.tier = 'primary'), false)
                                FROM layers.version_sources(%s) vs JOIN layers.sources s ON s.id = vs.source_id""",
                           (version_id,)).fetchone()
        return int(row[0]), bool(row[1])
