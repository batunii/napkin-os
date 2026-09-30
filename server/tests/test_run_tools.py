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
