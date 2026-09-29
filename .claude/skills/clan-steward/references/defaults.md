# Proposed defaults for a 3-5 person startup (suggest, user locks)

Sources: DORA trunk-based dev (dora.dev/capabilities/trunk-based-development), Conventional Commits 1.0 (conventionalcommits.org),
Nygard ADRs (cognitect.com/blog/2011/11/15/documenting-architecture-decisions), Fowler Practical Test Pyramid, Google Testing Blog on flaky tests,
Next.js/Google Fonts docs. Items marked (judgement) are not from a source.

- Format+lint enforced by tooling, pre-commit + same check in CI: Biome or Prettier+ESLint (TS); Ruff (Python); rustfmt+clippy (Rust). No style debates in review.
- Trunk-based: branches live hours, merge daily, <=3 active branches, PRs ~200 lines, 1 approval, feature flags for unfinished work.
- Conventional Commits `type(scope): description`; enforce with commitlint or a squash-merge PR-title check.
- ADRs: Title/Context/Decision/Consequences, `doc/adr/NNNN-title.md`, never renumber, supersede rather than edit; only for hard-to-reverse choices.
- Docstrings on public functions only; state contract and why; enforce via ruff D / eslint-plugin-jsdoc.
- Definition of Done (PR template): CI green; test for new/changed behaviour; docstrings; component doc/ADR/clan updated or "N/A because"; reviewed; safe to deploy.
- Tests: pyramid (many unit, some integration, few e2e). New tests run 20x before gating. Flaky: quarantine tag + issue, out of gate, fix in 7 days or delete, no suite-wide auto-retry.
- LLM/RAG code (judgement): mock or replay model calls in the gate; live evals nightly and always report tokens + cost.
- Docs drift: CI warns if `component/` changed and its clan/CONTEXT did not; `last_verified` older than 60 days is flagged.

## Stack-specific (Napkin: Rust + Tauri/React/TS + Python RAG engine)
Opened sources: hamel.dev/blog/posts/evals, Anthropic develop-tests docs, docs.astral.sh/ruff/linter (incremental adoption), vcrpy.readthedocs.io, biomejs.dev. Everything else (nextest, insta, pyright, mockIPC, rust-cache, paths-filter) is recalled - verify before locking.

| Layer | Format/lint/types | Tests | Adoption on existing code |
|---|---|---|---|
| Rust | rustfmt, `clippy --workspace --all-targets -D warnings`, pin `rust-toolchain.toml` | cargo test/nextest (+`cargo test --doc`), insta snapshots for CLI/.clan output | one "format only" commit, then gate |
| TS/React/Tauri | ESLint + Prettier (Biome optional), `tsc --noEmit` strict, react-hooks plugin | Vitest + Testing Library; mock Tauri IPC; e2e nightly only | enable strict flag by flag; one prettier commit in .git-blame-ignore-revs |
| Python | Ruff (select E4,E7,E9,F,I,B - never ALL), pyright basic, strict only on clean dirs | pytest; markers `live`,`slow` deselected by default | `ruff check --add-noqa` baseline, one format commit, add rules one PR each |

LLM/RAG testing tiers: T0 every commit (prompt builders, schema validators, parsers, chunking, cost accounting, fake LLM client) - T1 every PR, offline (record/replay, cache keyed by hash of model+prompt+params, cache miss FAILS in CI; judge tests use stubbed replies) - T2 nightly/manual paid golden-brief eval (per-rubric scores, pass rate, tokens in/out, cost; grader model != generator) - T3 prompt/model changes gated by `eval/baseline.json` (fail if score drops >2 pts or cost >+20%; baseline edit needs justification).

AI-agent rules: root AGENTS.md (+ short CLAUDE.md pointer, nested per layer) with exact commands and a never-do list; failing test first; "done" = layer check passes; ~300-line one-layer PRs; no reformat/rename outside touched lines; CODEOWNERS on prompts, schemas, golden set, .clan (needs branch protection); agents work on branches/worktrees; reviewers reject weakened assertions, new skip/xfail/noqa/allow, lowered thresholds without justification.

Monorepo CI: path-filtered jobs per layer + one always-run `ci-ok` required check; caching (rust-cache, setup-node cache, uv); concurrency cancel-in-progress; nightly = paid eval, Tauri e2e, OS builds, audits; fork PRs get no secrets; budget cap on API key.

Top 8 to lock first: 1 CI never calls a paid model - 2 eval score+cost ratchet - 3 ci-ok + path filters + branch protection - 4 Ruff baseline + one format commit - 5 AGENTS.md + no-rewrite rule + CODEOWNERS - 6 failing test first, weakened tests rejected - 7 clippy -D warnings / tsc strict / pyright basic - 8 pre-commit + .editorconfig + .mailmap.

## Git workflow (detected in napkin-os-pr, 2026-09-29)
Commits already use `area: sentence` (engine/eval/rag/docs) but subjects run 100-200+ chars. Branches are mixed (feature/, feat/, fix/, bug_fix/, cleanup, ragAdded, Research-tool-experiment; ~34 total).
Proposal: keep `area:` (cheaper than switching to Conventional Commits `type(scope):`; switch only if you want commitlint/changelog tooling), cap subject at 72 chars, details in body. Branches `<type>/<short-kebab>`. Squash-merge PRs, branch protection on main, PR title = commit subject.
