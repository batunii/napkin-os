# Brief maker: handoff to Shrey (2026-10-03)

Branch `integrate/jev-hardening`, local commits not pushed yet: Sai pushes after he has checked them.
What changed on the engine side, and what the app, middleware and research tool need so that a brief
made in the app is as good as one made from the CLI. Every item names its row in
`docs/brief-maker/edge-cases.md` or its ADR in `engine/rag/docs/adr/`.

## 1. Decided

- **The engine (`engine/parse_brief.py`) is the brief harness.**
  - Measured 2026-10-02 through the middleware against `server/napkin/brief` (BriefJob) on 7 inputs:
    `engine/outputs/harness_2026_10_02/compare2.md`.
  - The engine filled 10-11 of 11 fields everywhere, was 3-7x faster and 2.5-4x cheaper, and won the
    blind head-to-head once tuned.
  - BriefJob's capture is cut off at 12,000 output tokens on 8-19k-character briefs (EC-040).
- **New engine defaults, all documented in `engine/README.md`:**
  - The hero writer runs on Opus 5.5 at high effort with the sharpen pass, and the hero judge on
    Sonnet 5.5 (ADR 0011 update).
  - Every field is filled: labelled proposals, plus one repair, then flagged (ADR 0018).
  - The gap-filler is on (ADR 0019).
  - Reasons to believe hold proof only (ADR 0020).
  - Image-heavy PDF pages are read by Claude.
  - The coherence check is built but off (ADR 0011 note).

## 2. Wiring the app and middleware need

| # | What | Why | Row / ADR |
|---|---|---|---|
| W1 | Read `reply.meta` from the engine: the agent-server now sends `research_facts`, `research_decisions`, `gap_fill`, `degraded`, `clipped`, `transcribed` and `field_flags` | Your `brief/engine.py` already reads `reply.meta` for research facts and decisions. It never got any, because the agent-server sent fields only; your test stub hid that | EC-020 (engine side fixed) |
| W2 | Show `field_flags` per field: `proposed` + `basis` + `confirm` as "Proposed (not given by the client), based on … To confirm: …"; `review.status == failed_checks` as a draft to review; `proof_needed` as "Proof still needed"; `decision_refs` | A proposal must never look like the client's words. The engine's client brief already renders these | ADR 0018, 0020 |
| W3 | Show what the capture read but no golden field holds ("Also in the client's documents": success measures, how the work is judged, timings, deliverables, constraints, decision makers…) | Captured and then lost (the 5% awareness baseline, the tender scoring). `engine/brief_render.py` `captured_extras()` has the list and order | EC-043 |
| W4 | Send page images for image-heavy PDF pages (or the original file) to the engine, not only the host's extracted text | Chart and scan pages are never read in the app path. The engine reads them from the file (Claude vision) but gets text only | EC-047 |
| W5 | Configure the engine with `BRIEF_RESEARCH_URL` (the research port) and a persistent `BRIEF_FACT_STORE` | Without the URL the gap-filler's web tier is off and says so. The store should become the knowledge layers / AWS fact store | ADR 0019 |
| W6 | Check that the decision rows `clan-sdk extract::upstream` sends match `research_decisions.from_clan` (ADR 0017): the reviewer as `who`, no ids inside `about`/`statement` | Ids with digits inside decision text failed honest drafts as "figures from a decision"; fixed in the engine for both paths, but the shapes should agree | ADR 0017 |
| W7 | Make the fallback to BriefJob loud, or remove it; `regenerate_field` still uses BriefJob's drafters in engine mode | A silent switch gives a different-quality brief | EC-022, EC-023 |
| W8 | Default the `middleware` proxy; the engine takes one draft at a time with a 900 s wait | Wrong service without a proxy; concurrent drafts queue into the fallback | EC-024, EC-025 |
| W9 | The user's only step is uploading the client's document. The agent-server now makes the full brief by default (`BRIEF_LOOPS37=0` for the light one), so the app should not ask for settings, flags or a research file; research arrives on its own when the research tool has run | Sai, 2026-10-03: "all the user has to do is upload the handoff document. keep it that simple" | - |
| W10 | Render `reply.meta.sections` (ADR 0022): seven sections in `order`, each `{title, items: [{label, text or measure/baseline/target/method, source, refs, cite?, figure_unchecked?, detail_unchecked?, unsupported?, method_proposed?}]}`; show `source: proposed` items as proposals and the flags as "to check"; the client brief's placement is in `engine/brief_render.py` | The sections are most of what a creative team and producer work from | ADR 0022 |

## 3. Research tool

- **EC-001.** After the host refuses the research change, synthesis still cites facts the document
  never got. A test pins it as xfail: `server/tests/test_refused_stage.py`.
- **From the Samaritans research sample review (2026-10-02), most useful first:**
  1. Turn the client's own documents (tracker, caller study) into facts.
  2. A barriers / help-seeking lens.
  3. Competitor and alternative facts.
  4. Fact hygiene: duplicates; one market per fact; primary sources (the brand's own site is primary).
  5. Honest confidence or verified status per fact.
  6. Export human decisions as ADR 0015 rows.
  7. Timings and the exact budget on `campaign`.
  8. A category mapping to the engine's list (EC-013).
- **`research-tool-optimisation` is merged into `integrate`.** On `campaign.py`, your never-halt runner
  is kept, with Sai's metrics, the parallel identify call and the raised caps added. The stall-and-fail
  path is dropped.

## 4. How to run and check

- **CLI with research:**
  ```
  napkin-brief brief.docx --attach x.pdf --research Brand.clan
  ```
  Run it from `engine/`, with `RAG_STORE=local` for the local index. One file in gives the full
  brief: `--loops37 --golden` and `RAG_INDEX` are no longer needed (`--quick` gives the light one).
- **Tests:** add `BRIEF_REQUIRE_STORE=1` so a missing store stops the run.
- **Suites on 2026-10-03:** engine 1,111, agent-server 34, server 239 (+1 xfail), mock-backend 88.
- **Plan to reach a senior agency's brief:** `docs/brief-maker/reference-level-plan.md` (phases P0-P5).
  P0 and the gap-filler are done.
