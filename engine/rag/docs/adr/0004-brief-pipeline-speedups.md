# ADR 0004 — Brief pipeline: TOON capture, sentence citations, stage graph, batched judge

Status: accepted (Sai, 2026-09-23) · Code: `engine/parse_brief.py`, `engine/toon_lite.py` ·
Tests: `engine/rag/test_brief_speedups.py` · Measurement: `engine/rag/e2e_eval.py --trace`

## Context

A traced run of one real brief (2026-09-23) took 316 s, 29 LLM calls and $0.48, with
retrieval only 3.4 s of it. Three causes:

1. **Capture wrote 5,256 output tokens** (78 s, 30% of cost) for a ~1,200-token brief:
   43% of the fact text was verbatim `source_quote` copying the brief back out, JSON keys
   repeated on every list item, and how_to_win generated in the same call.
2. **Every stage waited for the one before it**, although golden extraction reads only
   the raw brief and the hero fields never read the loop synthesis.
3. **Hero fields made 21 calls in series**: a ranking call, then one rubric-gate call
   per draft until one passed, plus a territory call per passing SMP draft.

## Decision

- **Sentence citations.** The model sees the brief as `[i]` numbered sentences (the same
  `segment()` rows the no-loss ledger counts) and writes `src: 4 7`; code attaches the
  verbatim sentences as `source_quote` and keeps `source_refs`. Quotes are exact by
  construction.
- **TOON output** for the capture and how-to-win (the format CLAN uses for agent context;
  CLAN's own agents still answer in JSON). Read by `toon_lite.decode`, a lenient in-house
  decoder: the PyPI `toon-format` 0.1.0 was a stub and `python-toon` writes a non-spec
  header. A reply that cannot be read falls back to the JSON capture.
- **how_to_win in its own call**, at most 5 points per table.
- **Stage graph in `run()`**: capture ∥ how-to-win ∥ golden; then scorecard ∥ retrieval;
  then hero fields ∥ synthesis (`loops_3_7(synthesize=False)`).
- **`_judge_and_gate`**: one call ranks every hero draft and runs every llm rubric test
  (and, for the SMP, `own_territory` / `not_rival_line`) on each. The pass rule is
  unchanged: a code failure is final, one soft failure is tolerated, the best-ranked
  passing draft wins. The sharpened line is re-checked in one call.
- **Waves from `depends_on`**: the SMP territory map runs alongside the insight;
  reasons_to_believe and desired_response run together.

Each is switchable: `BRIEF_CAPTURE=json`, `BRIEF_PARALLEL=0`, `BRIEF_BATCH_GATES=0`.

## Evidence (3 real client briefs, old vs new, same day)

| | old | new |
|---|---|---|
| wall, 3 briefs | 973 s | 396 s |
| LLM calls | 66 | 57 |
| cost (Opus 4.6) | $1.60 | $1.18 |
| golden_critic health, summed | 143 | 145 |
| blind judge, summed /30 (ran on Opus 4.6 — see correction below) | 21 | 21 (old preferred 2, same 1 — on insight sharpness) |
| no-loss coverage | 67–77% | 86–94% |

## Consequences

- The hero chain is now the critical path (115 of 152 s on the traced brief); the batched
  judge writes a reason per test per draft (the SMP's took 27 s). Next: reasons only on
  failures; consider skipping the sharpen pass when the winner passes cleanly (needs A/B).
- Open questions per finished brief fell from 11–14 to 5–7; not yet reviewed item by item.
- Found during the check: one wrapped table row made a how_to_win reply unreadable; the
  decoder now joins wrapped rows back (test: `test_toon_wrapped_row_is_joined_back`).

## Addendum 2026-09-24 — the health score was scoring the client's text

`golden_critic.from_brief_object` read the SMP from the capture's `key_message` and the
RTBs from `proof_points` — the CLIENT's own words — never the generated, gated fields in
`loop2_golden`, and took one objective level instead of the golden extraction's three. So
"SMP is two sentences" was the client's key message. It now prefers `loop2_golden` and falls
back to the capture. Re-scored on the same outputs: 51 / 36 / 67 -> **60 / 57 / 67**
(old pipeline on the corrected checker: 60 / 53 / 67). The ceiling is 74 while the 14 llm
rubric checks stay unjudged (half credit each); what remains is mostly client gaps
(no budget, objectives not at three levels, a client RTB list over 5 items).

The generator was also never told the schema's code rules, and its gate did not run them;
`_rubric_hard` now runs golden_critic's own AUTO checks (strict word limit, one sentence,
item cap, a stated 'why', think/feel/do filled), `_gen_field_system` states them, and a draft
that breaks only those rules gets one repair rewrite before the field is given up.

## Correction 2026-09-24 — the "Sonnet" judges ran on Opus 4.6

Until 0fb52fc, `_call_link` sent every Claude call with `model_for("anthropic")`
(claude-opus-4-6) whatever `model=` asked for. So every judge described as Sonnet 5 —
compare_paths' path comparison, e2e_eval's blind old/new comparison, and the first
`golden_critic --judge` scores (78 / 71 / 75, commit 17b3d73) — actually ran on Opus 4.6,
the generator's own model: separate calls with no shared context, but the same model.
The comparisons still compared like with like (both sides judged by the same model); the
"independent judge" claim did not hold. Numbers re-measured with the real Sonnet 5 judge
are recorded in the project plan (health_rescored_sonnet) and supersede 17b3d73's.
