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
| Validation (relevance gate, jev ↔ fallback switch) | planned | — |
| Pool width set by the live validator | planned | — |
| Rerank | partial — LLM rerank in `parse_brief.loops_3_7` only | `parse_brief._rerank_hits` |
| Edge ordering (strongest at both ends) | planned | — |
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
contract response. Contract v1.0.0, `locked: false` until the middleware owner signs off
the request shape.

**Interface.**

| Call | Returns | Notes |
|---|---|---|
| `handle(request, *, index_dir=None)` | response dict | Raises `RequestInvalid` (with `.problems`, every violation) on a bad request |
| `validate(instance, definition="request")` | `list[str]` | `[]` when valid; `definition="response"` checks a response |
| `to_build_args(request)` | `(kwargs, notes)` | The mapping onto `build()`, exposed for tests and debugging |
| `response_from(ctx, run_id, notes)` | response dict | Shapes any `BriefContext` |
| `version()` | `"1.0.0"` | The contract version this code speaks |

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
| `campaign.effectiveness_type`, `brand.parent`, `target`, `research`, `attachments`, `memory`, `limits.latency_ms / target_model / recency_years` | planned | Validated, not acted on yet |

**Response** (`$defs/response`). `blocks` in prompt reading order (instructions, rules,
craft, exemplars), each hit with `cite`, `doc_id`, `source`, `text`, `retrieval_score`,
`scope`, `tenant`, `category`, `year`, `weight` (`constraint` = a reviewer rejected it,
`advice` = textbook pitfall, `evidence` = everything else) and `relevance` (null until
the validation stage exists). Plus `prompt_text`, `tokens`, `validation` (null for now),
`notes` and `trace`.

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

## Configuration

| Env | Default | Purpose |
|---|---|---|
| `RAG_STORE` | `local` — **but `engine/.env` sets `qdrant`** | store backend |
| `RAG_INDEX` | `./index` next to `rag.py` | local index dir; the current one is `_index_v3` |
| `RAG_SEARCH` | `hybrid` | `hybrid` or `dense` |
| `RAG_EMBED` | unset | `offline` = deterministic hash embedder, no network |
| `RAG_EMBED_MODEL` | `nvidia/nemotron-3-embed-1b` | must match the index's manifest |
| `RAG_EMBED_BASE` | `https://integrate.api.nvidia.com/v1` | embedding endpoint |
| `NVIDIA_API_KEY` | — | embeddings |
| `QDRANT_CLUSTER_ENDPOINT`, `QDRANT_API_KEY`, `QDRANT_COLLECTION` | — | Qdrant backend |
| `BRIEF_RERANK` | `1` | `0` disables the LLM rerank in `loops_3_7` |

`rag.py` loads `engine/.env` itself.

---

## Decision records

Non-obvious choices are recorded in `docs/adr/`: context, decision, alternatives
rejected, consequences.

| # | Decision |
|---|---|
| [0001](docs/adr/0001-rag-io-contract.md) | RAG I/O contract: JSON Schema, authority as the only boundary input, live/planned field status |
| [0002](docs/adr/0002-tuning-rebaseline.md) | Tuning re-baseline after a holdout leak: defaults kept, gain is +2.0 points not +6.8 |

## Plans

- [DGX Spark execution plan](docs/dgx-spark-plan.md) — self-hosting the embedder and reranker, full-scale evals, batch jobs, and the production licensing decision. Status in `project_plan.clan` → `dgx_spark_plan`.
