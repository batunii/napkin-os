# 0002 — Tuning re-baseline after a holdout leak

- **Status:** accepted
- **Date:** 2026-09-23
- **Owner:** Sai
- **Files:** `tune.py`, `store_local.py` (`holdout_ids`, `build_bm25`), `lexical.py`,
  `test_store_local_hybrid.py`, `golden/remeasure_2026-09-23/`

## Context

The golden set holds out 1 in 5 documents: their "Retrieval Queries" are kept out of
everything indexed, so held-out recall measures retrieval on questions the index has
never seen. `LocalStore.bm25()` honoured this. `tune.py` did not: for every configuration
that changed a BM25 parameter it rebuilt the keyword index with an empty holdout set, so
those variants were scored on an index containing the held-out questions' own text, while
the baseline row was not. Found while backfilling docstrings; confirmed at `tune.py:125`.

Every tuned default and the published headline ("held-out recall@5 0.906 → 0.974,
recall@10 1.000") came from that sweep.

## Decision

1. One holdout-safe builder: `LocalStore.build_bm25(**params)`, used by `bm25()` and by
   `tune.py`. `LocalStore.holdout_ids()` is the only reader of the manifest's
   `holdout_file`.
2. Every sweep configuration, baselines included, states its BM25 parameters, so every
   row is scored on an index built the same way.
3. Keep the tuned defaults (`b=0.3`, `rrf_k=10`, `k1=1.5`). The re-measurement confirms
   the direction; only the size of the gain was wrong.
4. Correct every place that quoted the old figures.

## Evidence (re-measured, 150 held-out cases of 750, ipa + cannes + playbook)

| Configuration | held-out recall@5 | held-out recall@10 |
|---|---|---|
| baseline b=0.75, rrf_k=60 | 0.927 | 0.953 |
| b=0.3 | 0.940 | 0.960 |
| b=0.3, rrf_k=10 (current defaults) | **0.947** | 0.953 |

Golden eval with the current defaults, 1,365 cases (500 per source cap): held-out
recall@5 **0.928**, recall@10 **0.945** (290 held-out). Weakest group: templated IPA
held-out queries, recall@5 0.825.

The gain from tuning is **+2.0 points**, not the +6.8 first reported. On 150 cases that is
three queries; the direction is consistent across both sweep rounds, the magnitude is
within noise of +1.3 to +2.0.

## Alternatives rejected

- **Reverting to textbook defaults.** They measure worse, holdout-safe.
- **Re-running only the leaky rows.** A sweep is only comparable if every row is built by
  the same code path; hence the shared builder.

## Consequences

- The build record in `project_plan.clan` and anything citing 0.974 / 1.000 is wrong and
  must be corrected there.
- Held-out recall@10 is 0.945, not 1.000: the right document is NOT always in a top-10
  pool. This strengthens the case for wider candidate pools when a validator is live
  (plan step 3): recall@40 in the reranker pilot was 0.987.
- `test_store_local_hybrid.test_rebuilt_keyword_index_still_excludes_holdout_questions`
  guards the builder.
