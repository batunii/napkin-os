"""The run tooling: invariant checks that can fail, list-price cost, the extraction scorer and the recorders."""

import json
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import extraction_eval  # noqa: E402
import invariants  # noqa: E402
import run_report  # noqa: E402

from napkin import metrics  # noqa: E402


def fact(fid="f_AAAAAA", value=432000000, unit="eur", quotes=None, sources=("src_1",)):
    return {"id": fid, "entity": "category/x", "key": "market.size_eur", "value": value, "unit": unit,
            "confidence": "low", "sources": list(sources),
            "quotes": quotes if quotes is not None else {"src_1": "The market was worth €432 million in 2025."}}


def write_run(tmp_path, facts, decisions=(), report=None):
    (tmp_path / "clan.json").write_text(json.dumps({
        "facts": facts, "findings": [], "decision_chain": {"decisions": list(decisions)},
        "data": {"selection": {"lenses_run": [{"lens": l, "market": "IE"} for l in invariants.LENSES]},
                 "report": report or {}}}))
    return tmp_path


def test_a_clean_run_has_no_violations(tmp_path):
    assert invariants.check(write_run(tmp_path, [fact()])) == []


def test_a_number_the_quote_does_not_hold_is_caught(tmp_path):
    vs = invariants.check(write_run(tmp_path, [fact(value=999, unit="count")]))
    assert [v["id"] for v in vs] == ["I2"]


def test_a_quote_naming_an_unlisted_source_is_caught(tmp_path):
    vs = invariants.check(write_run(tmp_path, [fact(quotes={"src_9": "The market was worth €432 million in 2025."})]))
    assert "I5" in {v["id"] for v in vs}


def test_a_pin_decision_without_reasoning_is_caught(tmp_path):
    vs = invariants.check(write_run(tmp_path, [fact()], decisions=[{"id": "d_1", "kind": "pin", "reasoning": {}}]))
    assert [v["id"] for v in vs] == ["I7"]


def test_a_lens_neither_run_nor_skipped_is_caught(tmp_path):
    run = write_run(tmp_path, [fact()])
    clan = json.loads((run / "clan.json").read_text())
    clan["data"]["selection"]["lenses_run"].pop()
    (run / "clan.json").write_text(json.dumps(clan))
    assert [v["id"] for v in invariants.check(run)] == ["I9"]


def test_a_report_cite_the_document_does_not_hold_is_caught(tmp_path):
    rep = {"sections": [{"blocks": [{"kind": "claim", "text": "x", "cites": ["f_ZZZZZZ"]}]}]}
    assert [v["id"] for v in invariants.check(write_run(tmp_path, [fact()], report=rep))] == ["CITE"]


def test_list_cost_prices_fresh_cache_and_output_apart():
    # Opus 5.5: $4 in, $20 out, $5 cache write, $0.20 cache read per million
    got = run_report.list_cost("claude-opus-5-5", 3_000_000, 500_000,
                               {"fresh": 1_000_000, "cache_write": 1_000_000, "cache_read": 1_000_000})
    assert got == pytest.approx(4 + 5 + 0.2 + 10)
    assert run_report.list_cost("claude-opus-5", 1_000_000, 0, None) == pytest.approx(5.0)
    assert run_report.list_cost("some-unknown-model", 10, 10, None) is None


PAYLOAD = {"lens": "market_structure", "wanted": ["size_eur", "share"],
           "sources": [{"source_id": "src_1", "excerpts": ["The market was worth €432 million in 2025, up 4%."]}]}


def good(**kw):
    f = {"key_suffix": "size_eur", "value_number": 432000000, "unit": "eur",
         "evidence": [{"source_id": "src_1", "quote": "The market was worth €432 million in 2025"}]}
    f.update(kw)
    return {"facts": [f]}


def test_the_extraction_scorer_keeps_a_verbatim_supported_fact():
    s = extraction_eval.score(PAYLOAD, good(), None)
    assert (s["returned"], s["kept"], s["verbatim"], s["figure_ok"], s["in_wanted"]) == (1, 1, 1, 1, 1)


def test_the_extraction_scorer_rejects_a_paraphrase_and_a_missing_number():
    para = extraction_eval.score(PAYLOAD, good(evidence=[{"source_id": "src_1", "quote": "Worth about 432m euros last year"}]), None)
    assert para["kept"] == 0 and para["verbatim"] == 0
    wrong = extraction_eval.score(PAYLOAD, good(value_number=999), None)
    assert wrong["kept"] == 0 and wrong["verbatim"] == 1 and wrong["figure_ok"] == 0


def test_the_extraction_scorer_counts_vocabulary_names():
    vocab = {"market_structure": ({"size_eur"}, {"market_value_eur"})}
    assert extraction_eval.score(PAYLOAD, good(), vocab)["in_vocab"] == 1
    assert extraction_eval.score(PAYLOAD, good(key_suffix="market_value_eur"), vocab)["in_vocab"] == 0


def test_the_ledger_and_the_recorder_are_off_unless_switched_on(tmp_path, monkeypatch):
    monkeypatch.delenv("NAPKIN_METRICS_FILE", raising=False)
    monkeypatch.delenv("NAPKIN_RECORD_DIR", raising=False)
    metrics.emit("stage", stage="x")
    metrics.record("model_calls", purpose="x")
    assert not list(tmp_path.iterdir())
    monkeypatch.setenv("NAPKIN_METRICS_FILE", str(tmp_path / "led.jsonl"))
    monkeypatch.setenv("NAPKIN_RECORD_DIR", str(tmp_path / "rec"))
    token = metrics.UNIT.set("media_spend/IE")
    try:
        metrics.emit("model", purpose="x")
        metrics.record("model_calls", purpose="x", reply="r")
    finally:
        metrics.UNIT.reset(token)
    assert json.loads((tmp_path / "led.jsonl").read_text())["unit"] == "media_spend/IE"
    assert json.loads((tmp_path / "rec" / "model_calls.jsonl").read_text())["reply"] == "r"


def test_the_report_splits_a_units_spend_by_the_model_that_ran(tmp_path):
    (tmp_path / "mock").mkdir()
    (tmp_path / "middleware.jsonl").write_text("")
    row = {"family": "research", "secs": 30, "cost_usd": 0.122, "turns": 8, "web_searches": 3, "web_fetches": 3,
           "by_model": {"claude-sonnet-5-5": {"in": 10, "out": 1225, "cache_read": 30416, "cache_write": 7676, "cost": 0.049},
                        "claude-haiku-4-5-20251001": {"in": 35247, "out": 1625, "cache_read": 0, "cache_write": 0, "cost": 0.073}}}
    (tmp_path / "mock" / "metrics.jsonl").write_text(json.dumps(row) + "\n")
    r = run_report.build(tmp_path)
    assert {(x["stage"], x["model"]): round(x["cost"], 3) for x in r["by_model"]} == {
        ("research", "claude-sonnet-5-5"): 0.049, ("research", "claude-haiku-4-5-20251001"): 0.073}
    assert "claude-haiku-4-5-20251001" in run_report.table(r)


def _jev_run(tmp_path, rows, log=""):
    (tmp_path / "mock").mkdir()
    (tmp_path / "middleware.jsonl").write_text("")
    (tmp_path / "mock" / "metrics.jsonl").write_text("".join(json.dumps(x) + "\n" for x in rows))
    (tmp_path / "mock.log").write_text(log)
    return run_report.build(tmp_path)


def test_search_jev_lines_are_not_research_calls_and_jev_has_its_own_row(tmp_path):
    agent = {"family": "research", "alias": "sonnet", "secs": 20, "cost_usd": 0.08, "web_searches": 2, "web_fetches": 0,
             "by_model": {"claude-sonnet-5-5": {"in": 10, "out": 500, "cache_read": 0, "cache_write": 0, "cost": 0.08}}}
    jev = {"family": "research", "alias": "jev", "secs": 2, "cost_usd": 0.005, "web_searches": 0, "web_fetches": 0,
           "reader": {"candidates": 10, "read": 8, "failed": {"bot_wall": 1, "http_error": 1}, "fallback": False,
                      "passages": 300, "kept_chars": 4100, "sources": 4, "jev_tokens": 120000}}
    fell = {"family": "research", "alias": "reader", "secs": 1, "cost_usd": 0.0, "web_searches": 0, "web_fetches": 0,
            "reader": {"candidates": 3, "read": 0, "failed": {"network": 3}, "fallback": True,
                       "passages": 0, "kept_chars": 0, "sources": 0, "jev_tokens": 0}}
    r = _jev_run(tmp_path, [agent, jev, agent, fell])
    assert r["stages"]["research"]["research_calls"] == 2                 # two agents; the reader lines are not calls
    assert round(r["stages"]["research"]["cost"], 3) == 0.165              # but their cost is in the stage
    rows = {(x["stage"], x["model"]): x for x in r["by_model"]}
    assert round(rows[("research", "jev (TypeSafe)")]["cost"], 3) == 0.005 and rows[("research", "jev (TypeSafe)")]["tin"] == 120000
    rd = r["reader"]
    assert rd["recorded"] and (rd["units"], rd["candidates"], rd["read"], rd["fallback_units"]) == (2, 13, 8, 1)
    assert rd["failed"] == {"bot_wall": 1, "http_error": 1, "network": 3}
    assert (rd["passages"], rd["kept_chars"], rd["sources"], rd["jev_tokens"]) == (300, 4100, 4, 120000)
    md = run_report.table(r)
    assert "page reader (search-jev)" in md and "bot_wall 1" in md and "13 / 8 (62%)" in md


def test_an_older_run_gets_its_reader_counts_from_the_mock_log(tmp_path):
    log = ("[mock-backend 10:31:14] search-jev category_codes/IE: 8 candidates, 7 readable\n"
           "[mock-backend 10:31:15] search-jev: could not read www.example.com: HTTPError\n"
           "[mock-backend 10:31:18] search-jev media_spend/GB: 3 candidates, 0 readable\n"
           "[mock-backend 10:31:19] search-jev: could not read www.x.com: URLError\n")
    jev = {"family": "research", "alias": "jev", "secs": 2, "cost_usd": 0.004, "web_searches": 0, "web_fetches": 0}
    rd = _jev_run(tmp_path, [jev], log)["reader"]
    assert not rd["recorded"] and (rd["units"], rd["candidates"], rd["read"], rd["fallback_units"]) == (2, 11, 7, 1)
    assert rd["failed"] == {"http_error": 1, "network": 1, "bot_wall_or_thin_text": 2}
    assert rd["passages"] is None and rd["jev_cost"] == 0.004


def test_a_run_without_search_jev_has_no_reader_section(tmp_path):
    agent = {"family": "research", "alias": "sonnet", "secs": 20, "cost_usd": 0.1, "web_searches": 3, "web_fetches": 3}
    r = _jev_run(tmp_path, [agent])
    assert r["reader"] is None and "page reader" not in run_report.table(r)
