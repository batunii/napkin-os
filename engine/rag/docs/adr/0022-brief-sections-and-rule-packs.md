# ADR 0022: The brief's sections beyond the 11 fields, and the rule packs

- **Status:** accepted (Sai, 2026-10-03: "get them all done, only commit if the tests are satisfying").
- **Code:**
  - `engine/sections.py` (the seven section writers and their code checks);
  - `engine/rule_packs.py` and `engine/rule_packs/*.yaml` (the rules, each with its primary source);
  - `engine/parse_brief.py` (the step after the proposals, before the fact-check);
  - `engine/evidence.py` (the fact-check and the appendix read the sections too);
  - `engine/brief_render.py` (`section_lines`, the placement in the client brief);
  - `engine/agent-server/mapping.py` (`reply.meta.sections`);
  - `engine/brief_llm.py` (route `section`: Sonnet 5.5, medium effort, Opus 5.5 fallback).
- **Switches:** `BRIEF_SECTIONS` (on by default), `BRIEF_RULE_PACKS_UNREVIEWED` (off by default).
- **Tests:** `engine/rag/test_sections.py`.

## Context

The senior agency's reference brief for the Samaritans tender has 27 sections. After P2 the engine's brief
covered about 16 of them: the 11 fields, the evidence appendix, the open questions, what the client's
documents also say. What was missing is what a creative team and a producer work from, around the strategy:

- how success is measured, with baselines and targets;
- the person, the moments to meet them, what stops them, where to reach them;
- what each channel does;
- the safety and legal lines;
- the rivals and the real alternatives;
- the languages and versions;
- the practical facts: deliverables, dates, budget, sign-off, how the work is judged.

The grader also failed "objectives not measurable" on almost every brief (2026-10-03 results).

## Decisions

1. **Seven sections, each its own writer, all in parallel** (route `section`: Sonnet 5.5 at medium effort).
   - *Why:* one call per section keeps each writer on one job, and in parallel the step takes about one
     call's time (15-20 s measured). Sonnet, not Opus: these sections organise and apply facts, the hero
     writers' hard thinking is already done.
   - *Rejected:* one call for all seven (slower, one long reply, and one failure loses every section).
2. **The writers read the capture, not the raw text.** The capture is the engine's full record of the client's
   documents (its coverage is checked in Loop 1), with sentence numbers; plus the fields as written and the
   research facts. Seven calls on a 228k-character tender would otherwise cost seven reads of it.
3. **Every item says where it comes from:** `client` (sentence numbers), `research` (a fact id), `pack` (a
   rule id) or `proposed` (the agency's suggestion). Every section is filled (REQ-01): what the documents do
   not give is a short proposed item, labelled in the brief.
4. **Code checks, not trust:**
   - a ref that does not exist is dropped;
   - a `research` item with no fact becomes `proposed`;
   - a `pack` item with no given rule is **dropped**: the brief never states a law the packs do not hold;
   - a figure no source states (the capture, the fields, the facts and their keys, the rules) is flagged
     "figure to check" with an open question.
5. **The sections' claims go through the same fact-check as the fields** (ADR 0021), and the facts they cite
   join the evidence appendix with [A#] marks.
6. **Rule packs are files of rules, each with its primary source** (title, publisher, url, section, verbatim
   quote, date checked), by market (IE, GB, RO), category (alcohol, gambling, food high in fat, salt or sugar,
   charity fundraising) and topic (suicide and self-harm). They are picked by the brief's markets (every
   country it names, most named first), its category and its text.
   - **A pack is used only after a person has checked it** (`reviewed: true`). Trials set
     `BRIEF_RULE_PACKS_UNREVIEWED=1`, and the brief then says the rules are unreviewed.
   - *Why:* a legal line in a client brief must be right; a model's memory of a code is not a source
     (Sai's rule: domain claims need primary-source cites).
   - Rules are by market, category and topic, never by client.
7. **Placement follows an agency brief:** measurement after the objectives; the person after the audience;
   competitors after the competitor context; channel roles after tone; safety and legal, language and
   adaptation, and practicalities after the mandatories. What a section already says is not repeated under
   "Also in the client's documents".
8. **The app gets the sections** in `reply.meta.sections` (order, sections, meta).

9. **Further rules from the test rounds (2026-10-03 to 2026-10-06):**
   - **Strict labels:** an item is `client`, `research` or `pack` only when it adds nothing to its source. A
     role, method, cadence, reading or judgement makes it `proposed`.
   - **Two waves:** practicalities, safety and legal, competitors and the person are written first; measurement,
     channel roles and language read them and must not contradict them (round 1 had a channel plan ignoring a
     TV ban, and a frequency claim on a production-only budget).
   - **Channel roles state a job, never an execution:** no scenes, films or lines (round 1 failed the job
     check's "no prescribed solution" test on two more briefs).
   - **The fact-check reads the rules** (a new verdict, `pack`), so a correct legal line is never called
     unsupported. It also checks proposals for the facts they state. An item it rejects shows "no source: to
     confirm", so a section never states what "What we will not claim" rules out.
   - **A fact line names its market**, so a UK figure is not applied to Ireland.
   - **Contact details** (email, web address, handle) only from a source; else "contact detail to check".
   - **Measurement rows keep the client's own measures and targets** as `client`, with "method proposed"
     when the agency adds the method.
   - **A writer's source tags never reach the reader** ('(s6)' leaked into a target cell in round 4).
   - **Years are dates, not claims** for the figure check.

## Measured

Six briefs (Mamaliga RO food, Betfair RO gambling, Employer awareness IE public sector, Barry's Tea and Oatly
one-liners with research, the Samaritans tender with research). The 11 fields were the P2 ones (replayed), so
only the sections differed; one end-to-end Samaritans run checked the wiring. Criteria were written before
any result (`engine/outputs/p3_2026_10_03/CRITERIA.md`, git-ignored, with every round's results). The
section judge (Fable 5.1) was run twice on the final rounds, because on identical briefs it moves about 0.1 on
faithfulness and 1-3 on the unsupported count.

| | Round 3 | Round 4 | **Round 5 (shipped)** |
|---|---|---|---|
| Specific / faithful / useful (of 5, mean of 2 judgings) | 4.02 / 4.20 / 3.55 | 4.07 / 4.42 / 3.45 | **4.11 / 4.25 / 3.56** |
| Unsupported claims per brief (mean of 2) | 6.8 | 5.2 | **5.1** |
| Criteria passed | 8 of 9 | 6 of 9 | **8 of 9** |

- **Round 5 against the brief without sections** (Fable, blind, both orders): won 6 of 6.
- **Round 5 against round 3:** 1 win each, 4 ties.
- **Samaritans against the Headcase reference brief:** 12 to 16 of 27 sections (+4; between +3.5 and +7
  across the rounds, the "before" brief alone scoring 12-13).
- **Job check (14 tests):** 5.0 to 6.2.
- **Cost and time:** the step plus the fact-check, at most $0.45 and 50 s a brief.
- **End-to-end Samaritans (round 4 code):** 7 sections, health 80-85, quality 84-87, 11 of 11 fields,
  $2.85-3.59, 235-425 s (the slow run was an Opus JSON retry in the field stage).

**Not met: unsupported claims <= 2 a brief.** Four briefs are at 2-5. The two over (Oatly 8.5, Betfair 7.5)
rest on unverified single-source research or on facts the judge reads more strictly; the flags show these
to the reader. Shipped on Sai's decision (2026-10-06: "go on" after the recommendation to commit round 5).

## Open

- **Unsupported claims to <= 2 a brief.** Most of the rest is research quality (single-source, unverified,
  markets missing), so the research tool's fact hygiene is the main lever, then a per-item support check.
- **Sai reviews the rule packs** (`engine/rule_packs/SOURCES.md` lists every source and the drafter's doubts:
  Romanian sources weakest, the green-claims transposition, the ASA Code refresh, GB HFSS scope), then sets
  `reviewed: true` per pack.
- More markets and categories as briefs need them.
- The app's rendering of the sections (Shrey, W10).
