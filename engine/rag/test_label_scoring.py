"""labelset label scoring (phase A item 3, audit D7/BW10): Cohen's kappa and balanced
accuracy, the best cut-off per bucket, per-rater confirmation with a consensus, and
rater-vs-rater agreement. Offline.
Run: cd engine/rag && python3 -m pytest -q test_label_scoring.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import labelset  # noqa: E402


def test_keep_everything_scores_high_raw_agreement_and_no_real_skill():
    """90 useful, 10 not, a validator that keeps all: raw 0.9, kappa 0, balanced 0.5."""
    m = labelset.binary_metrics([(True, True)] * 90 + [(False, True)] * 10)
    assert m["raw_agreement"] == 0.9 and m["kappa"] == 0.0 and m["balanced_accuracy"] == 0.5
    assert m["useful_kept"] == 1.0 and m["not_useful_dropped"] == 0.0


def test_kappa_matches_a_hand_worked_table():
    """tp 40, fn 10, fp 5, tn 45: po 0.85, pe 0.5, kappa 0.7, balanced 0.85."""
    m = labelset.binary_metrics([(True, True)] * 40 + [(True, False)] * 10 + [(False, True)] * 5 + [(False, False)] * 45)
    assert (m["raw_agreement"], m["kappa"], m["balanced_accuracy"]) == (0.85, 0.7, 0.85)


def test_best_threshold_separates_the_classes():
    """Useful passages score 0.6-0.9, not useful 0.1-0.3: the best cut-off is 0.6, perfect."""
    rows = [{"truth": True, "score": s} for s in (0.6, 0.7, 0.9)] + [{"truth": False, "score": s} for s in (0.1, 0.3)]
    b = labelset.best_threshold(rows)
    assert b["threshold"] == 0.6 and b["balanced_accuracy"] == 1.0
    assert labelset.best_threshold([{"truth": True, "score": 0.5}]) is None


def test_two_raters_consensus_and_disputes(tmp_path, monkeypatch):
    """The CD and the planner confirm separately; agreed pairs get `confirmed`, a disputed
    pair gets None; rater agreement is measured on the overlap."""
    folder = tmp_path / "golden" / "labels"; (folder / "client").mkdir(parents=True)
    rows = [{"brief_id": "b", "cite": c, "bucket": "craft", "useful": True, "confirmed": None} for c in ("c1", "c2", "c3")]
    labelset._write(folder / "prelabels.jsonl", rows)
    labelset._write(folder / "client" / "prelabels.jsonl", [])
    monkeypatch.setattr(labelset, "HERE", tmp_path)
    exp = lambda vals: [{"set": "ipa", "brief_id": "b", "cite": c, "bucket": "craft", "useful": v} for c, v in vals]
    (tmp_path / "cd.json").write_text(json.dumps(exp([("c1", True), ("c2", False), ("c3", True)])))
    (tmp_path / "sp.json").write_text(json.dumps(exp([("c1", True), ("c2", True), ("c3", True)])))
    labelset.cmd_confirm(str(tmp_path / "cd.json"), "cd")
    labelset.cmd_confirm(str(tmp_path / "sp.json"), "planner")
    got = {l["cite"]: l for l in labelset._jsonl(folder / "prelabels.jsonl")}
    assert got["c1"]["confirmed"] is True and got["c3"]["confirmed"] is True and got["c2"]["confirmed"] is None
    assert got["c2"]["human"] == {"cd": False, "planner": True}
    agree = labelset.rater_agreement(list(got.values()))
    assert agree["cd vs planner"]["n"] == 3 and agree["cd vs planner"]["raw_agreement"] == round(2 / 3, 3)
