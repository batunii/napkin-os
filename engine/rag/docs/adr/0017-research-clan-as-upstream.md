# ADR 0017: A research CLAN as the brief's upstream (facts, decisions, brand)

- **Status:** accepted (Sai, 2026-10-03).
- **Code:**
  - `engine/research_decisions.py` (`from_clan`, `_clan_file`, `_date`);
  - `engine/research_facts.py` (`load_clan`);
  - `engine/parse_brief.py` (CLI `--research`).
- **Tests:** `engine/rag/test_research_decisions.py` (from_clan, load_clan).
- **Builds on:** ADR 0014 (research facts) and ADR 0015 (research decisions).

## Context

ADR 0015 gave the writers a place for what people decided about the research: a finding rejected or
verified, and why. It expects rows of the form `{id, kind, who, role, about, statement, reason,
as_of, status}`.

The research tool does not write such rows. A research CLAN keeps each person's verdict in two places:

- on the finding itself: `shared/findings.yaml`, with `status: verified | rejected` and a
  `verification` or `rejection` block holding when, who, the decision id and, for a rejection, the
  reason;
- in `agent/decision-chain.yaml`: `verify_finding` and `reject_finding` entries, with the reviewer's
  rationale.

Nothing converted one into the other, so on the Samaritans tender (3 verified and 2 rejected findings)
no decision ever reached the writers. A writer could still state a finding a person had rejected.

There was a second gap: the CLI had no way to pass research at all (EC-006). Facts reached the engine
only through Python `run(upstream=...)`, and the research CLAN's facts file wraps its rows in a
`facts:` key, which silently gave "1 skipped, 0 used" when unwrapped wrongly (RUNBOOK trap).

## Decisions

1. **One row per finding a person decided on: verified or rejected.**
   - *Why:* those are the human verdicts.
   - *Rejected alternative:* also passing proposed findings as "unreviewed" rows. Nobody decided on
     them, and ADR 0015 is about decisions, not findings. The facts that support them already reach
     the writers through ADR 0014.
2. **The finding's own status and block are the source of truth; the decision chain only fills in
   a missing reason.**
   - *Why:* the finding records its current state. The chain is history, and could hold a verify
     later undone by a reject.
   - *Rejected alternative:* replaying the chain. It is more complex, and adds nothing while the
     finding carries its final status.
3. **The row id is the CLAN's decision id** (`d_...`), so a writer's `[D:id]` points at the record a
   person can open. If the block has no decision id, `d_<finding id>` is used.
4. **`who` is "a reviewer".**
   - *Why:* the CLAN stores reviewers as user ids (`human:<uuid>`), which mean nothing to a writer or
     a reader, and a name must not be invented.
   - *To revisit* when the research tool exports display names.
5. **The statement says what was decided, then the finding's words:** "rejected this finding: ...".
   - *Why:* `research_decisions.line()` renders `<who> (<role>) <statement>`, so the verb comes first.
   - **The finding's figures do not become allowed figures.** ADR 0015 already fails a draft that
     takes a figure from a decision line.
6. **`about` names the lens and the finding id** (e.g. "the positioning finding fi_B"), so a person
   can trace the row back. `as_of` is the date of the verdict.
7. **A rejected finding does not withdraw the facts it cites.**
   - *Why:* a person rejected the finding (the inference), not the facts. In the Samaritans case
     they were rejected as "irrelevant". The decision line tells the writers not to use the claim,
     and the facts stay usable on their own merits.
   - *To revisit* if reviewers start rejecting findings because the facts behind them are wrong. That
     needs a fact-level verdict in the research tool.
8. **`load_clan` reads facts, decisions and brand from one CLAN, in any of the three forms the
   research tool writes:** a `.clan` zip, an unzipped folder, or a dev run's single `clan.json`
   (`research_decisions.clan_parts`).
   - *Why:* the shipped tool writes CLANs, but most research runs on disk are `clan.json`, and the
     brief maker must not be tested on one client's file alone (Sai, 2026-10-03).
   - It unwraps `facts:`, turns YAML dates into strings, and takes the brand name from
     `shared/data.yaml` `campaign.brand`.
   - It does not pass the CLAN's category: it is a research-taxonomy code (e.g.
     `public.charities_ngos`), not one of the engine's categories, so jev keeps picking the category
     from the brief. A taxonomy-to-enum mapping is open (EC-013).
9. **The CLI takes `--research CLAN`** (a `.clan` zip or an unzipped folder). It prints what it
   loaded (facts, decisions, brand), so a wrong file is visible at once.
   - *Why:* a planner can run a brief with research without writing Python, and the runbook's
     Python-only path is no longer needed.
10. **Unchanged** (ADR 0014 and 0015 hold): without `--research` a run is exactly as before.
    Decisions reach only the hero writers, never the capture, extraction or scorecard. Every row
    goes through `current()` with its caps (40 rows, 300 characters per part).

## Measured

Briefs run through the CLI with `--research`, on 2026-10-03:

| Research | Facts | Decisions given / used | Fields filled | Time |
|---|---|---|---|---|
| Barry's Tea `clan.json` (copy with 1 rejected + 1 verified, test decisions) | 37 | 2 / 2 | 8/11 | 121 s |
| Oatly `clan.json` (copy with 1 rejected + 1 verified, test decisions) | 72 | 2 / 2 | 9/11 | 118 s |
| Samaritans `.clan` (real reviews: 2 rejected, 3 verified) | 94 | 5 / 5 | 10/11 | 309 s |

- None of the rejected findings' claims appears in its client brief. They did not appear in the
  earlier runs without decisions either, so these runs show the rows arrive and nothing breaks.
  They do not prove the rows changed what was written.
- No writer cited a `[D:id]` (citing is optional).
- The empty fields are REQ-01's next step.
- IBM's research loads too (149 facts, brand, no decisions).

## Open

- **Display names** for reviewers (research tool).
- **A fact-level verdict** (research tool).
- **The category mapping** (EC-013).
- **The app path:** the middleware already sends decision rows built by `clan-sdk extract::upstream`
  for spun-off documents. That path and this one should produce the same rows. Check with Shrey.
