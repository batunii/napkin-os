"""A permanent local Postgres 16 for the knowledge layers, set up the way
provision-databases.sh sets up Aurora: per database an owner role (NOLOGIN)
and an app role (LOGIN, owns nothing, connects to its own database only).

    uv run python -m napkin_layers.localdb [--data ~/.local/share/napkin-layers/pg16] [--agency org/acme ...]

Starts the server (it keeps running after this exits), creates what is
missing, applies the migrations and prints the app-role DSN of each database.
Safe to re-run. Local and disposable: to start over, stop the server and
delete the data directory.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import psycopg
from psycopg import sql

from .migrate import migrate

DEFAULT_DATA = Path.home() / ".local" / "share" / "napkin-layers" / "pg16"


def _uri(base: str, db: str, user: str) -> str:
    scheme, rest = base.split("://", 1)
    rest = rest.split("@", 1)[-1]
    host_part, _, query = rest.partition("?")
    return f"{scheme}://{user}@{host_part.split('/')[0]}/{db}" + (f"?{query}" if query else "")


def ensure(data: Path, agencies: list[str], category_db: str = "napkin_category") -> dict:
    import pgserver
    data.mkdir(parents=True, exist_ok=True)
    srv = pgserver.get_server(str(data), cleanup_mode=None)   # stays up after we exit
    base = srv.get_uri("postgres")
    dbs = {category_db: ("category", None)}
    for a in agencies:
        dbs["napkin_agency_" + a.split("/", 1)[1].replace("-", "_")] = ("agency", a)
    with psycopg.connect(base, autocommit=True) as su:
        if not su.execute("SELECT 1 FROM pg_roles WHERE rolname = 'napkin_admin'").fetchone():
            su.execute("CREATE ROLE napkin_admin LOGIN CREATEROLE CREATEDB")
        for db in dbs:
            owner, app = f"{db}_owner", f"{db}_app"
            for role, login in ((owner, False), (app, True)):
                if not su.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
                    su.execute(sql.SQL("CREATE ROLE {} " + ("LOGIN" if login else "NOLOGIN")).format(
                        sql.Identifier(role)))
            su.execute(sql.SQL("GRANT {} TO napkin_admin").format(sql.Identifier(owner)))
            if not su.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db,)).fetchone():
                su.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(db), sql.Identifier(owner)))
            su.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(db)))
            su.execute(sql.SQL("GRANT CONNECT, TEMPORARY ON DATABASE {} TO {}").format(
                sql.Identifier(db), sql.Identifier(app)))
    out = {}
    for db, (kind, agency) in dbs.items():
        with psycopg.connect(_uri(base, db, "napkin_admin"), autocommit=True) as c:
            applied = migrate(c, kind, f"{db}_owner", f"{db}_app", agency)
        out[db] = {"app_dsn": _uri(base, db, f"{db}_app"), "admin_dsn": _uri(base, db, "napkin_admin"),
                   "applied": applied}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA)
    ap.add_argument("--agency", action="append", default=[], help="org/<slug>; repeat for more")
    ap.add_argument("--category-db", default="napkin_category",
                    help="name of the category database, e.g. napkin_category_v2 for a comparison run")
    a = ap.parse_args(argv)
    print(json.dumps(ensure(a.data.expanduser(), a.agency, a.category_db), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
