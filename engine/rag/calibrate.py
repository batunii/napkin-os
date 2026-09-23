#!/usr/bin/env python3
"""
calibrate.py — turn a scoring backend's raw output into a probability and pick its gate.

The hosted and local rerankers return logits. Verdict.score promises a probability, and
the relevance gate needs a threshold on something whose meaning does not change when a
backend is swapped. This module fits Platt scaling, p = sigmoid(a*raw + b), from labelled
(query, passage, label) pairs, chooses the threshold on p, and writes
calibration/<backend>.json in the shape judge_base.Calibration.from_dict reads.

    set -a; . ../.env; set +a
    RAG_STORE=local RAG_INDEX=./_index_v3 python3 calibrate.py nemotron local

Where the labels come from, for now. The golden set's HELD-OUT cases, the same selection
the rerank pilot scored (every held-out `specific` case, i.e. "<client> <title>" for IPA
and Cannes, plus 40 held-out playbook cases shuffled with seed 7). For each case the
hybrid pool the pilot used is rebuilt: the top POOL chunks from golden._store_search over
_index_v3, filtered by the case's source. A pooled passage is labelled positive when its
doc_id is in the case's accept set and negative otherwise. Held-out only, because for the
other four fifths of the corpus the index contains the literal golden question and the
pools would be easier than anything a brief produces.

Label noise, stated so nobody reads the metrics as exact. The labels are document-level
and the gate is passage-level, so they are wrong in both directions:
  * a pool member OUTSIDE the accept set can still be genuinely relevant — another
    playbook section that answers the same question, or a second case that makes the
    same point. It is counted as a false positive when the backend passes it.
  * a pool member INSIDE the accept set can be a part of the right document that does
    not answer the query (a credits line, a results table for a different question). It
    is counted as a false negative when the backend rejects it.
Measured precision is therefore a lower bound on the first kind of error and recall a
lower bound on the second; neither is corrected here.

The domain caveat, which is why every file this writes says provisional: true. Golden
queries are retrieval-shaped — a case name or a templated question of a few words. Briefs
are not: production pairs a 1000-character query-plus-context with each passage. The
backend's logits on brief-shaped queries may sit elsewhere, so the fitted a, b and the
threshold are a starting point that keeps the gate honest (a probability, not a raw
logit), not a measurement of how the gate behaves on briefs. `fitted_on` names the data;
refit on labelled brief pairs when they exist and set provisional to false then.

How the data is split and fitted:
  * 70/30 split of CASES, not pairs, by a hash of source and query (deterministic across
    machines, like golden.is_holdout). Pairs from one pool are strongly correlated — same
    query, same logit offset — so a pair-level split would put near-duplicates on both
    sides and flatter the held-out numbers.
  * a and b are fitted on the training cases only, and the threshold is chosen on the
    training cases' fitted probabilities. The test block of the JSON is therefore an
    honest description of the parameters actually written.
  * The fit is Newton's method on the logistic loss with Platt's smoothed targets
    (Lin, Lin & Weng 2007). Smoothing matters on near-separable data such as nemotron's
    (AUC 0.98): the unsmoothed maximum-likelihood slope runs off to infinity.

Threshold policy. The gate REMOVES evidence, and judge.select() already guarantees that
when every passage in a bucket fails, the top `floor` come back flagged. So an
over-strict threshold is not catastrophic and an over-lax one only lets noise through;
neither error is clearly worse. The threshold is therefore the one that maximises F1 on
the training cases' fitted probabilities, and precision/recall at 0.3, 0.5, 0.7 and the
chosen value are recorded so the choice can be revisited without refitting.

Raw scores always come from the backend itself (backend.score() -> Verdict.raw), with the
full passage text handed over: the backend's own clipping and query construction are
what is calibrated, not a re-implementation of them.
"""
from __future__ import annotations

import argparse
import bisect
import collections
import datetime
import hashlib
import json
import math
import os
import random
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:                    # let `import judge_base` resolve next to us
    sys.path.insert(0, str(HERE))

from judge_base import (BackendNotConfigured, BackendUnavailable, Calibration,  # noqa: E402
                        Passage, Query, check_verdicts, sigmoid)

CALIBRATION_DIR = HERE / "calibration"
DEFAULT_INDEX = HERE / "_index_v3"
GOLDEN_DIR = HERE / "golden"

# The pilot's selection and pool, kept identical so the fitted parameters can be compared
# with the pilot's AUC and logit medians (nemotron) and the local builder's reference fit.
POOL = 40
PLAYBOOK_CASES = 40
PLAYBOOK_SEED = 7

TRAIN_FRACTION = 0.7
REPORT_THRESHOLDS = (0.3, 0.5, 0.7)
ECE_BINS = 10

# Per-call deadline while calibrating. Not the production deadline: here nobody is
# waiting on a brief, and a timeout loses a case from the data. 20 s is the pilot's
# timeout for nemotron (healthy p90 0.6 s, so this only ever catches hangs). The local
# model on a loaded laptop measured up to 59 s p90 at pool 40 on cpu, so it gets longer.
DEADLINE_S = {"nemotron": 20.0, "local": 120.0}
FALLBACK_DEADLINE_S = 60.0

# Being gentle with a hosted endpoint: calls are sequential, with a short pause between
# them, and after a failure the pause grows (2, 4, 8 ... s, capped) so a hung worker the
# backend is still holding can drain before the next call. A run of failures this long,
# or any failure that no amount of waiting fixes, aborts the backend instead.
PAUSE_S = 0.2
MAX_BACKOFF_S = 30.0
MAX_CONSECUTIVE_FAILURES = 6
FATAL_KINDS = ("retired", "not_entitled")


# ---- labelled data ----------------------------------------------------------------

# Backends whose raw output is a logit a Platt fit can map to a probability.
PLATT_BACKENDS = ("nemotron", "local")

def select_cases(cases: list[dict], *, n_playbook: int = PLAYBOOK_CASES,
                 seed: int = PLAYBOOK_SEED) -> list[dict]:
    """The rerank pilot's selection from the golden set: every held-out `specific` case
    (IPA and Cannes, one accepted document each) plus `n_playbook` held-out playbook cases
    in a seeded shuffle.

    Only held-out cases are ever returned. Seen cases have their literal question
    embedded in the index, so their pools would be easier than any brief's and would
    push the fitted slope towards overconfidence."""
    hold = [c for c in cases if c.get("holdout")]
    spec = [c for c in hold if c.get("kind") == "specific"]
    pb = [c for c in hold if c.get("source") == "playbook"]
    random.Random(seed).shuffle(pb)
    return spec + pb[:n_playbook]


def case_key(case: dict) -> str:
    """A stable identifier for a case: source and query. Two cases with the same query
    text share a key, so the split can never put one on each side."""
    return f"{case['source']}|{case['query']}"


def passage_text(row: dict) -> str:
    """The text a pooled chunk is judged on: context header, newline, body — exactly
    what the pilot sent. Unclipped; each backend clips to its own limits."""
    return (row.get("header") or "") + "\n" + (row.get("text") or "")


def build_pools(cases: list[dict], search, *, pool: int = POOL, log=None) -> list[dict]:
    """The labelled pool for each case: the top `pool` hybrid hits filtered by the case's
    source, each labelled by whether its doc_id is in the case's accept set.

    `search(query, k, where)` is golden._store_search's callable, injectable so tests run
    without an index. Returns one dict per case that returned any rows: key, query,
    source, kind, accept, passages (judge_base.Passage), doc_ids, labels (bool), in fused
    order. A case with no rows is dropped and counted in the log line, not raised: it has
    nothing to label."""
    out, empty = [], 0
    for c in cases:
        accept = set(c.get("accept") or [c["expected_doc_id"]])
        rows = search(c["query"], pool, {"source": c["source"]})
        if not rows:
            empty += 1
            continue
        ids = [(r.get("metadata") or {}).get("doc_id") for r in rows]
        out.append({"key": case_key(c), "query": c["query"], "source": c["source"],
                    "kind": c.get("kind"), "accept": sorted(accept),
                    "passages": [Passage(str(r.get("id") or i), passage_text(r))
                                 for i, r in enumerate(rows)],
                    "doc_ids": ids, "labels": [d in accept for d in ids]})
    if log:
        log(f"pools: {len(out)} built, {empty} cases returned no rows")
    return out


def pools_to_json(pools: list[dict]) -> list[dict]:
    """Pools as plain JSON (Passage objects flattened), for a scratch cache."""
    return [{**p, "passages": [{"id": x.id, "text": x.text} for x in p["passages"]]} for p in pools]


def pools_from_json(data: list[dict]) -> list[dict]:
    """Inverse of pools_to_json."""
    return [{**p, "passages": [Passage(x["id"], x["text"]) for x in p["passages"]]} for p in data]


# ---- scoring through the backend --------------------------------------------------
def score_pools(backend, pools: list[dict], *, deadline_s: float, pause_s: float = PAUSE_S,
                sleep=time.sleep, log=None) -> tuple[list[dict], dict]:
    """Score every pool through `backend.score()` and collect Verdict.raw per passage.

    Sequential, one call per case. A BackendUnavailable skips that case and is counted by
    kind; after a failure the pause before the next call doubles (capped at
    MAX_BACKOFF_S). A kind that waiting cannot fix (FATAL_KINDS) or
    MAX_CONSECUTIVE_FAILURES failures in a row stops the run: the remaining cases are
    counted as "aborted" and what was scored so far is returned.

    Returns (scored, stats). Each scored item is the pool dict plus `raws` (floats, input
    order) and `ranks` (fused positions). stats has attempted, scored, skipped (by kind),
    aborted and latency_s (p50, p90, max of successful calls)."""
    scored, skipped = [], collections.Counter()
    lat: list[float] = []
    consecutive, aborted = 0, 0
    for i, p in enumerate(pools):
        if i:
            sleep(pause_s if not consecutive else min(MAX_BACKOFF_S, 2.0 ** consecutive))
        t = time.monotonic()
        try:
            verdicts = check_verdicts(backend.name, p["passages"],
                                      backend.score(Query(p["query"]), p["passages"],
                                                    deadline_s=deadline_s))
            raws = [v.raw for v in verdicts]
            if not all(isinstance(x, (int, float)) and math.isfinite(x) for x in raws):
                raise BackendUnavailable(f"{backend.name}: a verdict has no finite raw",
                                         kind="bad_response")
        except BackendUnavailable as e:
            skipped[e.kind] += 1
            consecutive += 1
            if log:
                log(f"  [{i + 1}/{len(pools)}] skipped ({e.kind}): {str(e)[:120]}")
            if e.kind in FATAL_KINDS or consecutive >= MAX_CONSECUTIVE_FAILURES:
                aborted = len(pools) - i - 1
                if log:
                    log(f"  stopping {backend.name}: {e.kind}, {consecutive} failure(s) in a row; "
                        f"{aborted} case(s) not attempted")
                break
            continue
        lat.append(time.monotonic() - t)
        consecutive = 0
        scored.append({**p, "raws": [float(x) for x in raws], "ranks": list(range(len(raws)))})
        if log and (len(scored) % 20 == 0):
            log(f"  [{i + 1}/{len(pools)}] scored {len(scored)}")
    lat.sort()
    stats = {"attempted": len(pools) - aborted, "scored": len(scored), "skipped": dict(skipped),
             "aborted": aborted,
             "latency_s": ({"p50": round(lat[len(lat) // 2], 3), "p90": round(lat[int(len(lat) * 0.9)], 3),
                            "max": round(lat[-1], 3)} if lat else None)}
    return scored, stats


# ---- the maths ----------------------------------------------------------------------
def _softplus(z: float) -> float:
    """log(1 + e^z) without overflow for large |z|."""
    return max(z, 0.0) + math.log1p(math.exp(-abs(z)))


def fit_platt(raws: list[float], labels: list[bool], *, max_iter: int = 100,
              tol: float = 1e-10) -> tuple[float, float]:
    """Fit p = sigmoid(a*raw + b) to binary labels; returns (a, b).

    Newton's method on the cross-entropy with Platt's smoothed targets, following Lin,
    Lin & Weng (2007): positives aim at (N+ + 1)/(N+ + 2) and negatives at 1/(N- + 2)
    rather than 1 and 0. Without that, perfectly separated data has no finite maximum-
    likelihood slope and the fit diverges; with it, a stays finite and the probabilities
    stop short of certainty by an amount that shrinks as data grows. Each step is
    backtracked until the loss decreases, and a tiny ridge keeps the Hessian invertible.

    Raises ValueError if the inputs differ in length or either class is absent: a
    calibration fitted on one class says nothing about the other."""
    if len(raws) != len(labels):
        raise ValueError(f"fit_platt: {len(raws)} raws for {len(labels)} labels")
    n_pos = sum(1 for y in labels if y)
    n_neg = len(labels) - n_pos
    if not n_pos or not n_neg:
        raise ValueError(f"fit_platt needs both classes (positives {n_pos}, negatives {n_neg})")
    hi, lo = (n_pos + 1.0) / (n_pos + 2.0), 1.0 / (n_neg + 2.0)
    xs = [float(x) for x in raws]
    ts = [hi if y else lo for y in labels]

    def loss(a: float, b: float) -> float:
        """Cross-entropy of the smoothed targets under (a, b), computed stably."""
        return sum(t * _softplus(-(a * x + b)) + (1.0 - t) * _softplus(a * x + b)
                   for x, t in zip(xs, ts))

    a, b = 0.0, math.log((n_pos + 1.0) / (n_neg + 1.0))
    f = loss(a, b)
    for _ in range(max_iter):
        ga = gb = haa = hab = hbb = 0.0
        for x, t in zip(xs, ts):
            p = sigmoid(a * x + b)
            d = p - t
            w = p * (1.0 - p)
            ga += d * x
            gb += d
            haa += w * x * x
            hab += w * x
            hbb += w
        if abs(ga) < 1e-9 and abs(gb) < 1e-9:
            break
        haa += 1e-12
        hbb += 1e-12
        det = haa * hbb - hab * hab
        if det <= 0:
            break
        da = -(hbb * ga - hab * gb) / det
        db = -(haa * gb - hab * ga) / det
        step = 1.0
        while step >= 1e-10:
            na, nb = a + step * da, b + step * db
            nf = loss(na, nb)
            if nf < f + 1e-4 * step * (ga * da + gb * db):
                break
            step /= 2.0
        else:
            break                                 # no descent possible: at the optimum
        converged = abs(f - nf) <= tol * max(1.0, abs(f))
        a, b, f = na, nb, nf
        if converged:
            break
    return a, b


def best_f1_threshold(probs: list[float], labels: list[bool]) -> float:
    """The threshold t (pass when p >= t) that maximises F1 on these probabilities.

    Candidates are the cuts between distinct probability values. The returned t is the
    midpoint between the lowest passing and the highest failing probability, not a data
    point: a new passage scored exactly at a training point's value then gets the same
    answer that point did, and one scored just below it is not rejected by a hair.
    Among cuts with equal F1 the one that passes MORE is chosen: equal F1 means the
    extra passages are as likely right as wrong, and a passage kept can still be ranked
    down while a passage removed is gone.

    Raises ValueError without positives (F1 is undefined everywhere)."""
    if len(probs) != len(labels):
        raise ValueError(f"best_f1_threshold: {len(probs)} probs for {len(labels)} labels")
    n_pos = sum(1 for y in labels if y)
    if not n_pos:
        raise ValueError("best_f1_threshold needs at least one positive")
    order = sorted(zip(probs, labels), key=lambda r: -r[0])
    best_f1, best_k = -1.0, 0
    tp = fp = 0
    for k, (p, y) in enumerate(order, 1):
        tp += bool(y)
        fp += not y
        if k < len(order) and order[k][0] == p:
            continue                              # a cut can only fall between distinct values
        f1 = 2.0 * tp / (2.0 * tp + fp + (n_pos - tp))
        if f1 >= best_f1:                          # >=: ties go to the cut that passes more
            best_f1, best_k = f1, k
    lowest_pass = order[best_k - 1][0]
    highest_fail = order[best_k][0] if best_k < len(order) else 0.0
    return (lowest_pass + highest_fail) / 2.0


def metrics_at(probs: list[float], labels: list[bool], threshold: float) -> dict:
    """Confusion counts and precision, recall, F1 and pass rate for `p >= threshold`.

    precision is None when nothing passes and recall None when there are no positives:
    undefined, not zero, so an empty gate is not mistaken for a wrong one."""
    tp = fp = fn = tn = 0
    for p, y in zip(probs, labels):
        if p >= threshold:
            tp += bool(y)
            fp += not y
        else:
            fn += bool(y)
            tn += not y
    n = tp + fp + fn + tn
    prec = tp / (tp + fp) if tp + fp else None
    rec = tp / (tp + fn) if tp + fn else None
    f1 = 2 * tp / (2 * tp + fp + fn) if tp + fp + fn else None
    return {"threshold": round(threshold, 6), "precision": _r(prec), "recall": _r(rec),
            "f1": _r(f1), "pass_rate": _r((tp + fp) / n if n else None),
            "tp": tp, "fp": fp, "fn": fn, "tn": tn}


def auc(scores: list[float], labels: list[bool]) -> float | None:
    """Area under the ROC curve: the chance a random positive outscores a random
    negative, ties counting a half (Mann-Whitney U from average ranks). None when a
    class is absent. Invariant under Platt scaling with a > 0, so it measures the
    backend, not the fit."""
    n_pos = sum(1 for y in labels if y)
    n_neg = len(labels) - n_pos
    if not n_pos or not n_neg:
        return None
    order = sorted(range(len(scores)), key=lambda i: scores[i])
    rank_sum, i = 0.0, 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and scores[order[j + 1]] == scores[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0                 # ranks are 1-based
        rank_sum += avg * sum(1 for k in order[i:j + 1] if labels[k])
        i = j + 1
    return (rank_sum - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


def reliability(probs: list[float], labels: list[bool], bins: int = ECE_BINS) -> list[dict]:
    """Equal-width bins over [0, 1]: count, mean predicted probability and observed
    positive rate per non-empty bin. p = 1.0 falls in the top bin."""
    acc = [[0, 0.0, 0] for _ in range(bins)]
    for p, y in zip(probs, labels):
        b = min(int(p * bins), bins - 1)
        acc[b][0] += 1
        acc[b][1] += p
        acc[b][2] += bool(y)
    return [{"bin": f"{i / bins:.1f}-{(i + 1) / bins:.1f}", "n": n,
             "mean_p": round(s / n, 4), "pos_rate": round(k / n, 4)}
            for i, (n, s, k) in enumerate(acc) if n]


def ece(probs: list[float], labels: list[bool], bins: int = ECE_BINS) -> float | None:
    """Expected calibration error: over equal-width bins, the count-weighted mean gap
    between predicted probability and observed positive rate. 0 is perfectly calibrated.
    None for empty input."""
    if not probs:
        return None
    acc = [[0, 0.0, 0] for _ in range(bins)]
    for p, y in zip(probs, labels):
        b = min(int(p * bins), bins - 1)
        acc[b][0] += 1
        acc[b][1] += p
        acc[b][2] += bool(y)
    return sum(abs(s / n - k / n) * n for n, s, k in acc if n) / len(probs)


def _r(x: float | None, nd: int = 4) -> float | None:
    """Round for the JSON, leaving None alone."""
    return None if x is None else round(x, nd)


def _median(xs: list[float]) -> float | None:
    """Median, or None for an empty list."""
    if not xs:
        return None
    s = sorted(xs)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2.0


# ---- split, fit, report -----------------------------------------------------------
def in_train(key: str, fraction: float = TRAIN_FRACTION) -> bool:
    """Deterministic case-level split: the same key lands on the same side on every
    machine and every run (sha1, as golden.is_holdout does), independent of the order or
    number of cases scored."""
    h = int(hashlib.sha1(key.encode("utf-8")).hexdigest()[:8], 16)
    return (h % 1000) < int(round(fraction * 1000))


def flatten(scored: list[dict], *, max_rank: int | None = None) -> tuple[list[float], list[bool], list[str]]:
    """(raws, labels, sources) over every pair of the given scored pools, optionally only
    pairs whose fused position is below `max_rank`."""
    raws, labels, sources = [], [], []
    for p in scored:
        for x, y, rk in zip(p["raws"], p["labels"], p["ranks"]):
            if max_rank is None or rk < max_rank:
                raws.append(x)
                labels.append(bool(y))
                sources.append(p["source"])
    return raws, labels, sources


def evaluate(cal_a: float, cal_b: float, threshold: float, raws: list[float],
             labels: list[bool], *, with_reliability: bool = False) -> dict:
    """Everything the JSON reports about one set of pairs under one fitted calibration:
    class counts, raw medians per class, AUC, ECE, and the gate's metrics at
    REPORT_THRESHOLDS and at the chosen threshold."""
    probs = [sigmoid(cal_a * x + cal_b) for x in raws]
    pos = [x for x, y in zip(raws, labels) if y]
    neg = [x for x, y in zip(raws, labels) if not y]
    out = {"n_pos": len(pos), "n_neg": len(neg), "auc": _r(auc(raws, labels)),
           "ece_10": _r(ece(probs, labels)),
           "raw_median_pos": _r(_median(pos), 3), "raw_median_neg": _r(_median(neg), 3),
           "at": {**{f"{t:g}": metrics_at(probs, labels, t) for t in REPORT_THRESHOLDS},
                  "chosen": metrics_at(probs, labels, threshold)}}
    if with_reliability:
        out["reliability"] = reliability(probs, labels)
    return out


def fit_calibration(scored: list[dict], *, backend: str, fitted_on: str, capacity: int | None = None,
                    train_fraction: float = TRAIN_FRACTION, extra: dict | None = None) -> dict:
    """The calibration JSON for one backend's scored pools.

    Splits CASES 70/30 with in_train(), fits Platt a, b on the training pairs, picks the
    best-F1 threshold on the training probabilities and reports train and held-out test
    metrics for those exact parameters. When the backend's `capacity` is below the pool,
    the test metrics are also given for pairs inside it, since those are the only pairs
    the backend judges in production. The result always has provisional True: golden
    pairs are not brief pairs (module docstring).

    Raises ValueError when either side of the split lacks a class, rather than writing a
    calibration whose held-out numbers do not exist."""
    train = [p for p in scored if in_train(p["key"], train_fraction)]
    test = [p for p in scored if not in_train(p["key"], train_fraction)]
    tr_x, tr_y, _ = flatten(train)
    te_x, te_y, te_src = flatten(test)
    for name, ys in (("train", tr_y), ("test", te_y)):
        if not any(ys) or all(ys):
            raise ValueError(f"{backend}: the {name} split has only one class "
                             f"({len(ys)} pairs); cannot fit or report")
    a, b = fit_platt(tr_x, tr_y)
    threshold = best_f1_threshold([sigmoid(a * x + b) for x in tr_x], tr_y)
    by_src: dict[str, dict] = {}
    for src in sorted(set(te_src)):
        xs = [x for x, s in zip(te_x, te_src) if s == src]
        ys = [y for y, s in zip(te_y, te_src) if s == src]
        probs = [sigmoid(a * x + b) for x in xs]
        by_src[src] = {"cases": sum(1 for p in test if p["source"] == src),
                       "n_pos": sum(ys), "n_neg": len(ys) - sum(ys), "auc": _r(auc(xs, ys)),
                       "chosen": metrics_at(probs, ys, threshold)}
    split_by_src = collections.Counter(
        (p["source"], "train" if in_train(p["key"], train_fraction) else "test") for p in scored)
    out = {
        "a": round(a, 6), "b": round(b, 6), "threshold": round(threshold, 6),
        "fitted_on": fitted_on, "n": len(tr_x), "provisional": True,
        "backend": backend,
        "fitted_at": datetime.date.today().isoformat(),
        "method": "Platt scaling p = sigmoid(a*raw + b); Newton on cross-entropy with "
                  "Platt's smoothed targets (Lin, Lin & Weng 2007); fitted on train cases only",
        "threshold_policy": "maximises F1 on the train cases' fitted probabilities; midpoint "
                            "between the adjacent distinct probabilities; ties pass more",
        "n_pos": sum(tr_y), "n_neg": len(tr_y) - sum(tr_y),
        "split": {"unit": "case", "method": f"sha1(source|query) % 1000 < {int(round(train_fraction * 1000))}",
                  "train_cases": len(train), "test_cases": len(test),
                  "by_source": {f"{s}/{side}": n for (s, side), n in sorted(split_by_src.items())}},
        "train": evaluate(a, b, threshold, tr_x, tr_y),
        "test": evaluate(a, b, threshold, te_x, te_y, with_reliability=True),
        "test_by_source": by_src,
        "label_noise": "document-level labels on passages: a pooled chunk outside the accept set "
                       "can be genuinely relevant (counted FP), a chunk of an accepted document "
                       "can be off-topic (counted FN); precision and recall are approximate",
        "domain_caveat": "golden queries are retrieval-shaped (case names, templated questions); "
                         "briefs pair a long query-plus-context with each passage, so logits may "
                         "shift. Provisional until refitted on labelled brief pairs",
    }
    pool = max((len(p["raws"]) for p in scored), default=0)
    if capacity and capacity < pool:
        cx, cy, _ = flatten(test, max_rank=capacity)
        out["test_within_capacity"] = {"capacity": capacity,
                                       **evaluate(a, b, threshold, cx, cy)}
    if extra:
        out.update(extra)
    Calibration.from_dict(out)                   # the shape the backends load must round-trip
    return out


def write_calibration(path: Path, data: dict) -> None:
    """Write the calibration JSON atomically (temp file then rename), so a backend
    constructed mid-write never reads half a file and raises BackendNotConfigured."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# ---- CLI --------------------------------------------------------------------------
def check_environment(env=None) -> list[str]:
    """Problems that make a calibration run wrong or harmful, as messages ([] = fine).

    The store must be local: engine/.env points RAG_STORE at the shared Qdrant, and bulk
    evaluation there sheds connections (README, Stores). Offline embedding must be off:
    hash vectors against a nemotron-embedded index would build meaningless pools."""
    env = os.environ if env is None else env
    problems = []
    if (env.get("RAG_STORE") or "local").strip().lower() != "local":
        problems.append(f"RAG_STORE={env.get('RAG_STORE')!r}: run with RAG_STORE=local "
                        f"RAG_INDEX=./_index_v3 (bulk evals never go to the shared Qdrant)")
    if (env.get("RAG_EMBED") or "").strip().lower() == "offline":
        problems.append("RAG_EMBED=offline: query vectors would not match the index's embedder")
    return problems


def _index_label(index_dir: Path) -> str:
    """The index directory and its embedding model (from manifest.json), for fitted_on."""
    try:
        m = json.loads((index_dir / "manifest.json").read_text(encoding="utf-8"))
        model = m.get("embed_model") or m.get("model") or "unknown embedder"
    except (OSError, ValueError):
        model = "unknown embedder"
    return f"{index_dir.name} ({model})"


def _fitted_on(selected: list[dict], index_dir: Path, pool: int) -> str:
    """The human sentence naming the data a calibration was fitted on."""
    kinds = collections.Counter(("specific" if c.get("kind") == "specific" else c["source"]) for c in selected)
    return (f"golden held-out, rerank-pilot selection ({kinds.get('specific', 0)} specific ipa/cannes + "
            f"{kinds.get('playbook', 0)} playbook, seed {PLAYBOOK_SEED}); hybrid pool {pool} from "
            f"{_index_label(index_dir)} filtered by source; label = doc_id in accept set; "
            f"Platt fit on {int(TRAIN_FRACTION * 100)}% of cases")


def main(argv: list[str] | None = None) -> int:
    """CLI: build the labelled pools once, score them through each named backend, fit and
    write calibration/<backend>.json. A backend that is not configured is reported and
    skipped (exit 2 if none could be calibrated); --dry-run fits and prints without
    writing."""
    ap = argparse.ArgumentParser(description="Fit Platt calibration for validation backends")
    ap.add_argument("backends", nargs="+", help="backend names, e.g. nemotron local")
    ap.add_argument("--index", default=str(DEFAULT_INDEX))
    ap.add_argument("--golden", default=str(GOLDEN_DIR))
    ap.add_argument("--pool", type=int, default=POOL)
    ap.add_argument("--pools-cache", default=None,
                    help="JSON file: reuse pools from it if present, else build and save them there")
    ap.add_argument("--save-raws", default=None, help="directory for <backend>_raws.json (scratch)")
    ap.add_argument("--deadline", type=float, default=None, help="per-call deadline, seconds")
    ap.add_argument("--out-dir", default=str(CALIBRATION_DIR))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    def log(msg: str) -> None:
        """Progress on stderr, flushed so a long run can be watched."""
        print(f"[calibrate] {msg}", file=sys.stderr, flush=True)

    problems = check_environment()
    if problems:
        for p in problems:
            log(p)
        return 1
    import golden                                 # imports rag, which loads engine/.env
    problems = check_environment()                # .env never overrides, but check again after it loaded
    if problems:
        for p in problems:
            log(p)
        return 1
    import judge

    index_dir = Path(a.index)
    cases, _ = golden.load_golden(Path(a.golden))
    selected = select_cases(cases)
    log(f"selected {len(selected)} held-out cases")
    cache = Path(a.pools_cache) if a.pools_cache else None
    if cache and cache.exists():
        pools = pools_from_json(json.loads(cache.read_text(encoding="utf-8")))
        log(f"pools: {len(pools)} loaded from {cache}")
    else:
        search = golden._store_search(index_dir, [c["query"] for c in selected])
        pools = build_pools(selected, search, pool=a.pool, log=log)
        if cache:
            cache.write_text(json.dumps(pools_to_json(pools)), encoding="utf-8")
    fitted_on = _fitted_on(selected, index_dir, a.pool)
    missing = Path(a.out_dir) / ".calibrate-none.json"   # never exists: build uncalibrated
    done = 0
    for name in a.backends:
        # Only the backends whose raw output is a logit take a Platt fit. jev is
        # vendor-calibrated (its output is already a probability) and llm is pass/fail
        # with no score; building them with calibration_path would raise TypeError.
        if name not in PLATT_BACKENDS:
            log(f"{name}: no Platt calibration applies (jev is vendor-calibrated, llm returns "
                f"pass/fail) — skipped")
            continue
        try:
            kw = {"calibration_path": missing}
            if name == "local":
                kw["deadline_s"] = a.deadline or DEADLINE_S["local"]
            backend = judge.build_backend(name, **kw)
        except BackendNotConfigured as e:
            log(f"{name}: not configured, skipped: {e}")
            continue
        deadline = a.deadline or DEADLINE_S.get(name, FALLBACK_DEADLINE_S)
        log(f"{name}: scoring {len(pools)} pools sequentially, deadline {deadline:g}s")
        scored, stats = score_pools(backend, pools, deadline_s=deadline, log=log)
        log(f"{name}: {json.dumps(stats)}")
        if a.save_raws:
            Path(a.save_raws).mkdir(parents=True, exist_ok=True)
            (Path(a.save_raws) / f"{name}_raws.json").write_text(json.dumps(
                [{"key": p["key"], "source": p["source"], "raws": p["raws"], "labels": p["labels"]}
                 for p in scored]), encoding="utf-8")
        describe = getattr(backend, "describe", None)
        cfg = describe() if callable(describe) else {}
        cfg = {k: v for k, v in cfg.items() if k not in ("calibrated", "calibration", "provisional", "fitted_on")}
        try:
            data = fit_calibration(scored, backend=name, fitted_on=fitted_on,
                                   capacity=getattr(backend, "capacity", None),
                                   extra={"backend_config": cfg,
                                          "run": {"cases_selected": len(selected), "pools": len(pools),
                                                  "deadline_s": deadline, **stats}})
        except ValueError as e:
            log(f"{name}: cannot fit: {e}")
            continue
        summary = {k: data[k] for k in ("a", "b", "threshold", "n", "n_pos", "n_neg")}
        summary["test"] = {k: data["test"][k] for k in ("auc", "ece_10")} | {"chosen": data["test"]["at"]["chosen"]}
        log(f"{name}: {json.dumps(summary)}")
        if a.dry_run:
            print(json.dumps(data, indent=1))
        else:
            out = Path(a.out_dir) / f"{name}.json"
            write_calibration(out, data)
            log(f"{name}: wrote {out}")
        done += 1
    return 0 if done else 2


if __name__ == "__main__":
    sys.exit(main())
