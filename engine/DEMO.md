# Demoing the Brief Maker with knowledge — who needs which credentials

The knowledge ships in tiers. Pick the row that matches what you have:

| You have | What runs | Grounding |
|---|---|---|
| nothing | keyless heuristic draft | none (honestly labelled) |
| any ONE chat key (Groq / Cerebras / NVIDIA) — or just Claude Code for the mock-agent | full LLM pipeline | **digest mode** — paraphrased pack digests, committed in `packs_dist/` |
| + Qdrant creds + NVIDIA key (ask the maintainer — never in the repo) | full pipeline | **vector mode** — per-query retrieval with citations |

Why the tiers exist: the raw award corpora (IPA / Cannes / D&AD) are licensed and
live only in a private corpus repo. What ships publicly is (a) `rag/packs.lock`,
the generated table of contents, and (b) `packs_dist/<pack>/digest.md` — original,
paraphrased pattern notes distilled from each pack. Digests remove the *retrieval*
dependency (vector store + embedding key), not the *generation* dependency.

## Digest-mode demo (recommended first run — no store, no Qdrant)

```bash
cd engine
pip install -e ".[yaml,dotenv]"
export GROQ_API_KEY=...            # or CEREBRAS_API_KEY / NVIDIA_API_KEY — any one
export BRIEF_LOOPS37=1 BRIEF_GOLDEN=1
napkin-brief samples/messy_brief_sample.txt --loops37 --golden --out outputs/demo
```

Look for: `Loops 3–7` reporting `index: digests:packs_dist`, citations like
`ipa digest`, and Golden Brief filling ~9/11 fields (ungrounded runs manage ~7/11).
Then the app: `python3 agent-server/server.py` (:8787) and
`cd ../app && npm install && npx tauri dev` — draft a brief, check the context panel.

## Vector-mode demo (full retrieval)

```bash
# .env: QDRANT_CLUSTER_ENDPOINT / QDRANT_API_KEY / NVIDIA_API_KEY (private hand-off)
export RAG_STORE=qdrant QDRANT_COLLECTION=napkin_rag_v2 BRIEF_LOOPS37=1 BRIEF_GOLDEN=1
napkin-brief samples/messy_brief_sample.txt --loops37 --golden --out outputs/demo-vector
```

Citations become per-query (`<case-file> › <section>`), pulled live per pack
(`napkin-packs status` shows what the store holds).

## Refreshing knowledge (maintainer, corpus repo only)

```bash
export BRIEF_CORPUS=<corpus root>            # pack dirs; a folder IS a pack
napkin-packs sync                            # incremental: only changed docs embed
python3 scripts/distil_pack.py               # regenerate digests (skips unchanged packs)
```
