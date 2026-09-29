---
name: clan-steward
description: Keep a project's <folder_name>.clan file as its living source of truth - standards, testing policy, task style, fonts, docs, plan, component links and teammate handoffs. Use at the start of work in any project folder, when the user says "handoff", "assign", "update the clan", "set up project standards", or when no <folder_name>.clan exists. Never edits existing source code.
---

# clan-steward

A `.clan` is a zip; CLI is `clan` (`clan agent-help` for syntax). Read with `clan read agent X.clan`. Write ONLY with `clan patch-data` (RFC 7396 merge patch; arrays REPLACE, so restate whole lists from a fresh read; back up the file to the scratchpad first). Never Read the zip directly.

## Hard rules
1. **Never modify existing source code.** Files this skill may create/edit: `<name>.clan`, and one pointer line in `AGENTS.md`/`CLAUDE.md` (step 6, ask once). Anything else (linters, hooks, CI, fonts) is a *suggestion* written into the clan or shown to the user, never applied unprompted.
2. Existing clan wins. Read it first; add missing sections, never overwrite filled ones.
3. Cite before you claim. Standards recommendations name a source or say "my judgement".
4. One thing at a time: explain simply, wait for the user's approval before locking a decision (font, tool, policy).

## Every session start
1. Run `~/.claude/skills/clan-steward/scripts/scan.sh` (read-only). It reports the root clan, component folders, tooling, tests and fonts in use.
2. If `<folder_name>.clan` exists: `clan read agent` it, then give a **status check** before the task (keep it to ~10 lines):
   - **Status**: `agent/state.yaml` status + next_step; the newest `decision-chain` entries (who/what/when).
   - **Todo**: every `todo*`/`next_checklist` key (open items, owner, date), `open_decisions`, `open_blockers`, and `handoffs` addressed to the current user (git user.name) with status open.
   - **Freshness**: scan's `clan freshness` line. If `commits_since` is high or `uncommitted_change`=1, or the newest chain entry is older than the recent commits (`git log --oneline -15`), say what code work the clan does not mention and propose the patch. Flag stale todos (dated before today, not done) and `last_verified` older than 60 days.
   - **Then** continue the user's task guided by the clan; its `standards`, `testing`, `task_resolution`, `git_workflow` and `typography` sections bind you. If the task is not on the todo/plan, say so and ask whether to add it.
3. If missing: go to *Bootstrap*.
4. If the folder has component subfolders (each with its own package.json/pyproject/Cargo.toml/go.mod): offer one clan per component and link them (see *Linking*).

## Bootstrap (no clan yet)
`clan create --title "<name>" --brief "<one-line purpose from README>" --doc-type project-context --output ./<name>.clan`, then `patch-data` the skeleton below, filled from the scan. Unknown = `unset` plus an entry in `open_decisions`.

```yaml
project: {name, purpose, owner, stack, run_cmd, test_cmd}
git_workflow: {status, branch_pattern, commit_pattern, merge_method, protected_branches}
mode: existing_code | greenfield          # from scan source_files
standards:                                # see references/defaults.md
  status: detected | proposed | locked
  formatter_linter: ...
  workflow: trunk-based, PR <=200 lines, 1 approval, squash-merge
  commits: see git_workflow
  definition_of_done: [ci_green, test_for_change, docstrings_on_public, docs_updated, reviewed, safe_to_deploy]
  adoption: ratchet        # existing code: report-only, ratchet, boy-scout; never mass rewrite
testing:
  policy: {local: impacted-only <1min, pr_ci: full fast suite <10min, nightly: e2e+live-model evals}
  rules: [never delete/skip old tests to go green, bug fix ships with failing-first test, flaky -> quarantine tag + issue, fix in 7d or delete]
task_resolution: {how_user_wants_tasks_solved: ..., approval_style: ..., branch_naming: ...}
typography: {status: detected|options|locked, in_use: [...], options: [...], locked: null}
docs: {index: [{path, purpose, last_verified}], adr_dir: doc/adr}
metrics: [{name, kind, measure_cmd, source, target, tolerance, owner, baseline, latest, history}]
plan: {exists: bool, summary, now: [], next: [], later: []}
components: [{name, path, clan, owner}]   # root only
parent: {path}                            # component clans only
handoffs: []
open_decisions: []
```

## Standards (existing code vs new)
- **Existing code**: detect what is already configured, record it as `status: detected`. Missing pieces go in `standards.proposed` with a one-line why. Adoption order: report-only tools -> one `style: format` commit added to `.git-blame-ignore-revs` (user runs it) -> CI ratchet (fail only if violations rise / only changed files) -> boy-scout on touched files -> flip rules warn->error one at a time. Do NOT edit code to comply.
- **New/greenfield**: propose the defaults in `references/defaults.md`, let the user accept/edit, then lock.

## Git workflow (branches, commits, pushing)
Detect first (`git branch -a`, `git log --format=%s -30`, CONTRIBUTING.md), record what the team already does as `git_workflow.status: detected`, and propose the gaps. Never rename/rewrite existing branches or history.
Proposed default (user locks it; see references/defaults.md):
- Branch: `<type>/<short-kebab>` with type in feat|fix|docs|chore|eval|refactor|spike; lowercase, no owner prefix, <=40 chars, one layer per branch. Branches live days not weeks; delete after merge.
- Commit subject: `<area>: <imperative summary>` <=72 chars, where area is a component (engine, rag, eval, app, cli, sdk, docs) - this matches the team's existing habit. Detail, audit ids, decisions and numbers go in the body, not the subject. Add `!` after area for breaking changes.
- Push: never to main; open a PR from the branch; squash-merge so main reads as one line per change; PR title = the commit subject; PR body = what, why, how verified (test command + result), doc/clan updated or N/A. Never force-push shared branches; `--force-with-lease` only on your own.
- Before push: layer check command passes, `git status` clean of stray files (.env, outputs, caches), clan updated.
- Detected mixed styles: report them, do not fix; offer a one-off cleanup list of stale remote branches for the user to delete.

## Fonts
1. Use the scan's font list (next/font, tailwind, CSS, @fontsource, .woff2). If fonts are in use, record them and ask "lock these?".
2. If none: present 3 options (Inter alone; Geist + Geist Mono; DM Sans + Fraunces; system stack as zero-cost fallback) with one line each on feel/perf/licence (all OFL). User picks -> write `typography.locked` (families, weights, fallback stack, CSS-variable tokens) and suggest an ADR. Max 2-3 families, variable fonts, latin subset, `font-display: swap`. Do not edit the app's CSS; give the snippet.
3. Mixed fonts already in the repo: report the inconsistency, don't fix.

## Metrics (what the project is judged on)
Goal: find what "good" means for this project, then track it through development.
1. **Discover** (in order; cite where each came from): existing clan keys (scores, baselines, checkpoints, `tests_passing`, cost tables); the scan's `metric hints` (eval/benchmark/baseline dirs, thresholds in CI/README/spec); README/spec/PRD goals; CI gates. Classify each candidate as **product** (quality of what users get, e.g. eval score, pass rate, accuracy), **speed/cost** (latency p95, tokens in/out, cost per run), **engineering** (tests passing, flaky count, coverage on changed files, CI time, lint violations vs baseline), or **delivery** (PRs merged, open handoffs, stale todos).
2. **Propose, don't invent**: present at most 5-7 metrics with: name, how it is measured (exact command), where the number comes from, target/threshold, and who owns it. Mark unknowns `unset` and put them in `open_decisions`. User approves before they are locked. If nothing measurable exists, propose a minimal starter set (tests pass, CI time, one product-quality check) and say why.
3. **Store** - ASK the user (AskUserQuestion, recommend option 1): where should metric history live? (1) inside the clan `metrics:` (default, simplest); (2) a dedicated `metrics/<name>.jsonl` file (append-only; not shared by git unless committed); (3) both: latest+baseline in the clan, full history in the file. Record the answer in `metrics_storage`. Default layout: clan `metrics:` holds definitions, `baseline`, `latest` {value, date, commit, run_ref} and the last ~10 points per metric. Raw runs, reports and big series stay where they are (eval checkpoints, CI artifacts); the clan only stores the path/ref. Only if the series outgrows the clan (~150 KB) or several people write it, propose a dedicated file `metrics/<name>.jsonl` (append-only, one line per run) with the clan pointing to it - and remember that file is not shared by git either unless the user commits it.
4. **Track**: after any task that could move a metric (prompt/model, retrieval, schema, perf, dependency change), run the cheap offline measure if it exists, compare with `baseline`, and report delta in the end-of-task summary. Paid/live runs (tokens, cost) only after asking, and always report tokens in/out and cost beside scores. Regression beyond the metric's tolerance = flag before finishing; an intentional baseline change needs a `--rationale`.
5. **Review**: at session start's status check, show each metric's latest vs target and its date; flag any not measured in 14 days or trending the wrong way.

## Plan
Check `plan` (or any todo/plan keys). If present, evaluate it: stale dates, items without owner, no "next step". If absent, draft Now/Next/Later from README, open TODOs and recent git log, and **suggest** it; write it only after approval.

## Handoff / assignment
Exactly one owner per task. To assign or hand off, append to `handoffs` (restate the full list):
```yaml
- id: H-<n>, title, from, to, date, status: open|acked|done
  state: what is done + branch/PR link
  next_step: one sentence
  blockers: who/what since when, or none
  decisions: what and why (ADR link)
  gotchas: ...
  verify: exact command -> expected result
  read_first: [max 3 files]
```
Write it before stopping work. Receiver sets `acked` and re-runs `verify`. Per-person WIP limit 2. Also refresh `agent/state.yaml` via `clan patch-state` (`next_step`, `status`).

## Linking (multi-component)
Hub and spoke: root `<root>.clan` `components` lists each spoke (name, relative path, clan, owner, one-line purpose); each component `<comp>.clan` has `parent.path`. The root never copies spoke content, only links. Component clans hold purpose, interface, run/test, decisions, gotchas, owner, `last_verified`. When touching a component, read root + only that component's clan.

## Testing question (answer to give)
Run old tests too. Locally only impacted tests (<1 min); PR CI full fast suite (<10 min); slow e2e nightly/on merge. Never skip or delete old tests to get green; intentional behaviour change = update that test in the same PR with a reason. Keep live-model/LLM calls out of the merge gate (mock/replay fixtures; live evals nightly).

## Maintaining the clan file (mechanics)
- **Which command**: data facts -> `patch-data`; next step/status -> `patch-state`; free-text notes -> `patch-context`; history only -> `patch-decision`; the human view -> `patch-html` with `--selector` (`pack-html` is expensive, avoid). Always pass `--agent`, `--action`, `--rationale`.
- **Before writing**: back up the .clan to the scratchpad; re-read current data (a teammate may have changed it); build a minimal merge patch. Arrays replace wholesale, so restate the full list (`--append <key>` to add one item). `null` deletes a key.
- **After writing**: `clan validate X.clan`; unzip to a fresh dir and check the changed keys; grep `human/index.html` for the old text (the view often duplicates data prose) and fix with `patch-html`. "data key(s) not reflected in view" is normal.
- **What belongs where**: facts and decisions in `shared/data.yaml` (short, one fact per key, dated); detailed function docs stay in the code/README, the clan only indexes them; big logs and outputs never go in.
- **Hygiene**: keep one place per fact; move finished todos to a dated `done_` list and prune it monthly; close `open_decisions` when decided and record the decision in the chain; flag `last_verified` older than 60 days; if the file passes ~150 KB, propose splitting per component.
- **Teammates**: get the latest copy before editing; two people editing different copies of the same .clan diverge (it is a zip; nothing merges it automatically). Rule: one editor per clan at a time (`owner` field), others send changes through a handoff; for parallel work use `clan fork`/`clan merge` and adjudicate contested keys.
- **The clan is shared outside git** (not committed). So: (1) suggest, once, that it is in `.gitignore` (and `git rm --cached` if tracked) - suggestion only, never edit; (2) git cannot show who changed it, so every write must carry `--agent` (use the person's name, e.g. `claude for Sai`) and the decision chain is the only history; (3) before editing, ask the user whether their copy is the latest (compare `manifest updated_at` with the copy the teammate sent); (4) after editing, tell the user to re-share it and name the file `<name>.clan` (no version suffix) with `updated_at` shown in your summary; (5) a handoff must say 'send the updated clan' as a step.

## Stay current
- Step 6 (once per project, ask): add to `AGENTS.md`/`CLAUDE.md`: "Before any task read `<name>.clan` (`clan read agent`); update it after any change to behaviour, interface, decisions, plan or handoffs."
- **End of every task**: list what changed in behaviour/interface/decisions/plan, and **always suggest** the exact `clan patch-data` update (offer to run it). Bump `docs.index[].last_verified`. Flag components not verified in 60 days.
- Record each locked decision with `--agent claude --action ... --rationale ...` so the decision chain stays useful.
