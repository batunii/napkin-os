# Demoing the Brief Maker with knowledge

## The whole thing, three commands

```bash
git clone -b ragAdded https://github.com/batunii/napkin-os.git && cd napkin-os
python3 serve.py                          # picks the backend FOR you (see below)
cd app && npm install && npx tauri dev    # the app (needs Node + Rust)
```

`serve.py` decides — you never choose: a chat key in your env → the full engine
pipeline; no key but Claude Code installed → briefs via your Claude login
(mock-agent); neither → it tells you how to get one of the two.

**The knowledge needs no setup.** Paraphrased pack digests are committed at
`engine/packs_dist/` and both backends load them automatically — draft a brief
and the context panel cites them. No Qdrant, no embedding key, no corpus access:
those are maintainer-side upgrades, not user requirements.

(Engine backend additionally needs a one-time `cd engine && pip install -e ".[yaml,dotenv]"`.)

The knowledge ships in tiers. Pick the row that matches what you have:

| You have | What runs | Grounding |
|---|---|---|
| nothing | keyless heuristic draft | none (honestly labelled) |
| any ONE chat key (Groq / Cerebras / NVIDIA) — or just Claude Code for the mock-agent | full LLM pipeline | **digest mode** — paraphrased pack digests, committed in `packs_dist/` |
| Claude Code + `NAPKIN_RETRIEVE=agentic` | mock-agent, two calls | **agentic mode** — digests *plus* per-brief retrieval over owned methodology docs, cited by file and line. No embeddings, no store, no key. |
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
