"""The seeding run pauses, resumes and waits out limits instead of failing."""

from __future__ import annotations

import threading
import time

import pytest

from napkin.model import ModelError
from napkin.research import ResearchError
from napkin.seed import control as ctl
from napkin.seed.control import Control, GaveUp, Stopped
from napkin.seed.pagecheck import page_text


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(ctl, "DELAYS", [0.01, 0.01, 0.01])
    monkeypatch.setattr(ctl, "LONG_WAIT", 0.05)


def test_a_temporary_failure_is_retried_not_failed(tmp_path):
    c = Control(tmp_path)
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise ResearchError("research returned 502")
        return {"ok": True}
    assert c.call("research", flaky) == {"ok": True}
    assert len(calls) == 3


def test_a_real_error_is_not_retried(tmp_path):
    c = Control(tmp_path)
    with pytest.raises(ModelError):
        c.call("m", lambda: (_ for _ in ()).throw(ModelError("bad schema", "invalid_request")))


def test_a_failure_that_outlasts_every_retry_leaves_the_unit_for_later(tmp_path):
    c = Control(tmp_path)
    with pytest.raises(GaveUp):
        c.call("m", lambda: (_ for _ in ()).throw(ModelError("busy", "overloaded")))


def test_a_rate_limit_holds_the_whole_run(tmp_path):
    c = Control(tmp_path)
    c.hold_all(0.3, "limit")
    t0 = time.monotonic()
    c.wait_turn()
    assert time.monotonic() - t0 >= 0.25


def test_answers_are_cached_so_a_resume_never_pays_twice(tmp_path):
    n = []
    fn = lambda: n.append(1) or {"v": len(n)}
    assert Control(tmp_path).call("research", fn, cache_key=["q"]) == {"v": 1}
    assert Control(tmp_path).call("research", fn, cache_key=["q"]) == {"v": 1}
    assert len(n) == 1


def test_the_pause_file_pauses_until_removed(tmp_path):
    c = Control(tmp_path)
    (tmp_path / "PAUSE").touch()
    done = threading.Event()
    threading.Thread(target=lambda: (c.wait_turn(), done.set()), daemon=True).start()
    assert not done.wait(0.5)
    assert '"paused": true' in (tmp_path / "status.json").read_text()
    (tmp_path / "PAUSE").unlink()
    assert done.wait(7)


def test_the_stop_file_stops_cleanly_and_a_new_start_clears_it(tmp_path):
    c = Control(tmp_path)
    (tmp_path / "STOP").touch()
    with pytest.raises(Stopped):
        c.wait_turn()
    Control(tmp_path).wait_turn()          # a new start is a resume


def test_three_give_ups_in_a_row_make_the_run_wait(tmp_path):
    c = Control(tmp_path)
    for _ in range(3):
        c.unit_done({"gave_up": True, "left_for_next_run": True})
    assert c._capacity_until > time.time() - 0.01


def test_page_text_reads_metadata_dates_and_skips_scripts():
    html = b"""<html><head><title>Cider 2025</title>
      <meta property="article:published_time" content="2026-03-14T09:00:00Z">
      <script>var x = "not text";</script></head>
      <body><p>Cider held 6.1% of the market in 2025.</p></body></html>"""
    text, title, pub = page_text(html, "text/html")
    assert "6.1% of the market in 2025" in text and "not text" not in text
    assert title == "Cider 2025" and pub == "2026-03-14"


def test_page_text_falls_back_to_json_ld():
    html = b"""<html><head><script type="application/ld+json">{"@type":"NewsArticle","datePublished":"2025-11-02"}
      </script></head><body><p>Body</p></body></html>"""
    assert page_text(html, "text/html")[2] == "2025-11-02"


# ── the cell figure check (cases from the first live run) ─────────────────────

from napkin.seed.run import Seeder  # noqa: E402


def judge(summary, cited, facts=(), passages=()):
    s = Seeder.__new__(Seeder)
    kf = {f["id"]: f for f in facts}
    kp = {p["id"]: p for p in passages}
    return Seeder._judge_cell(s, {"summary": summary, "cited_ids": cited}, kf, kp)


P_SHELF = {"id": "exc_a", "publisher": "Shelflife", "published_at": "2026-05-13",
           "text": "Bulmers Lime and Berries aims to elevate flavour in cider."}
F_CASES = {"id": "f_b", "key": "market.cases", "value": "16150000", "unit": "count", "quote": "16.15 million cases",
           "market": "GLOBAL", "status": "active", "period_start": None, "period_end": None}
P_MURPHY = {"id": "exc_e4e2705883a697f6cbea", "publisher": "Irish Times", "published_at": None,
            "text": "Murphy's 170 Whiskey Cask Stout was in more than 80 outlets."}


def test_a_date_is_not_a_figure():
    verdict, summary, _ = judge("Shelflife Magazine (13 May 2026) links this to fruit-forward cider.", ["exc_a"],
                                passages=[P_SHELF])
    assert verdict is None and "13 May 2026" in summary


def test_a_number_the_model_calculated_is_refused():
    verdict, _, _ = judge("Ireland is roughly 4% of the 16.15 million cases, our own arithmetic.", ["f_b"],
                          facts=[F_CASES])
    assert verdict and "4" in verdict


def test_a_figure_in_given_but_uncited_material_gets_cited():
    verdict, _, cited = judge("Murphy's 170 Whiskey Cask Stout reached more than 80 outlets.", ["exc_a"],
                              passages=[P_SHELF, P_MURPHY])
    assert verdict is None and "exc_e4e2705883a697f6cbea" in cited


def test_citation_ids_are_taken_out_of_the_text():
    verdict, summary, _ = judge("Murphy's 170 reached 80 outlets (exc_e4e2705883a697f6cbea).",
                                ["exc_e4e2705883a697f6cbea"], passages=[P_MURPHY])
    assert verdict is None and "exc_" not in summary and "()" not in summary


# ── period basis and the measure list: the runner labels honestly, the database enforces

MEASURE = {"key": "market.player_share", "units": ["proportion"], "cardinality": "one", "qualifier": "player",
           "definition": "A named player's share.", "search_group": 2}
LEADER = {"key": "market.leader", "units": ["text"], "cardinality": "one", "qualifier": "none",
          "definition": "The leader.", "search_group": 2}
BY_KEY = {m["key"]: m for m in (MEASURE, LEADER)}


def _src(text):
    return {"s0": {"sid": "s0", "publisher": "Just Drinks", "tier": "tertiary", "excerpts": [text],
                   "excerpt_ids": ["exc_63e8d0c5551fd74f394c"]}}


def _fact(**kw):
    f = {"key_suffix": "player_share", "qualifier": "Bulmers", "value_number": 0.061, "unit": "proportion",
         "period_start": "2025-01-01", "period_end": "2025-12-31", "period_basis": "stated", "market": "IE",
         "market_basis": "stated", "evidence": [{"source_id": "s0", "excerpt_index": 0, "quote": "6.1%"}]}
    f.update(kw)
    return f


def _check(f, text):
    return Seeder._check(Seeder.__new__(Seeder), f, "alcohol.cider", "market", _src(text), BY_KEY)


def test_a_stated_period_whose_year_is_not_in_the_passage_becomes_inferred():
    fact, _, pubs = _check(_fact(), "Its share remained unchanged compared to 2024, at 6.1%.")
    assert fact["period_basis"] == "inferred" and pubs == {"Just Drinks"}


def test_a_stated_period_whose_year_is_in_the_passage_stays_stated():
    fact, _, _ = _check(_fact(), "Bulmers held 6.1% of the market in 2025.")
    assert fact["period_basis"] == "stated" and fact["qualifier"] == "Bulmers"


def test_an_undated_fact_carries_no_period_basis():
    fact, _, _ = _check(_fact(period_start=None, period_end=None, period_basis=None), "A 6.1% share.")
    assert "period_basis" not in fact and "period_start" not in fact


def test_an_unlisted_measure_is_rejected_by_the_runner():
    assert _check(_fact(key_suffix="share_bulmers"), "Bulmers 6.1% in 2025.") == "not a listed measure"


def test_a_qualified_measure_without_its_qualifier_is_rejected():
    assert _check(_fact(qualifier=None), "Bulmers 6.1% in 2025.") == "qualifier missing"


def test_a_unit_the_measure_does_not_take_is_rejected():
    assert _check(_fact(unit="eur", value_number=5), "Bulmers had 5 in 2025.") == "unit not allowed for the measure"


def test_a_text_fact_gets_the_text_unit():
    fact, _, _ = _check(_fact(key_suffix="leader", qualifier=None, value_number=None, value_text="Bulmers",
                              unit="code", period_start=None, period_end=None, period_basis=None,
                              evidence=[{"source_id": "s0", "excerpt_index": 0, "quote": "Bulmers"}]),
                        "Bulmers is the market leader.")
    assert fact["unit"] == "text" and fact["key"] == "market.leader"


GROWTH = {"key": "market.volume_growth_yoy", "units": ["proportion"], "cardinality": "one", "qualifier": "none",
          "definition": "Volume change.", "search_group": 1}


def test_a_falling_growth_rate_is_kept_but_a_negative_share_is_not():
    by_key = dict(BY_KEY, **{GROWTH["key"]: GROWTH})
    s = Seeder.__new__(Seeder)
    fall = _fact(key_suffix="volume_growth_yoy", qualifier=None, value_number=-0.002,
                 evidence=[{"source_id": "s0", "excerpt_index": 0, "quote": "down 0.2%"}])
    fact, _, _ = Seeder._check(s, fall, "alcohol.cider", "market", _src("Volumes were down 0.2% in 2025."), by_key)
    assert fact["value"] == -0.002
    rise = _fact(key_suffix="volume_growth_yoy", qualifier=None, value_number=-0.002,
                 evidence=[{"source_id": "s0", "excerpt_index": 0, "quote": "up 0.2%"}])
    assert Seeder._check(s, rise, "alcohol.cider", "market", _src("Volumes were up 0.2% in 2025."), by_key) == \
        "quote not verbatim or does not state the value"          # a rise is never stored as a fall
    bad = _fact(value_number=-0.1)
    assert Seeder._check(s, bad, "alcohol.cider", "market", _src("Bulmers -10% in 2025."), by_key) == \
        "proportion out of range"


def test_many_workers_writing_status_at_once_never_fail(tmp_path):
    """The first re-pilot crashed units when four workers renamed one shared tmp file."""
    c = Control(tmp_path)
    errors = []

    def hammer():
        try:
            for _ in range(200):
                c.write_status()
        except Exception as e:  # pragma: no cover - the bug this guards
            errors.append(e)
    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and (tmp_path / "status.json").exists()
    assert not list(tmp_path.glob("status.json.*.tmp"))
