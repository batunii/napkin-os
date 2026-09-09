# Planner / Effectiveness RAG — Loops 3–7 retrieval layer

The "green lane" of the architecture. Build a knowledge base from the planner
playbooks + IPA/Effie cases once, retrieve from it at runtime. **Kept separate
from `parse_brief.py` on purpose — Loop 1 capture stays RAG-free.** This only
serves Loops 3–7 (research / insight / single-minded proposition / substantiation).

## Build

```bash
# 1) drop the corpus (.md playbooks etc.) into ../reference/rag/
# 2) build the index with real NIM embeddings (reads ../.env for NVIDIA_API_KEY)
./build_rag.sh
#    dry-run with no key / no spend:
RAG_EMBED=offline python3 rag.py build --corpus ../reference/rag --index ./index
```

## Query

```bash
python3 rag.py query --index ./index "challenger brand, nervous CMO" -k 5
python3 rag.py query --index ./index "focus the message" --where type=proposition
```

## How it works (matches `00-rag-ingestion-guide.md`)

- **Chunk** — per-source strategies in `chunking.py`: IPA/Effie cases = one whole-case
  *parent* + one *child* per section; Cannes = whole case; playbooks = one chunk per section
  (split at paragraphs above 400 words); templates = ~300-word windows with 15% overlap;
  D&AD = skipped (title-only entries). Every chunk is embedded behind a **context header**
  (`title (year) · tier · sector · effectiveness type`) and carries `metadata.level`,
  `doc_id`, `parent_id`, `strategy`. Retrieval Queries attach to every chunk of a file.
  Loops 4/6 retrieve `level=parent` so k=2 means two cases.
- **Metadata** — YAML frontmatter parsed *generically* (any keys) and attached to
  every chunk, so it fits your real frontmatter without hard-coding fields.
  `RETRIEVAL_QUERIES` (frontmatter key or section) is folded in for recall.
- **Embed** — NVIDIA NIM (`nvidia/nemotron-3-embed-1b`, 2048-d; replaced the retired `nv-embedqa-e5-v5`) via the
  OpenAI-compatible `/embeddings` endpoint, your Inception key. Deterministic
  **offline** fallback (`RAG_EMBED=offline`) so it runs/tests with no key or spend.
- **Store** — local JSON index (`index/chunks.jsonl` + `manifest.json`). Swappable
  to Qdrant later via one adapter; nothing else changes.

Override the model/endpoint with `RAG_EMBED_MODEL` / `RAG_EMBED_BASE`.

## Corpora & the `source` schema

Every chunk carries a top-level **`source`** frontmatter key so retrieval can target one
corpus (`--where source=cannes`) or blend across all of them (no filter). `category` stays
for sub-type. All corpora live under `../reference/rag/<source>/` and are built into one index.

| `source` | What | Ingest script | Input → `reference/_raw/` |
|---|---|---|---|
| `playbook` | 130 planner/strategy frameworks | (corpus is source-of-truth) | — |
| `template` | briefing templates | — | — |
| `ipa` | IPA effectiveness cases | `ingest_ipa.py` | `archive/ipa-award-winners-dataset/intelligence_layer.json` |
| `cannes` | Cannes Lions winners | `ingest_cannes.py` | `cannes.json` (scraped from lovethework) |
| `effie` | Effie effectiveness cases (incl. `effie_cautionary`) | `ingest_effie.py` | `effie.csv` / `effie.json` |
| `dandad` | D&AD Pencil winners | `ingest_dandad.py` | `dandad.json` (scraped from dandad.org) |

Add/refresh a corpus: run its `ingest_*.py` (writes markdown under `reference/rag/<source>/`),
then `./build_rag.sh` to re-embed the whole index. New `source` frontmatter needs no code change
(frontmatter is parsed generically). `scripts/backfill_source.py` retro-tags pre-existing files.

Loops 4 (insight) & 6 (substantiation) in `parse_brief.py` pull precedent **cases** from each
award corpus (`ipa·cannes·effie·dandad`) via the `source` filter — corpora with no data are no-ops.

## Store layer — switch backends whenever needed

The brain is **one logical vector store**; where it lives is a config choice. `rag.py`,
`retrieve.py` and `parse_brief.py` only talk to the `VectorStore` contract in
`store_base.py`. Backends are registered by name and selected with `RAG_STORE`:

| `RAG_STORE` | File | Config | Notes |
|---|---|---|---|
| `local` (default) | `store_local.py` | `RAG_INDEX` (dir) | canonical artefact; `build` always writes it |
| `qdrant` | `store_qdrant.py` | `QDRANT_URL`/`QDRANT_CLUSTER_ENDPOINT`, `QDRANT_API_KEY`, `QDRANT_COLLECTION` | Qdrant Cloud or self-hosted, REST on 443 |
| *(add yours)* | copy `store_template.py` | — | pgvector on AWS, OpenSearch, Milvus on Nebius… |

```bash
python3 rag.py stores                       # every backend: configured? populated? rows
python3 rag.py store-check qdrant           # contract test against a backend (offline vectors)

# build once (writes ./index; also mirrors to RAG_STORE if it isn't local)
./build_rag.sh

# move the brain between stores — copies rows WITH vectors, never re-embeds
# (refuses if the source was embedded with a different model than RAG_EMBED_MODEL; --force overrides)
python3 rag.py migrate --from local  --to qdrant
python3 rag.py migrate --from qdrant --to local --to-index ./index_backup
python3 rag.py migrate --from qdrant --to pgvector --replace      # once pgvector is registered

# then run everything off the chosen store
RAG_STORE=qdrant python3 rag.py query "challenger brand" --where source=cannes
RAG_STORE=qdrant BRIEF_LOOPS37=1 python3 parse_brief.py <brief> --out outputs/run
```

`rag.py` loads `briefing/.env` itself, so none of this needs `source .env` first.

Rules the contract guarantees:
- **Never crashes the brief.** A misconfigured or unreachable store makes `index_available()`
  return `False`; the run records `meta.rag.store` and `meta.rag.index` so a silent fallback is visible.
- **Idempotent.** Rows are keyed by chunk id (Qdrant: a deterministic UUID), so re-push/migrate upserts.
- **Same embedding everywhere.** `manifest.json` records `embed_model`/`embed_mode`/`dim`; all stores
  must hold vectors from the same model — changing the model means rebuild, then migrate.
- **Filters** are `{metadata_key: value}`; Qdrant needs payload indexes (created by `ensure`) on
  `metadata.source / category / award_tier / year`.

Adding a backend: copy `store_template.py` → `store_<name>.py`, implement six methods, add one line to
`REGISTRY` in `store_base.py`, run `rag.py store-check <name>`.

## Config knobs

| Env | Default | Purpose |
|---|---|---|
| `NVIDIA_API_KEY` | — | NIM embeddings (from `briefing/.env`) |
| `RAG_EMBED` | (unset) | `offline` = deterministic hash embedder, no network |
| `RAG_EMBED_MODEL` | `nvidia/nemotron-3-embed-1b` | embedding model id (2048-d) |
| `RAG_EMBED_BASE` | `https://integrate.api.nvidia.com/v1` | endpoint base |
| `RAG_STORE` | `local` | backend name (`local`, `qdrant`, …) |
| `RAG_INDEX` | `rag/index` | local index dir |

## Smoke test

`_testcorpus/` holds two tiny sample playbooks and `_testindex/` a prebuilt
offline index — a working example of the expected file shape. Safe to ignore or
overwrite once your real corpus is indexed.

## Next

Once the index is built, wire retrieval into the runtime tool at Loops 3–7
(the dashed arrow in the architecture diagram): classify intent → retrieve
top-k playbooks + effectiveness evidence → ground strategy/insight/proof.
Capture (Loop 1) never calls this.
