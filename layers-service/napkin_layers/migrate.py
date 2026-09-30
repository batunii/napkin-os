"""Apply the knowledge-layer migrations to one database.

    python -m napkin_layers.migrate --dsn postgresql://admin@host/napkin_category \\
        --kind category --owner-role napkin_category_owner --app-role napkin_category_app

    python -m napkin_layers.migrate --dsn postgresql://admin@host/napkin_agency_acme \\
        --kind agency --agency org/acme \\
        --owner-role napkin_agency_acme_owner --app-role napkin_agency_acme_app

Connects as an admin who is a member of the owner role (provision-databases.sh
grants that) and runs every migration as the owner, so the tables belong to it
and not to the admin or the app. Each migration commits alone and is recorded
in layers_schema.migrations; re-running applies only what is missing. Every
database gets the geographies (data/geographies.json); a category database
also gets the category tree from taxonomy.json. Both are safe to re-run
whenever the files change: rows are upserted, never deleted.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import psycopg
from psycopg import sql

HERE = Path(__file__).resolve().parent
MIGRATIONS = HERE.parent / "migrations"
REPO = HERE.parent.parent
TAXONOMY = REPO / "docs" / "contracts" / "peripherals" / "taxonomy.json"
GEOGRAPHIES = HERE.parent / "data" / "geographies.json"
MEASURES = HERE.parent / "data" / "measures.json"

PLAN = {
    "category": ["0001_core.sql", "0002_category.sql", "0003_append.sql", "0004_cells.sql", "0005_cell_confidence.sql",
                 "0006_period_basis.sql", "0007_measures.sql", "0008_retract.sql", "0009_shared_scope.sql"],
    "agency": ["0001_core.sql", "0002_agency.sql", "0003_append.sql", "0006_period_basis.sql", "0007_measures.sql",
               "0008_retract.sql"],
}
ROLE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
AGENCY = re.compile(r"^org/[a-z0-9][a-z0-9-]{0,62}$")


def render(text: str, app_role: str, agency: str | None) -> str:
    """The migration with its placeholders filled. Values are validated first,
    because they land in DDL where a parameter cannot go."""
    if not ROLE.match(app_role):
        raise ValueError(f"app role {app_role!r} is not a plain role name")
    text = text.replace("__APP_ROLE__", app_role)
    if "__AGENCY_ORG__" in text:
        if not agency or not AGENCY.match(agency):
            raise ValueError("an agency database needs --agency org/<slug>")
        text = text.replace("__AGENCY_ORG__", agency)
    return text


def migrate(conn: psycopg.Connection, kind: str, owner_role: str, app_role: str,
            agency: str | None = None, taxonomy: Path = TAXONOMY) -> list[str]:
    """Apply what is missing; return the names applied this time."""
    if kind not in PLAN:
        raise ValueError(f"kind must be one of {sorted(PLAN)}")
    if not ROLE.match(owner_role):
        raise ValueError(f"owner role {owner_role!r} is not a plain role name")
    applied = []
    with conn.transaction():
        conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(owner_role)))
        conn.execute("CREATE SCHEMA IF NOT EXISTS layers_schema")
        conn.execute("""CREATE TABLE IF NOT EXISTS layers_schema.migrations (
                          name text PRIMARY KEY, kind text NOT NULL,
                          applied_at timestamptz NOT NULL DEFAULT now())""")
        done = {r[0] for r in conn.execute("SELECT name FROM layers_schema.migrations")}
        kinds = {r[0] for r in conn.execute("SELECT DISTINCT kind FROM layers_schema.migrations")}
        if kinds and kinds != {kind}:
            raise ValueError(f"this database was migrated as {sorted(kinds)}, not {kind}")
    for name in PLAN[kind]:
        if name in done:
            continue
        body = render((MIGRATIONS / name).read_text(), app_role, agency)
        with conn.transaction():
            conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(owner_role)))
            conn.execute(body)
            conn.execute("INSERT INTO layers_schema.migrations (name, kind) VALUES (%s, %s)", (name, kind))
        applied.append(name)
    seed_geographies(conn, owner_role)
    if kind == "category":
        seed_taxonomy(conn, owner_role, taxonomy)
        seed_measures(conn, owner_role)
    return applied


def seed_measures(conn: psycopg.Connection, owner_role: str, path: Path = MEASURES) -> int:
    """The measure list, upserted. A measure that leaves the file is retired, not
    deleted: facts may already use its key."""
    doc = json.loads(path.read_text())
    status = doc.get("status", "draft")
    keys = []
    with conn.transaction():
        conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(owner_role)))
        for lens, spec in doc["lenses"].items():
            for m in spec["measures"]:
                keys.append(m["key"])
                conn.execute("""INSERT INTO layers.measures (key, lens, units, cardinality, qualifier, definition,
                                                             search_group, status)
                                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                                ON CONFLICT (key) DO UPDATE SET lens = EXCLUDED.lens, units = EXCLUDED.units,
                                  cardinality = EXCLUDED.cardinality, qualifier = EXCLUDED.qualifier,
                                  definition = EXCLUDED.definition, search_group = EXCLUDED.search_group,
                                  status = EXCLUDED.status, updated_at = now()""",
                             (m["key"], lens, m["units"], m["cardinality"], m["qualifier"], m["definition"],
                              m["group"], m.get("status", status)))
        conn.execute("UPDATE layers.measures SET status = 'retired', updated_at = now() "
                     "WHERE NOT (key = ANY (%s)) AND status <> 'retired'", (keys,))
    return len(keys)


def seed_geographies(conn: psycopg.Connection, owner_role: str, path: Path = GEOGRAPHIES) -> int:
    """Countries, regions and their members, GLOBAL and UNKNOWN. A region's
    member list is replaced on each run (a country can leave a region, as GB
    left the EU); countries themselves are never deleted, facts may name them."""
    g = json.loads(path.read_text())
    rows = [(c, "country") for c in g["countries"]]
    rows += [(code, "region") for code in g["regions"]]
    rows += [(g["global"], "global"), (g["unknown"], "unknown")]
    with conn.transaction():
        conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(owner_role)))
        for code, kind in rows:
            name = g["regions"].get(code, {}).get("name")
            conn.execute("""INSERT INTO layers.geographies (code, kind, name) VALUES (%s, %s, %s)
                            ON CONFLICT (code) DO UPDATE SET kind = EXCLUDED.kind, name = EXCLUDED.name""",
                         (code, kind, name))
        for region, r in g["regions"].items():
            conn.execute("DELETE FROM layers.geography_members WHERE region = %s", (region,))
            for country in r["members"]:
                conn.execute("INSERT INTO layers.geography_members (region, country) VALUES (%s, %s)",
                             (region, country))
    return len(rows)


def seed_taxonomy(conn: psycopg.Connection, owner_role: str, path: Path = TAXONOMY) -> int:
    """Upsert the category tree. A leaf that leaves the file stays in the table:
    facts may already cite it."""
    tree = json.loads(path.read_text())
    n = 0
    with conn.transaction():
        conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(owner_role)))
        for vi, v in enumerate(tree["verticals"]):
            conn.execute("""INSERT INTO layers.verticals (code, name, aliases, ordinal) VALUES (%s, %s, %s, %s)
                            ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name, aliases = EXCLUDED.aliases,
                                                             ordinal = EXCLUDED.ordinal""",
                         (v["code"], v["name"], v.get("aliases", []), vi))
            for li, leaf in enumerate(v["leaves"]):
                conn.execute("""INSERT INTO layers.categories
                                  (code, vertical, name, aliases, regulated, provisional, ordinal)
                                VALUES (%s, %s, %s, %s, %s, %s, %s)
                                ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name, aliases = EXCLUDED.aliases,
                                  regulated = EXCLUDED.regulated, provisional = EXCLUDED.provisional,
                                  ordinal = EXCLUDED.ordinal""",
                             (f"{v['code']}.{leaf['code']}", v["code"], leaf["name"], leaf.get("aliases", []),
                              bool(leaf.get("regulated")), bool(leaf.get("provisional")), vi * 1000 + li))
                n += 1
        conn.execute("""INSERT INTO layers.meta (key, value) VALUES ('taxonomy_version', %s)
                        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""", (tree["taxonomy_version"],))
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dsn", required=True, help="an admin connection (member of the owner role)")
    ap.add_argument("--kind", required=True, choices=sorted(PLAN))
    ap.add_argument("--owner-role", required=True)
    ap.add_argument("--app-role", required=True)
    ap.add_argument("--agency", help="org/<slug>, for an agency database")
    ap.add_argument("--taxonomy", type=Path, default=TAXONOMY)
    a = ap.parse_args(argv)
    with psycopg.connect(a.dsn, autocommit=True) as conn:
        applied = migrate(conn, a.kind, a.owner_role, a.app_role, a.agency, a.taxonomy)
    print("applied: " + (", ".join(applied) if applied else "nothing (up to date)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
