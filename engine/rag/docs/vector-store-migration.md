# Moving the RAG vector store off Qdrant (to AWS)

For Shrey. Written 2026-09-24 when the decision was taken to move the store to AWS (about
$10k of AWS credit). The Qdrant code is frozen: do not change `store_qdrant.py`. Everything
below uses tools that already exist; the only new code is one backend module.

## 1. What exists today

| | |
|---|---|
| Canonical index (local, the source of truth) | `engine/rag/_index_v4/` — `chunks.jsonl` (rows + vectors) and `manifest.json` |
| Rows | 7,392 chunks (IPA 3,480, D&AD 2,049, playbook 1,213, Cannes 497, templates 153) |
| Embeddings | `nvidia/nemotron-3-embed-1b`, **2048 dimensions**, L2-normalised (cosine = dot) |
| Production store | Qdrant Cloud, collection `Napkin_OS` (`RAG_STORE=qdrant` in `engine/.env`) |
| Search | hybrid: dense cosine + BM25 sparse vectors, fused with reciprocal-rank fusion (`lexical.rrf`, `rrf_k=10`) |
| Previous index | `engine/rag/_index_v3/` (7,315 chunks, before BetterBriefs) |

Every caller above the store layer (`rag.py`, `retrieve.py`, `brief_context.py`,
`parse_brief.py` Loops 3–7) talks only to the `VectorStore` contract in `store_base.py`.
Switching stores is one environment variable; moving data is one command.

## 2. The store contract a new backend must meet

Required (`store_base.VectorStore`):

| Method | Must do |
|---|---|
| `__init__(**kwargs)` | read config from env; raise `StoreConfigError` here and only here when config is missing; accept and ignore `index_dir=`; do not contact the server |
| `available()` | True only if reachable **and** non-empty; return False (don't raise) when down |
| `ensure(dim)` | create the table/collection for `dim`-sized vectors; idempotent |
| `upsert(rows)` | insert-or-replace keyed by `row["id"]` (stable, deterministic → re-runs are idempotent); return rows written |
| `search(qvec, k, where)` | top-k by cosine → `[(score, row), …]`; rows may omit `vector` |
| `scroll(batch)` | yield every row **with** its vector (used by `migrate` and `store-check`) |
| `count()` | number of rows |

Optional, but used when present:

| Method | Used by | If missing |
|---|---|---|
| `search_hybrid(qvec, qtext, k, where, n, rrf_k, weights)` | `rag.search` (hybrid is the default) | falls back to **dense only**: lower recall, silently |
| `search_lexical(qtext, k, where)` | lexical-only paths and evals | not available |
| `get(id)`, `get_many(ids)` | `brief_context._collapse` (parent lookups; `get_many` cut Qdrant requests 91 → 40 per brief) | one request per parent |
| `delete_all()` | `migrate --replace` | the store cannot be a `--replace` target |
| `describe()` | run metadata (`{"store": ..., "label": ...}`) | a generic label |

**Row shape** (what `upsert` receives and `search`/`scroll` return):
`{id, source, section, chunk_index, metadata{...}, text, retrieval_queries, vector[float]}`.

**Filters** (`filters.py`, one language for every store): a value is either a plain value
(equality) or one operator: `eq ne in nin gt gte lt lte exists`, e.g.
`{"status": {"ne": "superseded"}, "scope": {"in": ["global", "brand:bmw"]}}`.
Two rules the new backend must honour exactly:
- `eq` / `in` on an **absent** field do **not** match; `ne` / `nin` on an absent field **do**
  match (so "not superseded" includes chunks written before `status` existed).
- a filter the backend cannot render must raise `filters.FilterError`, never be ignored
  (a filter that silently does nothing returns confident, wrong results).
Index at least `metadata.source`, `category`, `bucket`, `section_role`, `scope`, `tenant`,
`status`, `stage`, `level`, `parent_id`, `year`.

## 3. Steps

```bash
cd engine/rag

# 0. See what is configured and populated now
python3 rag.py stores

# 1. Write the backend: copy the template and register it
cp store_template.py store_pgvector.py        # or store_opensearch.py — see §4
#    implement the methods, then in store_base.py REGISTRY add:
#    "pgvector": "store_pgvector:PgVectorStore",

# 2. Contract test against the live AWS instance (3 throwaway rows, ids "__check__*")
RAG_STORE=pgvector python3 rag.py store-check pgvector

# 3. Copy the index — rows AND vectors, no re-embedding (~7.4k rows)
RAG_STORE=local python3 rag.py migrate --from local --to pgvector --index ./_index_v4
#    add --replace only to wipe a destination you own (it calls delete_all)

# 4. Check counts and spot-check queries against the local baseline
python3 rag.py stores
RAG_STORE=local    python3 rag.py query "launch a challenger bank to under-30s" --index ./_index_v4 -k 5
RAG_STORE=pgvector python3 rag.py query "launch a challenger bank to under-30s" -k 5

# 5. Quality gate: the golden eval must match the local baseline
RAG_STORE=local    python3 golden.py eval --index ./_index_v4 --golden golden --sources ipa,cannes,playbook
RAG_STORE=pgvector python3 golden.py eval --index ./_index_v4 --golden golden --sources ipa,cannes,playbook

# 6. End-to-end on real briefs (no API credit needed: Claude calls on the Claude Code login)
RAG_STORE=pgvector python3 e2e_eval.py --trace mamaliga-engleza --paths mix --transport cli

# 7. Cut over: set RAG_STORE=pgvector (+ its env vars) in engine/.env. Roll back = set it
#    back to qdrant. Keep Qdrant untouched until the new store has served real briefs.
```

Other commands that already exist:

| Command | What it does |
|---|---|
| `rag.py build --corpus <corpus>/rag --index ./_index_vN [--holdout golden/holdout.json]` | rebuild the local index from the corpus (re-embeds; only when the corpus or chunker changes) |
| `rag.py retag --corpus <corpus>/rag --index ./_index_v4 [--apply]` | refresh metadata without re-embedding (dry run by default) |
| `rag.py push --index ./_index_v4` | alias for `migrate --from local --to $RAG_STORE` |
| `rag.py migrate --from qdrant --to local --to-index ./_index_from_qdrant` | pull the production collection back to a local index (a backup, or to diff) |

## 4. Traps

1. **`engine/.env` sets `RAG_STORE=qdrant`** and `rag.py` loads it. Always set `RAG_STORE`
   explicitly on the command line while migrating, as above, or you will read or write
   Qdrant by accident.
2. **2048 dimensions.** pgvector's HNSW and IVFFlat indexes support at most 2,000
   dimensions for `vector`. Options: store as `halfvec(2048)` (HNSW supports halfvec up to
   4,000 dimensions; check the pgvector version on RDS/Aurora), or switch to a narrower
   embedder and re-embed (a `rag.py build`, and every query must use the same model).
   OpenSearch k-NN handles 2048 natively.
3. **Hybrid search is not copied.** `migrate` copies dense vectors; Qdrant builds its BM25
   sparse vectors itself at upsert time (`SPARSE_AVG_LEN=191.2`, `b=0.3`). The new store
   must provide keyword search (`search_hybrid`): OpenSearch has BM25 built in; on Postgres
   use `tsvector`/`ts_rank` or an extension, then fuse with `lexical.rrf` as
   `store_qdrant.search_hybrid` does. Without it retrieval silently degrades to dense-only.
   Compare step 5 with and without it.
4. **The embed-model guard.** `migrate` refuses a local index built with a different embed
   model than `RAG_EMBED_MODEL` (queries would not match the vectors). Do not `--force`
   past it unless you know why.
5. **Tenancy.** Rows carry `scope` / `tenant`; `brief_context` enforces egress (a hit outside
   the allowed scopes is dropped and logged). If the AWS design is one database per agency
   with row-level security, see the open AWS questions A1–A5 in `judgement-architecture.clan`
   (per-agency RDS vs database-per-tenant, `SET LOCAL` through RDS Proxy, `FORCE` RLS, region).

## 5. Requirements carried from Qdrant (lessons, 2026-09-24)

- **An overall deadline per request, including retries.** `store_qdrant._req` retries
  60 s × 4 with backoff and no overall deadline; in a smoke run single requests hung
  61–307 s and one brief waited 410 s on retrieval (normally ~3 s). Target: ~10–15 s per
  request including retries, and a per-brief retrieval deadline.
- **Degrade loudly.** On a deadline, continue with what returned and record it in the
  brief; never hang, never fail silently. (The digest fallback is already loud:
  `loops3_7.fallback`.)
- **Batch.** A brief makes ~40 search requests (five field queries × buckets, widening,
  parent lookups). Use the store's multi-search / batched query so one brief is a handful of
  round trips.
- **Burst tolerance.** The hosted Qdrant tier shed connections under burst load, so bulk
  evals ran against the local store. Size the AWS store so `golden.py eval` and
  `e2e_eval.py` can run against it directly.
- **Same query embedder.** `migrate` keeps the stored vectors, which came from
  `nvidia/nemotron-3-embed-1b` (2048-d, NVIDIA-hosted today). Queries must be embedded by the
  same model wherever the AWS store runs, or retrieval is silently wrong; changing the model
  means a `rag.py build` (re-embed) and a new golden baseline. Decide where the embedder runs
  (NVIDIA hosted, the DGX Spark, or AWS) as part of the move.
- **Fewer, smaller payloads.** A brief makes ~40 round trips that can batch to ~2, and the
  candidate pool and payload are ~3x what the brief uses; IPA snippets start with metadata, so
  store the body text separately from the header.
- **Parity gate.** Local vs hosted Qdrant returned the same cites for 83-100% of identical
  queries; add a cite-parity check (same queries, compare cites) to step 5, not only recall.
- **Size for the app path**, which retrieves twice per draft today (the brief and the research
  panel).
- **Record the store in every run** (`retrieve.index_label` → `loops3_7.index`), so a run
  on the new store is never mistaken for one on the old.
