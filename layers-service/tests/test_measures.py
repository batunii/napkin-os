"""0007: the category layer speaks the measure list; qualifiers tell rows of a
measure apart; list measures never contest."""

from __future__ import annotations

from psycopg.types.json import Jsonb

from test_layers_schema import Y2025, refused

Y2024 = ("2024-01-01", "2024-12-31")
AG = dict(db="napkin_agency_acme", org="org/acme")


def test_the_measure_list_is_loaded(session):
    with session() as s:
        assert s.one("SELECT count(*) FROM layers.measures WHERE status <> 'retired'")[0] == 66
        assert s.one("SELECT cardinality, qualifier FROM layers.measures WHERE key = 'positioning.claim'") == \
            ("many", "player")


def test_an_unlisted_measure_is_refused(session):
    with session() as s:
        refused(s, lambda: s.append(1, quote="€432 million", excerpt=s.capture(), period=Y2025,
                                    key="market.share_aldi"), "unknown_measure")


def test_a_measure_takes_only_its_units(session):
    with session() as s:
        refused(s, lambda: s.append(5, quote="€432 million", excerpt=s.capture(), period=Y2025,
                                    key="market.player_count", unit="eur"), "measured in count")


def test_a_qualified_measure_needs_its_qualifier_and_others_refuse_one(session):
    with session() as s:
        refused(s, lambda: s.append(0.2, quote="€432 million", excerpt=s.capture(), period=Y2025,
                                    key="market.player_share", unit="proportion"), "needs a qualifier")
        refused(s, lambda: s.append(1, quote="€432 million", excerpt=s.capture(), period=Y2025,
                                    qualifier="Aldi"), "takes no qualifier")


def test_qualifiers_keep_players_apart(session):
    with session() as s:
        aldi = s.append(0.24, quote="24%", excerpt=s.capture(text="Aldi held 24% in 2025."), period=Y2025,
                        key="market.player_share", qualifier="Aldi", unit="proportion",
                        entity="category/retail.discounters")
        lidl = s.append(0.21, quote="21%", excerpt=s.capture(text="Lidl held 21% in 2025."), period=Y2025,
                        key="market.player_share", qualifier="Lidl", unit="proportion",
                        entity="category/retail.discounters")
        assert aldi["outcome"] == "created" and lidl["outcome"] == "created"
        again = s.append(0.24, quote="24%", excerpt=s.capture(uri="https://kantar.example", text="Aldi at 24% in 2025."),
                         period=Y2025, key="market.player_share", qualifier="  ALDI ", unit="proportion",
                         entity="category/retail.discounters")
        assert again == {"fact": aldi["fact"], "outcome": "corroborated"}   # qualifier compared normalised
        rival = s.append(0.26, quote="26%", excerpt=s.capture(uri="https://other.example", text="Aldi 26% in 2025."),
                         period=Y2025, key="market.player_share", qualifier="Aldi", unit="proportion",
                         entity="category/retail.discounters")
        assert rival["outcome"] == "contested"                               # same player, same period: a dispute


def test_list_items_sit_side_by_side_and_never_contest(session):
    with session() as s:
        e = "category/alcohol.cider"
        a = s.append("Crafted in Clonmel since 1935", quote="Crafted in Clonmel since 1935", entity=e,
                     excerpt=s.capture(text="Crafted in Clonmel since 1935."), key="positioning.claim",
                     qualifier="Bulmers", unit="text")
        b = s.append("Ireland's number one cider", quote="Ireland's number one cider", entity=e,
                     excerpt=s.capture(uri="https://cc.example", text="Ireland's number one cider."),
                     key="positioning.claim", qualifier="Bulmers", unit="text")
        assert a["outcome"] == "added" and b["outcome"] == "added"
        assert s.status(a["fact"]) == "active" and s.status(b["fact"]) == "active"
        same = s.append("Crafted in Clonmel since 1935", quote="Crafted in Clonmel since 1935", entity=e,
                        excerpt=s.capture(uri="https://third.example", text="Crafted in Clonmel since 1935, it says."),
                        key="positioning.claim", qualifier="Bulmers", unit="text")
        assert same == {"fact": a["fact"], "outcome": "corroborated"}


def test_a_dated_list_item_does_not_push_an_older_one_out(session):
    with session() as s:
        e = "category/retail.department_stores"
        old = s.append("Christmas", quote="Christmas", entity=e, excerpt=s.capture(text="Christmas in 2024."),
                       period=Y2024, key="rhythm.key_moment", unit="text")
        new = s.append("Black Friday", quote="Black Friday", entity=e,
                       excerpt=s.capture(uri="https://bf.example", text="Black Friday in 2025."), period=Y2025,
                       key="rhythm.key_moment", unit="text")
        assert (old["outcome"], new["outcome"]) == ("added", "added")
        assert s.status(old["fact"]) == "active"


def test_an_unlisted_measure_is_proposed_not_invented(session):
    with session() as s:
        exc = s.capture(text="Easter egg spend reached €32m in 2025.")
        s.q("""INSERT INTO layers.measure_proposals (id, lens, proposed, entity, market, value, unit, excerpt_id, quote,
                                                     proposed_by_org)
               VALUES ('mp_1', 'rhythm_moments', 'rhythm.easter_egg_spend', 'category/food.snacks_confectionery',
                       'IE', '32000000', 'eur', %s, '€32m', 'org/forged')""", (exc[0],))
        assert s.one("SELECT proposed_by_org FROM layers.measure_proposals WHERE id = 'mp_1'")[0] == "org/acme"
        refused(s, lambda: s.q("UPDATE layers.measure_proposals SET value = 'x'"), "permission denied|append-only")


def test_brand_facts_are_free_of_the_category_list_but_get_period_rules(session):
    with session(**AG, brand="brand/lunasa") as s:
        exc = s.capture(uri="https://acme.example/b2", text="Lunasa awareness was 40% in 2025.",
                        licence="client-confidential")
        r = s.append(0.4, quote="40%", excerpt=exc, layer="brand", entity="brand/lunasa",
                     key="brand.awareness_custom", unit="proportion", licence="client-confidential",
                     period=Y2025, period_basis="stated")
        assert r["outcome"] == "created"
        assert s.one("SELECT period_basis FROM layers.facts WHERE id = %s", (r["fact"],))[0] == "stated"
