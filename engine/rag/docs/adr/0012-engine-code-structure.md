# ADR 0012 — Engine code structure: parse_brief split by responsibility, one .env loader, dead code out

Status: accepted (Sai, 2026-09-27: "clean up the code base, add documentation if missing,
refactor, make it more professionally written") · Findings: C14 (parse_brief.py too
large), C15 (four .env loaders), C8 (dead functions), C11 (unused judge_llm backend), C16
(stale docstring), C5 (undocumented environment variables), BW16 (a new Anthropic client
per call) · Code: `engine/parse_brief.py`, new `engine/brief_llm.py`,
`engine/brief_ingest.py`, `engine/brief_render.py`, `engine/engine_env.py` · Tests: the
full suites unchanged in count except the removed backend's 48, plus
`engine/rag/test_env_documented.py`.

## Context

`parse_brief.py` had grown to 4,867 lines holding four jobs: talking to models (providers,
chains, routes, two Claude transports, the call ledger), reading brief files, the pipeline
stages, and rendering. Six modules loaded `engine/.env` with four different parsers, one of
which (the critic's) silently did nothing without python-dotenv. `check_chain`,
`_word_count` and `packs.packs_for_loop` had no callers; `judge_llm.py` (401 lines, 48
tests) was in no chain and never ran. 16 environment variables were documented nowhere,
and the README still described a non-Claude fallback chain as the default.

## Decisions

**Split by responsibility, keep one import path.** Everything between a pipeline stage and
a model's reply moved to `brief_llm.py` (1,032 lines); reading brief files and segmentation
to `brief_ingest.py`; the client brief, the review file, provenance and pandoc output to
`brief_render.py`. The moved code depends only on itself (checked name by name before the
move), so there are no circular imports. `parse_brief.py` (3,400 lines) keeps the
pipeline stages and `run()`, and re-exports every moved name.

**Assignments on parse_brief reach the moved code.** The split modules call their own
globals, so `monkeypatch.setattr(parse_brief, "_call_link", fake)` or e2e_eval's tracer
wrapping `parse_brief._stats_call` would otherwise change nothing. parse_brief's module
type (`_ForwardingModule`) mirrors an assignment of any moved name (each module's
`MOVED_NAMES`) into the owning module as well as setting it on parse_brief. Every existing
test, the eval tracer and the agent server work unchanged; new code imports from the owning
module directly.

**One default system prompt, set by the pipeline.** Calls that pass no system prompt used
the extraction prompt. `brief_llm.DEFAULT_SYSTEM` holds a neutral default and parse_brief
sets it to `EXTRACTION_SYSTEM` on import, so behaviour is unchanged and brief_llm does not
depend on the pipeline's prompts.

**One `.env` loader.** `engine_env.load()` (rag.py's parser: `export` prefix, quotes,
comments) is used by parse_brief, the critic, rag.py and the replay tool; the validators
already delegate to rag.py. It never overrides a variable already set.

**Dead code out.** `check_chain`, `_word_count`, `packs_for_loop`, the `judge_llm`
backend and its tests, 26 unused imports and three unused variables. Git history keeps them.

**Small corrections.** One Anthropic SDK client per (SDK class, key), reused (BW16); the
reranker snippet's docstring now describes the regex it uses (C16); every environment
variable the code reads is documented, and `test_env_documented.py` fails on a new one
without a row (C5); the README's model paragraph describes the Claude-only routes.

## Consequences

- Same behaviour: 855 tests pass (903 before, less the removed backend's 48), the
  per-file counts are otherwise identical, and the checkpoint after the refactor is the
  end-to-end check.
- A module's size now matches one responsibility; `parse_brief.py` is still the largest
  (the fill and Loops 3-7 are long). Splitting the fill and retrieval stages is left until
  the phase C levers settle, because those are the stages still changing.
- The forwarding module is a compatibility layer, documented in the module and here. Once
  tests patch the owning modules directly it can go.

## Alternatives rejected

- **Updating every test to patch the new modules instead of forwarding.** About 30 test
  sites plus the eval tracer and the agent server; the forwarding layer keeps them and any
  outside caller working without edits, at the cost of one documented class.
- **Moving the extraction prompt into brief_llm.** It is the pipeline's domain text, not
  transport.
