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

Started 2026-10-02 on `integrate/jev-hardening`, from code reading (file:line refs as of `7cbe533`) and offline probes
(EC-060 to EC-081: no model calls; probe scripts kept with the session notes, to be added as tests when each is fixed).

## Requirements (hard gates for every test)

- **REQ-01 (Sai, 2026-10-02): every one of the 11 brief fields is filled, on every input.** An empty
  field fails the run. Where the client gave nothing, the field carries a labelled proposal or
  assumption with its basis and an open question, never an invented client fact. Rules (Sai, same day):
  a field the client never gave is filled as "Proposed (not given by the client): <value>, based on
  <fact ids / category norms / the brief>. To confirm: <question>"; a field whose drafts all fail a
  check gets one targeted repair pass, then the best draft ships marked "(Draft - failed check:
  <which>. To confirm.)".
- **REQ-02 (Sai, 2026-10-01): nothing from the input is left unread.** See EC-002, EC-030.

## Found so far

| ID | Harness | Kind | Sev | What happens | Repro | Status |
|---|---|---|---|---|---|---|
| EC-001 | research | bug | S2 | When the host refuses the research change, synthesise still writes findings that cite facts the document never received (dangling `f_` cites). | `server/tests/test_refused_stage.py` (RefusingHost) | xfail test_refused_stage |
| EC-002 | engine | drop | S1 | Capture, golden extraction, scorecard, how-to-win and territory read only the first 40,000 chars (`CLIP_EXTRACT`, `parse_brief.py:1615`); the rest of a tender is unread (Samaritans: 83% unread, health about 40 vs 74 read whole). Read once at import. | Samaritans tender without `BRIEF_CLIP_EXTRACT_CHARS` | open (plan: `brief_maker_2026_10_01` steps 1-6) |
| EC-003 | engine | slow | S3 | With research facts, the jev fact-conflict check runs on the main thread before capture and golden extraction are submitted (`parse_brief.py:3266-3280`). | any run with `upstream.facts` | open |
| EC-004 | engine | fragile | S2 | A jev state failing or passing its 8 s deadline makes the whole check "did not run": figures, conflicts and synthesis go unchecked with only a stderr line; a failed conflict check treats every fact as agreed (`jev_checks.py:224-236`, `research_facts.py:220`). | jev key unset or slow network | open |
| EC-005 | engine | drop | S1 | A research `facts.yaml` still wrapped in `facts:` gives 1 skipped, 0 used, silently. | `samaritans_youth_brief/no_research` run | fixed (current() unwraps the facts: wrapper, with a note) |
| EC-006 | engine | gap | S2 | The CLI has no way to pass research facts, brand, category or competitors (`run(upstream=...)` only). | `napkin-brief --help` | fixed: `--research CLAN` (facts, decisions, brand; ADR 0017); category and competitors still not passed |
| EC-007 | engine | doc | S3 | `BRIEF_CLIP_CHARS` was documented as the judges' clip but does nothing. | README / RUNBOOK | fixed e2d64a9 |
| EC-008 | engine | fragile | S2 | Hero writers never see the brief text: only their `depends_on` fields (70-80 words each), background, the first 40 figure sentences and research lines. A long input depends entirely on golden extraction (8,000 output tokens). | read `parse_brief.py:1324-1355`, `:1173` | open |
| EC-009 | engine | drop | S1 | A scanned PDF is transcribed for its first 20 pages only, by a non-Claude vision model; for attachments passed as `raw_text` the "transcribed" warning is lost. | 25-page image-only PDF | fixed (no page limit, Claude only); the raw_text warning is still open |
| EC-010 | engine | bug | S3 | The critic hardcodes `brief_type: "launch"` and the engine never sets a brief type, so type-dependent rules (tone_world_assets required) are fixed. | `golden_critic.py:528` | open |
| EC-011 | engine | fragile | S2 | A research figure written without its `[F:id]` fails the citation gate and can empty the field (Samaritans desired response, 2026-10-01). | samaritans_youth_brief latest | open |
| EC-012 | engine | bug | S2 | Every research fact reaches the writers as "VERIFIED RESEARCH FACTS" whatever its confidence (Samaritans: 88 of 94 low); `entity: brand/<x>` loses brand vs category scope; sources given as ids lose their titles. | Samaritans CLAN facts | fixed (ADR 0021): verified/unverified per fact, source ids resolved; brand vs category scope still open |
| EC-013 | engine | fragile | S3 | A research category outside the engine's enum (e.g. `public.charities_ngos`) is ignored and jev guesses the category. | Samaritans CLAN | open |
| EC-014 | engine | cost | S3 | No cap or ranking on research fact lines: all go to every hero writer (94 on Samaritans). | Samaritans with facts | open |
| EC-015 | engine | gap | S3 | The run ledger (`meta.llm_stats`) has no USD, no per-model split and no jev tokens; cost needs `e2e_eval --trace`. | any `brief_object.json` | open |
| EC-020 | wiring | drop | S1 | The engine's `meta` (research_facts, research_decisions) and each field's `decision_refs` never reach the middleware (`agent-server/mapping.py:182-186`, `server.py:280-296`); the "left out N decisions" attention never fires. A stub in `test_engine_adapter.py` hides it. | engine mode with decisions | fixed engine side: reply.meta carries research, decisions, gap-fill and field_flags (middleware already reads meta) |
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
| EC-060 | engine | drop | S1 | .eml: only the first body part is read; a forwarded original (`message/rfc822`) or a second text part is dropped. | `probe_a.py` | fixed (every text part, forwarded messages labelled) |
| EC-061 | engine | drop | S2 | .eml attachments (pdf/docx/txt) are never read, with no note; HTML-only mail loses link URLs and glues table cells ("Budget5m"). | `probe_a.py` | fixed (attachments read through ingest; HTML link URLs still open) |
| EC-062 | engine | bug | S2 | .eml with an unknown charset or attachments only crashes with a raw `LookupError`/`KeyError`. | `probe_a.py` | fixed (unknown charset decoded; attachment-only mail read) |
| EC-063 | engine | drop | S1 | .txt in UTF-16 (BOM) decodes as garbage; cp1252 turns "€"/"é" into U+FFFD ("�5m"); no warning. | `probe_a.py` | fixed (BOM, UTF-8, UTF-16, else Windows-1252 with a note) |
| EC-064 | engine | drop | S1 | .docx drops nested tables, tracked insertions (`w:ins`), content controls (SDT), smart tags, text boxes, footnotes and comments, silently. | `probe_docx.py` | fixed (XML walk: insertions, content controls, smart tags, text boxes, nested tables; notes, comments, headers once) |
| EC-065 | engine | drop | S2 | .docx `HYPERLINK` field-code links keep the text and lose the URL (native links are fine). | `probe_docx.py` | fixed (field-code hyperlinks keep their address) |
| EC-066 | engine | fragile | S2 | No empty-input guard: an image-only docx or a scan without vision gives "" and `run()` still spends calls (11 open questions from nothing). | `probe_run_empty.py` | fixed (EmptyInput before any model call) |
| EC-067 | engine | fragile | S3 | Corrupt or encrypted docx/pdf and 0-byte files raise raw library errors with no readable message. | `probe_a.py` | open |
| EC-068 | engine | drop | S2 | `_numbered` adds `[i] ` per sentence but `_clip_report` counts raw chars: a brief just under 40,000 reports no clip while capture loses its last sentences (about 5% of a near-limit brief). | `probe_b.py` | open |
| EC-069 | engine | gap | S3 | The clip note names an 80-char snippet, not which attachment was cut (always the last). | `probe_b.py` | open |
| EC-070 | engine | fragile | S2 | Prompt fences: `</client_brief>` in the client text is not neutralised; capture, how-to-win, the agent server's name call, competitor/rival lines, loop synthesis and regeneration guidance embed client-derived text with no "data, not instructions" fence; `</research>` inside a fact value closes its fence. | `probe_b.py`, code reading | open |
| EC-071 | engine | bug | S2 | A fact row with a list/dict `id` (or int `sources`) raises `TypeError` in `research_facts.current/line`; `run()` has no guard, so bad research kills the brief (breaks "upstream is optional"). Same for a non-hashable decision `kind`/`role`. | `probe_c.py`, `probe_d.py` | fixed for facts (a non-text id is skipped with its reason); decisions still open |
| EC-072 | engine | bug | S1 | Duplicate fact ids with different versions: the first wins, even when it is the older version. | `probe_c.py` | fixed (the higher version wins wherever it sits) |
| EC-073 | engine | bug | S2 | Fact status "draft"/"unverified"/unknown and values `[]`, `{}`, False, NaN count as current and usable. | `probe_c.py` | open |
| EC-074 | engine | fragile | S2 | Fact lines are not sanitised: a multi-line value can forge `[F:...]` lines; a 10k-char value goes to every writer; no cap on fact count (decisions cap at 40, and that cap can drop `rejected_finding` rows). | `probe_c.py`, `probe_d.py` | open |
| EC-075 | engine | bug | S2 | Citation forms the strip regex misses (`[F:a, F:b]`, `[f:a]`, `[F: a]`, `(F:a v1)`, `[F:a v1.2]`) leak into client prose; a cited version different from the given one is accepted. Same for `[D:...]`. | `probe_e.py` | open |
| EC-076 | engine | fragile | S2 | Figure checks fail honest writing: "20% decline" for -0.2, "1.2k"/"1.2 million" for 1,234,567, "28%" for 0.275, "1,200" vs European "1.200"; full-width and Arabic-Indic digits bypass the check; a small-integer fact (3) flags any uncited "3 pillars". | `probe_c.py` | open |
| EC-077 | engine | bug | S3 | `rivals()` turns None/"12%"/-3 into rival names; `scope()` does not recognise `competitor/`, `brand/`, `category/` entities; nested values print as Python reprs. | `probe_c.py` | open |
| EC-078 | engine | cost | S3 | `_upstream` accepts 100k fact or decision rows with no cap; an unknown category falls back to the model's pick with no message. | `probe_e.py` | open |
| EC-079 | engine | bug | S2 | `_scrub_markers` deletes any `[digits]` and "sentence N" from the client's own words ("Budget [5] million"). | `probe_g.py` | open |
| EC-080 | engine | bug | S3 | Render: dict items in objectives/desired_response are not scrubbed; `{}`/None `meta` or a None open question raises; lists print as Python lists; a `# heading` inside a value renders as a heading. | `probe_g.py` | open |
| EC-081 | engine | drop | S2 | PDF via pandoc/xelatex: CJK, Arabic and emoji become missing-glyph boxes yet the render reports success; HTML tags in values pass through unescaped. | `probe_g.py` | open |
| EC-040 | drafter | drop | S1 | Capture's output cap (12,000 tokens) is hit on ordinary briefs (employer 8.6k chars, OMV 19k chars): the capture is cut off, every client field comes out empty, and the job still ends "done" with one gap line. Health 34 and 0 vs the engine's 83 and 75. | drafter on employer-awareness / omv-btl-brief, 2026-10-02 | open |
| EC-041 | engine | drop | S1 | (fixed 2026-10-03, ADR 0018: 11/11 on 7 briefs) Fields left empty (REQ-01): budget_scope when the client gives no budget (mamaliga, friskies); desired_response when every draft fails a gate (mamaliga, OMV); competitor_context (employer, every Samaritans run). | test-five checkpoint_test5_routes55 | open |
| EC-042 | engine | drop | S1 | Image-only PDF pages inside an otherwise text PDF are never transcribed (vision runs only when the whole text layer is thin): a client tracker's current-wave charts and a research page were unread, so older "average" figures were used instead of the current ones. | Samaritans tracker PDF, 2026-10-02 | fixed (engine reading; app path: EC-047) |
| EC-043 | engine | drop | S1 | The Loop-1 capture reads success metrics, scoring, constraints, but the client brief renders only the golden extraction (no slot for them): captured content is lost at render (`brief_render.py:110`). | Samaritans, `loop1_capture.fields.success_metrics` | fixed in the engine's client brief ("Also in the client's documents"); the app's mapping still needs it (Shrey) |
| EC-044 | engine | fragile | S2 | Identifiers (phone numbers like "116 123") count as uncited research figures and fail the citation gate, emptying the field (desired response). | Samaritans | fixed (unit `code` facts are identifiers: not checked as figures, attached automatically) |
| EC-045 | engine | slow | S2 | Loop-1 capture on Opus over a 228k-char input is the critical path: 334 s, $1.78 of $3.41, 29k output tokens, and its output only feeds the ledger and open questions. | Samaritans, 2026-10-02 | resolved 2026-10-03: read whole now 169 s, $2.25, 100% coverage (Word reader no longer repeats merged cells; Opus 5.5 extraction); chunking only above 600k chars |
| EC-046 | engine | gap | S2 | The golden schema has 11 fields; `modules` are never read; 16 of 27 sections of a senior agency brief have no slot (reframe, barrier, persona, objection, channel roles, safety, legal, measurement, evidence appendix, requirement trace...). | ref_check vs the Headcase reference | open |
| EC-047 | wiring | drop | S1 | In the app, attachments reach the engine as text the host extracted (`human/assets/.extracted/`), so the engine's page-image reading (EC-042 fix) never sees chart or scanned pages there. The host or the middleware must send page images for image-heavy pages (or the original file). | Samaritans tracker attached through the app | open |
| EC-048 | engine | fragile | S2 | `RAG_INDEX` is resolved from `engine/rag`, but the RUNBOOK's CLI example set `RAG_INDEX=rag/_index_v4` (from `engine/`): the store was "unavailable" and Loops 3-7 fell back to pack digests with only a log line, so every CLI test run of 2026-10-03 (steps 3-4, gap-filler) had no precedent retrieval. Comparisons inside those tests stay fair (both arms the same). RUNBOOK fixed. | `RAG_INDEX=rag/_index_v4 napkin-brief ...` from engine/ | fixed: meta.degraded + CLI 'DEGRADED BRIEF'; BRIEF_REQUIRE_STORE=1 stops the run; without RAG_INDEX the local store now uses rag/_index_v4 |
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
