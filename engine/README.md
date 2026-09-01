# Napkin Briefing Tool

Takes a client brief in any format (Word / PDF / text / email / scraps) and produces a
clean, structured, **RAG-grounded** agency brief — with provenance on every value and a
no-loss guarantee on capture.

- **Loop 1 — Parse/Ingest** *(no RAG — hard invariant)*: faithful structured capture +
  win-rules, with a **no-loss ledger** proving nothing was dropped.
- **Loop 2 — First-round brief**: problem, objective, audience, scope + the **open
  questions** to ask before research.
- **BetterBriefs scorecard**: 7-dimension quality grade of the client brief
  (LLM judge; heuristic fallback).
- **Loops 3–7 — RAG-grounded strategy** *(opt-in)*: research → insight → single-minded
  proposition → substantiation → QA, grounded in **four award corpora**
  (IPA · Cannes Lions · D&AD · Effie) + 130 planning playbooks, every claim cited.
- **Golden Brief fill**: generates insight/SMP/RTBs/desired-response via a judged
  candidate tournament with rubric + competitor-territory gates. Never overwrites a
  client-stated fact; failures become open questions, not inventions.

## Install

```bash
git clone <repo> && cd briefing
python3 -m venv .venv && source .venv/bin/activate   # or your env of choice (Python ≥3.11)
pip install -e .            # core (.pdf/.docx ingestion)
pip install -e ".[all]"     # + PyYAML, python-dotenv, PyMuPDF (vision), anthropic
cp .env.example .env        # then add your keys (see table below)
```

> Editable install (`-e`) is the supported mode: schemas, `rag/` and `reference/` are
> resolved relative to the repo.

## Run

```bash
napkin-brief samples/messy_brief_sample.txt                  # Loops 1–2 (works with zero keys)
BRIEF_LOOPS37=1 napkin-brief <brief.pdf> --out outputs/x     # + RAG strategy (local index)
RAG_STORE=qdrant BRIEF_LOOPS37=1 napkin-brief <brief.docx>   # + remote Qdrant RAG (shared DB)
```

Outputs land in `outputs/<name>/`: `client_brief.md` (clean deliverable),
`review.md` (working doc with citations + scorecard), `brief_object.json` (structured,
incl. `meta.llm_stats` — per-run LLM call/token ledger).

`--format md,docx,pdf` emits rich formats via pandoc (PDF needs xelatex, or render the
markdown with headless Chrome: `--headless=new --print-to-pdf`).

## Keys & config (.env)

| Var | Needed for |
|---|---|
| `NVIDIA_API_KEY` | NIM embeddings (RAG) + chat backstop |
| `GROQ_API_KEY` / `CEREBRAS_API_KEY` | fast free-tier chat links (recommended) |
| `QDRANT_CLUSTER_ENDPOINT` + `QDRANT_API_KEY` (+`QDRANT_COLLECTION`) | remote RAG (`RAG_STORE=qdrant`) |
| `BRIEF_MODEL_CHAIN` / `BRIEF_MODEL` | override the model fallback chain |
| `BRIEF_LOOPS37` / `BRIEF_RERANK` / `BRIEF_HERO_CANDIDATES` | stage toggles |

The tool is **model-agnostic**: every LLM step walks a best→reliable provider chain
(Cerebras → Groq → NVIDIA NIM by default) and degrades to heuristic mode with no keys
at all. `.env` is loaded automatically (dependency-free fallback included).

## RAG corpora

Vectors live in a **remote Qdrant** collection so the repo ships no data — a
collaborator needs only the three `QDRANT_*` vars. Corpus sources, the unified
`source:` schema, ingest scripts (`ingest_ipa|cannes|effie|dandad.py`) and rebuild/push
instructions: see **`rag/README.md`**.

## Quality gates

`check_invariants.py <brief_object.json>` is the regression ruler (fill-vs-flag, SMP
word limit, observable desired-response, no duplicate questions). The one hard rule:
**Loop 1 never touches RAG** — capture stays a faithful record.

## Data hygiene

This directory is a **scrubbed fresh-file export** of the private briefing engine:
no client briefs, no scraped corpus, no local RAG index ship here. Corpus vectors
live in the remote Qdrant collection (see `rag/README.md`) — from this export, RAG
is **Qdrant-only**; `rag/build_rag.sh` needs a local corpus that is intentionally
not included. Keep it that way: client data and raw corpus exports never belong in
this repository.

## Napkin OS integration

This engine is the real backend for the **Brief Maker** app: `agent-server/`
implements the `{payload, clan} → brief fields` contract the app's Generate flow
speaks (see `agent-server/README.md`). Vision transcription of image/scanned-PDF
briefs and `.eml` ingest are CLI-only paths — app attachments arrive already
host-extracted.
