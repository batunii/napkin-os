# Napkin Briefing Tool

> **New here, or an agent picking this up?** Start with [`RUNBOOK.md`](RUNBOOK.md): setup, making and
> grading a brief, long inputs, research facts, checkpoints, costs and the known traps.

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
napkin-brief <brief.docx>                                    # the full brief: one file in, nothing else needed
napkin-brief <brief.docx> --research Brand.clan              # + the research CLAN's facts, decisions on its findings, brand (ADR 0017)
napkin-brief samples/messy_brief_sample.txt --quick          # the light brief only, Loops 1–2 (works with zero keys)
RAG_STORE=local napkin-brief <brief.docx>                    # the local index (rag/_index_v4) instead of the shared store
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
| `capture_fallback.py` | the Loop 1 capture when the model capture fails: the jev sentence sorter first (45% of the model's fields on 7 saved briefs), then a deterministic reader (sections from headings + per-field sentence cues, regex only, 37%); also the keyless demo's reader. The brief says which one ran |
| `research_facts.py` | verified facts from the brand and category research (knowledge-layer fact rows): the current ones become cited `[F:id vN]` lines for every hero writer and count as allowed for the figure check; recorded in `meta.research_facts`; citations checked in the gate and moved into each field's `fact_refs`; never in the Loop 1 capture (ADR 0014) |
| `research_decisions.py` | what people decided on the research (rejected or verified findings, contests, edits, verdicts, the client's review): the current rows become `[D:id]` lines in their own block after the facts for every hero writer; never allowed figures, never in the Loop 1 capture, the golden extraction or the scorecard; an unknown `[D:id]` fails the draft; recorded in `meta.research_decisions` (ADR 0015, for Sai to review) |
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
| `BRIEF_ROUTES` | `1` (default): each call runs on its job's model (ADR 0011): extraction on Opus 5.5, insight/SMP drafts on Opus 4.6, RTB and desired response on Opus 5.5, insight/SMP judges and the territory map on Opus 5.5 (low effort), other judges, scorecard and how-to-win on Sonnet 5.5 (low effort), loop syntheses on Sonnet 5.5; a judge never runs on its writer's model. `0`, or setting `BRIEF_MODEL` / `BRIEF_MODEL_CHAIN`, restores one model for every call. The routes a run used are in `meta.model_routes` |
| `BRIEF_ROUTE_<JOB>` | override one job's models, `model[,fallback]`; jobs: `EXTRACT`, `HERO`, `GROUNDED_WRITER`, `HERO_JUDGE`, `JUDGE`, `MECHANICAL`, `SYNTH` |
| `BRIEF_EFFORT_<JOB>` | override one job's effort on thinking models (`low`, `medium`, `high`, `xhigh`, `max`); same jobs; unset = the table in `brief_llm.ROUTE_EFFORT`, else the model's default |
| `BRIEF_JEV_CHECKS` | `1` (default; needs `TYPESAFE_API_KEY`): jev checks RTB and desired-response figures (a figure not in the brief fails the draft), the scorecard's verdicts (disputes flagged), picks the retrieval category when no upstream category is given, and marks synthesis sentences their cited sources do not support. `0` turns all four off. See `rag/jev_checks.py`, ADR 0011 |
| `BRIEF_LOOPS37` / `BRIEF_RERANK` / `BRIEF_HERO_CANDIDATES` | stage toggles |
| `BRIEF_PARALLEL` | `1` (default): stages run as a dependency graph; `0`: one step at a time |
| `BRIEF_SHARPEN` | `1` (on again since 2026-10-02, Sai, with the hero writer on Opus 5.5 at high effort; it was off from 2026-09-29): after the judge picks the best insight and SMP, one sharpen call each plus a re-judge. `0` turns it off. Evidence: a blind head-to-head against the research tool's drafter over 7 inputs went from 2 engine wins and 4 losses to 3 wins, 3 ties and 1 loss, for about +$0.05 and +10-30 s a brief |
| `BRIEF_FILL_ALL` | `1` (default since 2026-10-03, Sai, REQ-01: every field filled): a generated field left empty gets one repair (its writer told why the last drafts failed), then the best rejected draft ships flagged with the checks it failed (`fill_repair`, `review`); every other field the client never gave gets a labelled proposal in one call on the grounded writer's route (`proposed`, `basis`, `confirm`), shown as "Proposed (not given by the client), based on …. To confirm: …" with an open question. A client's own line is never touched. `0` keeps the old rule (empty plus an open question). ADR 0018 |
| `BRIEF_REQUIRE_STORE` | `0` (default): a brief made without precedent retrieval (store missing or unreachable, e.g. a wrong `RAG_INDEX`) says so in `meta.degraded` and the CLI prints DEGRADED BRIEF; `1` stops the run instead (use it for tests and checkpoints, EC-048) |
| `BRIEF_CHUNK_ABOVE` / `BRIEF_CHUNK_CHARS` | inputs longer than `BRIEF_CHUNK_ABOVE` characters (600000; a 228k tender read whole was faster, cheaper, 100% covered and won the blind head-to-head, 2026-10-03) are captured in chunks of up to `BRIEF_CHUNK_CHARS` (35000), cut at a document or section boundary where one is near, 4 in parallel, with the global sentence numbers, then merged (lists de-duplicated; a second, different value for a one-item field kept in `alternatives`, with a "which holds?" question only for short factual values that differ, at most 5); every sentence is in exactly one chunk, whatever `BRIEF_CLIP_EXTRACT_CHARS` says. `meta.capture_chunks` records the ranges and any chunk that failed. Shorter inputs are captured exactly as before (P1 step A, 2026-10-03) |
| `BRIEF_FACT_CHECK` | `1` (default, ADR 0021): one judge call (Sonnet) lists every factual claim in the brief's fields (proposals left out) and says whether the client's documents, verified research or unverified research support it, or nothing does; the client brief then shows "Still to verify" and "What we will not claim", every unsupported claim raises an open question, and an "Evidence and sources" appendix numbers the cited facts (A1, A2...) with source, date and status, marked in the fields. `0` turns the check off (the appendix stays) |
| `BRIEF_SECTIONS` | `1` (default, ADR 0022): after the 11 fields, seven Sonnet writers in parallel add the sections a senior agency's brief has: how we will know it worked (measure, baseline, target, method), the person we are talking to, competitors and alternatives, what each channel does, safety and legal lines, language and adaptation, practicalities. Every item says where it comes from (client, research, rule pack, or the agency's proposal); a figure no source states is flagged; a legal line must cite a rule pack or the client. Their claims go through the fact-check; the app gets them in `reply.meta.sections`. `0` turns them off |
| `BRIEF_RULE_PACKS_UNREVIEWED` | `0` (default): the safety and legal section uses only rule packs (`engine/rule_packs/*.yaml`) a person has checked (`reviewed: true`); `1` also uses unreviewed ones, for trials, and the brief says so |
| `BRIEF_GAP_FILL` | `1` (default, ADR 0019): the gap-filler finds facts for every field written as a proposal and for reasons to believe no given fact could prove: the fact store first (jev keeps the facts that help), the web only for what the store could not answer; found facts are cited like research facts, and the report is in `meta.gap_fill`. `0` turns it off |
| `BRIEF_RESEARCH_URL` / `BRIEF_RESEARCH_TIMEOUT` | the research tool's port (`napkin.research/1`, e.g. the mock-backend at `http://127.0.0.1:8797`) the gap-filler's web tier calls, one search per gap; unset = web tier off (the run says so) / seconds per search (300) |
| `BRIEF_FACT_STORE` | where the gap-filler keeps every fact the web found, one file per brand and market (default `~/.cache/napkin/facts`); the next brief for the same brand reads them first |
| `BRIEF_COHERENCE` | `0` (off by default, Sai 2026-10-02): `1` checks, after the fields are filled, that problem → insight → proposition → reasons to believe → desired response hold together; the weakest broken link gets one repair (a proof or response field is re-written by its own writer with the facts; an insight or proposition is rewritten by Opus), kept only if it clears its own gate and leaves fewer links broken, else the break is shown on the field (`coherence`, with `repair.outcome`) and raises an open question. Measured: 6 blind head-to-heads, 2 wins / 2 ties / 2 losses, +40-100 s a brief; the detector finds real breaks (mostly missing proof), so it is kept for the gap-filler and the strategy chain |
| `BRIEF_COHERENCE_JUDGE` | `claude` (default): the link check is ONE Sonnet 5.5 call on the judge route (never the writers' Opus) that says why a link breaks and what to change, about $0.02 a brief; jev answers when Claude cannot. `jev`: jev first (four yes/no questions, a link holds at p ≥ 0.5, a fraction of a cent). Measured 2026-10-02 on 88 links: jev agreed with Claude on 84%, caught 17 of the 30 breaks Claude found, 1 false alarm |
| `BRIEF_ACYCLIC_FILL` | `0` (default): the SMP writer reads the extracted RTB, which the RTB writer then rewrites from the SMP (audit N9); `1`: a strategy field reads a field written after it only when the client stated that field |
| `BRIEF_JUDGE_FORMAT` | `full`: one `{"pass": bool, "why"}` object per test. `compact` (phase C change 2, tested 2026-09-29, off by default): each judge answers a draft with the numbers of the tests it passes and a reason of at most 12 words per failed test. It halved the hero judges' output (about 8 s and $0.02 a brief) but judged "ownable" harder. The reader accepts either shape, and a test with no clear verdict leaves the draft unjudged in both. |
| `BRIEF_JUDGE_DUMP` | unset: a JSON-lines path; every judge call appends its inputs (field, drafts, context, territory) for the paired judge test `rag/judge_format_ab.py`. Diagnostic only. |
| `BRIEF_ALLOW_NONCLAUDE` | unset (default): with Claude as the lead link the chain is Claude-only, and a run with no route to Claude stops with a clear error; `1`: non-Claude links stay as fallbacks (not for production; a brief they answer is named in `meta.fallback_links`). See [ADR 0006](rag/docs/adr/0006-cannot-fail-silently.md) |
| `BRIEF_CLI_FALLBACK_TTL` | seconds an `auto` switch to the Claude Code login lasts before the API is tried again (600) |
| `ANTHROPIC_API_KEY` | Claude (the default lead link; also the independent critic) |
| `BRIEF_CLAUDE_TRANSPORT` | how Claude links are sent: `api` (default, the key above), `cli` (your Claude Code login via `claude -p`, no API credit used), `auto` (the key, switching to the Claude Code login if it has no credit or is rejected). Also `serve.py --claude-code / --api / --auto` and `e2e_eval.py --transport`. See [ADR 0005](rag/docs/adr/0005-claude-transport.md) |
| `BRIEF_PROVIDER` | pin one provider as the lead link (the rest stay as fallback) |
| `BRIEF_SMP_CANDIDATES` / `BRIEF_GOLDEN` | SMP draft count (default 4, one per angle seed, since 2026-09-29; was 6) / run the golden-brief pass |
| (temperature) | not sent | Claude calls never send it: Opus 5.5 and Sonnet 5 reject it with a 400 (ADR 0011, 2026-09-29). Only the non-Claude fallback path sends 0.2. |
| `BRIEF_CLIP_EXTRACT_CHARS` | how much of the brief the capture and every call that embeds it read (40000; 12000 before 2026-09-29). Read at import. (`BRIEF_CLIP_CHARS`, the old judge clip, is unused: judges never receive the brief.) |
| `BRIEF_BASE_URL` / `BRIEF_LINK_COOLDOWN` | custom OpenAI-compatible endpoint / seconds a rate-limited link rests |
| `BRIEF_VISION_CACHE` | where Claude's page transcriptions are kept between runs (default `~/.cache/napkin/vision`). Every PDF page with under 300 characters of text or pictures over 30% of its area, and every image file, is read by Claude (route `vision`: Sonnet 5.5 at low effort, Opus 5.5 fallback) and the transcription is added after the page's text layer, all pages, in parallel (2026-10-02; the NVIDIA vision model and the 20-page limit are gone). A page no model could read is marked unread in the text |
| `GEMINI_API_KEY` / `OPENAI_API_KEY` | optional further chat links, auto-detected |
| `BRIEF_THINKING_HEADROOM` / `BRIEF_LINK_TIMEOUT` | extra output tokens for Claude models that think by default (2500) / per-request timeout for OpenAI-compatible links (90 s) |
| `CRITIC_MODEL` / `CRITIC_SAMPLES` | the independent critic: model (default `claude-sonnet-5`) and samples per brief (default `1`); with more than one, each check keeps the majority verdict and an even split is REVIEW (`golden_critic.run_critic_sampled`). On mamaliga, three Fable 5.1 samples spread 2 health points against Sonnet's 5. `checkpoint_run.py --critic claude-fable-5-1x3` sets both for a run |
| `BRIEF_CLI_TIMEOUT` / `BRIEF_CLI_EFFORT` | Claude Code login transport: seconds per `claude -p` call (240) / force one `--effort` for every thinking-model call (default: the job's effort, else the model's API default) |
| `BRIEF_CORPUS` / `BRIEF_PACKS_LOCK` | pack sync: corpus root (default `engine/reference/rag` or `../reference/rag`) / path of `packs.lock` (default `engine/packs.lock`) |
| `BRIEF_RESEARCH` / `RESEARCH_WEB` | agent server: `0` turns the research dossier off (default on; skipped when Loops 3–7 are on and the web track is off) / `claude` adds the web track through `claude -p` with WebSearch (default `off`) |
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
