"""0006: a dated fact says whether its period was stated or worked out, and
"stated" is checked against its own passages, not taken on trust."""

from __future__ import annotations

from test_layers_schema import Y2025, refused


def basis(s, fact_id):
    return s.one("SELECT period_basis FROM layers.facts WHERE id = %s", (fact_id,))[0]


def test_a_period_the_passage_names_is_stated(session):
    with session() as s:
        r = s.append(0.061, quote="6.1% in 2025", excerpt=s.capture(text="Cider held 6.1% in 2025."),
                     period=Y2025, key="market.player_share", qualifier="A", unit="proportion", period_basis="stated")
        assert basis(s, r["fact"]) == "stated"


def test_stated_is_refused_when_the_year_is_not_in_the_passage(session):
    with session() as s:
        exc = s.capture(text="Its share remained unchanged compared to 2024, at 6.1%.")
        refused(s, lambda: s.append(0.061, quote="at 6.1%", excerpt=exc, period=Y2025,
                                    key="market.player_share", qualifier="B", unit="proportion",
                                    period_basis="stated"), "appears in none")
        r = s.append(0.061, quote="at 6.1%", excerpt=exc, period=Y2025, key="market.player_share", qualifier="B", unit="proportion",
                     period_basis="inferred")
        assert basis(s, r["fact"]) == "inferred"      # the same fact, honestly labelled, goes in


def test_one_false_claim_does_not_spoil_the_transaction(session):
    with session() as s:
        bad = s.capture(text="Share was 5% last year.")
        refused(s, lambda: s.append(0.05, quote="5%", excerpt=bad, period=Y2025,
                                    key="market.player_share", qualifier="C", unit="proportion",
                                    period_basis="stated"), "appears in none")
        ok = s.append(0.04, quote="4% in 2025", excerpt=s.capture(text="Share was 4% in 2025."), period=Y2025,
                      key="market.player_share", qualifier="D", unit="proportion", period_basis="stated")
        s.conn.commit()                                  # commits cleanly
        assert ok["outcome"] == "created"


def test_a_dated_fact_must_say_how_its_period_is_known(session):
    with session() as s:
        exc = s.capture(text="Worth €432 million in 2025.")
        from psycopg.types.json import Jsonb
        fact = {"id": "f_pb1", "layer": "category", "entity": "category/automotive.ev_charging",
                "key": "market.size_value", "market": "IE", "market_basis": "stated", "value": 432000000,
                "unit": "eur", "licence": "open", "period_start": "2025-01-01", "period_end": "2025-12-31"}
        ev = [{"excerpt_id": exc[0], "quote": "€432 million", "quote_start": 9}]
        refused(s, lambda: s.one("SELECT layers.append_fact(%s, %s, %s)",
                                 (Jsonb(fact), Jsonb(ev), Jsonb({"id": "d_pb1", "kind": "pin"}))),
                "stated or inferred")


def test_period_basis_without_a_period_is_refused(session):
    with session() as s:
        refused(s, lambda: s.append(1, quote="€432 million", excerpt=s.capture(), key="market.size_value",
                                    period_basis="stated"), "without a period")


def test_period_basis_never_changes(session):
    with session() as s:
        r = s.append(1, quote="€432 million", excerpt=s.capture(), period=Y2025, key="market.size_value")
        refused(s, lambda: s.q("UPDATE layers.facts SET period_basis = 'stated' WHERE id = %s", (r["fact"],)),
                "permission denied|immutable")
