# 0003 — Validation stage: a chain of relevance backends

- **Status:** accepted; components built and tested, not yet wired into retrieval (plan steps 3–5)
- **Date:** 2026-09-23
- **Owner:** Sai
- **Files:** `judge_base.py`, `judge.py`, `judge_code.py`, `judge_llm.py`, `judge_nemotron.py`,
  `judge_jev.py`, `judge_local.py`, `calibrate.py`, `calibration/{nemotron,local}.json`, and their tests

## Context

Sai's plan for the RAG module puts a validation step between extraction and reranking: check each
retrieved chunk against the query (plus the brief context and the user's added material), get a
relevance score, and keep what is directly useful. jev (TypeSafe) is the intended backend but there
is no access yet; the code had to be ready for it, with an alternative that acts as a failsafe and a
switch between them. Retrieval should go wide only when a validator is live, and when every chunk
fails the threshold the top-scoring ones come back rather than an empty bucket.

Measurements that shaped it (2026-09-23):

- The whole hosted nv-rerankqa family is **retired** (410); `nvidia/llama-nemotron-rerank-vl-1b-v2`
  is the live hosted reranker on this key.
- Hosted nemotron-rerank-vl on 154 held-out golden queries, pool 40: recall@1 0.942 → 0.968,
  p50 0.49 s, p90 0.59 s, AUC 0.983 — and 5 of 154 calls hung past 20 s.
- Local `bge-reranker-v2-m3` on this Mac's GPU, same 159 pools: recall@1 0.943 → 0.950,
  recall@5 0.975 → 0.987, AUC 0.965; p90 3.37 s at pool 20 with brief-shaped queries.
- `bge-reranker-base` is 3x faster but reranked recall@1 0.906 is *worse* than no reranking (0.943).
- On CPU alone, v2-m3 at pool 40 reached p90 59 s under load.

## Decision

1. **One contract, many backends** (`judge_base.py`): `Query`, `Passage`, `Verdict` (value, score,
   why, backend, raw). `score` is a calibrated probability or None; only calibrated backends set it,
   and a threshold is never compared with None. `raw` is the native output, unmodified, so a later
   calibration or a backend swap can be diffed against recorded runs. `score` and `why` are never
   both set.
2. **Two failure classes with opposite actions.** `BackendNotConfigured` at construction (missing
   key, SDK, weights, or a mismatched calibration) is a hard error. `BackendUnavailable` at run time
   (timeout, HTTP failure, retired model) falls through to the next backend, and `kind` names who
   owns the fix. HTTP failures are classified by one shared function, so one kind always means one
   owner: `retired` (our config), `not_entitled` (the vendor), `rate_limited`, `http_error`.
3. **The chain** (`judge.py`), from `RAG_VALIDATOR` in priority order. Recommended:
   `jev,nemotron,local` once jev access exists, `nemotron,local` until then. **No default backend**:
   unset means validation off, because a default naming a backend some machine lacks would hard-fail
   every run there. The chain enforces each backend's deadline itself, marks a failed backend down
   for 60 s, and checks every backend's output against the invariants before accepting it.
4. **Width.** `pool_width(default)` is the larger of the caller's default and the lead backend's
   capacity: retrieval widens for a validator that can judge more, and is **never narrower** than
   validation-off retrieval. (First built as "the lead's capacity", which made local-on-cpu, capacity
   6, thin retrieval from 40 to 6; the critic caught it.)
5. **Floor** (Sai's decision). When every chunk in a pool fails and nothing unjudged survives, the
   top `floor` (default 2) come back flagged `floor`, ranked by calibrated score, else raw output.
   When unjudged chunks survive, no rejected chunk is put back ahead of them.
6. **Admission rules in code, before relevance** (`judge_code.py`): excluded doc ids, oversized
   chunks (> 12,000 chars; refuses 5 of 7,315, all malformed template sections), and a recency cap
   for award cases only. Licence class is not implemented: the metadata contract has no licence field.
7. **Local is one call per brief.** All local inference shares one worker; four per-bucket calls at
   pool 20 took 12.4 s against an 8 s deadline, one combined call 3.4 s. Capacity per call: mps 20,
   cpu 6, sized so 2 x p90 (brief-shaped queries) fits the deadline.
8. **Calibration is Platt scaling fitted per backend** (`calibrate.py`), on held-out golden cases,
   split 70/30 by case, threshold at best F1. Both files are `provisional: true`. The loader refuses a
   file whose recorded model or input clipping differs from the running backend's.
9. **jev** is built against the real SDK source (`typesafe-sdk` 0.7.1) and tested with fakes shaped
   exactly like it: the brief is the state, each passage its own Noul question, batched under the token
   limits, SDK retries off. Marked `provisional` — its 0.5 threshold is unvalidated on our data.

## Calibration results (held-out test split)

| Backend | AUC | threshold | precision | recall | playbook recall |
|---|---|---|---|---|---|
| nemotron | 0.980 | 0.128 | 0.822 | 0.930 | 0.731 |
| local | 0.968 | 0.439 | 0.801 | 0.809 | **0.372** |

## Alternatives rejected

- **LLM judge in the default chain.** Slow (~12 s for 5 calls today), and its self-reported confidence
  cannot back a threshold (measured earlier: 92–95 while its answer flipped). Kept as `llm`, opt-in.
- **bge-reranker-base as the local default.** Faster, but it damages the ranking.
- **A fall-through default when a named backend is not configured.** Silent degradation is the failure
  this codebase already paid for once (has_sparse swallowing errors).
- **Sizing local for four parallel calls** (capacity 10). Chose one combined call per brief instead:
  fewer calls, wider pool.
- **Per-bucket thresholds now.** The test split has 13–14 playbook cases; too few to fit on.

## Consequences and open items

- **Not wired yet.** Integration (steps 3–5) must: call the chain once per brief with all buckets'
  candidates combined and deduplicated; split the result back per bucket; apply the floor per bucket;
  map retrieval width (chunks, before collapse) onto judged width (documents, after); pass a per-brief
  deadline; hold one chain per process so the breaker survives across briefs; and bump the rag_io
  contract for `relevance`, the `floor` flag and a per-brief `validation` summary.
- **Local's playbook recall of 0.37** means the craft bucket would mostly come from the floor on local.
  Fix in step 6 with brief-shaped labels, or a per-bucket threshold once there is data for it.
- **Cannes precision 0.61 for both backends** looks like label noise (several entries per campaign,
  one accepted id). Read those pairs before trusting it.
- **Every calibration is provisional** until refitted on ~300 labelled brief-shaped pairs.
- **jev's real SDK path has never run** (10 tests skip without the SDK). First-access checklist is in
  `judge_jev.py`'s module docstring.
- The DGX Spark ([plan](../dgx-spark-plan.md)) should make local a GPU peer of nemotron; its cuda
  capacity is unmeasured and set to 6 until then.
