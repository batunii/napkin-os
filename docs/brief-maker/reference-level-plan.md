# Brief maker: reaching a senior agency's brief

Measured 2026-10-02 on the Samaritans tender, against a senior agency's reference brief for the same
job (kept outside git). The reference has 27 sections, and the engine's brief covers 10 of them. The
insight and proposition are already comparable to the reference's. Everything around them is missing:
the reframe, barrier, objection, persona, channel roles, safety and legal lines, measurement against
baselines, the evidence appendix and the requirement trace. The drafter covers 3.5 of 27.

## Root causes, ranked

1. **The schema is too narrow.** It has 11 fields, and its `modules` are never read. 16 of the 27
   sections have no slot (EC-046).
2. **Captured content is lost.** The Loop-1 capture reads metrics, scoring and constraints, but the
   client brief renders only the golden extraction (EC-043).
3. **Image-only PDF pages are never read**, so old figures replace current ones (EC-042).
4. **No strategy stage.** Nothing writes the reframe, barrier, objection, persona, or the insights
   considered and not chosen.
5. **No evidence ledger or fact-check step.** Unverified research is stated as fact.
6. **Research reaches only the hero writers.** Safety and legal facts have nowhere to go, and research
   decisions were not passed.
7. **Gates empty fields instead of repairing them** (EC-011, EC-044; REQ-01).
8. **No requirement extraction or trace.**

## Target pipeline

- **S0. Read everything** (code + Sonnet vision, parallel per page). Transcribe image-only pages and
  embedded images, and keep a read ledger.
- **S1. Requirement extraction and trace** (Sonnet, low effort, parallel chunks over the whole input).
  Rows carry a verbatim quote and a locator. The supersession rule is Q&A > TRD > RFT. This replaces
  the Opus Loop-1 capture (EC-045) and feeds the golden extraction.
- **S2. Evidence ledger and fact-check** (code, then Sonnet with medium effort):
  - client figures, research facts and decisions in one place;
  - a rejected finding blocks its facts, and a proposed one marks them unverified;
  - a claim fact-check (supported / unverified / unfalsifiable);
  - an optional web gap-filler that reuses the research tool's search port.
- **S3. Strategy chain** (Opus, high effort, serial): situation → reframe → barrier → job → feel →
  think-about. Then the existing insight/SMP writers and judge, whose losing candidates become
  "considered and not chosen". Then the objection.
- **S4. Section writers** (Sonnet, medium effort, parallel): persona and audience reach; objectives and
  measurement from the baselines; channel roles; safety and legal from category and market packs;
  language and adaptation; competitors; precedent; practicalities and scoring.
- **S5. Assembly** (code). A section schema of about 20; REQ-01 labels; the trace check (every
  requirement maps to a section).
- **S6. Critic and one repair pass** per failing section, then the best draft is flagged.

## Knowledge to add (house packs, versioned, with sources)

- **Category safety packs:** suicide and self-harm (Samaritans media guidelines for Ireland), then
  gambling, alcohol, HFSS food and financial promotion.
- **Market legal packs:** for IE, the ASAI Code, Coimisiún na Meán codes and Charities Regulator
  statements; then RO.
- **Measurement and channel-role packs.**

## Phases (each tested on the Samaritans tender and one short brief, with cost and time)

Section counts are against the Samaritans reference.

| Phase | Change | Expected | Tender / short, per brief (estimate) |
|---|---|---|---|
| P0 | Vision for image-only pages; render captured metrics and criteria; exempt identifiers from the citation gate; pass research decisions; REQ-01 labels in code | ~13/27 | $3.5, 6 min / $0.4, 1.7 min |
| P1 | S1 replaces the Opus capture; requirement trace | same sections, faster | $2.0, 2.5 min / $0.4, 1.5 min |
| P2 | S2 ledger and fact-check; evidence appendix; "what we will not claim" | ~16/27 | $2.4, 3.2 min / $0.6, 1.8 min |
| P3 | Section schema, S4 writers, IE and suicide packs | ~22/27 | $3.1, 4 min / $1.0, 2.2 min |
| P4 | S3 strategy chain | ~25/27; job check ≥10/14 | $3.9, 5 min / $1.3, 2.5 min |
| P5 | Reference-style critic and repair; more references | stable across references | $4.3, 5.5 min / $1.5, 2.7 min |

## Evaluation

- Section coverage against a section contract.
- Content agreement where a reference exists.
- Citation accuracy in code.
- Trace completeness.
- The 14-test job check and the critic's health.
- Repeats read against the noise line.

To avoid fitting one reference, build 2-3 more references for test-five briefs (an engine run, then a
long agentic run, then a planner's merge) and keep one back as a test set.
