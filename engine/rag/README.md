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

- **Chunk** — one chunk per H2 (`## `) section, ~10% word overlap between chunks.
- **Metadata** — YAML frontmatter parsed *generically* (any keys) and attached to
  every chunk, so it fits your real frontmatter without hard-coding fields.
  `RETRIEVAL_QUERIES` (frontmatter key or section) is folded in for recall.
- **Embed** — NVIDIA NIM (`nvidia/nv-embedqa-e5-v5`, 1024-d) via the
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
| `ipa` | IPA effectiveness cases | `ingest_ipa.py` | `intelligence_layer.json` (private dataset, not in this export) |
| `cannes` | Cannes Lions winners | `ingest_cannes.py` | `cannes.json` (scraped from lovethework) |
| `effie` | Effie effectiveness cases (incl. `effie_cautionary`) | `ingest_effie.py` | `effie.csv` / `effie.json` |
| `dandad` | D&AD Pencil winners | `ingest_dandad.py` | `dandad.json` (scraped from dandad.org) |

Add/refresh a corpus: run its `ingest_*.py` (writes markdown under `reference/rag/<source>/`),
then `./build_rag.sh` to re-embed the whole index. New `source` frontmatter needs no code change
(frontmatter is parsed generically). `scripts/backfill_source.py` retro-tags pre-existing files.

Loops 4 (insight) & 6 (substantiation) in `parse_brief.py` pull precedent **cases** from each
award corpus (`ipa·cannes·effie·dandad`) via the `source` filter — corpora with no data are no-ops.

## Remote store (share the code without the data)

The index (and the corpus behind it) shouldn't travel with the code. Set `RAG_STORE=qdrant`
and the same build/query paths talk to a **Qdrant** vector DB instead of the local JSON file —
so a shared repo carries only the adapter + config, never the vectors or the scraped content.

The store is pluggable; `local` (default) keeps everything as-is. The adapter (`store_qdrant.py`)
is dependency-free REST, so the **same code hits Qdrant Cloud and a self-hosted Qdrant** — only
the URL changes.

```bash
# .env  (gitignored — never commit these)
QDRANT_CLUSTER_ENDPOINT=https://<cluster-id>.<region>.aws.cloud.qdrant.io   # or QDRANT_URL
QDRANT_API_KEY=<key>                 # omit for local/self-hosted Qdrant
QDRANT_COLLECTION=napkin_rag         # optional (this is the default)

# one-time migration: build locally once, then upload (no re-embedding)
./build_rag.sh                                   # or reuse an existing ./index
RAG_STORE=qdrant python3 rag.py push --index ./index

# thereafter, query / run briefs entirely off the remote DB — nothing local needed
RAG_STORE=qdrant python3 rag.py query "challenger brand" --where source=cannes
RAG_STORE=qdrant BRIEF_LOOPS37=1 python3 parse_brief.py <brief> --out outputs/run
```

Notes:
- REST is used over **port 443** (Qdrant Cloud serves it there; 6333 is often firewalled). For
  self-hosted, include the port in the URL (`http://host:6333`).
- `push` auto-creates the collection (Cosine, 1024-d) and the **payload indexes** Qdrant needs to
  filter on `metadata.source` / `category` / `award_tier` / `year`. Re-running `push` is idempotent
  (points keyed by a deterministic UUID).
- A collaborator runs it by putting the same three env vars in their `.env` — no data files, no
  rebuild. Embeddings at query time still need `NVIDIA_API_KEY`.

## Config knobs

| Env | Default | Purpose |
|---|---|---|
| `NVIDIA_API_KEY` | — | NIM embeddings (from `briefing/.env`) |
| `RAG_EMBED` | (unset) | `offline` = deterministic hash embedder, no network |
| `RAG_EMBED_MODEL` | `nvidia/nv-embedqa-e5-v5` | embedding model id |
| `RAG_EMBED_BASE` | `https://integrate.api.nvidia.com/v1` | endpoint base |

## Smoke test

`_testcorpus/` holds two tiny sample playbooks and `_testindex/` a prebuilt
offline index — a working example of the expected file shape. Safe to ignore or
overwrite once your real corpus is indexed.

## Next

Once the index is built, wire retrieval into the runtime tool at Loops 3–7
(the dashed arrow in the architecture diagram): classify intent → retrieve
top-k playbooks + effectiveness evidence → ground strategy/insight/proof.
Capture (Loop 1) never calls this.
