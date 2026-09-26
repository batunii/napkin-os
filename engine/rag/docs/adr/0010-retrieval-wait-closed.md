# ADR 0010 — The local store answers in under a second: masks, one store per process, warm-up

Status: accepted (2026-09-27; step 1 of the optimisation plan in project_plan.clan) ·
Findings: not an audit finding (found by profiling the BMW anatomy run); closes the local
half of CC10 and N8 · Code: `engine/rag/store_local.py` (`_filtered`, `_column`,
`_field_mask`, `warm`, the build lock), `engine/rag/store_base.py` (`get_store`,
`_memo_key`), `engine/rag/brief_context.py` (`warm_store`), `engine/parse_brief.py`
(`_warm_store`) · Tests: `engine/rag/test_store_local_masks.py` · Commit 9b06017.

## Context

Every recorded run, before and after the audit fixes, spent 23-27 s between the query
embedding and the first fill draft with no model running. It was the same with the
validator off, so it was not jev. Profiling `build_multi` on `_index_v4` (7,392 chunks x
2,048 dims, 356 MB of JSON) found two causes:

1. The local store applied metadata filters by calling `filters.matches()` on every row
   for every query and every widening round: about 110,000 calls per brief. Every bucket
   filter has five clauses (bucket, status != superseded, stage != production, scope in,
   tenant in), so each call walked five fields.
2. Each brief opened a new `LocalStore`, and `build_multi` runs its five field searches in
   parallel. On a cold store each thread read the 356 MB file and built its own BM25
   index, because nothing serialised the lazy builds. ADR 0001 had accepted "store
   instances are not cached" as a dev-only cost, assuming production would be the hosted
   Qdrant, which filters server-side. Until the AWS move the local store is what runs.

## Decisions

**Filters become cached masks.** Each filtered field is factorised once into integer codes,
one per distinct `str()` value and -1 for an absent field. `eq`, `ne`, `in`, `nin` and
`exists` are NumPy comparisons over those codes; the row list for each distinct filter is
cached (512 entries). The codes use the `str()` form because `matches()` compares
`str(got)` with `str(value)`, so 3 and "3" match as before. Range operators compare raw
values, so they keep the exact `matches()` loop on that one field. The absent-field rules
are unchanged: `eq`/`in` do not match an absent field, `ne`/`nin` do. Every cache is
dropped on a write.

**One local store per process.** `get_store("local")` returns the same instance for the
same index, keyed on the chunks file's path, size and modification time, so a rebuilt or
retagged index is picked up on the next call. A missing file is not memoised.
`RAG_STORE_MEMO=0` restores a fresh store per call.

**Lazy builds under one lock.** Rows, the vector matrix, the BM25 index and the filter
columns are built once, under an `RLock`, however many threads ask.

**Warm-up beside the opening calls.** `parse_brief.run()` submits `_warm_store` with the
validator warm-up, so the rows, matrix and BM25 index are built while the capture, golden
extraction, scorecard and how-to-win calls run (20-30 s). A remote store is a no-op.

## Consequences

- `build_multi` with BMW queries and the validator off: 23-30 s before; 5.7 s in a fresh
  process without the warm-up; 0.6-0.7 s warm. The evidence is identical across runs and to
  the run before the change. On the first checkpoint (9b06017) mamaliga's brief took 152 s
  against 166-188 s at earlier checkpoints.
- Memory: about 810 MB resident per process while warm (rows as Python dicts ~680 MB,
  including each vector as a list; the NumPy matrix 61 MB; BM25 ~130 MB). No money cost.
  If the AWS box is memory-bound, keeping vectors only in the matrix saves about 450 MB.
- Cold only once per process. The agent server pays it once per start or index rebuild;
  one-shot CLI and checkpoint runs pay it every brief, hidden behind the opening calls.
- No change to what the writers read, so no change to brief quality on its own.

## Alternatives rejected

- **Pre-filter in `brief_context` instead of the store.** It would duplicate the filter
  language outside `filters.py`, which the module docstring forbids: every store must agree
  on what a filter means.
- **Cache per brief only.** The file load (4 s) and BM25 build would still be paid every
  brief on the long-lived agent server.
- **Wait for the AWS store.** The hosted store's own stalls (RAG-4) are a separate problem,
  and every checkpoint until the move was paying the wait.
