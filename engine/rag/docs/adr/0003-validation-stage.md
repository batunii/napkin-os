# 0003 — Validation stage: a chain of relevance backends

- **Status:** accepted; wired into `brief_context.build` and `rag_io` v1.1.0 (steps 3–4), **off by default — do not enable for briefs yet** (see *Live finding*)
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

- **Integration requirements (done in steps 3–4 for `brief_context`; `loops_3_7` is step 5; per-brief deadline still open).** Integration had to: call the chain once per brief with all buckets'
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

## Live finding after integration (2026-09-23) — why it stays off

One real brief (BMW, automotive) through `rag_io.handle` with `RAG_VALIDATOR=nemotron`: one
validator call, 557 ms, 40 passages judged. Nemotron rejected every judged passage except the floor.
The two automotive IPA precedents scored raw logits of about −8.6, **both with the brief text as
the query and rephrased as a question** ("Which past campaign or planning evidence would help a brief
where: …?"). A QA cross-encoder judges whether a passage *answers a question*; an award case never
answers a brief, so for the exemplars bucket it measures the wrong thing. This is a tool mismatch,
not a threshold problem, and the golden calibration (question-shaped queries) could not have shown it.

Consequences:
- Validation stays **off** (`RAG_VALIDATOR` unset) for brief generation until step 6 proves it helps.
- Step 6 must test, on brief-shaped labelled pairs: role-specific queries per bucket; jev's Noul
  (instruction-following — it can be asked "is this comparable precedent for this brief?"); and
  applying rerankers only to craft/rules, where playbook text does answer "how to" questions.
- Fixed in integration from the same run: judging every candidate (82) at capacity 40 left craft and
  rules with 19–23 low-ranked *unjudged* hits while their judged top was rejected — an inverted
  ranking. Now each bucket gets an equal share of capacity (at least 5, what a budget can hold) and
  the unjudged tail is dropped when a validator answered.

Integration as built: one validator call per brief, candidates interleaved by rank and deduplicated;
reviewer rejections exempt; admission rules first; per-bucket floor; passing hits sorted by score (the
rerank); `edge_order()` after the budget behind `RAG_ORDER=edge` (default `score`); one chain per
process; floor hits marked "weak match" in the prompt; `rag_io` 1.1.0 carries per-hit `relevance`
(with `kept`) and the run's `validation` record.

## Addendum 2026-09-25 — batch 3 of the audit fix plan: retrieval that counts

Findings (audit 2026-09-24, second-checked 2026-09-25): RAG-1, JL-1, JL-5, JL-6, JL-7, RAG-7,
RAG-9, critic-G14. Tests: `test_retrieval_that_counts.py`.

- **The validator's result now reaches the evidence (RAG-1).** `build_multi` validated a
  *copy* of each field's buckets and dropped it, so the validator's order, the gate's drops
  and the admission refusals (`exclude_doc_ids`, recency) never changed what the fill served.
  Every jev-vs-nemotron comparison before this date was therefore A/A on evidence, and the
  38–52% evidence overlap in `compare_jev/compare.json` measured query drift between runs,
  not validator disagreement (critic-G14). The validated buckets now replace the fused ones.
  Consequence: `RAG_VALIDATOR` changes evidence selection for the first time since the mix
  path shipped, so it needs the A/B before it is on by default.
- **The validator sees the brief (JL-5).** `_loops_via_mix` passes the gist plus background
  and competitor context (≤ 3,000 chars) as `context`; jev's state carries it after the
  query. Replayed on 960 passages, p ≥ 0.5 went from 36 to 70; top-5 overlap with the
  production order 3.95–4.15 of 5 (a modest re-order).
- **One question per bucket (JL-6).** `judge_jev.BUCKET_QUESTIONS`: a comparable precedent
  (exemplars), a planning method that applies (craft), a rule this brief must respect
  (rules). `Passage.bucket` carries the bucket to the backend. On the 94 client prelabels
  the AUC moved from 0.765 to 0.870 (craft 0.783 → 0.884, rules 0.345 → 0.810). Same
  request, same cost. The prelabels are unconfirmed (BW10: ~700–800 labels for ± 0.1 kappa).
- **jev gets a deadline it can meet (JL-1).** `RAG_JEV_DEADLINE_S` (8 s, jev's own; an
  explicit `RAG_VALIDATOR_DEADLINE_S` still caps it) replaces the chain default of 3 s that
  cut jev off on a cold process (6 of 10 fields fell back in the CLI smoke runs; ok calls
  took 1.2–2.5 s, warm p90 0.45 s). `parse_brief.run()` warms the chain
  (`brief_context.warm_validator`, one ~400-token request) alongside the capture.
- **Order mode reports no rejections (JL-7).** The contract's `rejected` is 0 and `passed`
  counts the ordered hits, matching `per_bucket`; a trace no longer says "rejected 45" for
  a mode that drops nothing. Gate mode is unchanged and still not safe with one global 0.5.
- **Dedupe favours the fields a writer reads (RAG-7).** `MIX_DEDUPE_FIRST` = loop4_insight,
  loop5_proposition, loop6_substantiation claim a shared hit before loop3 and loop7 (which
  only review.md reads); loop4 used to lose 1.7 of its 5 slots on average.
- **The brief path is unfiltered, and says so (RAG-9/JL-8).** The capture's field names match
  none of `plan()`'s filter or keyword keys, so no category filter, brand keywords, widening
  or `RAG_ORDER=edge` apply to briefs; the retrieval trace now carries a note. A real category
  filter from the golden extraction (JL-8) is an A/B first.
- **The measurement (critic-G14).** `replay_validators.py` replays recorded queries through
  `build_multi` on the local store with validator none / jev / nemotron and reports served
  cites, overlap with the no-validator arm and with the recorded run, fallbacks and timing,
  with no Anthropic calls. It tells you what the validator changes, not whether the brief
  gets better; that is the writer A/B (T3).

Still open here: which validator to keep and its threshold (BW14: score jev and nemotron on
confirmed brief-domain labels with AUC and kappa CIs), gate mode's per-bucket thresholds
(exemplars 0.3, craft 0.7, rules 0.5 on the prelabels), and the `RAG_VALIDATOR` line in
Sai's local `.env` (T4).

### Decision 2026-09-26 (Sai): jev on for every brief

`brief_context.brief_chain()` gives the brief path `RAG_VALIDATOR=jev` when the variable is
unset (`none` switches it off; an explicit list is honoured); `parse_brief._loops_via_mix`
uses it and `run()` warms it. `rag_io` keeps `default_chain()` and its unset-means-off rule.
If jev cannot be built the brief runs unvalidated, says so on stderr and lists every loop
in `loops3_7.validation_degraded`; it never falls back to another validator silently.
Reasoning recorded with the decision: jev reads the brief and judges "useful for this
brief", it is the only backend that answered every field with no fallbacks in the
2026-09-25 replay, and it costs about 1.5 s and a fraction of a cent per brief. What it
does NOT do: check facts (invented figures are batch 2 and the grounding gate) or save
time against today's default (which ran no validator). Whether jev-ordered evidence makes
a better brief is still the writer A/B (T3). Nemotron is not used for briefs: on the
client prelabels it scores craft and rules far below jev.
