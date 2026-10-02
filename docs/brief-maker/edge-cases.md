# Brief maker: edge cases, bottlenecks and known failures

The register for making the brief maker never fail. Every row has a way to see it again (repro)
and a status. No client text goes in here: name the input by its file stem or describe it.

- **Harness:** `engine` = `engine/parse_brief.py`; `drafter` = `server/napkin/brief/` (BriefJob);
  `wiring` = middleware ↔ engine ↔ app; `research` = the research tool upstream.
- **Kind:** `bug` (wrong result), `drop` (input silently lost), `slow` (wall-time bottleneck),
  `cost`, `fragile` (works until a condition), `doc`, `env`, `gap` (the brief does not do its job).
- **Severity:** S1 wrong or lost content reaches the client; S2 degraded or silently unchecked;
  S3 slow, costly or misleading to a developer.
- **Status:** `open`, `fixed <commit>`, `xfail <test>` (pinned by a test that expects the bug), `wontfix`.

Started 2026-10-02 on `integrate/jev-hardening`, from code reading (file:line refs as of `7cbe533`).

## Found so far

| ID | Harness | Kind | Sev | What happens | Repro | Status |
|---|---|---|---|---|---|---|
| EC-001 | research | bug | S2 | When the host refuses the research change, synthesise still writes findings that cite facts the document never received (dangling `f_` cites). | `server/tests/test_refused_stage.py` (RefusingHost) | xfail test_refused_stage |
| EC-002 | engine | drop | S1 | Capture, golden extraction, scorecard, how-to-win and territory read only the first 40,000 chars (`CLIP_EXTRACT`, `parse_brief.py:1615`); the rest of a tender is unread (Samaritans: 83% unread, health about 40 vs 74 read whole). Read once at import. | Samaritans tender without `BRIEF_CLIP_EXTRACT_CHARS` | open (plan: `brief_maker_2026_10_01` steps 1-6) |
| EC-003 | engine | slow | S3 | With research facts, the jev fact-conflict check runs on the main thread before capture and golden extraction are submitted (`parse_brief.py:3266-3280`). | any run with `upstream.facts` | open |
| EC-004 | engine | fragile | S2 | A jev state failing or passing its 8 s deadline makes the whole check "did not run": figures, conflicts and synthesis go unchecked with only a stderr line; a failed conflict check treats every fact as agreed (`jev_checks.py:224-236`, `research_facts.py:220`). | jev key unset or slow network | open |
| EC-005 | engine | drop | S1 | A research `facts.yaml` still wrapped in `facts:` gives 1 skipped, 0 used, silently. | `samaritans_youth_brief/no_research` run | open |
| EC-006 | engine | gap | S2 | The CLI has no way to pass research facts, brand, category or competitors (`run(upstream=...)` only). | `napkin-brief --help` | open |
| EC-007 | engine | doc | S3 | `BRIEF_CLIP_CHARS` was documented as the judges' clip but does nothing. | README / RUNBOOK | fixed e2d64a9 |
| EC-008 | engine | fragile | S2 | Hero writers never see the brief text: only their `depends_on` fields (70-80 words each), background, the first 40 figure sentences and research lines. A long input depends entirely on golden extraction (8,000 output tokens). | read `parse_brief.py:1324-1355`, `:1173` | open |
| EC-009 | engine | drop | S1 | A scanned PDF is transcribed for its first 20 pages only, by a non-Claude vision model; for attachments passed as `raw_text` the "transcribed" warning is lost. | 25-page image-only PDF | open |
| EC-010 | engine | bug | S3 | The critic hardcodes `brief_type: "launch"` and the engine never sets a brief type, so type-dependent rules (tone_world_assets required) are fixed. | `golden_critic.py:528` | open |
| EC-011 | engine | fragile | S2 | A research figure written without its `[F:id]` fails the citation gate and can empty the field (Samaritans desired response, 2026-10-01). | samaritans_youth_brief latest | open |
| EC-012 | engine | bug | S2 | Every research fact reaches the writers as "VERIFIED RESEARCH FACTS" whatever its confidence (Samaritans: 88 of 94 low); `entity: brand/<x>` loses brand vs category scope; sources given as ids lose their titles. | Samaritans CLAN facts | open |
| EC-013 | engine | fragile | S3 | A research category outside the engine's enum (e.g. `public.charities_ngos`) is ignored and jev guesses the category. | Samaritans CLAN | open |
| EC-014 | engine | cost | S3 | No cap or ranking on research fact lines: all go to every hero writer (94 on Samaritans). | Samaritans with facts | open |
| EC-015 | engine | gap | S3 | The run ledger (`meta.llm_stats`) has no USD, no per-model split and no jev tokens; cost needs `e2e_eval --trace`. | any `brief_object.json` | open |
| EC-020 | wiring | drop | S1 | The engine's `meta` (research_facts, research_decisions) and each field's `decision_refs` never reach the middleware (`agent-server/mapping.py:182-186`, `server.py:280-296`); the "left out N decisions" attention never fires. A stub in `test_engine_adapter.py` hides it. | engine mode with decisions | open |
| EC-021 | wiring | bug | S2 | `research_decisions.citation_failures` builds known figures from raw values, not `research_facts.forms()`, so "27%" in a decision line is flagged after 3413771. | `research_decisions.py:140` | open |
| EC-022 | wiring | fragile | S2 | In engine mode `regenerate_field` still uses the middleware's own drafters: two writers in one brief. | middleware-api.md §10.14 | open |
| EC-023 | wiring | fragile | S2 | If the engine fails, `draft_brief` falls back to BriefJob; the brief's quality changes with only an attention note. | stop the engine mid-draft | open |
| EC-024 | wiring | fragile | S2 | Without a `middleware` proxy in `workspace.yaml`, middleware requests go to :8787, which speaks the agent protocol. | remove the proxy | open |
| EC-025 | wiring | slow | S2 | The engine takes one draft at a time (`_DRAFT_LOCK`) and the middleware waits up to 900 s: concurrent drafts queue and can time out into the fallback. | two drafts at once | open |
| EC-030 | drafter | drop | S1 | Capture reads 60,000 chars in total across all materials; the rest is cut and only flagged `truncated` (`capture.py:35,286-295`). | Samaritans tender | open |
| EC-031 | drafter | drop | S1 | Only the first 40 research pins, in file order, reach the writers, without market, quote or confidence; findings and the report are never read (`drafters.py:145-147`). | Samaritans CLAN (94 facts) | open |
| EC-032 | drafter | drop | S2 | Capture items capped at 40; background_context dropped when business_problem exists; key_message, proof_points, evaluation_criteria, strategic_angle, anti_target, decision_makers map to no field. | read `drafters.py:472` | open |
| EC-033 | drafter | slow | S3 | 8 rubric-group judge calls run one after another. | any draft | open |
| EC-034 | drafter | gap | S3 | No CLI, no cost record; how_to_win and the scorecard are never passed to the writers. | — | open |
| EC-035 | drafter | bug | S3 | Open questions duplicated (timeline, mandatories, decision makers, key message: once from capture, once from the core-gap rule); the second copy reads "What is the mandatories?". | one-line Barry's Tea | open |
| EC-036 | drafter | bug | S3 | Severity tags (`[blocker]`, `[important]`, `[nice_to_have]`) are stored inside the open-question text. | one-line Barry's Tea | open |
| EC-037 | drafter | bug | S3 | The same "no target number" critique is attached to all three objective levels. | one-line Barry's Tea | open |
| EC-038 | drafter | fragile | S2 | Captured fields are filled with "Implied: ..." values on a one-line brief: inferred content sits in client-fact slots. | one-line Barry's Tea | open |
| EC-039 | drafter | slow | S2 | One-line brief: 27 calls, 385 s (judge stage 210 s), about $0.86; 74k cache-write tokens. | one-line Barry's Tea | open |
| EC-050 | env | env | S3 | `mock-backend` FetchReasons test needs `trafilatura`, not installed in the default Python. | `pytest mock-backend/tests/test_search_jev.py` | open |

## The brief's job (gaps against what a brief is for)

From the 2026-10-02 research (BetterBriefs/IPA, IPA/ISBA *The Client Brief*, WARC): a brief is a
reasoned argument, not an information dump. Neither harness yet writes these:

| ID | Missing | Why it matters |
|---|---|---|
| GAP-01 | One-sentence problem that communications can solve | the brief's first job; "one brief = one strategy" |
| GAP-02 | From → to: what the audience thinks and does now, and what we want | IPA "where are we now / where do we want to be" |
| GAP-03 | Tension, and the fact → observation → insight chain with fact ids | makes the insight checkable and non-obvious |
| GAP-04 | Evaluation criteria for the work | only 30% of brands set them; "on brief" is the #2 way ideas are judged |
| GAP-05 | Springboard / territories marked as stimulus (never executions) | points where to look without prescribing |
| GAP-06 | how_to_win and the SMP territory are computed but not shown in the client brief (engine) | work already paid for |
| GAP-07 | Measured 2026-10-02: 8 engine briefs (test-five, Samaritans x2, Barry's Tea) pass 2-6 of 14 "does a brief's job" tests (Sonnet judge, 1 sample); all 8 fail problem, linked objectives, vivid audience, provenance, evaluation | the critic's health (70-88) measures completeness, not whether the brief argues |
