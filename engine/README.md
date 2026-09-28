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
BRIEF_CLAUDE_TRANSPORT=cli napkin-brief <brief.docx>         # Claude calls on your Claude Code login, not the API key
```

Outputs land in `outputs/<name>/`: `client_brief.md` (clean deliverable),
`review.md` (working doc with citations + scorecard), `brief_object.json` (structured,
incl. `meta.llm_stats` — per-run LLM call/token ledger).

`--format md,docx,pdf` emits rich formats via pandoc (PDF needs xelatex, or render the
markdown with headless Chrome: `--headless=new --print-to-pdf`).

## Code map

The pipeline is `parse_brief.py`; since 2026-09-27 (ADR 0012) the parts that are not
pipeline stages live in their own modules, and `parse_brief` re-exports them so existing
callers (`parse_brief._json_call`, `parse_brief.docx_text`, ...) keep working.

| File | What it owns |
|---|---|
| `parse_brief.py` | the pipeline: capture (Loop 1), golden extraction, the gates (`_judge_and_gate`), the zone-3 fill, Loops 3-7 retrieval and synthesis, provenance, `run()` and the CLI |
| `brief_llm.py` | talking to models: providers, the Claude-only chain, model routes by job, the API and Claude Code transports, `_json_call`, the per-run call ledger, `NoClaudeAvailable` |
| `brief_ingest.py` | reading a brief: `.txt` / `.md` / `.docx` (document order, hyperlink addresses kept as `text <url>`, since research documents cite by link) / `.pdf` / `.eml` / images (vision model), and sentence segmentation |
| `brief_render.py` | the brief for people: client brief, review file, Loops 3-7 evidence, provenance, `.docx` / `.pdf` output, marker scrubbing |
| `golden_critic.py` | the independent critic: schema checks plus one Sonnet-judged call, health and quality scores |
| `toon_lite.py` | the TOON reader/writer the capture uses |
| `engine_env.py` | the one `engine/.env` loader |
| `packs.py`, `napkin_packs.py` | knowledge packs: discovery, `packs.lock`, sync into the index |
| `agent-server/` | the app's HTTP backend: draft and field regeneration over the pipeline |
| `rag/` | the RAG module: stores, retrieval (`brief_context.py`), validators (`judge*.py`, jev), jev checks (`jev_checks.py`), evaluation tools (`checkpoint_run.py`, `e2e_eval.py`, `eval_history.py`, `score_as_sent.py`, `labelset.py`); see `rag/README.md` |

## Keys & config (.env)

| Var | Needed for |
|---|---|
| `NVIDIA_API_KEY` | NIM embeddings (RAG) + chat backstop |
| `GROQ_API_KEY` / `CEREBRAS_API_KEY` | fast free-tier chat links (recommended) |
| `QDRANT_CLUSTER_ENDPOINT` + `QDRANT_API_KEY` (+`QDRANT_COLLECTION`) | remote RAG (`RAG_STORE=qdrant`) |
| `BRIEF_MODEL_CHAIN` / `BRIEF_MODEL` | override the model fallback chain |
| `BRIEF_ROUTES` | `1` (default): each call runs on its job's model (ADR 0011): extraction and insight/SMP drafts on Opus 4.6, RTB and desired response on Opus 5.5, insight/SMP judges and the territory map on Opus 5.5 (low effort), other judges, scorecard and how-to-win on Sonnet 5 (low effort), loop syntheses on Sonnet 5; a judge never runs on its writer's model. `0`, or setting `BRIEF_MODEL` / `BRIEF_MODEL_CHAIN`, restores one model for every call. The routes a run used are in `meta.model_routes` |
| `BRIEF_ROUTE_<JOB>` | override one job's models, `model[,fallback]`; jobs: `EXTRACT`, `HERO`, `GROUNDED_WRITER`, `HERO_JUDGE`, `JUDGE`, `MECHANICAL`, `SYNTH` |
| `BRIEF_JEV_CHECKS` | `1` (default; needs `TYPESAFE_API_KEY`): jev checks RTB and desired-response figures (a figure not in the brief fails the draft), the scorecard's verdicts (disputes flagged), picks the retrieval category when no upstream category is given, and marks synthesis sentences their cited sources do not support. `0` turns all four off. See `rag/jev_checks.py`, ADR 0011 |
| `BRIEF_LOOPS37` / `BRIEF_RERANK` / `BRIEF_HERO_CANDIDATES` | stage toggles |
| `BRIEF_CAPTURE` | `toon` (default): Loop 1 capture in TOON citing sentence numbers, how_to_win in its own call; `json`: the one-call JSON capture |
| `BRIEF_PARALLEL` | `1` (default): stages run as a dependency graph; `0`: one step at a time |
| `BRIEF_ALLOW_NONCLAUDE` | unset (default): with Claude as the lead link the chain is Claude-only, and a run with no route to Claude stops with a clear error; `1`: non-Claude links stay as fallbacks (not for production; a brief they answer is named in `meta.fallback_links`). See [ADR 0006](rag/docs/adr/0006-cannot-fail-silently.md) |
| `BRIEF_CLI_FALLBACK_TTL` | seconds an `auto` switch to the Claude Code login lasts before the API is tried again (600) |
| `ANTHROPIC_API_KEY` | Claude (the default lead link; also the independent critic) |
| `BRIEF_CLAUDE_TRANSPORT` | how Claude links are sent: `api` (default, the key above), `cli` (your Claude Code login via `claude -p`, no API credit used), `auto` (the key, switching to the Claude Code login if it has no credit or is rejected). Also `serve.py --claude-code / --api / --auto` and `e2e_eval.py --transport`. See [ADR 0005](rag/docs/adr/0005-claude-transport.md) |
| `BRIEF_PROVIDER` | pin one provider as the lead link (the rest stay as fallback) |
| `BRIEF_SMP_CANDIDATES` / `BRIEF_GOLDEN` | SMP draft count (default 6) / run the golden-brief pass |
| `BRIEF_MAX_TOKENS` | override every call's output ceiling |
| `BRIEF_CLIP_CHARS` / `BRIEF_CLIP_EXTRACT_CHARS` | brief clip for judge calls (6500) / for the capture (12000) |
| `BRIEF_BASE_URL` / `BRIEF_LINK_COOLDOWN` | custom OpenAI-compatible endpoint / seconds a rate-limited link rests |
| `BRIEF_VISION_MODEL` / `BRIEF_VISION_BASE` / `BRIEF_VISION_API_KEY` | image and scanned-PDF transcription: model (`nvidia/llama-3.1-nemotron-nano-vl-8b-v1`), OpenAI-compatible endpoint (NVIDIA NIM; a local Ollama works keyless), key (falls back to `NVIDIA_API_KEY`) |
| `GEMINI_API_KEY` / `OPENAI_API_KEY` | optional further chat links, auto-detected |
| `BRIEF_RETRIEVE_FROM` | `golden` (default): retrieval starts from the golden extraction, 16–26 s earlier, with the capture as fallback; `capture`: retrieval waits for the Loop 1 capture. A/B on 3 briefs: health 216 = 216, faster on every brief |
| `BRIEF_THINKING_HEADROOM` / `BRIEF_LINK_TIMEOUT` | extra output tokens for Claude models that think by default (2500) / per-request timeout for OpenAI-compatible links (90 s) |
| `CRITIC_MODEL` / `CRITIC_SAMPLES` | the independent critic: model (default `claude-sonnet-5`) and samples per brief (default `1`); with more than one, each check keeps the majority verdict and an even split is REVIEW (`golden_critic.run_critic_sampled`). On mamaliga, three Fable 5.1 samples spread 2 health points against Sonnet's 5. `checkpoint_run.py --critic claude-fable-5-1x3` sets both for a run |
| `BRIEF_CLI_TIMEOUT` / `BRIEF_CLI_EFFORT` | Claude Code login transport: seconds per `claude -p` call (240) / force one `--effort` for every thinking-model call (default: the job's effort, else the model's API default) |
| `BRIEF_CORPUS` / `BRIEF_PACKS_LOCK` | pack sync: corpus root (default `engine/reference/rag` or `../reference/rag`) / path of `packs.lock` (default `engine/packs.lock`) |
| `BRIEF_RESEARCH` / `RESEARCH_WEB` | agent server: `0` turns the research dossier off (default on) / `claude` adds the web track through `claude -p` with WebSearch (default `off`) |
| `NAPKIN_AGENT_PORT` | agent server port (8787, the same slot as the mock agent: run one) |
| `LABELSET_BRIEF_MODEL` / `LABELSET_JUDGE_MODEL` | label tool (`rag/labelset.py`): brief-pair extraction model (`claude-haiku-4-5-20251001`) / pre-label judge (`claude-sonnet-5`) |
| `RAG_EMBED_LOCAL_MODEL` / `RAG_EMBED_LOCAL_DEVICE` | the local copy of the query embedder used when the hosted one fails (`nvidia/Nemotron-3-Embed-1B-BF16`) / `mps` or `cpu` (default: mps when available) |
| `RAG_SPARSE_AVG_LEN` | Qdrant sparse vectors: the corpus's average document length for BM25 weighting (191.2) |
| `TEMPLATE_STORE_URL` | connection string for `store_template.py`, the starting point for a new store backend |

Every model call is **Claude by default**: each call names its job and the job picks
the model (`BRIEF_ROUTES`, ADR 0011), with another Claude model as the fallback. With no
route to Claude, or when not one Claude call in a run answers, `parse_brief.run()` stops
with `NoClaudeAvailable` instead of writing a brief with another model or with the
heuristics (ADR 0006). `BRIEF_ALLOW_NONCLAUDE=1` restores the non-Claude chain (Cerebras,
Groq, NVIDIA NIM) for experiments. `engine/.env` is loaded by `engine_env.py`, the one
loader, which never overrides a variable already set. A test keeps this table complete:
every variable the code reads must appear here, in `rag/README.md` or in `.env.example`
(`rag/test_env_documented.py`).

### Pipeline stages (`parse_brief.run`)

| Stage | Calls | Starts after | Code |
|---|---|---|---|
| Capture (TOON, `src` sentence numbers) | 1 | — | `capture_toon` (+ `toon_lite.decode`) |
| How-to-win | 1 | — (alongside capture) | `how_to_win_toon` |
| Golden extraction | 1 | — (alongside capture) | `extract_golden_brief` |
| Scorecard | 1 | capture | `score_betterbriefs` |
| Retrieval (RAG, no LLM) | 0 | capture | `loops_3_7(..., synthesize=False)` |
| Loop synthesis | 5 | retrieval (alongside hero fields) | `_synthesize_loops37` |
| Hero fields | ~13 | retrieval + golden | `fill_derivable_fields` → `_judge_and_gate` |

Measured on 3 real briefs, 2026-09-23: 110–157 s and $0.35–0.44 per brief (was 294–351 s,
$0.46–0.64). Why and how: `rag/docs/adr/0004-brief-pipeline-speedups.md`.

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
