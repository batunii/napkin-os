"""The knowledge-layer schema against a real Postgres: the append rule with the
owner's dating rules (2026-09-29), verbatim evidence, append-only history,
and the isolation S3 asks for."""

from __future__ import annotations

import psycopg
import pytest

Y2024 = ("2024-01-01", "2024-12-31")
Y2025 = ("2025-01-01", "2025-12-31")
Q3_2025 = ("2025-07-01", "2025-09-30")
Q4_2025 = ("2025-10-01", "2025-12-31")


def refused(s, fn, match):
    """fn fails with a message containing `match`; the transaction carries on."""
    with pytest.raises(psycopg.Error, match=match):
        with s.conn.transaction():
            fn()


# ── the append rule ────────────────────────────────────────────────────────────

def test_first_dated_fact_is_created_active(session):
    with session() as s:
        r = s.append(432_000_000, quote="€432 million in 2025", excerpt=s.capture(), period=Y2025)
        assert r["outcome"] == "created"
        assert s.status(r["fact"]) == "active"


def test_same_value_same_period_corroborates_and_adds_evidence(session):
    with session() as s:
        a = s.append(432_000_000, quote="€432 million in 2025", excerpt=s.capture(), period=Y2025)
        other = s.capture(uri="https://other.ie/news", text="Revenue hit €432 million in 2025, SEAI said.")
        b = s.append(432_000_000, quote="€432 million in 2025", excerpt=other, period=Y2025)
        assert b == {"fact": a["fact"], "outcome": "corroborated"}
        assert s.one("SELECT count(*) FROM layers.evidence WHERE fact_id = %s", (a["fact"],))[0] == 2


def test_numbers_compare_as_numbers(session):
    with session() as s:
        text = "Share was 0.20 in 2025 and 0.2 by another count."
        a = s.append(0.20, quote="0.20 in 2025", excerpt=s.capture(text=text), period=Y2025,
                     key="market.player_share", qualifier="Tesla", unit="proportion")
        b = s.append(0.2, quote="0.2 by another count", excerpt=s.capture(uri="https://b.ie", text=text),
                     period=Y2025, key="market.player_share", qualifier="Tesla", unit="proportion")
        assert b["outcome"] == "corroborated" and b["fact"] == a["fact"]


def test_later_period_supersedes(session):
    with session() as s:
        old = s.append(313_000_000, quote="€313 million",
                       excerpt=s.capture(text="Up from €313 million in 2024."), period=Y2024)
        new = s.append(432_000_000, quote="€432 million in 2025", excerpt=s.capture(), period=Y2025)
        assert new["outcome"] == "superseded"
        row = s.one("SELECT status, superseded_by FROM layers.facts WHERE id = %s", (old["fact"],))
        assert row == ("superseded", new["fact"])
        assert s.one("SELECT supersedes FROM layers.facts WHERE id = %s", (new["fact"],))[0] == old["fact"]


def test_same_period_different_value_always_contests(session):
    with session() as s:
        a = s.append(410_000_000, quote="€410m", excerpt=s.capture(text="CSO: €410m for 2025."), period=Y2025)
        b = s.append(432_000_000, quote="€432m",
                     excerpt=s.capture(uri="https://cso.ie/rev", text="CSO revised: €432m for 2025."), period=Y2025)
        assert b["outcome"] == "contested"
        assert s.status(a["fact"]) == "contested" and s.status(b["fact"]) == "contested"


def test_earlier_period_is_kept_as_history_not_contest(session):
    with session() as s:
        cur = s.append(432_000_000, quote="€432 million in 2025", excerpt=s.capture(), period=Y2025)
        old = s.append(313_000_000, quote="€313 million",
                       excerpt=s.capture(text="Up from €313 million in 2024."), period=Y2024)
        assert old["outcome"] == "history"
        assert s.status(old["fact"]) == "history" and s.status(cur["fact"]) == "active"


def test_different_granularity_at_the_same_end_is_not_the_same_period(session):
    with session() as s:
        year = s.append(432_000_000, quote="€432 million in 2025", excerpt=s.capture(), period=Y2025)
        q4 = s.append(120_000_000, quote="€120m in Q4", excerpt=s.capture(text="€120m in Q4 2025."), period=Q4_2025)
        assert q4["outcome"] == "history"
        assert s.status(year["fact"]) == "active"


def test_undated_fact_never_supersedes_a_dated_one(session):
    with session() as s:
        dated = s.append(432_000_000, quote="€432 million in 2025", excerpt=s.capture(), period=Y2025)
        und = s.append(380_000_000, quote="worth €380 million",
                       excerpt=s.capture(uri="https://blog.ie", text="The market is worth €380 million.",
                                         published=None))
        assert und["outcome"] == "undated"
        assert s.status(und["fact"]) == "undated" and s.status(dated["fact"]) == "active"
        assert s.one("SELECT date_basis FROM layers.facts WHERE id = %s", (und["fact"],))[0] == "unknown"


def test_undated_same_value_corroborates_the_dated_row(session):
    with session() as s:
        dated = s.append(432_000_000, quote="€432 million in 2025", excerpt=s.capture(), period=Y2025)
        und = s.append(432_000_000, quote="€432 million",
                       excerpt=s.capture(uri="https://blog.ie", text="A €432 million market.", published=None))
        assert und == {"fact": dated["fact"], "outcome": "corroborated"}


def test_undated_rows_disagreeing_contest(session):
    with session() as s:
        a = s.append("Tesla", quote="Tesla", excerpt=s.capture(text="Tesla leads the market."),
                     key="market.leader", unit="text")
        b = s.append("BYD", quote="BYD",
                     excerpt=s.capture(uri="https://c.ie", text="BYD now leads the market."),
                     key="market.leader", unit="text")
        assert a["outcome"] == "created" and b["outcome"] == "contested"
        assert s.status(a["fact"]) == "contested"


def test_a_dated_fact_takes_precedence_over_undated_rows(session):
    with session() as s:
        und = s.append(380_000_000, quote="worth €380 million",
                       excerpt=s.capture(uri="https://blog.ie", text="The market is worth €380 million.",
                                         published=None))
        assert und["outcome"] == "created"
        dated = s.append(432_000_000, quote="€432 million in 2025", excerpt=s.capture(), period=Y2025)
        assert dated["outcome"] == "created"
        assert s.status(dated["fact"]) == "active" and s.status(und["fact"]) == "undated"


def test_versions_count_per_entity_and_key_across_markets(session):
    with session() as s:
        ie = s.append(1, quote="1", excerpt=s.capture(text="1 in Ireland"), period=Y2025, key="market.player_count",
                      unit="count")
        gb = s.append(2, quote="2", excerpt=s.capture(text="2 in Britain"), period=Y2025, key="market.player_count",
                      market="GB", unit="count")
        v = dict(s.q("SELECT market, version FROM layers.facts WHERE id = ANY(%s)", ([ie["fact"], gb["fact"]],)))
        assert v == {"IE": 1, "GB": 2}


def test_concurrent_appends_to_one_identity_are_serialised(session, pg):
    """Two transactions racing on one identity: the second waits for the first
    and sees its row, so exactly one is created and the other contests."""
    import threading
    from conftest import uri as dburi
    conns = [psycopg.connect(dburi(pg, "napkin_category", "napkin_category_app")) for _ in range(2)]
    try:
        from conftest import Session
        results = []
        for c in conns:
            c.execute("SELECT set_config('napkin.org', 'org/acme', true)")
        s1, s2 = Session(conns[0]), Session(conns[1])
        e1 = s1.capture(uri="https://race1.ie", text="Race: 7 players in 2025.")
        e2 = s2.capture(uri="https://race2.ie", text="Race: 9 players in 2025.")
        conns[0].commit(); conns[1].commit()
        for c in conns:
            c.execute("SELECT set_config('napkin.org', 'org/acme', true)")
        r1 = s1.append(7, quote="7 players", excerpt=e1, period=Y2025, key="market.player_count", unit="count",
                       entity="category/automotive.used_dealer")
        t = threading.Thread(target=lambda: results.append(
            s2.append(9, quote="9 players", excerpt=e2, period=Y2025, key="market.player_count", unit="count",
                      entity="category/automotive.used_dealer")))
        t.start()
        t.join(1.0)
        assert t.is_alive(), "the second writer must wait for the first"
        conns[0].commit()
        t.join(10)
        assert results[0]["outcome"] == "contested"
        assert r1["outcome"] == "created"
    finally:
        for c in conns:
            c.rollback()
            c.close()


# ── evidence is verbatim, checked by the database ─────────────────────────────

def test_a_quote_not_in_its_excerpt_is_refused(session):
    with session() as s:
        exc = s.capture()
        refused(s, lambda: s.one(
            """SELECT layers.append_fact('{"id":"f_x","layer":"category","entity":"category/automotive.ev_charging",
               "key":"market.size_value","market":"IE","market_basis":"stated","value":1,"unit":"eur","licence":"open"}',
               %s, '{"id":"d_x","kind":"pin"}')""",
            (psycopg.types.json.Jsonb([{"excerpt_id": exc[0], "quote": "€450 million", "quote_start": 0}]),)),
            "not verbatim")


def test_a_quote_at_the_wrong_offset_is_refused(session):
    with session() as s:
        exc = s.capture()
        refused(s, lambda: s.one(
            """SELECT layers.append_fact('{"id":"f_y","layer":"category","entity":"category/automotive.ev_charging",
               "key":"market.size_value","market":"IE","market_basis":"stated","value":1,"unit":"eur","licence":"open"}',
               %s, '{"id":"d_y","kind":"pin"}')""",
            (psycopg.types.json.Jsonb([{"excerpt_id": exc[0], "quote": "€432 million", "quote_start": 0}]),)),
            "not verbatim")


def test_a_fact_needs_evidence(session):
    with session() as s:
        refused(s, lambda: s.one(
            """SELECT layers.append_fact('{"id":"f_z","layer":"category","entity":"category/automotive.ev_charging",
               "key":"market.size_value","market":"IE","market_basis":"stated","value":1,"unit":"eur","licence":"open"}', '[]', '{"id":"d_z","kind":"pin"}')"""),
            "at least one piece of evidence")


def test_the_excerpt_hash_is_computed_not_trusted(session):
    with session() as s:
        exc = s.capture(text="abc")
        h = s.one("SELECT text_sha256 FROM layers.excerpts WHERE id = %s", (exc[0],))[0]
        assert h == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


# ── dates ──────────────────────────────────────────────────────────────────────

def test_a_period_that_ends_before_it_starts_is_refused(session):
    with session() as s:
        refused(s, lambda: s.append(1, quote="€432 million", excerpt=s.capture(),
                                    period=("2025-12-31", "2025-01-01")), "check")


def test_a_published_date_after_the_reading_is_refused(session):
    with session() as s:
        refused(s, lambda: s.capture(published="2027-01-01", retrieved="2026-10-02T10:00:00Z"), "check")


def test_published_basis_must_match_the_date(session):
    with session() as s:
        src = s.capture()
        refused(s, lambda: s.q("""INSERT INTO layers.captures (id, source_id, retrieved_at, published_at,
                                   published_basis) SELECT 'cap_bad', source_id, now(), NULL, 'page_metadata'
                                   FROM layers.captures LIMIT 1"""), "check")


# ── append-only ────────────────────────────────────────────────────────────────

def test_a_facts_value_can_never_change(session):
    with session() as s:
        r = s.append(432_000_000, quote="€432 million in 2025", excerpt=s.capture(), period=Y2025)
        refused(s, lambda: s.q("UPDATE layers.facts SET value_num = 1 WHERE id = %s", (r["fact"],)),
                "permission denied|immutable")


def test_nothing_is_deleted(session):
    with session() as s:
        r = s.append(432_000_000, quote="€432 million in 2025", excerpt=s.capture(), period=Y2025)
        refused(s, lambda: s.q("DELETE FROM layers.facts WHERE id = %s", (r["fact"],)), "permission denied|append-only")
        refused(s, lambda: s.q("UPDATE layers.excerpts SET text = 'x'"), "permission denied|append-only")
        refused(s, lambda: s.q("TRUNCATE layers.evidence"), "permission denied|not allowed")


def test_a_decision_id_never_carries_two_bodies(session):
    with session() as s:
        dec = {"id": "d_same", "kind": "pin", "rationale": "first"}
        s.append(1, quote="€432 million", excerpt=s.capture(), period=Y2025, key="market.player_share", qualifier="A",
                 unit="proportion", decision=dec)
        s.append(0.2, quote="€432 million", excerpt=s.capture(), period=Y2025, key="market.player_share",
                 qualifier="B", unit="proportion", decision=dec)
        refused(s, lambda: s.append(0.3, quote="€432 million", excerpt=s.capture(), period=Y2025,
                                    key="market.player_share", qualifier="C", unit="proportion",
                                    decision={**dec, "rationale": "changed"}), "idempotency_conflict")


# ── the category database ──────────────────────────────────────────────────────

def test_category_tree_is_seeded(session):
    with session() as s:
        n = s.one("SELECT count(*) FROM layers.categories")[0]
        assert n >= 108
        assert s.one("SELECT value FROM layers.meta WHERE key = 'taxonomy_version'")[0]


def test_an_unknown_leaf_is_refused(session):
    with session() as s:
        refused(s, lambda: s.append(1, quote="€432 million", excerpt=s.capture(), period=Y2025,
                                    entity="category/automotive.hoverboards"), "unknown_leaf")


def test_client_material_never_enters_the_category_database(session):
    with session() as s:
        refused(s, lambda: s.capture(uri="https://client.example/deck", licence="client-confidential"), "check")


def test_category_facts_are_shared_but_their_decisions_are_not(session, pg):
    with session(org="org/acme") as a:
        r = a.append(432_000_000, quote="€432 million in 2025", excerpt=a.capture(), period=Y2025,
                     decision={"id": "d_acme_private", "kind": "pin", "rationale": "for the BMW pitch"})
        a.conn.commit()   # append-only: the row stays; unique ids keep tests apart
    with session(org="org/globex") as g:
        assert g.one("SELECT count(*) FROM layers.facts WHERE id = %s", (r["fact"],))[0] == 1
        assert g.one("SELECT count(*) FROM layers.decisions WHERE id = 'd_acme_private'")[0] == 0


def test_a_write_without_scope_is_refused(session, pg):
    from conftest import uri as dburi
    with psycopg.connect(dburi(pg, "napkin_category", "napkin_category_app")) as c:
        with pytest.raises(psycopg.Error, match="napkin.org is not set"):
            c.execute("SELECT layers.scope_org()")


def test_scope_in_the_body_is_refused(session):
    with session() as s:
        exc = s.capture()
        refused(s, lambda: s.one(
            """SELECT layers.append_fact('{"id":"f_s","layer":"category","org":"org/other",
               "entity":"category/automotive.ev_charging","key":"market.size_value","value":1,"unit":"eur",
               "licence":"open"}', %s, '{"id":"d_s","kind":"pin"}')""",
            (psycopg.types.json.Jsonb([{"excerpt_id": exc[0], "quote": "€432", "quote_start": 22}]),)),
            "scope comes from the transaction")


# ── agency databases ───────────────────────────────────────────────────────────

AG = dict(db="napkin_agency_acme", org="org/acme")
BRAND_FACT = dict(layer="brand", entity="brand/lunasa", key="positioning.claim", market="IE",
                  licence="client-confidential")


def test_brands_in_one_agency_cannot_see_each_other(session):
    with session(**AG, brand="brand/lunasa") as s:
        exc = s.capture(uri="https://acme.example/brief", text="Lunasa: the calm drink.", licence="client-confidential")
        r = s.append("the calm drink", quote="the calm drink", excerpt=exc, **BRAND_FACT)
        assert r["outcome"] == "created"
        s.conn.commit()
    with session(**AG, brand="brand/rival") as other:
        assert other.one("SELECT count(*) FROM layers.facts WHERE id = %s", (r["fact"],))[0] == 0
        assert other.one("SELECT count(*) FROM layers.evidence WHERE fact_id = %s", (r["fact"],))[0] == 0
    with session(**AG, brand="brand/lunasa") as same:
        assert same.one("SELECT count(*) FROM layers.facts WHERE id = %s", (r["fact"],))[0] == 1


def test_brand_scope_is_required_in_an_agency_database(session):
    with session(**AG) as s:
        refused(s, lambda: s.q("SELECT * FROM layers.facts"), "napkin.brand is not set")


def test_an_agency_database_refuses_another_agency(session):
    with session(db="napkin_agency_acme", org="org/globex", brand="brand/lunasa") as s:
        exc = s.capture(uri="https://g.example/x", text="Lunasa: the calm drink.", licence="client-confidential")
        refused(s, lambda: s.append("the calm drink", quote="the calm drink", excerpt=exc, **BRAND_FACT),
                "belongs to org/acme")


def test_one_agencys_role_cannot_connect_to_anothers_database(pg):
    from conftest import uri as dburi
    with pytest.raises(psycopg.OperationalError, match="permission denied"):
        psycopg.connect(dburi(pg, "napkin_agency_globex", "napkin_agency_acme_app"))


def test_a_fact_is_never_less_restricted_than_its_source(session):
    with session(**AG, brand="brand/lunasa") as s:
        exc = s.capture(uri="https://acme.example/deck", text="Lunasa: the calm drink.", licence="client-confidential")
        refused(s, lambda: s.append("the calm drink", quote="the calm drink", excerpt=exc,
                                    **{**BRAND_FACT, "key": "positioning.tagline", "licence": "open"}),
                "less restricted")
        # also when it would only corroborate an existing row
        s.append("the calm drink", quote="the calm drink", excerpt=exc, **{**BRAND_FACT, "key": "positioning.line"})
        refused(s, lambda: s.append("the calm drink", quote="the calm drink", excerpt=exc,
                                    **{**BRAND_FACT, "key": "positioning.line", "licence": "open"}),
                "less restricted")


def test_licence_is_required(session):
    with session() as s:
        refused(s, lambda: s.append(1, quote="€432 million", excerpt=s.capture(), period=Y2025, licence=None),
                "licence is required")


def test_rag_exclusions_take_the_latest_action(session):
    with session(**AG, brand="brand/lunasa") as s:
        s.q("INSERT INTO layers.decisions (id, kind, org, brand, body) VALUES ('d_ro', 'classify', 'org/acme', NULL, '{}')")
        uri_ = "passage://cannes/some-case.md#the-insight@9c41e0aa12b3f4d5"
        s.q("""INSERT INTO layers.rag_overrides (id, passage_uri, action, reason, decision_id, created_at)
               VALUES ('ro_1', %s, 'exclude', 'not for our clients', 'd_ro', now() - interval '1 day')""", (uri_,))
        assert s.one("SELECT count(*) FROM layers.rag_exclusions")[0] == 1
        s.q("""INSERT INTO layers.rag_overrides (id, passage_uri, action, reason, decision_id)
               VALUES ('ro_2', %s, 'include', 'relevant again', 'd_ro')""", (uri_,))
        assert s.one("SELECT count(*) FROM layers.rag_exclusions")[0] == 0


def test_migrations_are_idempotent(pg):
    from conftest import uri as dburi
    from napkin_layers.migrate import migrate
    with psycopg.connect(dburi(pg, "napkin_category", "napkin_admin"), autocommit=True) as c:
        assert migrate(c, "category", "napkin_category_owner", "napkin_category_app") == []
        with pytest.raises(ValueError, match="migrated as"):
            migrate(c, "agency", "napkin_category_owner", "napkin_category_app", "org/acme")
