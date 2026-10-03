# ADR 0018: Every field filled (REQ-01)

- **Status:** accepted (Sai, 2026-10-02 rule; built 2026-10-03).
- **Code:**
  - `engine/parse_brief.py` (`_fill_empty_generated`, `FILL_REPAIR_NOTE`, `propose_missing`,
    `PROPOSABLE`, `PROPOSE_SYSTEM`; `_one(repair_note=)`);
  - `engine/brief_render.py` (the "Proposed" label).
- **Switch:** `BRIEF_FILL_ALL`, 1 by default.
- **Tests:** `engine/rag/test_fill_all.py`.
- **Supersedes, in part:** ADR 0009, which said "no invention at the source": a field nobody could fill
  stayed empty with an open question.

## Context

Sai, 2026-10-02: "filling all fields is something we cannot compromise." An empty field leaves the
brief unusable for the agency, and the engine left 1-3 of the 11 golden fields empty on most briefs.

There were two causes, measured on 2026-10-03 on the Barry's Tea, Oatly and Samaritans briefs:
- **The client never gave it.** Competitor context and tone came out empty on all three.
- **Every draft of a generated field failed a check.** Reasons to believe were emptied on 2 of 3 by the
  citation checks: a research figure written without its `[F:id]`, and a number taken from a
  decision line.

Sai chose both rules the same day:
1. **A labelled proposal** for anything the client never gave.
2. **One repair pass for a failed field, then the best draft flagged.** Never blank.

## Decisions

1. **Generated fields (insight, SMP, reasons to believe, desired response) get one repair, then the
   best draft flagged.**
   - The writer runs again with `FILL_REPAIR_NOTE`, which states why the last drafts failed and the
     rules they broke. The repaired draft goes through the same judge and gate as the first pass.
   - If that fails too, the best rejected draft ships with `review.status = failed_checks`, the checks
     it failed, and an open question. The client brief already shows such a field as "Draft — to
     review: it failed …".
   - *Why one repair:* it is Sai's choice. A loop of repairs costs time (a top priority) for a falling
     chance of success, as the coherence check showed on 2026-10-02.
   - **Shipping a flagged draft that fails the figure checks is deliberate.** The flag and the open
     question make the failure visible, which is the rule's point.
2. **A client's own line is never touched.**
   - A `client_stated` value stays as written. Where it breaks a rule, the existing open question
     still says so.
3. **Every other field the client never gave gets a labelled proposal, from ONE call.**
   - The fields: background, objectives, audience, budget & scope, competitor context, tone & world,
     mandatories.
   - It runs after the generated fields, so the hero writers still read only the client's brief and the
     research, never the agency's own guesses.
   - It uses the `grounded_writer` route (Opus 5.5): it invented 0 of 8 figures where Opus 4.6 invented
     11 (ADR 0011).
   - It reads the brief, the fields already filled and up to 60 research fact lines.
4. **What a proposal looks like.**
   - It is written as `source: inferred`, `method: proposal`, `proposed: true`, plus `basis` and
     `confirm`. It is not a new `source` value, so the critic and the app's schema stay valid.
   - The client brief prints "Proposed (not given by the client), based on <basis>. To confirm:
     <question>" under the field's heading.
   - Every proposal raises a "To confirm" open question.
5. **Research figures in proposals carry `[F:id]` and move to `fact_refs`.** A budget is proposed as
   a range with its basis, or asked as a question; the agency never invents a figure as fact.
6. **A failed proposal call never fails the brief.** The fields stay empty, `proposal_error` is
   recorded, and the old open questions stand.
7. **The gap-filler builds on this.** The search agent (stored facts first, then the web) will give
   proposals sourced facts instead of category norms. It is the next step.

## Measured

2026-10-03, through the CLI, on 7 briefs from 6 clients: mamaliga, employer, friskies, betfair, OMV,
plus Barry's Tea and Oatly with research (Samaritans deliberately left out, so the result does not rest
on one client):

| Brief | Fields filled | Proposals | Repairs | Time | Cost |
|---|---|---|---|---|---|
| mamaliga | **11/11** | budget & scope | – | 127 s | $0.47 |
| employer | **11/11** | competitor context | – | 119 s | $0.52 |
| friskies | **11/11** | budget & scope | – | 119 s | $0.48 |
| betfair | **11/11** | – | desired response repaired | 95 s | $0.40 |
| OMV | **11/11** | competitor context | desired response repaired | 134 s | $0.73 |
| Barry's Tea + research | **11/11** | budget, competitors, tone | reasons to believe shipped flagged | 168 s | $0.51 |
| Oatly + research | **11/11** | tone | reasons to believe shipped flagged | 155 s | $0.58 |

- **Blind Fable 5.1 head-to-head against the same briefs without the fill step** (Barry's Tea and
  Oatly): the fill step won both, in both orders. The judge's words: "clearly labelled proposed
  context", "proposed tone with real distinctive assets".
- **Cost:** about +$0.05-0.10 and +35-50 s on the two research briefs, the ones with the most gaps.
- **Weak spot:** reasons to believe ship as a flagged draft on 6 of 7 briefs, usually because no given
  fact proves the proposition. That is the gap-filler's job.

## Open

- The gap-filler (sources for proposals).
- A field-level proposal for *parts* of objectives (today the whole field must be empty).
- The app's mapping must carry `proposed`, `basis` and `confirm` (Shrey).
