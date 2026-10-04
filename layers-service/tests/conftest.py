"""A real PostgreSQL 16 (pgserver), set up the way provision-databases.sh sets
up Aurora: per database an owner role (NOLOGIN, owns it) and an app role
(LOGIN, owns nothing, may connect to its own database only). Tests run as the
app role, so row-level security applies to them as it will to the service. A
superuser would bypass it, which is why nothing here queries as one except
setup."""

from __future__ import annotations

import contextlib
import itertools
import tempfile

import psycopg
import pytest
from psycopg import sql

from napkin_layers.migrate import migrate

DATABASES = {
    "napkin_category": ("category", None),
    "napkin_agency_acme": ("agency", "org/acme"),
    "napkin_agency_globex": ("agency", "org/globex"),
    # copy.py: a finished source and an empty target, each migrated like Aurora
    "napkin_category_copy_src": ("category", None),
    "napkin_category_copy_dst": ("category", None),
}


@pytest.fixture(scope="session")
def pg():
    pgserver = pytest.importorskip("pgserver")
    srv = pgserver.get_server(tempfile.mkdtemp(prefix="napkin-layers-"), cleanup_mode="stop")
    admin = psycopg.connect(srv.get_uri("postgres"), autocommit=True)
    admin.execute("CREATE ROLE napkin_admin LOGIN CREATEROLE CREATEDB")
    for db, (kind, agency) in DATABASES.items():
        owner, app = f"{db}_owner", f"{db}_app"
        admin.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(owner)))
        admin.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(app)))
        admin.execute(sql.SQL("GRANT {} TO napkin_admin").format(sql.Identifier(owner)))
        admin.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(db), sql.Identifier(owner)))
        admin.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(db)))
        admin.execute(sql.SQL("GRANT CONNECT, TEMPORARY ON DATABASE {} TO {}").format(
            sql.Identifier(db), sql.Identifier(app)))
    for db, (kind, agency) in DATABASES.items():
        with psycopg.connect(uri(srv, db, "napkin_admin"), autocommit=True) as c:
            migrate(c, kind, f"{db}_owner", f"{db}_app", agency)
    yield srv
    admin.close()


def uri(srv, db: str, user: str) -> str:
    base = srv.get_uri(db)
    scheme, rest = base.split("://", 1)
    return f"{scheme}://{user}@{rest.split('@', 1)[-1]}"


class Session:
    """One transaction as the app role, under a scope, the way the service runs."""

    ids = itertools.count(1)

    def __init__(self, conn):
        self.conn = conn

    def q(self, query, params=None):
        return self.conn.execute(query, params)

    def one(self, query, params=None):
        return self.conn.execute(query, params).fetchone()

    def capture(self, uri="https://example.ie/report", text="The market was worth €432 million in 2025.",
                published="2026-04-14", licence="open", retrieved="2026-10-02T10:00:00Z",
                publisher="Example", tier="primary"):
        """A source (once per URI), one reading of it, one excerpt: the excerpt id."""
        n = next(self.ids)
        src = self.one("SELECT id FROM layers.sources WHERE uri = %s", (uri,))
        if src is None:
            src = (f"src_{n}",)
            self.q("""INSERT INTO layers.sources (id, uri, domain, publisher, tier, licence, added_by_org)
                      VALUES (%s, %s, 'example.ie', %s, %s, %s, current_setting('napkin.org'))""",
                   (src[0], uri, publisher, tier, licence))
        self.q("""INSERT INTO layers.captures (id, source_id, retrieved_at, published_at, published_basis)
                  VALUES (%s, %s, %s, %s, %s)""",
               (f"cap_{n}", src[0], retrieved, published, "page_metadata" if published else "unknown"))
        self.q("INSERT INTO layers.excerpts (id, capture_id, ordinal, text) VALUES (%s, %s, 0, %s)",
               (f"exc_{n}", f"cap_{n}", text))
        return f"exc_{n}", text

    def append(self, value, *, quote, excerpt, period=None, layer="category",
               entity="category/automotive.ev_charging", key="market.size_value", market="IE",
               market_basis=None, period_basis=None, licence="open", contested=False, decision=None,
               qualifier=None, unit="eur"):
        text = excerpt[1]
        start = text.index(quote)
        n = next(self.ids)
        basis = market_basis or ("unknown" if market == "UNKNOWN" else "stated")
        fact = {"id": f"f_{n}", "layer": layer, "entity": entity, "key": key, "market": market,
                "market_basis": basis, "value": value, "unit": unit, "licence": licence, "method": "report"}
        if qualifier:
            fact["qualifier"] = qualifier
        if period:
            fact["period_start"], fact["period_end"] = period
            fact["period_basis"] = period_basis or "inferred"
        elif period_basis:
            fact["period_basis"] = period_basis
        if contested:
            fact["contested"] = True
        dec = decision or {"id": f"d_{n}", "kind": "pin", "handler": "research_lens@1",
                           "rationale": "test", "cites": []}
        ev = [{"excerpt_id": excerpt[0], "quote": quote, "quote_start": start}]
        return self.one("SELECT layers.append_fact(%s, %s, %s)",
                        (psycopg.types.json.Jsonb(fact), psycopg.types.json.Jsonb(ev),
                         psycopg.types.json.Jsonb(dec)))[0]

    def status(self, fact_id):
        return self.one("SELECT status FROM layers.facts WHERE id = %s", (fact_id,))[0]


@pytest.fixture
def session(pg):
    """session(db, org, brand=None) -> a Session in an open transaction, rolled back after."""
    opened = []

    @contextlib.contextmanager
    def open_(db="napkin_category", org="org/acme", brand=None):
        conn = psycopg.connect(uri(pg, db, f"{db}_app"))
        opened.append(conn)
        conn.execute("SELECT set_config('napkin.org', %s, true)", (org,))
        if brand:
            conn.execute("SELECT set_config('napkin.brand', %s, true)", (brand,))
        yield Session(conn)

    yield open_
    for c in opened:
        c.rollback()
        c.close()
