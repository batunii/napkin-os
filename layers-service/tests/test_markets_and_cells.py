"""Every fact names its place, and the cell layer (Planner Research Taxonomy,
panels 3 and 4): a dated, cited, never-overwritten summary per leaf x lens x market."""

from __future__ import annotations

import psycopg
import pytest
from psycopg.types.json import Jsonb

from test_layers_schema import Y2025, refused

LEAF = "telco.mobile_networks"          # amber: ComReg
PLAIN = "automotive.ev_charging"        # plain


def applicable(s, market):
    return s.q("SELECT code, reach FROM layers.applicable_markets(%s) ORDER BY reach", (market,)).fetchall()


# ── markets ────────────────────────────────────────────────────────────────────

def test_ireland_sees_ireland_then_the_eu_then_global(session):
    with session() as s:
        assert applicable(s, "IE") == [("IE", 0), ("EU", 1), ("GLOBAL", 2)]


def test_great_britain_is_not_in_the_eu(session):
    with session() as s:
        assert applicable(s, "GB") == [("GB", 0), ("GLOBAL", 2)]


def test_australia_sees_only_australia_and_global(session):
    with session() as s:
        assert applicable(s, "AU") == [("AU", 0), ("GLOBAL", 2)]


def test_an_unknown_market_applies_to_nothing(session):
    with session() as s:
        assert applicable(s, "UNKNOWN") == []
        assert all(code != "UNKNOWN" for m in ("IE", "GB", "AU", "EU") for code, _ in applicable(s, m))


def test_the_uk_is_not_a_code(session):
    with session() as s:
        refused(s, lambda: s.append(1, quote="€432 million", excerpt=s.capture(), period=Y2025, market="UK"),
                "foreign key|violates")


def test_every_fact_names_its_market(session):
    with session() as s:
        exc = s.capture()
        refused(s, lambda: s.one(
            """SELECT layers.append_fact('{"id":"f_nm","layer":"category","entity":"category/automotive.ev_charging",
               "key":"market.size_eur","value":1,"unit":"eur","licence":"open"}', %s, '{"id":"d_nm","kind":"pin"}')""",
            (Jsonb([{"excerpt_id": exc[0], "quote": "€432", "quote_start": 22}]),)), "names its market")


def test_unknown_market_needs_unknown_basis_and_back(session):
    with session() as s:
        refused(s, lambda: s.append(1, quote="€432 million", excerpt=s.capture(), period=Y2025,
                                    market="UNKNOWN", market_basis="stated"), "check")
        refused(s, lambda: s.append(1, quote="€432 million", excerpt=s.capture(), period=Y2025,
                                    market="IE", market_basis="unknown"), "check")


def test_an_irish_and_an_eu_figure_never_contest(session):
    with session() as s:
        ie = s.append(0.20, quote="20% in Ireland", excerpt=s.capture(text="EV share was 20% in Ireland."),
                      period=Y2025, key="market.segment_share", qualifier="BEV", unit="proportion")
        eu = s.append(0.15, quote="15% across the EU", excerpt=s.capture(uri="https://eu.example", text="15% across the EU."),
                      period=Y2025, key="market.segment_share", qualifier="BEV", unit="proportion", market="EU")
        assert ie["outcome"] == "created" and eu["outcome"] == "created"
        assert s.status(ie["fact"]) == "active" and s.status(eu["fact"]) == "active"


# ── cells ──────────────────────────────────────────────────────────────────────

def dec(n):
    return {"id": f"d_cell_{n}", "kind": "synthesis", "handler": "research_lens@1", "rationale": "test"}


def confirm(s, n, citations, lens="market_structure", leaf=PLAIN, market="IE", author="agent:research_lens@1",
            summary="Three players; public charging grew 38% in 2025.", change="first version"):
    return s.one("SELECT layers.confirm_cell(%s)", (Jsonb({
        "leaf": leaf, "lens": lens, "market": market, "summary": summary, "change_note": change,
        "author": author, "citations": citations, "decision": dec(n)}),))[0]


def test_opening_a_leaf_creates_its_eight_cells_once(session):
    with session() as s:
        assert s.one("SELECT layers.open_cells(%s, 'IE')", (PLAIN,))[0] == 8
        assert s.one("SELECT layers.open_cells(%s, 'IE')", (PLAIN,))[0] == 0
        states = s.q("SELECT DISTINCT state, verified FROM layers.cell_status WHERE leaf = %s AND market = 'IE'",
                     (PLAIN,)).fetchall()
        assert states == [("empty", False)]


def test_a_cell_must_be_opened_before_it_is_confirmed(session):
    with session() as s:
        f = s.append(432_000_000, quote="€432 million in 2025", excerpt=s.capture(), period=Y2025)
        refused(s, lambda: confirm(s, "closed", [{"fact_id": f["fact"]}], market="GB"), "unknown_cell")


def test_confirming_versions_a_cell_and_keeps_every_version(session):
    with session() as s:
        s.one("SELECT layers.open_cells(%s, 'IE')", (PLAIN,))
        f = s.append(432_000_000, quote="€432 million in 2025", excerpt=s.capture(), period=Y2025)
        v1 = confirm(s, "v1", [{"fact_id": f["fact"]}])
        v2 = confirm(s, "v2", [{"fact_id": f["fact"]}], change="No change; re-confirmed against SEAI.")
        assert (v1["version"], v2["version"]) == (1, 2)
        st = s.one("SELECT version, state, sources, thin, verified FROM layers.cell_status "
                   "WHERE leaf = %s AND lens = 'market_structure' AND market = 'IE'", (PLAIN,))
        assert st == (2, "fresh", 1, True, False)
        refused(s, lambda: s.q("UPDATE layers.cell_versions SET summary = 'x'"), "permission denied|append-only")


def test_a_cell_is_stale_when_its_clock_runs_out(session, pg):
    """The owner sets confirmed_at into the past (the app cannot): stale follows the lens cadence."""
    from conftest import uri as dburi
    with session() as s:
        s.one("SELECT layers.open_cells(%s, 'IE')", (PLAIN,))
        f = s.append(1, quote="1 player", excerpt=s.capture(text="Only 1 player in 2025."), period=Y2025,
                     key="market.top_n_share", qualifier="top 1", unit="proportion")
        v = confirm(s, "stale", [{"fact_id": f["fact"]}])
        s.conn.commit()
    with psycopg.connect(dburi(pg, "napkin_category", "napkin_admin"), autocommit=True) as a:
        a.execute("SET ROLE napkin_category_owner")
        a.execute("ALTER TABLE layers.cell_versions DISABLE TRIGGER cell_versions_frozen")
        a.execute("UPDATE layers.cell_versions SET confirmed_at = now() - interval '4 months' WHERE id = %s", (v["id"],))
        a.execute("ALTER TABLE layers.cell_versions ENABLE TRIGGER cell_versions_frozen")
    with session() as s:
        assert s.one("SELECT state FROM layers.cell_status WHERE leaf = %s AND lens = 'market_structure' "
                     "AND market = 'IE'", (PLAIN,))[0] == "stale"


def test_an_amber_leaf_keeps_regulation_on_a_monthly_clock(session, pg):
    """If the lens cadence for regulation is relaxed to quarterly for plain leaves, an
    amber leaf (its own regulator) still refreshes monthly."""
    import datetime as dt
    from conftest import uri as dburi
    with session() as s:
        s.one("SELECT layers.open_cells(%s, 'IE')", (LEAF,))
        s.one("SELECT layers.open_cells(%s, 'IE')", (PLAIN,))
        s.conn.commit()
    owner = psycopg.connect(dburi(pg, "napkin_category", "napkin_admin"), autocommit=True)
    owner.execute("SET ROLE napkin_category_owner")
    try:
        owner.execute("UPDATE layers.lenses SET cadence = '3 months' WHERE code = 'regulation_clearance'")
        with session() as s:
            q = ("SELECT amber, cadence FROM layers.cell_status WHERE leaf = %s "
                 "AND lens = 'regulation_clearance' AND market = 'IE'")
            assert s.one(q, (LEAF,)) == (True, dt.timedelta(days=30))
            assert s.one(q, (PLAIN,)) == (False, dt.timedelta(days=90))
    finally:
        owner.execute("UPDATE layers.lenses SET cadence = '1 month' WHERE code = 'regulation_clearance'")
        owner.close()


def test_a_cell_cites_only_facts_that_answer_it(session):
    with session() as s:
        s.one("SELECT layers.open_cells(%s, 'IE')", (PLAIN,))
        media = s.append(5, quote="5 million", excerpt=s.capture(text="Spend of 5 million in 2025."), period=Y2025,
                         key="media.adspend_total", qualifier="whole market")
        refused(s, lambda: confirm(s, "wronglens", [{"fact_id": media["fact"]}]), "do not answer")
        gb = s.append(9, quote="9 players", excerpt=s.capture(uri="https://gb.example", text="9 players in 2025."),
                      period=Y2025, key="market.player_count", unit="count", market="GB")
        refused(s, lambda: confirm(s, "wrongmarket", [{"fact_id": gb["fact"]}]), "do not answer")


def test_an_ie_cell_may_cite_eu_and_global_facts(session):
    with session() as s:
        s.one("SELECT layers.open_cells(%s, 'IE')", (PLAIN,))
        eu = s.append(0.15, quote="15% across the EU", excerpt=s.capture(uri="https://eu2.example",
                      text="15% across the EU."), period=Y2025, key="market.segment_share", qualifier="EV",
                      unit="proportion", market="EU")
        gl = s.append(17_000_000, quote="17 million", excerpt=s.capture(uri="https://iea.example",
                      text="17 million EVs sold worldwide in 2025."), period=Y2025, key="market.size_volume",
                      unit="units", market="GLOBAL")
        r = confirm(s, "eugl", [{"fact_id": eu["fact"]}, {"fact_id": gl["fact"]}])
        assert r["sources"] == 2


def test_qualitative_claims_cite_verbatim_excerpts(session):
    with session() as s:
        s.one("SELECT layers.open_cells(%s, 'IE')", (LEAF,))
        a = s.capture(uri="https://adarchive.example/1", text="Coverage maps and speed-test bars in every spot.")
        b = s.capture(uri="https://trade.example/2", text="The family on the sofa is the category's worn-out trope.")
        c = s.capture(uri="https://judged.example/3", text="Upbeat pop tracks dominate telco ads.")
        r = confirm(s, "codes", [{"excerpt_id": a[0]}, {"excerpt_id": b[0]}, {"excerpt_id": c[0]}], lens="category_codes",
                    leaf=LEAF, summary="Coverage maps, speed bars, sofa families, upbeat pop.")
        assert r["sources"] == 3
        assert s.one("SELECT thin FROM layers.cell_status WHERE leaf = %s AND lens = 'category_codes' AND market = 'IE'",
                     (LEAF,))[0] is False


def test_a_person_verifies_an_agent_version(session):
    with session() as s:
        s.one("SELECT layers.open_cells(%s, 'IE')", (LEAF,))
        a = s.capture(uri="https://asai.example/r", text="ASAI upheld a speed-claim complaint.")
        r = confirm(s, "reg", [{"excerpt_id": a[0]}], lens="regulation_clearance", leaf=LEAF,
                    summary="ASAI polices speed claims.")
        s.q("INSERT INTO layers.decisions (id, kind, org, body) VALUES ('d_ver', 'approve', 'org/acme', '{}')")
        s.q("INSERT INTO layers.cell_verifications (version_id, verified_by, decision_id) VALUES (%s, 'human:planner', 'd_ver')",
            (r["id"],))
        assert s.one("SELECT verified FROM layers.cell_status WHERE leaf = %s AND lens = 'regulation_clearance' "
                     "AND market = 'IE'", (LEAF,))[0] is True


def test_the_writer_org_is_stamped_from_scope(session):
    with session(org="org/globex") as s:
        s.one("SELECT layers.open_cells(%s, 'GB')", (PLAIN,))
        assert s.one("SELECT opened_by_org FROM layers.cells WHERE leaf = %s AND market = 'GB' LIMIT 1",
                     (PLAIN,))[0] == "org/globex"


# ── confidence is derived by the database (0005) ──────────────────────────────

def test_confidence_is_derived_from_the_sources_not_sent(session):
    with session() as s:
        s.one("SELECT layers.open_cells(%s, 'IE')", (LEAF,))
        one = s.capture(uri="https://c1.example", text="Mobile code one.", publisher="A", tier="secondary")
        two = s.capture(uri="https://c2.example", text="Mobile code two.", publisher="B", tier="secondary")
        three = s.capture(uri="https://c3.example", text="Mobile code three.", publisher="C", tier="primary")
        same = s.capture(uri="https://c4.example", text="Mobile code four.", publisher="A", tier="secondary")
        low = confirm(s, "c-low", [{"excerpt_id": one[0]}, {"excerpt_id": same[0]}], lens="category_codes", leaf=LEAF)
        mid = confirm(s, "c-mid", [{"excerpt_id": one[0]}, {"excerpt_id": two[0]}], lens="category_codes", leaf=LEAF)
        high = confirm(s, "c-high", [{"excerpt_id": one[0]}, {"excerpt_id": two[0]}, {"excerpt_id": three[0]}],
                       lens="category_codes", leaf=LEAF)
        assert (low["confidence"], low["publishers"]) == ("low", 1)      # two pages, one publisher
        assert (mid["confidence"], mid["publishers"]) == ("medium", 2)
        assert (high["confidence"], high["publishers"]) == ("high", 3)


def test_three_publishers_without_a_primary_source_are_medium(session):
    with session() as s:
        s.one("SELECT layers.open_cells(%s, 'IE')", (LEAF,))
        ids = [s.capture(uri=f"https://d{i}.example", text=f"Rhythm note {i}.", publisher=p, tier="tertiary")[0]
               for i, p in enumerate("XYZ")]
        r = confirm(s, "d-mid", [{"excerpt_id": e} for e in ids], lens="rhythm_moments", leaf=LEAF)
        assert r["confidence"] == "medium"


def test_a_self_reported_confidence_is_refused(session):
    with session() as s:
        s.one("SELECT layers.open_cells(%s, 'IE')", (LEAF,))
        e = s.capture(uri="https://e.example", text="Media note.")
        refused(s, lambda: s.one("SELECT layers.confirm_cell(%s)", (Jsonb({
            "leaf": LEAF, "lens": "media_spend", "market": "IE", "summary": "x", "change_note": "First version.",
            "confidence": "high", "author": "agent:t", "citations": [{"excerpt_id": e[0]}],
            "decision": dec("self")}),)), "derived from the sources")
