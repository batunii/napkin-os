"""Copy a finished category database into an empty one, as it was: every row
with its own id and timestamps, so each citation, decision and cell version
still names the same thing.

    uv run python -m napkin_layers.copy --from <source app DSN> --to <target app DSN> [--org org/napkin]

Built to move the local draft (editable, rebuilt from cache) into Aurora
(append-only). How:

  * as the app role on both sides, under the org's scope, so row-level
    security and every insert rule of the target apply. Nothing is copied
    around the rules: a row the target refuses stops the copy, with the row
  * one transaction on the target: it lands whole or not at all
  * the target must be migrated and empty (no facts, no decisions), and its
    reference data (geographies, taxonomy, lenses, measures) must equal the
    source's, or ids would point at different things
  * in dependency order; facts in creation order with superseded_by left
    empty, then set, since it points forward to a later fact
  * then every table is compared row for row (a hash of each ordered table);
    any difference rolls the whole copy back

Only the category database for now: agency databases carry brand-scoped rows
under several orgs and brands and need a per-scope copy.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import sys

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

# Written by the layers, in the order a foreign key allows.
DATA = ("decisions", "sources", "captures", "excerpts", "facts", "evidence", "measure_proposals",
        "retractions", "cells", "cell_versions", "cell_citations", "cell_verifications")
# Seeded by migrate.py; must already match.
REFERENCE = ("geographies", "geography_members", "verticals", "categories", "lenses", "measures")
# Columns the seed sets to now() on each database: not part of what a row means.
SEED_STAMPS = {"updated_at", "created_at"}
ORDER = {"facts": "created_at, id", "captures": "created_at, id", "decisions": "created_at, id"}


class CopyRefused(RuntimeError):
    """The copy did not start, or stopped and was rolled back; the message says why."""


def _scope(conn, org: str):
    conn.execute("SELECT set_config('napkin.org', %s, true)", (org,))


def _columns(conn, table: str) -> list[str]:
    """Stored columns (generated ones are computed by the target itself)."""
    return [r[0] for r in conn.execute(
        """SELECT attname FROM pg_attribute
            WHERE attrelid = %s::regclass AND attnum > 0 AND NOT attisdropped AND attgenerated = ''
            ORDER BY attnum""", (f"layers.{table}",))]


def _json_columns(conn, table: str) -> set[str]:
    return {r[0] for r in conn.execute(
        """SELECT attname FROM pg_attribute
            WHERE attrelid = %s::regclass AND attnum > 0 AND NOT attisdropped
              AND atttypid IN ('jsonb'::regtype, 'json'::regtype)""", (f"layers.{table}",))}


def _key(conn, table: str) -> list[str]:
    cols = [r[0] for r in conn.execute(
        """SELECT a.attname FROM pg_index i
             JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY (i.indkey)
            WHERE i.indrelid = %s::regclass AND i.indisprimary ORDER BY a.attname""", (f"layers.{table}",))]
    return cols or _columns(conn, table)


def _rows(conn, table: str, cols: list[str], order: str | None = None):
    q = sql.SQL("SELECT {} FROM layers.{} ORDER BY {}").format(
        sql.SQL(", ").join(map(sql.Identifier, cols)), sql.Identifier(table),
        sql.SQL(order or ", ".join(f'"{c}"' for c in _key(conn, table))))
    return conn.execute(q).fetchall()


def _predecessors_first(rows, id_i: int, sup_i: int) -> list:
    """Facts ordered so a fact's `supersedes` target is always inserted before it. Creation
    order is not enough: a supersession written in one transaction shares its now()."""
    by_id = {r[id_i]: r for r in rows}
    out, placed = [], set()

    def place(r, path=()):
        if r[id_i] in placed:
            return
        if r[id_i] in path:
            raise CopyRefused(f"facts form a supersession cycle through {r[id_i]}")
        prev = r[sup_i]
        if prev is not None and prev in by_id:
            place(by_id[prev], path + (r[id_i],))
        out.append(r)
        placed.add(r[id_i])

    for r in rows:
        place(r)
    return out


def _canon(v):
    """A value as the same text on any server: instants in UTC (a laptop in Europe/Dublin
    and Aurora in UTC print one instant two ways), everything else as JSON sees it."""
    if isinstance(v, datetime.datetime):
        return (v.astimezone(datetime.timezone.utc) if v.tzinfo else v).isoformat()
    return v


def digest(conn, table: str, skip: set[str] = frozenset()) -> tuple[int, str]:
    """(rows, sha256 of the table's rows), for comparing two databases. Rows are sorted
    here, not by the server, so two collations cannot order them differently."""
    cols = [c for c in _columns(conn, table) if c not in skip]
    lines = sorted(json.dumps([_canon(v) for v in row], default=str, sort_keys=True)
                   for row in _rows(conn, table, cols))
    h = hashlib.sha256()
    for line in lines:
        h.update(line.encode() + b"\n")
    return len(lines), h.hexdigest()


def copy(src, dst, org: str = "org/napkin", log=print) -> dict:
    """Copy src's layers into dst (two open connections, not in autocommit). Returns
    {table: rows}. Raises CopyRefused; dst is then rolled back and unchanged."""
    for c in (src, dst):
        _scope(c, org)
    for t in ("facts", "decisions"):
        n = dst.execute(sql.SQL("SELECT count(*) FROM layers.{}").format(sql.Identifier(t))).fetchone()[0]
        if n:
            raise CopyRefused(f"the target already holds {n} rows in layers.{t}; it must be empty")
    for t in REFERENCE:
        a, b = digest(src, t, SEED_STAMPS), digest(dst, t, SEED_STAMPS)
        if a != b:
            raise CopyRefused(f"reference table layers.{t} differs ({a[0]} rows in the source, {b[0]} in the "
                              "target): migrate both from the same code first")

    counts = {}
    try:
        for t in DATA:
            cols = _columns(src, t)
            if t == "facts":
                cols_in = [c for c in cols if c != "superseded_by"]
            else:
                cols_in = cols
            rows = _rows(src, t, cols_in, ORDER.get(t))
            if t == "facts":
                rows = _predecessors_first(rows, cols_in.index("id"), cols_in.index("supersedes"))
            ins = sql.SQL("INSERT INTO layers.{} ({}) VALUES ({})").format(
                sql.Identifier(t), sql.SQL(", ").join(map(sql.Identifier, cols_in)),
                sql.SQL(", ").join(sql.Placeholder() * len(cols_in)))
            js = [i for i, c in enumerate(cols_in) if c in _json_columns(src, t)]
            with dst.cursor() as cur:
                for row in rows:
                    row = list(row)
                    for i in js:
                        if row[i] is not None:
                            row[i] = Jsonb(row[i])
                    try:
                        cur.execute(ins, row)
                    except psycopg.Error as e:
                        ident = dict(zip(cols_in, row)).get("id", row[:2])
                        raise CopyRefused(f"layers.{t} row {ident} was refused: "
                                          f"{e.diag.message_primary or e}") from e
            counts[t] = len(rows)
            log(json.dumps({"copied": t, "rows": len(rows)}))
        links = src.execute("SELECT id, superseded_by FROM layers.facts WHERE superseded_by IS NOT NULL "
                            "ORDER BY created_at, id").fetchall()
        for fid, by in links:
            dst.execute("UPDATE layers.facts SET superseded_by = %s WHERE id = %s", (by, fid))
        log(json.dumps({"linked": "facts.superseded_by", "rows": len(links)}))

        # Row-for-row check before anything is committed.
        for t in DATA:
            a, b = digest(src, t), digest(dst, t)
            if a != b:
                raise CopyRefused(f"layers.{t} differs after the copy ({a[0]} rows in the source, {b[0]} in the "
                                  "target, or the same count with different content)")
        dst.execute("SET CONSTRAINTS ALL IMMEDIATE")   # the deferred period check, now, not at commit
    except BaseException:
        dst.rollback()
        raise
    dst.commit()
    src.rollback()
    log(json.dumps({"verified": "every table matches row for row", "committed": True}))
    return counts


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--from", dest="src", required=True, help="source DSN, as its app role")
    ap.add_argument("--to", dest="dst", required=True, help="target DSN, as its app role")
    ap.add_argument("--org", default="org/napkin", help="the scope the rows were written under")
    a = ap.parse_args(argv)
    with psycopg.connect(a.src) as src, psycopg.connect(a.dst) as dst:
        try:
            copy(src, dst, a.org)
        except CopyRefused as e:
            print(json.dumps({"refused": str(e), "committed": False}), file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
