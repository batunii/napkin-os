# Napkin RAG module (Track C)

Retrieval for the brief. The middleware sends one request per brief — the campaign, the
brand record, who the run is authorised for — and gets back budgeted, citable evidence
in prompt reading order. The brief model synthesises; this module selects, checks and
orders what it reads.

Owner: Sai. Build record and reasoning: `project_plan.clan` at the repo root.
Loop-1 capture never calls this module — retrieved text must not enter the no-loss
record of the client's own brief.

---

## The flow

```
middleware ──request──▶ rag_io.handle()
                          │  validate against schema/rag_io.v1.json
                          ▼
                        brief_context.build()
                          │  plan: pairs → query, keywords, filters
                          │  scopes_for / tenants_for: authority → boundary
                          │  per bucket: hybrid search → collapse → budget
                          │  egress check: drop anything outside the boundary
                          ▼
middleware ◀─response── rag_io.response_from()
```

| Stage | Status | Component |
|---|---|---|
| Input contract | **live** | `rag_io.py`, `../schema/rag_io.v1.json` |
| Extraction (hybrid BM25 + dense, filters, widening) | **live** | `brief_context.py`, `rag.py`, `lexical.py`, `filters.py` |
| Confidentiality boundary (scope, tenant, egress) | **live** | `brief_context.scopes_for / tenants_for / build` |
| Validation (relevance gate, jev → nemotron → local switch) | **wired, off by default** (ADR 0003) | `judge.py`, `judge_*.py`, `calibrate.py` |
| Pool width set by the live validator | **wired** | `judge.Chain.pool_width` |
| Rerank | `brief_context`: sort by validator score (when on). `loops_3_7`: validation chain orders hits when `RAG_VALIDATOR` is set (ordering only, no drops), else the LLM rerank; sources are deduplicated before the cut | `parse_brief._rerank_hits`, `_chain_order` |
| Edge ordering (strongest at both ends) | **built**, `RAG_ORDER=edge`, default off pending A/B | `brief_context.edge_order` |
| Output: structured chunks + rendered prompt text | **live** | `rag_io.response_from`, `BriefContext.prompt_text` |

Two retrieval paths exist today: `brief_context.build()` (behind `rag_io`) and
`parse_brief.loops_3_7()`, which is the one currently generating briefs. They are being
consolidated onto the first — see decision record 0001.

---

## Quick start

```bash
cd engine/rag
RAG_STORE=local RAG_INDEX=./_index_v3 python3 -m pytest -q     # full suite, no network
python3 rag_io.py request.json                                  # validate a request file
```

```python
import sys; sys.path.insert(0, "engine/rag")
from rag_io import handle, RequestInvalid

resp = handle({
    "run_id": "r-1",
    "authority": {"tenant": "acme", "brand": "bmw",
                  "references": [{"name": "Mercedes", "role": "competitor"}]},
    "brand": {"name": "BMW", "categories": ["automotive"], "markets": ["UK"]},
    "campaign": {"problem": "hybrids read as a compromise",
                 "objective": "shift consideration without discounting",
                 "audience": "urban professionals 30-45", "campaign_type": "launch"},
})
resp["prompt_text"]   # drop into the prompt prefix
resp["blocks"]        # the same content, structured
resp["trace"]         # LOG THIS per brief
```

**Trap:** `engine/.env` sets `RAG_STORE=qdrant` and `rag.py` loads it, so anything run
without an explicit store goes over the network. Use `RAG_STORE=local
RAG_INDEX=./_index_v3` for local work.

---

## Components

### RAG I/O contract — `rag_io.py`, `../schema/rag_io.v1.json`

**What it is.** The module's front door. Validates a middleware request against a JSON
Schema contract, maps it onto `brief_context.build()`, and shapes the result as a
contract response. Contract **v1.2.0**, `locked: false` until the middleware owner (Shrey)
signs off the request shape. Changes: 1.1.0 made validation, research, attachments,
`memory.exclude_doc_ids` and `limits.recency_years` live; 1.2.0 added `relevance.kept: ordered`.

**Interface.**

| Call | Returns | Notes |
|---|---|---|
| `handle(request, *, index_dir=None)` | response dict | Raises `RequestInvalid` (with `.problems`, every violation) on a bad request |
| `validate(instance, definition="request")` | `list[str]` | `[]` when valid; `definition="response"` checks a response |
| `to_build_args(request)` | `(kwargs, notes)` | The mapping onto `build()`, exposed for tests and debugging |
| `response_from(ctx, run_id, notes)` | response dict | Shapes any `BriefContext` |
| `version()` | `"1.2.0"` | The contract version this code speaks |

**Request** (`$defs/request`). Required: `run_id`, `authority`, `campaign`.

| Field | Status | Meaning |
|---|---|---|
| `authority.tenant` | live | The agency. Unlocks its own material; null = house corpus only |
| `authority.brand` | live | The authorised SUBJECT, snake_case. The only thing that unlocks brand scope |
| `authority.references[]` | live | Other brands with a role: `comparator` / `competitor` become exact search terms; `parent` does not. All are public-material-only |
| `brand.name`, `brand.aliases` | live | Exact lexical terms. Descriptive, never authorising |
| `brand.categories` | live | Ranked, max 2, values from the metadata contract's `category` enum. First is the filter; second is recorded, not yet used |
| `brand.markets` | live | Joins the query text |
| `campaign.*` | live | Campaign-clan pairs. Unknown keys are accepted and join the query text |
| `limits.token_budget` | live | Per-bucket token targets over the defaults |
| `research[]`, `attachments[]` | live | Text joins the **validator's context only** (attachments are untrusted: never pairs, scope, tenant or brand) |
| `memory.exclude_doc_ids` | live | Documents refused by the admission rules |
| `limits.recency_years` | live | Recency cap on award cases (admission rule) |
| `campaign.effectiveness_type`, `brand.parent`, `target`, `memory.used_cites`, `limits.latency_ms / target_model` | planned | Validated, not acted on yet |

**Response** (`$defs/response`). `blocks` in prompt reading order (instructions, rules,
craft, exemplars), each hit with `cite`, `doc_id`, `source`, `text`, `retrieval_score`,
`scope`, `tenant`, `category`, `year`, `weight` (`constraint` = a reviewer rejected it,
`advice` = textbook pitfall, `evidence` = everything else) and `relevance`: `{value, score,
backend, kept}` when a validator ran (`kept`: `ordered` in the default order-only mode;
`passed` / `floor` / `unjudged` / `exempt` in gate mode), null when validation is off.
Plus `prompt_text`, `tokens`, `validation` (which backend judged, pool size, passed,
rejected, fell back — null when off), `notes` and `trace`.

**Guarantees.**
- Scope, tenant and brand authorisation come from `authority` only. Brand names, campaign
  pairs and attachments are search text at most.
- `authority` rejects unknown keys; `campaign` accepts them. The boundary cannot grow a
  field nobody reviewed, and the campaign clan can grow without breaking callers.
- Enum values are resolved from `rag_metadata.v1.json` at check time (`x-enum-from`),
  never copied.
- Every field marked `live` provably changes what `build()` receives
  (`test_every_live_request_field_reaches_build`). A field cannot be advertised before it
  is wired.

**Failure behaviour.** An invalid request raises before any retrieval, listing every
problem. A major-version mismatch in `contract_version` is a validation error. Retrieval
errors propagate from `brief_context` unchanged.

**Tests.** `test_rag_io.py` — 23 tests: contract integrity, validation, the adapter
mapping, the live-field guard, response shape.

**Decision record.** [0001 — RAG I/O contract](docs/adr/0001-rag-io-contract.md).

### Brief retrieval — `brief_context.py`

Campaign pairs in, four budgeted citable blocks out: `exemplars` (precedent), `craft`
(how planners think), `rules` (never/always — selected by filter, never by similarity),
`instructions` (what a good brief contains). Budgets are token targets (~8k total,
`DEFAULT_BUDGET`); the top hit in a bucket is always kept. `plan()` turns pairs into a
query, exact keywords and contract filters deterministically. The widening ladder drops
`effectiveness_type` before `category`, on the creative director's ruling. The egress
check removes any hit outside the authorised scopes or tenants and records it in the
trace. Retrieve once per brief and freeze the result: it is the shared prompt prefix.

### Retrieval primitives — `rag.py`, `retrieve.py`, `lexical.py`, `filters.py`

`rag.py` builds the index, embeds (`nvidia/nemotron-3-embed-1b`, 2048-d) and searches.
`lexical.py` adds BM25 and reciprocal rank fusion (tuned: BM25 `b=0.3`, RRF `k=10`).
`filters.py` is one filter language with two renderers (local and Qdrant).
`retrieve.retrieve()` excludes production-stage and superseded material by default and
applies scope and tenant with a fail-closed default. `check_grounding()` verifies cited
ids against what was retrieved — string matching, no model.

### Chunking and metadata — `chunking.py`, `normalise.py`, `contract.py`

Chunker v2 follows document shape: IPA and Cannes get a parent per case and a child per
section or entry-form answer; playbooks a chunk per section; D&AD is grouped by
discipline and year and marked `stage=production`; templates keep tables whole.
`contract.py` reads `../schema/rag_metadata.v1.json` (v1.3.0, locked) and defines nothing
itself. `normalise.py` holds every spelling table and returns a contract value or None —
it never guesses.

### Stores — `store_base.py`, `store_local.py`, `store_qdrant.py`

One `VectorStore` contract, backends selected by `RAG_STORE`. `local` is the canonical
artefact `build` always writes; `qdrant` (collection `Napkin_OS`, 7,315 points, sparse
vectors live) is production. Add a backend by copying `store_template.py` and adding one
line to `REGISTRY`. Bulk evals do not run against the hosted Qdrant tier — it sheds
connections under burst load. Tune local, serve remote.

```bash
python3 rag.py stores                                        # every backend: configured? rows?
python3 rag.py migrate --from local --to qdrant --replace    # copies vectors, never re-embeds
python3 rag.py retag --corpus <corpus>/rag --index ./_index_v3 --apply   # metadata only
```

`migrate --replace` is destructive to a shared collection — ask first.

### Evaluation — `golden.py`, `golden_check.py`, `tune.py`, `simulate.py`

`golden.py` builds 11,651 cases from the corpus's own "Retrieval Queries" with a 1-in-5
held-out split. Held-out recall@5 **0.928**, recall@10 **0.945** (290 cases, re-measured
2026-09-23; the weak spot is templated IPA queries at 0.825). The earlier published 0.974 /
1.000 came from a tuning sweep whose keyword index leaked held-out question text — fixed in
`tune.py`, which now builds every configuration's index through `LocalStore.build_bm25()`. `tune.py` sweeps knobs on the
held-out set only. `simulate.py` shows what a model would actually receive — the golden
set measures whether the right document is found; only this shows whether it is worth
reading. Read the misses, not just the number.

---

## Validation stage — `judge*.py`, `calibrate.py`

Checks each retrieved chunk against the query plus brief context and keeps what is directly useful. A switch over interchangeable backends, each the failsafe for the one before: **jev** (TypeSafe; ready, no access yet) → **nemotron** (hosted NVIDIA reranker) → **local** (cross-encoder on this machine). Admission rules in code run first. Status: wired into `brief_context.build` and `rag_io` v1.1.0 (steps 3–4; `loops_3_7` is step 5). **Off by default and not yet safe to enable for briefs**: a live brief showed nemotron, a QA reranker, rejects award-case precedent whatever the query phrasing — see ADR 0003, *Live finding*. Decisions: [ADR 0003](docs/adr/0003-validation-stage.md).

Turn it on with `RAG_VALIDATOR=nemotron,local` in `engine/.env` (jev first once `TYPESAFE_API_KEY` exists). Check what is alive with `python3 judge.py check`. Unset means validation off.

Every backend implements `judge_base.py`: `Query`, `Passage`, `Verdict` (value / score / why / backend / raw), `BackendNotConfigured` (construction: a hard error) and `BackendUnavailable` (run time: fall through), plus the shared `load_calibration` and `classify_http`.

### Validation chain — `judge.py`

**What it is.** The switch every caller uses for the validation stage. It holds the relevance backends named in `RAG_VALIDATOR` in priority order, each a failsafe for the one before. It judges a pool of passages with the first backend that answers and applies the verdicts to hits. The backends implement `judge_base.py`: jev, NVIDIA reranker, local cross-encoder, LLM judge. The chain never raises because a backend failed at run time; at worst the caller keeps the fused order. It does raise at start-up when a backend someone asked for is not configured (M4).

```python
from judge import chain_from_env, select
chain  = chain_from_env()                         # RAG_VALIDATOR="jev,nemotron,local"
width  = chain.pool_width(default=40)             # never narrower than validation-off retrieval
result = chain.judge(query, passages)             # judge_base.Query, [judge_base.Passage]
kept   = select(list(zip(hits, result.aligned())), floor=2)
trace["validation"] = result.as_dict()            # response["validation"] = result.as_contract()
```

**Interface.**

| Call | Returns | Notes |
|---|---|---|
| `chain_from_env(env=None)` | `Chain` | Unset / `""` / `none` gives an empty chain (validation off). Duplicate or unknown names raise `ValueError`. A named backend that is not configured raises `BackendNotConfigured` naming `RAG_VALIDATOR` |
| `build_backend(name, **kw)` | `Backend` | Imports the module lazily from `REGISTRY`. An unknown name raises `ValueError` listing the known ones. A failed import raises `BackendNotConfigured` naming the module |
| `Chain(backends, *, deadline_s=3.0, down_for_s=60.0, clock=time.monotonic, grace_s=0.1)` | | `.names`, `.empty`. Refuses a non-Backend, a capacity below 1, or duplicate names |
| `Chain.pool_width(default)` | `int` | The larger of `default` and the capacity of the first backend not marked down; `default` if the chain is empty or every backend is down. Never narrower than validation-off retrieval |
| `Chain.judge(query, passages)` | `ValidationResult` | Thread-safe. Judges the first `capacity` passages; the rest are counted as `truncated` |
| `ValidationResult` | | `verdicts` (None when nobody answered), `backend_requested`, `backend_used`, `attempts`, `fell_back`, `judged`, `truncated`, `provisional`, `pool_size`. `.aligned()` gives one entry per pooled passage. `.as_contract()` returns exactly `$defs/validation`. `.as_dict()` adds `attempts`, `judged`, `truncated`, `provisional` for the trace |
| `select(pairs, *, floor=2)` | `[(item, verdict, kept)]` | Order: passing items (by score, highest first, then unscored ones in fused order) as `passed`; then unjudged items in fused order as `unjudged`; only if nothing passed **and** nothing unjudged survives (every chunk failed) the best `floor` judged items as `floor`, ranked by calibrated score, else raw backend output, else fused order. Rejected items are dropped. Stable |
| `REGISTRY` | `dict` | `jev`, `nemotron`, `local`, `llm` map to `"module:Class"` strings |

Each attempt is recorded as `{"backend", "outcome", "ms", "status", "detail"}`. `outcome` is `ok`, `skipped_down`, or a `BackendUnavailable` kind (`timeout`, `http_error`, `retired`, `not_entitled`, `bad_response`, `rate_limited`, `error`).

```bash
python3 judge.py check                        # probe every backend in RAG_VALIDATOR (hits the network)
python3 judge.py check --backends jev,local --deadline 5
```

`check` builds each backend on its own. It sends a two-passage probe (one relevant, one irrelevant) and prints, per backend: configured?, alive?, outcome, ms, and whether it ranked the relevant passage higher. Exit 1 when more than one backend was requested and at most one is alive, or when every requested backend is dead. Exit 2 on a malformed backend list.

**Configuration.**

| Env | Default | Purpose |
|---|---|---|
| `RAG_VALIDATOR` | unset (validation off) | Comma list in priority order, e.g. `jev,nemotron,local`. Deliberately no default backend |
| `RAG_VALIDATOR_DEADLINE_S` | `3.0` | Per-call deadline for any backend that does not declare its own `deadline_s`. Must be a positive number |

**Failure behaviour.**
- Not configured: missing key, SDK, model file, module or class. Raised at construction, and `chain_from_env` re-raises it naming `RAG_VALIDATOR`.
- Unreachable at run time: `BackendUnavailable`, or a hard timeout at the backend's deadline plus 0.1s grace. It is recorded in `attempts`, the backend is marked down for `down_for_s` (breaker), and the chain falls through.
- A bug inside a backend (any exception, even `SystemExit`) is recorded as `error`, trips the breaker and falls through.
- The chain checks each backend's output against the invariants before accepting it: one verdict per passage in input order, correct backend label, no score from an uncalibrated backend, never score and why together. A breach becomes `bad_response`.
- Nobody answered: `verdicts=None`, `fell_back=True`, and the caller keeps the fused order.

**Tests.** `test_judge.py` has 60 tests, all on in-memory fakes with no network: order and fall-through, the breaker and its recovery on a fake clock, the deadline enforced on a backend that ignores it, capacity truncation, containment of exceptions, output-invariant checks, the contract shape, `select()` semantics, env parsing and hard errors, a concurrency smoke test and the CLI report.

### Admission predicates — `judge_code.py`

**What it is.** Rules that run before any relevance backend and need no model. They answer "is this chunk allowed, and is it usable at all?", not "is it relevant?". A policy with an exact answer is computed in code. It is not handed to a reranker, where it would become probabilistic, use up a slow backend's capacity and be hidden inside a score nobody can audit.

**Interface.**

| Call / name | Returns | Notes |
|---|---|---|
| `admit(hit_metadata, text, *, exclude_doc_ids=frozenset(), recency_years=None, as_of=None, max_chars=MAX_PASSAGE_CHARS)` | `None` or a reason | `None` = admitted. Checks run in the order excluded, oversized, too_old; the first that fires is reported |
| `REFUSAL_REASONS` | `("excluded", "too_old", "oversized")` | Closed, so the trace can be grouped by reason |
| `MAX_PASSAGE_CHARS` | `12_000` | Measured on `_index_v3`: admits 7,310 of 7,315 chunks (99.93%). Refuses only five playbook sections from `91-pestle-steep-analysis.md` and `26-aaker-brand-equity-model.md` |
| `RECENCY_SOURCES` | `{ipa, effie, cannes, dandad}` | Sources whose `year` is the award year; kept equal to `chunking._CASE_SOURCES` by a test |
| `parse_year(value)` | `int` or `None` | Only an int or an exactly four-digit string counts |

**Rules.**
- `excluded`: `metadata.doc_id` is in the caller's list (compared as strings). A chunk with no doc_id cannot be excluded.
- `oversized`: `len(text) > max_chars`. This is a corpus defect, not a relevance question. It differs from `parse_brief.EVIDENCE_MAX_CHARS` (6,000), which clips text for display: refusing at 6,000 would drop 38 real case parents.
- `too_old`: an award case where `as_of.year - year > recency_years`. The boundary is inclusive, so a 5-year cap from 2026 admits 2021. A missing or unparseable year is admitted, since the age is never guessed. Playbook and template years are never capped, because a playbook's `year` is when the framework was invented (FCB Grid, 1980), not how old the evidence is.
- Not implemented: licence class. The metadata contract has no licence field; adding one needs a minor contract bump and a retag backfill first.

**Configuration.** None. Everything is an argument. `as_of` defaults to today; pass it explicitly when replaying a run.

**Failure behaviour.** Pure and total: bad metadata admits rather than raising. The only error is `ValueError` for a negative or bool `recency_years`, which is a bug in the caller.

**Tests.** `test_judge_code.py`, 47 tests. Covers each reason, the boundary years, the absent-year rule against real junk values from the index, check order, the chunking drift guard, and the cap checked against `_index_v3` (skipped if the index is absent).

### LLM judge backend — `judge_llm.py`

**What it is.** `LLMJudgeBackend`, a `judge_base.Backend` that asks a chat model, in one call, which passages are directly useful evidence. It is the slow, uncalibrated last resort: available, but not in the recommended chain. It reuses the engine's model chain (`parse_brief._json_call`) and has no client of its own, so a retired model or a missing key has only one place to hide.

**Interface.**

| Call / name | Returns | Notes |
|---|---|---|
| `LLMJudgeBackend(*, model=None, clip_chars=600, query_chars=2000)` | backend | `name="llm"`, `capacity=8`, `calibrated=False`. `model` pins the chain's first link |
| `.score(query, passages, *, deadline_s)` | `list[Verdict]` | One per passage, input order. `value` from `relevant`, `why` from the model, `score` and `raw` always None |
| `.prompt(query, passages)` | `str` | What the model is shown: passages numbered from 0, each with its cite id, whitespace collapsed and clipped to 600 chars |
| `.chain` | `list[str]` | The `provider:model` links the call will walk, for the trace |
| `RESPONSE_SCHEMA` | dict | `{"verdicts":[{"index":int,"relevant":bool,"why":str}]}`, strict-mode compatible, passed as `schema=` |

**Configuration.** Uses the engine's model-chain settings (`BRIEF_PROVIDER`, `BRIEF_MODEL`, `BRIEF_MODEL_CHAIN` and the provider keys). `parse_brief` is imported lazily in the constructor. **Trap:** that import loads `engine/.env`, which sets `RAG_STORE=qdrant` if it is not already set. Set `RAG_STORE` explicitly in local work.

**Failure behaviour.**

| When | Raises |
|---|---|
| `parse_brief` does not import, has no `_json_call`, or no chain link has its API key | `BackendNotConfigured` at construction |
| No answer within `deadline_s` (or `deadline_s <= 0`) | `BackendUnavailable(kind="timeout")`. The worker is a daemon thread and keeps running in the background; its result is discarded |
| A verdict index is missing, repeated, out of range, or not an int; or `relevant` is not a bool | `BackendUnavailable(kind="bad_response")`. Never repaired |
| The chain was exhausted with no JSON, or the call raised | `BackendUnavailable(kind="error")` |

**Tests.** `test_judge_llm.py`, 35 tests. A fake `parse_brief` goes into `sys.modules`, so there is no network and no real model.

### Nemotron reranker backend — `judge_nemotron.py`

**What it is.** A validation backend (`judge_base.Backend`, name `nemotron`) built on NVIDIA's hosted cross-encoder `nvidia/llama-nemotron-rerank-vl-1b-v2`, which is the only NVIDIA reranker this account can still reach. It reads the query and each passage together and returns one logit per passage. On the held-out golden set the pooled AUC is 0.983: relevant passages have a median logit of +7.4 and irrelevant ones -7.4. It makes one HTTP call per `score()` and never retries, because falling through to the next backend is the chain's job.

**Interface.**

| Call / attribute | Returns | Notes |
|---|---|---|
| `NemotronBackend(api_key=None, chars=None, query_chars=None, capacity=40, calibration=None, calibration_path=None)` | backend | Raises `BackendNotConfigured` if there is no key, a cap is bad or the calibration file is broken. Makes no network call. |
| `.score(query, passages, *, deadline_s)` | `list[Verdict]` | One per passage, in input order. Raises `BackendUnavailable(kind=...)` and never returns partial output. `[]` in means `[]` out with no call. |
| `.capacity` | `40` | Passages per call that the deadline is sized for |
| `.calibrated`, `.calibration` | bool, `Calibration \| None` | The chain reads `.calibration.provisional` for the trace |
| `.describe()` | dict | Model, caps and calibration state for the trace. Never includes the key. |
| `RECOMMENDED_DEADLINE_S`, `.deadline_s` | `3.0` | Measured at capacity; declared on the backend, so the chain uses it |
| `request_body()`, `parse_rankings()`, `classify_http()`, `load_calibration()` | | Exposed for tests and debugging |

**Verdicts.** `raw` is always the unmodified logit and `why` is always None.
- With `calibration/nemotron.json` present: `score = cal.probability(logit)` and `value = score >= cal.threshold`.
- Without it: `score = None`, `value = logit > 0` (the model's own boundary), and one line goes to stderr per process.

**Input shaping.**
- `Passage.text` is clipped to `RAG_NEMOTRON_CHARS` (1500). Callers should put the chunk header first so it survives the clip.
- The query is `Query.combined(max_chars=RAG_NEMOTRON_QUERY_CHARS)` (1000), which cuts context before the query.
- `truncate: END` stays on as a backstop.

**Configuration.**

| Env | Default | Purpose |
|---|---|---|
| `NVIDIA_API_KEY` | — | Required. Found in `engine/.env` through rag.py's loader, the same way embeddings find it |
| `RAG_NEMOTRON_CHARS` | `1500` | Passage clip |
| `RAG_NEMOTRON_QUERY_CHARS` | `1000` | Query-plus-context clip. The query is paid once per passage, so this cap matters |
| `calibration/nemotron.json` | absent | Platt `a`, `b`, `threshold`, `fitted_on`, `n`, `provisional`, written by the calibration step |

**Failure behaviour.**
- Construction raises `BackendNotConfigured` for a missing or blank key, a cap that is not a positive integer, a calibration file that exists but is unusable, or a missing `requests` package.
- `score()` raises `BackendUnavailable` with one of these kinds:
  - `retired`: 410 or "end of life"
  - `not_entitled`: 401 / 403, or 404 "Not found for account"
  - `http_error`: a bare 404 (wrong URL or model) and any other 4xx or 5xx
  - (the mapping is `judge_base.classify_http`, shared with jev, so one kind means one owner)
  - `rate_limited`: 429
  - `timeout`: a connect or read timeout, a hang reaching `deadline_s`, or `deadline_s <= 0`
  - `http_error` also covers connection refused and TLS failures (as jev reports them); `error` only for an unexpected exception
  - `bad_response`: a body that is not JSON, or rankings that are short, repeated, out of range or non-finite
- The call runs in a daemon thread, so `score()` returns at `deadline_s` even if the socket hangs.

**Tests.** `test_judge_nemotron.py`: 51 tests, all without network, covering index remapping, every failure class, the hang deadline, the missing key, calibrated and uncalibrated modes, and truncation.

### jev validation backend — `judge_jev.py`

**What it is.** The TypeSafe jev backend for the validation stage. For each retrieved passage it asks jev one calibrated yes/no question (a *Noul*): "Is PASSAGE directly useful evidence for this brief?" It is the only calibrated backend in the chain: `score` is jev's own probability, `value` is `score >= threshold`, and `why` is always None. It can be written and tested without access; when a key arrives, install the SDK, set the key and put `jev` first in `RAG_VALIDATOR`.

**Interface.**

| Call | Returns | Notes |
|---|---|---|
| `JevBackend(client=None, *, sdk=None, env=None, model=None, threshold=None, state_chars=None, passage_chars=None, batch_questions=None, concurrency=None)` | backend | `name="jev"`, `capacity=50`, `calibrated=True`. Raises `BackendNotConfigured` and lists every problem. Pass `client` to inject any object with the SDK's `system_one` method |
| `.score(query, passages, *, deadline_s)` | `list[Verdict]` | One per passage, input order. `score == raw` = P(yes) from `response.nouls[name].noul` |
| `.last_call` | dict | `requests`, `models` (the versioned model that answered), `input_tokens`, `seconds`, for the trace |
| `.close()` | — | Closes the SDK's HTTP client |
| `plan_batches(state, texts, *, passage_chars, batch_questions)` | batches | Exposed for tests and for checking the budget |

**How a call is shaped.** The **state** is the brief: `{"retrieval_query", "brief_context"}`, clipped to 4000 chars, context first. The same state is sent in every request. Each passage becomes its **own Noul question**, `{"type":"noul","instructions":{"question","passage"},"criteria":{"true","false"}}`, named by its position (`p000`…), and answers are joined back by name. jev evaluates all questions in a request in parallel against one state, so one request judges many passages. Keeping the state small matters because TypeSafe documents that accuracy falls as the state fills with unrelated content. Passages are clipped to 6000 chars and packed greedily, in order, so that every request stays within 75% of both hard limits: 64k tokens per request, and 32k for the state plus the longest question. The estimate counts 4 ASCII chars per token and 1 token per non-ASCII char. Batches run concurrently. At full capacity with full-size passages that is 2 requests, 29 and 21 passages, costing about $0.0035.

**Configuration.**

| Var | Default | Meaning |
|---|---|---|
| `TYPESAFE_API_KEY` | — | Required. Read by the SDK |
| `TYPESAFE_BASE_URL` | `https://api.typesafe.ai` | Read by the SDK |
| `RAG_JEV_MODEL` | `jev-latest` | Model or alias sent on every call |
| `RAG_JEV_THRESHOLD` | `0.5` | `value = score >= this` |
| `RAG_JEV_STATE_CHARS` / `RAG_JEV_PASSAGE_CHARS` | `4000` / `6000` | Clip sizes |
| `RAG_JEV_BATCH_QUESTIONS` | `50` | Most Nouls per request |
| `RAG_JEV_CONCURRENCY` | `4` | Most requests in flight at once |

Install: `pip install 'typesafe-sdk==0.7.1'`. The SDK is imported lazily, so the module loads without it.

**Failure behaviour.** *Not configured* is a hard error: missing SDK, missing key, a key the SDK rejects, an unparseable or out-of-range setting, or a state clip too large for the 32k rule each raise `BackendNotConfigured` at construction. One message lists every problem with the exact fix. *Unavailable* falls through to the next backend: `score()` raises `BackendUnavailable`, with SDK exceptions mapped as follows:
- timeout or 408 → `timeout`
- connection error → `http_error`
- 429 → `rate_limited`
- 402 → `not_entitled`; every other status through the shared `judge_base.classify_http`: 401/403 → `not_entitled`, 410 → `retired`, a bare 404 (wrong `RAG_JEV_MODEL`) → `http_error`
- 400/422/5xx → `http_error`, with the status recorded
- malformed 2xx body, or a missing, NaN or out-of-range answer → `bad_response`
- anything else → `error`

One failed batch fails the whole call; there is never partial output. SDK retries are off (`RetryPolicy(max_retries=0)`), and the whole call is bounded by `deadline_s` with `concurrent.futures.wait`, so it never blocks past the deadline.

**Tests.** `test_judge_jev.py`, 56 tests, no network. The fakes copy the SDK 0.7.1 response and exception classes and cite the source line of each. Seven of the tests drive the real SDK through `httpx2.MockTransport` and check our request against the SDK's own request schema. They are skipped unless `typesafe_sdk` imports; set `RAG_JEV_SDK_PATH` to run them without installing.

**Provisional.** jev's probabilities are vendor-calibrated, but the 0.5 threshold has never been checked on Napkin data, so `provisional = True` and the trace says so. `.describe()` reports model, capacity and threshold.

### Local cross-encoder — `judge_local.py`

**What it is.** `BAAI/bge-reranker-v2-m3` running in this process on mps or cpu: the vendor-free last line of the validation chain, which no vendor can retire. Weaker and slower than hosted nemotron (recall@1 +0.007 against +0.026, AUC 0.965 against 0.983 on the same pools), so it goes last.

**Contract: one call per brief.** All local inference shares one worker thread. Pass every bucket's passages in one `score()` call of at most `capacity`; four per-bucket calls at pool 20 took 12.4 s against an 8 s deadline, one combined call 3.4 s.

**Interface.** `LocalCrossEncoderBackend(model_name=None, *, device=None, capacity=None, deadline_s=8.0, calibration=None, calibration_path=None)`; `.score(query, passages, *, deadline_s=None)` returns one Verdict per passage in input order with `raw` = the logit; `.describe()` for the trace. Weights: `python3 judge_local.py --download` (2.3 GB, once), `--check` to load and score a sample.

**Configuration.**

| Env | Default | Purpose |
|---|---|---|
| `RAG_LOCAL_RERANKER` | `BAAI/bge-reranker-v2-m3` | HF repo id or model directory. `bge-reranker-base` is 3x faster but its reranked recall@1 (0.906) is worse than no reranking (0.943) |
| `RAG_LOCAL_DEVICE` | mps if available, else cpu | Asking for a device the machine lacks is an error, not a silent cpu fallback |

Capacity per call, sized so 2 x p90 fits the 8 s deadline, measured with brief-shaped queries (query + 1000 chars of context): **mps 20** (p90 3.37 s), **cpu 6** (p90 2.48 s; cpu was only partly re-measured), cuda unmeasured and set to 6 until the DGX Spark is measured.

**Calibration.** `calibration/local.json` (provisional, golden-fitted: a 0.664, b −0.166, threshold 0.439). Loaded through `judge_base.load_calibration`, which refuses a file fitted for a different model, max length or passage clipping. Without a file: `score` None, `value = logit > 0`. Known weakness: at this threshold playbook recall is 0.37 on held-out cases — see ADR 0003.

**Failure behaviour.** Missing weights, SDK, device or a mismatched calibration: `BackendNotConfigured` at construction (weights never download at brief time). Deadline passed: `timeout` — the prediction finishes in the background and queued calls whose callers gave up are cancelled. Wrong count or non-finite logit: `bad_response`; any other inference failure: `error`.

**Tests.** `test_judge_local.py` on a fake model: no weights, torch or network.

### Calibration — `calibrate.py`, `calibration/<backend>.json`

**What it is.** It turns a scoring backend's raw logit into a probability and picks the gate's threshold. It fits Platt scaling, `p = sigmoid(a*raw + b)`, from labelled (query, passage, label) pairs. It then writes `calibration/<backend>.json`, which `judge_nemotron` and `judge_local` load at construction. A backend with no file stays uncalibrated: `score=None`, and `value = raw > 0`.

**Where the labels come from, for now.** The golden set's held-out cases, using the rerank pilot's selection: 119 `specific` IPA/Cannes cases plus 40 playbook cases (seed 7), 159 in all. For each case the hybrid pool is rebuilt: the top 40 chunks from `_index_v3`, filtered by source. A passage is positive when its `doc_id` is in the case's accept set. Raw scores come from `backend.score()`, so the backend's own clipping is what gets calibrated.

**Caveats.** Every file is `provisional: true`.
- The labels are document-level on passages. An out-of-set chunk can be relevant (counted as a false positive), and an in-set chunk can be off-topic (counted as a false negative).
- Golden queries are retrieval-shaped; briefs are not. Refit on labelled brief pairs before setting `provisional: false`.

**Interface.**

| Call | Returns | Notes |
|---|---|---|
| `fit_platt(raws, labels)` | `(a, b)` | Newton with Platt's smoothed targets; raises on one class |
| `best_f1_threshold(probs, labels)` | `float` | Midpoint between adjacent distinct probabilities; F1 ties pass more |
| `metrics_at(probs, labels, t)` | dict | Precision, recall, F1, pass rate, confusion counts |
| `auc(scores, labels)` / `ece(probs, labels)` | `float \| None` | Mann-Whitney with ties; 10 equal-width bins |
| `fit_calibration(scored, backend=, fitted_on=, capacity=)` | JSON dict | 70/30 split by case; fits on train, reports held-out test |
| `build_pools(cases, search)` / `score_pools(backend, pools, deadline_s=)` | pools / `(scored, stats)` | Search and backend are injectable |

**File contents.** `a`, `b`, `threshold`, `fitted_on`, `n` and `provisional`, plus the fit's `n_pos` / `n_neg` and the split. Train and test blocks each give AUC, `ece_10`, raw medians, and P/R/F1/pass rate at 0.3, 0.5, 0.7 and the chosen threshold. The test block adds a reliability table. There are also `test_by_source`, `test_within_capacity` (local only), `backend_config` and `run` (skips and latency).

**Current fits (2026-09-23, held-out test cases).**

| Backend | a | b | threshold | Test AUC | ECE | P / R at threshold | Pass rate |
|---|---|---|---|---|---|---|---|
| nemotron | 0.531 | -0.088 | 0.128 | 0.980 | 0.027 | 0.822 / 0.930 | 17.9% |
| local (bge-reranker-v2-m3, mps) | 0.664 | -0.166 | 0.439 | 0.968 | 0.020 | 0.801 / 0.809 | 15.7% |

At the chosen threshold, local's playbook recall on held-out cases is 0.37; nemotron's is 0.73.

**Configuration.**

```bash
cd engine/rag && set -a && . ../.env && set +a
RAG_STORE=local RAG_INDEX=./_index_v3 python3 calibrate.py nemotron local \
    --pools-cache /tmp/.../pools.json --save-raws /tmp/.../   # both flags point at scratch
```

The per-call deadline is 20 s for nemotron and 120 s for local (`--deadline` overrides it). `--dry-run` prints the result without writing it.

**Failure behaviour.**
- The script refuses to run unless `RAG_STORE` is local, and also when `RAG_EMBED=offline`. It checks again after `engine/.env` loads.
- A backend that is not configured is reported and skipped. The script exits 2 if no backend could be calibrated.
- Calls are sequential with a 0.2 s pause. `BackendUnavailable` skips the case and counts it by kind, and the pause then backs off (2, 4, 8 s, up to 30 s).
- `retired`, `not_entitled` or 6 consecutive failures stop the backend.
- If either side of the split lacks a class, the fit is refused and no file is written.
- Files are written atomically (temp file, then rename).

**Tests.** `test_calibrate.py` has 32 tests, all on synthetic data: fit, threshold, AUC, ECE, split, JSON round-trip through both backends' loaders, pools, scoring and the environment guard.

Only `nemotron` and `local` take a Platt fit (`PLATT_BACKENDS`): jev is vendor-calibrated and llm is pass/fail, so `calibrate.py jev` is skipped with a message rather than crashing.

---

## Configuration

| Env | Default | Purpose |
|---|---|---|
| `RAG_STORE` | `local` — **but `engine/.env` sets `qdrant`** | store backend |
| `RAG_INDEX` | `./index` next to `rag.py` | local index dir; the current one is `_index_v4` (7,392 chunks, with BetterBriefs; migrated to Qdrant 2026-09-23). `_index_v3` is the previous build |
| `RAG_SEARCH` | `hybrid` | `hybrid` or `dense` |
| `RAG_EMBED` | unset | `offline` = deterministic hash embedder, no network |
| `RAG_EMBED_MODEL` | `nvidia/nemotron-3-embed-1b` | must match the index's manifest |
| `RAG_EMBED_BASE` | `https://integrate.api.nvidia.com/v1` | embedding endpoint |
| `NVIDIA_API_KEY` | — | embeddings |
| `QDRANT_CLUSTER_ENDPOINT`, `QDRANT_API_KEY`, `QDRANT_COLLECTION` | — | Qdrant backend |
| `BRIEF_RERANK` | `1` | `0` disables the LLM rerank in `loops_3_7` |
| `BRIEF_FULLTEXT` | unset | `1` = A/B arm: the brief generator reads evidence spans (capped 1200 / 800 chars) instead of 220 / 160-char snippets |
| `BRIEF_SYNTH_MODEL` | model chain default | model for the per-loop synthesis paragraphs (now one call per loop, in parallel) |
| `RAG_PATH` | `mix` | Which retrieval feeds the brief generator (`loops_3_7`): `mix` = each loop's per-field query through `brief_context.build_multi()` (scope, admission, budgets, widening, validation; ~1.4s warm); `loops` = the previous per-loop retrieve + rerank + case packs. Chosen 2026-09-23; `loops` stays one switch away until Shrey's finished-brief test |
| `RAG_VALIDATION_MODE` | `order` | `order` re-sorts by validator score and drops nothing; `gate` also drops what fails the threshold (measured unsafe for briefs: keeps 1/24 useful client exemplars) |
| `RAG_ORDER` | `score` | `edge` puts the strongest hits at both ends of exemplars and craft |
| `RAG_VALIDATOR` | unset (off) | Validation chain in priority order, e.g. `nemotron,local`; `jev` first once `TYPESAFE_API_KEY` is set |
| `RAG_VALIDATOR_DEADLINE_S` | `3.0` | Per-call deadline for backends that declare none |
| `RAG_LOCAL_RERANKER`, `RAG_LOCAL_DEVICE` | `BAAI/bge-reranker-v2-m3`, mps/cpu | Local cross-encoder |
| `RAG_NEMOTRON_CHARS`, `RAG_NEMOTRON_QUERY_CHARS` | `1500`, `1000` | Nemotron input clipping (a calibration file refuses a mismatch) |
| `TYPESAFE_API_KEY`, `RAG_JEV_MODEL`, `RAG_JEV_THRESHOLD` | —, `jev-latest`, `0.5` | jev backend |

`rag.py` loads `engine/.env` itself.

---

## Decision records

Non-obvious choices are recorded in `docs/adr/`: context, decision, alternatives
rejected, consequences.

| # | Decision |
|---|---|
| [0001](docs/adr/0001-rag-io-contract.md) | RAG I/O contract: JSON Schema, authority as the only boundary input, live/planned field status |
| [0002](docs/adr/0002-tuning-rebaseline.md) | Tuning re-baseline after a holdout leak: defaults kept, gain is +2.0 points not +6.8 |
| [0003](docs/adr/0003-validation-stage.md) | Validation stage: a backend chain with hard config errors and soft run-time fall-through, provisional calibration, one local call per brief |

## Plans

- [DGX Spark execution plan](docs/dgx-spark-plan.md) — self-hosting the embedder and reranker, full-scale evals, batch jobs, and the production licensing decision. Status in `project_plan.clan` → `dgx_spark_plan`.
