"""0008 retraction and 0009 shared scope."""

from __future__ import annotations

from psycopg.types.json import Jsonb

from test_layers_schema import Y2025, refused

DEC = {"kind": "classify", "handler": "test"}


def retract(s, fact, n, reason="wrong source"):
    return s.one("SELECT layers.retract_fact(%s, %s, %s)", (fact, reason, Jsonb(dict(DEC, id=f"d_r{n}"))))[0]


# ── retraction ─────────────────────────────────────────────────────────────────

def test_a_retracted_fact_leaves_use_but_stays_in_history(session):
    with session() as s:
        f = s.append(1, quote="€432 million", excerpt=s.capture(), period=Y2025, entity="category/retail.discounters")
        assert retract(s, f["fact"], 1)["outcome"] == "retracted"
        assert s.status(f["fact"]) == "retracted"
        assert s.one("SELECT reason FROM layers.retractions WHERE fact_id = %s", (f["fact"],))[0] == "wrong source"
        assert retract(s, f["fact"], 2)["outcome"] == "already_retracted"
        again = s.append(1, quote="€432 million", excerpt=s.capture(uri="https://again.example"), period=Y2025,
                         entity="category/retail.discounters")
        assert again["outcome"] == "created" and again["fact"] != f["fact"]   # a retracted fact never corroborates


def test_retracting_one_side_of_a_contest_settles_it(session):
    with session() as s:
        e = "category/retail.ecommerce"
        a = s.append(0.2, quote="20%", excerpt=s.capture(text="Online was 20% in 2025."), period=Y2025,
                     key="market.channel_share", qualifier="online", unit="proportion", entity=e)
        b = s.append(0.25, quote="25%", excerpt=s.capture(uri="https://b2.example", text="Online was 25% in 2025."),
                     period=Y2025, key="market.channel_share", qualifier="online", unit="proportion", entity=e)
        assert b["outcome"] == "contested"
        retract(s, b["fact"], 3, "misread the table")
        assert s.status(a["fact"]) == "active"


def test_a_retraction_needs_a_reason(session):
    with session() as s:
        f = s.append(1, quote="€432 million", excerpt=s.capture(), period=Y2025, entity="category/retail.diy_home")
        refused(s, lambda: s.one("SELECT layers.retract_fact(%s, ' ', %s)", (f["fact"], Jsonb(dict(DEC, id="d_r4")))),
                "needs a reason")


def test_a_retraction_is_never_undone_or_edited(session):
    with session() as s:
        f = s.append(1, quote="€432 million", excerpt=s.capture(), period=Y2025, entity="category/retail.diy_home")
        retract(s, f["fact"], 5)
        refused(s, lambda: s.q("DELETE FROM layers.retractions"), "permission denied|append-only")


def test_a_cell_cannot_cite_a_retracted_fact(session):
    with session() as s:
        leaf = "retail.department_stores"
        s.one("SELECT layers.open_cells(%s, 'IE')", (leaf,))
        f = s.append(1, quote="€432 million", excerpt=s.capture(), period=Y2025, entity="category/" + leaf)
        retract(s, f["fact"], 6)
        refused(s, lambda: s.one("SELECT layers.confirm_cell(%s)", (Jsonb({
            "leaf": leaf, "lens": "market_structure", "market": "IE", "summary": "x", "change_note": "First version.",
            "author": "agent:t", "citations": [{"fact_id": f["fact"]}],
            "decision": {"id": "d_c6", "kind": "synthesis"}}),)), "retracted")


# ── shared scope ───────────────────────────────────────────────────────────────

def ad(s, leaf, uri, value=345000000):
    return s.append(value, quote="€345m", excerpt=s.capture(uri=uri, text="Search spend was €345m in 2025."),
                    period=Y2025, key="media.channel_adspend", qualifier="search", entity="category/" + leaf,
                    period_basis="stated")


def test_the_same_figure_across_verticals_moves_to_all(session):
    with session() as s:
        a = ad(s, "alcohol.cider", "https://iab.example/1")
        b = ad(s, "retail.discounters", "https://iab.example/2")
        out = s.one("SELECT layers.promote_shared(NULL, 'run1')")[0]
        assert out == {"promoted": 1, "retracted": 2}
        assert s.status(a["fact"]) == "retracted" and s.status(b["fact"]) == "retracted"
        row = s.one("""SELECT id, status, period_basis, (SELECT count(*) FROM layers.evidence e WHERE e.fact_id = f.id)
                         FROM layers.facts f WHERE entity = 'category/all' AND key = 'media.channel_adspend'""")
        assert row[1] == "active" and row[2] == "stated" and row[3] == 2        # both copies' evidence
        assert "moved to category/all" in s.one("SELECT reason FROM layers.retractions WHERE fact_id = %s",
                                                (a["fact"],))[0]


def test_the_same_figure_within_one_vertical_moves_to_the_vertical(session):
    with session() as s:
        for i, leaf in enumerate(("alcohol.cider", "alcohol.wine", "alcohol.irish_whiskey")):
            s.append(25, quote="25", excerpt=s.capture(uri=f"https://asai.example/{i}",
                                                       text="People shown must look over 25."),
                     key="regulation.age_rule", qualifier="people shown in ads", unit="years",
                     entity="category/" + leaf)
        s.one("SELECT layers.promote_shared(NULL, 'run2')")
        assert s.one("SELECT entity FROM layers.facts WHERE key = 'regulation.age_rule' AND status = 'active'")[0] == \
            "category/alcohol"


def test_a_figure_under_one_leaf_stays_there(session):
    with session() as s:
        a = ad(s, "retail.ecommerce", "https://iab.example/solo", value=999)
        assert s.one("SELECT layers.promote_shared(NULL, 'run3')")[0]["promoted"] == 0
        assert s.status(a["fact"]) == "active"


def test_a_leaf_cell_may_cite_its_vertical_and_all_categories(session):
    with session() as s:
        ad(s, "alcohol.cider", "https://iab.example/3")
        ad(s, "retail.convenience_forecourt", "https://iab.example/4")
        s.one("SELECT layers.promote_shared(NULL, 'run4')")
        shared = s.one("SELECT id FROM layers.facts WHERE entity = 'category/all' AND status = 'active'")[0]
        s.one("SELECT layers.open_cells('alcohol.cider', 'IE')")
        r = s.one("SELECT layers.confirm_cell(%s)", (Jsonb({
            "leaf": "alcohol.cider", "lens": "media_spend", "market": "IE",
            "summary": "Search spend in Ireland was €345m in 2025.", "change_note": "First version.",
            "author": "agent:t", "citations": [{"fact_id": shared}], "decision": {"id": "d_c7", "kind": "synthesis"}}),))[0]
        assert r["version"] == 1


def test_an_unknown_scope_is_still_refused(session):
    with session() as s:
        refused(s, lambda: s.append(1, quote="€432 million", excerpt=s.capture(), period=Y2025,
                                    entity="category/hoverboards"), "unknown_leaf")
