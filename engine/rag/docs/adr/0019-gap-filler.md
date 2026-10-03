# ADR 0019: The gap-filler: stored facts first, then the web

- **Status:** accepted (Sai, 2026-10-03).
- **Code:**
  - `engine/gap_filler.py`;
  - `engine/rag/jev_checks.py` (`facts_relevant`);
  - `engine/parse_brief.py` (`_gap_fill_proof`, `gap_context` in `run()`, the proposals step).
- **Switches:** `BRIEF_GAP_FILL` (on), `BRIEF_RESEARCH_URL` (the web tier; off when unset), and
  `BRIEF_FACT_STORE`.
- **Tests:** `engine/rag/test_gap_filler.py`.
- **Builds on:** ADR 0014 (research facts) and ADR 0018 (every field filled).

## Context

ADR 0018 fills every field, but its proposals rest on category norms, and the reasons to believe shipped
as a flagged draft on 6 of 7 briefs. No fact given proved the proposition (2026-10-03).

Sai asked for an agent that "searches and fills them up, Shrey's flow had that", and set the order:
"stored facts first then web; right now there are no stored facts but it will be cached."

## Decisions

1. **What counts as a gap.**
   - A field written as a proposal.
   - Reasons to believe that are flagged (failed checks) or empty.
   - A client's own field is never a gap.
2. **One need per gap,** each phrased as a question on one of the research tool's eight lenses (e.g.
   reasons to believe → brands_positioning, "evidence that this is true of <brand>: <proposition>").
   - *Why the lenses:* the research port is built and cached around them.
3. **Tier 1, stored facts.**
   - The pool is the run's own research facts plus the local fact store (one JSON-lines file per
     brand and market).
   - jev decides which facts help with the need (yes/no, kept at p ≥ 0.6, at most 8).
   - *Why jev:* it is a check, not writing, for a fraction of a cent.
   - **With no jev answer, no stored fact is used:** an unchecked fact is not passed off as an answer.
4. **Tier 2, the web, only for needs the store could not answer.**
   - One call per need to the research tool's own port (`napkin.research/1`, `POST /v1/research`).
   - *Why the port:* it is Shrey's search (a Claude agent with web search and fetch, or search-jev),
     it already caches results and checks its quotes, and the engine reuses it through its contract
     instead of copying the code. Production points `BRIEF_RESEARCH_URL` at the deployed service;
     development uses the mock-backend.
   - Needs are searched in parallel (4 at a time), with 4 sources per need.
   - With no port set, or no market known, the web tier does not run, and the report says why.
5. **Every web quote becomes a research fact row.**
   - Its shape: id `g_<hash>`, key `gap.<field>`, the verbatim quote as value, unit text, its source
     title and URL, date, market, confidence low, method web.
   - Writers cite these as `[F:id]`, and the citation and figure checks treat them like any other fact.
   - *Why low confidence:* they are single-source and unreviewed.
6. **Every fact the web finds is written to the store,** so the next brief for the same brand and
   market finds it in tier 1. This is the cache Sai described.
7. **The market** is the commonest `market` among the research facts, else the country named most in
   the brief, else unknown.
8. **Proposals:** the gap-filler runs before the proposals call, which then gets the found facts with
   the research and cites them.
9. **Proof:**
   - The found facts are added to the writer's own fact list, and the reasons-to-believe writer runs
     once more.
   - The new draft is kept only if it passes its checks. Otherwise the flagged draft stays: never a
     worse field.
   - The outcome (`proved`, `not_proved` or `no_facts`) is recorded on the field and in
     `meta.gap_fill`.

## Measured

2026-10-03, 4 briefs from an empty fact store, the web tier through the research port (mock-backend,
the Claude agent with web search), then Barry's Tea run again:

| Brief | Gaps found (store / web facts) | Reasons to believe | 14-point check | Time | Cost, vs ADR 0018 run |
|---|---|---|---|---|---|
| mamaliga (RO) | proof 0/3, budget 0/7 | flagged → **proved** with 3 web facts | 6 → **9** | 193 s (+66) | $0.53 (+0.07) |
| OMV (RO) | competitors 0/0 (the search found nothing) | was fine | 4 → 5 | 158 s (+24) | $0.74 (+0.01) |
| Barry's Tea + research | proof 0/5, competitors 1/0, tone 1/0 | still flagged | 4 → 7 | 205 s (+36) | $0.58 (+0.07) |
| Oatly + research | proof 0/4, tone 7/0 | still flagged | 4 → 4 | 218 s (+62) | $0.67 (+0.09) |
| Barry's Tea again (store warm) | proof 0/5, competitors 2/0, tone 2/0, budget 0/5 | still flagged | 5 | 301 s | $0.66 |

- **Blind Fable head-to-head against the ADR 0018 runs:** Barry's Tea won, Oatly lost (both in both
  orders). The lost one was decided on its insight, which the gap-filler does not touch: run-to-run
  writing difference.
- **Web searches:** 7 port calls at about $0.09-0.16 and 19-27 s each, inside the cost above.
- **Why the proof stayed flagged on Barry's Tea and Oatly:**
  1. A bug: `from_clan` (ADR 0017) put the finding id into `about`, and the decision figure check
     read its digits ("fi_43HX2GVBYFRI" gave 43 and 2), so honest drafts with small numbers failed.
     Fixed here: the id moved to `finding`, and `research_decisions` now ignores id-like tokens.
     Re-checked offline, the Oatly draft goes from 1 such failure to 0.
  2. The judge rejected 'TO CONFIRM' placeholders as reasons to believe. That is a writing-rule issue
     for the proof writer, still open.
- **The cache works but is narrow:** on the second Barry's Tea run the stored facts answered
  competitors and tone. The proof need is phrased around each run's own proposition, so last run's
  proof facts were judged irrelevant and the web was searched again.
- **A second bug:** with no brand known, facts were filed under "unknown". Fixed: the brief's own
  name is used, or nothing is stored.

## Open

- **Point at the real fact store:** Shrey's knowledge-layers database and the AWS store, instead of the
  local file store, when they serve facts by brand and market.
- **Fact-level review:** facts found on the web are unreviewed. They should reach the research tool so a
  person can verify or reject them (ADR 0017).
- **The app path:** the middleware would need the same port URL configured for the engine.
