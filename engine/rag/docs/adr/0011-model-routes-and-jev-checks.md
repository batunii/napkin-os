# ADR 0011 — Model routes by job, and jev as a checker

Status: accepted (Sai, 2026-09-26: "make the model changes"; "Sonnet for loop, jev for
RTB, scorecard, category filter, as a checker") · Findings: CC7, BW5, J3 (judge on the
writer), CC3/RAG-6 (synthesis model), JL-11/H1 (RTB figures), JL-8/RAG-9 (category filter,
decision 4), the scorecard and grounding parts of Shrey's parked jev scope · Code:
`engine/parse_brief.py` (`ROUTES`, `ROUTE_EFFORT`, `routes_active`, `route_models`,
`writer_route`, `judge_route`, `model_routes`, `_json_call(route=, exclude=)`,
`_jev_figure_failures`, `_jev_scorecard`, `brief_facets`, `_jev_mark_unsupported`, `run(upstream=)`),
`engine/rag/jev_checks.py`, `engine/rag/judge_jev.py` (`JevBackend.ask`) · Tests:
`engine/rag/test_model_routes.py`, `engine/rag/test_jev_checks.py`.

## Context

Every pipeline call ran on one model, Opus 4.6, except the Sonnet 5 critic. That forced
one trade-off everywhere. Opus 4.6 writes the best hero lines but invented all 11 figures
the audit found in RTB and desired-response items; Opus 5.5 invented 0 of 8 but costs
about 55% more per brief (its tokenizer counts 1.38-1.51x the tokens, and it thinks).
The five judges ran on the model that wrote the drafts they judged. The five loop
syntheses, read only by the review file, were the single largest cost line ($0.125 of
$0.50 on the BMW brief). Retrieval never applied a category filter, because the
capture's field names matched no filter slot.

On ragAdded the models were: Opus 4.6 for every engine call; Sonnet 5 for the critic
(silently Opus before 0fb52fc); a silent non-Claude fallback chain (gpt-oss-120b, GLM-4.7
and Llama 3.3 70B on Cerebras and Groq, Nemotron-3-Super and gpt-oss-20b on NVIDIA);
Nemotron nano-VL 8B for image briefs; and, on the app's test path with no API key,
the mock agent on Claude Code's `opus` alias, which resolves to Opus 5.5.

## Decisions

**Every call names its job; the job picks the model.** `ROUTES` maps a job to
[model, fallback], Claude only:

| job | calls | model | fallback | effort |
|---|---|---|---|---|
| extract | capture, golden extraction | Opus 4.6 | Opus 5.5 | — |
| hero | insight and SMP drafts, refine, other generated fields | Opus 4.6 | Opus 5.5 | — |
| grounded_writer | RTB, desired response | Opus 5.5 | Opus 4.6 | default |
| hero_judge | insight and SMP judges, territory map | Opus 5.5 | Sonnet 5 | low |
| judge | every other field's judge | Sonnet 5 | Haiku 4.5 | low |
| mechanical | scorecard, how-to-win, rerank | Sonnet 5 | Haiku 4.5 | low |
| synth | loop syntheses | Sonnet 5 | Haiku 4.5 | medium |

A routed call walks its job's own models, never the default chain. **A judge never runs
on its writer's model**: the judge call excludes the writer job's lead model, so the
RTB judge (writer Opus 5.5) runs on Sonnet or Haiku, and the SMP judge (writer Opus 4.6)
on Opus 5.5 or Sonnet. If the exclusion leaves nothing, the field is unjudged, as when a
judge is down. Effort travels on a per-thread setting read by both transports (API
`output_config.effort`, CLI `--effort`), only on thinking models, and is cleared after
every call. `BRIEF_ROUTE_<JOB>` overrides one job. `BRIEF_ROUTES=0`, an explicit
`BRIEF_MODEL` or `BRIEF_MODEL_CHAIN`, or a non-Anthropic provider restores the single
model chain (the whole-pipeline swaps the comparisons use). An explicit `model=` (the
critic, `BRIEF_SYNTH_MODEL`) wins over the route. `meta.model_routes` records the
routes; `meta.extraction_mode` names the extraction job's model, not whichever model
answered most calls.

**jev as a checker in four places** (`jev_checks.py`), each from the 2026-09-24 jev lab:

- *RTB and desired-response figures.* "Does every number in ITEM appear in the client
  brief, used for the same thing?" per item with a figure. AUC 0.997 on 156 real claims;
  at p(unsupported) >= 0.9 precision 1.0, recall 0.91 on generated items. A draft that
  crosses that line fails the gate like a code rule, beside the code number check.
- *Scorecard.* jev's own pass / vague / missing per dimension is written on each row;
  a disagreement at p >= 0.9 is listed in `jev_check.disputes`. The scorecard's verdict
  stands, because jev's accuracy on this rubric is unmeasured.
- *Category (decision 4).* `brief_facets` takes brand, category and competitors from the
  upstream research steps (`run(upstream=...)`); with no upstream category, jev picks one
  of the 18 locked categories (13 of 13 correct in the lab, lowest correct 0.85) and it
  is used at p >= 0.85 unless it is `other`. It becomes the exemplars' category filter and
  a category scope; brand and competitor names become exact keywords. The capture's
  competitor paragraph is not used as keywords: the slot takes names. Recorded in
  `loops3_7.retrieval_trace.facets`.
- *Synthesis.* Each cited sentence is asked whether its cited passages support it; at
  p(support) <= 0.1 the sentence gets "(not supported by its cited source)" and the loop
  an `unsupported` count. Marks only: the review file alone reads it, and this question
  is unmeasured against sources (against the brief the lab measured AUC 0.73).

Every jev check returns "not run" when jev cannot answer (no key, SDK missing, timeout,
`BRIEF_JEV_CHECKS=0`) and says so once; it never blocks a brief. The checks use their own
`JevBackend` client, separate from the validation chain's, through `JevBackend.ask()`
with the same lock and deadline as `score()`.

## Consequences

- Estimated BMW cost at list price: about $0.38 against $0.50, from the syntheses and
  mechanical calls on Sonnet, less the Opus 5.5 hero judges and RTB. Judges on the critical
  path should be faster (Sonnet and Opus 5.5 decode two to three times faster than Opus
  4.6). To be measured on the next checkpoint, not assumed.
- Health may move for two reasons unrelated to writing quality: the hero judges are a
  different model, and the category filter changes what exemplars the writers read.
  The checkpoint reads per-check fails and invented-figure counts first.
- The critic stays Sonnet 5 for now. Moving it to a model the pipeline never uses
  (Fable 5.1, three samples, calibrated on the CD/planner labels) is part of phase A,
  with every past checkpoint re-graded so the trend stays comparable.
- The mock agent (the app's no-key test path) still uses Claude Code's `opus` alias, now
  Opus 5.5, and writes whole drafts itself; it does not use these routes.
- Grounding for every other client-visible field (the full H9 gate) stays with Shrey.

## Alternatives rejected

- **Opus 5.5 for everything.** About 55% more per brief for the same health within
  noise; thinking on calls that answer a rule.
- **Haiku for syntheses.** Sai chose Sonnet: planners may read the review file.
- **jev replacing the scorecard or the judges.** jev cannot write the scorecard's
  evidence or the judges' ranking notes, and its accuracy on these rubrics is unmeasured.
