# ADR 0006 — Cannot fail silently: every gate can fail, every failure is visible

Status: accepted (Sai, 2026-09-25; batch 1 of the 2026-09-24 audit fix plan) ·
Code: `engine/parse_brief.py`, `engine/golden_critic.py`, `engine/toon_lite.py`,
`engine/agent-server/server.py`, `engine/agent-server/mapping.py`, `mock-agent/server.py`,
`serve.py`, `engine/rag/e2e_eval.py`, `engine/rag/compare_paths.py` ·
Tests: `engine/rag/test_cannot_fail_silently.py`, `engine/rag/test_brief_speedups.py`,
`engine/rag/test_claude_cli_transport.py`, `engine/agent-server/test_server.py`,
`mock-agent/test_mock_agent.py` · Findings: audit page, section "Batch 1 plan"
(project_plan.clan → `deep_audit_fix_plan_2026_09_24.batch_1_cannot_fail_silently`).

## Context

The deep audit of 2026-09-24 (165 findings, 0 refuted after a second check) found that
several errors in the brief pipeline looked like success:

- The reasons-to-believe and desired-response gates could never fail: the pass rule
  tolerated one failed judge test and those fields have exactly one (F1/G1). The
  independent critic failed the RTB on `supports_smp` in 4 of 13 recorded briefs; all
  shipped.
- A broken judge was a pass. 7 of 8 malformed reply shapes passed every candidate; keys
  numbered from 1 shifted verdicts so the draft whose verdict was lost won (F5/J2).
- A missing or `0.0` confidence passed the floor; `"high"` crashed the run, and the
  agent-server then re-ran the brief with no strategy and returned it as normal (F6/N6).
- A reply cut off at `max_tokens` was parsed anyway: a 3-draft tournament became one
  draft; a refusal fell through to a non-Claude link (F8/BW3).
- Text labelled `client_stated` by the extractor skipped every gate; in 4 of 13 briefs it
  was the extractor's own writing, overlapping its "quote" by 11–32% (F4b).
- A store that failed after the availability check crashed the brief, and the fallback
  path hit the same store again (N1/RAG-2/C3).
- With no API credit every call fell to NVIDIA models (whose terms exclude production
  use), labelled as Claude; the critic could silently run on the generator model; every
  Opus 5.5 run was labelled `claude-opus-4-6` (N2/C6/J4/D4/F12).
- Unvalidated retrieval, glued sentence numbers in how-to-win rows, `(sentence 35)`
  markers in client text, `"Pass"` read as vague and `"Multiple"` as single (JL-2/H6/H11/J11).

## Decisions

**One pass rule, one gate.** `_pass_rule(hard, soft, n_llm)`: a code failure is final; a
field with three or more llm tests (insight, SMP) tolerates one failed llm test, a field
with fewer tolerates none. Every generated field goes through `_judge_and_gate`; the
per-candidate `_rubric_gate`, the ranking-only `_judge_hero_candidates`, the separate
`_smp_territory_gate` and the `BRIEF_BATCH_GATES` switch are deleted. The territory test
is worded positively (`brand_only`, pass = only this brand can say it). The judge's
ranking guidance follows the field's shape: a hero *line* is judged on purity and
single-mindedness; a `list` field (reasons to believe) or a think/feel/do set is judged as
a set and never failed for having several items (the first live check failed a four-item
RTB "for being a list" under the hero wording).

**Strict verdicts.** `_verdict()` reads a boolean, `pass`/`fail` or `true`/`false` in any
case; anything else is *no verdict*. A candidate with any missing verdict is **unjudged**:
not ok, failure `unjudged: …`. Keys `1..n` are realigned and logged. A judge that is down
leaves every candidate unjudged, order kept, so the field becomes missing with an open
question that says the judge was unavailable; no repair or rescue calls are spent on it.
A failed territory map (`_smp_territory` → None) skips the territory tests and asks
"Which competitor must the proposition beat?" instead of testing against a placeholder.

**Confidence is a number in [0, 1]** (`_conf`); anything else counts as below the floor
("no confidence reported"). A crash in one field's generation becomes that field's
`missing` plus an open question (`_one_safe`), never a failed run.

**stop_reason decides whether text may be read.** `max_tokens` raises `_Truncated`:
`_json_call` retries once on the same link with 1.5× the cap, then moves on; the partial
text is never parsed. `refusal` raises `_Refused`: logged with `[!]`, counted, next link.
Tournaments, judges and the critic read with `whole=True` (no inner-object salvage). The
golden extraction is one chain walk with a shape check, so a wrong-shaped reply reaches
the next link. The critic cap is 6,000 tokens.

**"Client said it" is proved by code.** A zone-3 field labelled `client_stated` is kept
only if its `source_quote` is verbatim in the brief (`_quote_in_brief`: punctuation and
case ignored, fragments split on ellipses) and the value matches the quote (≥ 60% of its
words). Otherwise it is generated and gated like any other. A genuine client line that
breaks a code rule is kept as written with an open question.

**Retrieval never crashes a brief.** `loops_3_7` wraps its whole grounded stage: a mix
failure, a store that dies mid-run, a validator that raises or a result with no evidence
all fall back to the pack digests with `fallback: {to, reason}` recorded and a `[!]` line.
`run()` also contains retrieval from the golden extraction and the capture path.

**Claude only, and say who answered.** With the lead link on Claude, non-Claude links are
dropped from the chain unless `BRIEF_ALLOW_NONCLAUDE=1`. `run()` raises
`NoClaudeAvailable` before any call when the chosen transport has no route to Claude (the
CLI prints it; the agent-server returns a non-2xx). Every used reply is recorded under
`llm_stats.answered_by`; `meta.extraction_mode` is the link that answered most calls;
`meta.model_chain` is the chain walked; `meta.fallback_links` names any non-Claude link
that answered, also printed once. The critic and the eval judges run with
`only_model=True` (the pinned model or nothing) and record `judge_model`; an unjudged
brief has `health: None` in the eval rows, never a score.

**Degraded runs say so.** `loops3_7.validation_degraded` lists loops no validator judged
(`validated_by` per loop, a `[!]` line, an eval row column, the app's context panel).
The agent-server's retry after a `run()` exception keeps the golden extraction and sets
`meta.degraded`, which `build_rationale` prefixes as `Degraded run (<reason>): …`.

**Clean text.** `toon_lite` joins all-numeric surplus cells under a `src` column into
`src`; the readers strip any glued `|n` that remains. `_scrub_markers` removes
`(sentence 35 …)` and `[5]` from client text, open questions and review notes; judge notes
name drafts by their opening words. The scorecard reads verdicts and dimensions in any
case, keeps one row per dimension, downgrades a pass whose evidence is not in the brief,
and keeps `split_into` only with a `multiple` verdict.

**Start the fill as soon as it can start.** The strategy fill waits only for the golden
extraction and retrieval; the scorecard is submitted at t=0 on the text (its heuristic
fallback is rebuilt from the capture); the pool has 8 workers. Measured on the audit's 11
runs: −15.6 s per brief on Opus 4.6 (up to −37 s), −7.2 s on Opus 5.5, same output.

**Honest numbers.** `prompt_tokens` is uncached input; `cache_read_tokens` and
`cache_creation_tokens` are recorded apart on both transports and priced at 0.1× / 1.25×
input by `e2e_eval._usd`. The stats snapshot is a deep copy. Each run records into its own
ledger (`_stats_scope`, carried into worker threads by `_scoped`); the agent-server's name
derivation, regeneration and research calls use their own scopes. An `auto` switch to the
CLI expires after `BRIEF_CLI_FALLBACK_TTL` (600 s) so a server re-tries the API.

**The test mock behaves like the engine.** `mock-agent` filters replies to schema keys of
the right JSON type, drops locked fields in code, requires the regenerated field, runs
`claude -p` with the engine's isolation flags and without the API key; `serve.py` strips
the key before starting it and, with no flag and Claude Code installed, starts the engine
on transport `auto`.

## Consequences

- Healthy runs produce the same output except the RTB/desired-response gate (weak ones
  are rewritten or become open questions) and the client-stated check (extractor prose is
  now generated and checked). Health on some Opus 4.6 briefs drops about 15 points at
  first: the score becoming honest. Re-baseline before comparing with runs before
  2026-09-25.
- Broken runs finish, say what went wrong, and ask a question.
- More open questions when a judge link is flaky. Pair with the model-chain failover:
  a transient failure retries on the next Claude link before a field is given up.
- The `BRIEF_BATCH_GATES=0` path and the free-tier fallback behaviour are gone; anyone
  who relied on NIM answering behind a Claude pin must set `BRIEF_ALLOW_NONCLAUDE=1`.
- CI now runs the offline engine suite with the network blocked
  (`BRIEF_TESTS_OFFLINE=1`, `.github/workflows/ci.yml`).
- Not in this batch: temperature (Opus 5.5 / Sonnet 5 reject it), JSON outputs for the
  capture (batch 5), anything touching Qdrant (moving to AWS), the grounding gate (parked).

## Addendum 2026-09-28 — a field whose every draft fails keeps its best draft, marked

Sai's decision (settles audit J5/J6): when every generated draft of a field fails a judged
check, the field keeps the best-ranked draft instead of being emptied. It carries
`review = {status: failed_checks, failed: [check ids], why}`, an open question says so, the
client brief shows "_Draft — to review: it failed …_" under the heading, review.md and the
provenance mark say DRAFT TO REVIEW, and the app's rationale names it. Media-gaa showed the
cost of the old rule: an honest RTB with one real fact and "TO CONFIRM" lines was dropped
because the judge failed `supports_smp`, and the brief lost the content and 12 health points.

Still left open, as before: a draft that fails an invented-figure check (the code number check
or jev's figure check: `INVENTION_MARKERS`) is never kept, so no invented fact reaches the
page; a draft the judge could not check (judge down) stays open; a below-floor confidence with
no failed check stays an open question (audit H2). Code: `parse_brief.fill_derivable_fields`
(`_invents`, `_check_ids`), `brief_render.render_client_brief`, `agent-server/mapping.build_rationale`.
Tests: `test_cannot_fail_silently.py` (kept and marked; an inventing draft still left open;
the client-page tag; the app rationale).

