# ADR 0021: The evidence appendix and the fact-check

- **Status:** accepted (Sai, 2026-10-03: "go ahead with P2").
- **Code:**
  - `engine/evidence.py`;
  - `engine/research_facts.py` (`status_of`, line tags, `load_clan` resolving source ids);
  - `engine/parse_brief.py` (the step after the proposals; the writers' research header);
  - `engine/brief_render.py`;
  - `engine/agent-server/mapping.py` (`engine_meta` carries `evidence` and `fact_check`).
- **Switch:** `BRIEF_FACT_CHECK`, on by default.
- **Tests:** `engine/rag/test_evidence.py`.

## Context

A senior agency's brief, the one held as the reference, traces every number to a source in an evidence
appendix, marks what is unverified, and says what it will not claim. Ours did none of this:

- **Every research fact reached the writers as "VERIFIED RESEARCH FACTS".** In one research CLAN, 88 of
  94 facts were low confidence and single-source (EC-012).
- **Source ids never resolved to titles.**
- **The client brief showed no sources,** so the "provenance" test of the 14-point check failed on every
  brief.
- **The reference flagged 4 claims in our brief that were stated as fact on single sources.**

## Decisions

1. **Every fact gets an honest status.**
   - **Verified:** high confidence, status verified, or a synthesis row (a finding a person verified,
     ADR 0017).
   - **Unverified:** everything else.
   - The writers see "RESEARCH FACTS, each marked verified or unverified", with the status on every
     line, and are told to word an unverified fact "as reported, never as settled". They no longer see
     "VERIFIED RESEARCH FACTS".
2. **`load_clan` resolves source ids to title, publisher, link, date and tier** from the CLAN's sources.
3. **The appendix is code, not a model.**
   - The facts the fields cite (`fact_refs`, the research and what the gap-filler found) are numbered
     A1, A2… in the order the brief cites them.
   - Each shows its value, source, date, status and link.
   - Fields and list items carry `[A#]` marks.
4. **One fact-check call** on the judge route (Sonnet, never the writers' Opus) lists every factual claim
   in the brief's fields. For each it says where support comes from:
   - the client's documents (the Loop-1 capture, with sentence numbers);
   - verified research;
   - unverified research;
   - nothing.

   Proposals are left out, because they are labelled already (ADR 0018).
5. **What the client brief shows.**
   - "Still to verify" lists the claims that rest on unverified research.
   - "What we will not claim" lists the unsupported ones, each also raised as a high-priority open
     question. The claim is not deleted from the field, because the person deciding is the planner.
6. **A check that cannot run is recorded** as `fact_check.status = did_not_run`, and the brief goes on.

## Measured

2026-10-03, 5 briefs from 5 clients: mamaliga, betfair and employer (no research), Barry's Tea and
Oatly (research). Samaritans was left out.

| Brief | Appendix | Claims: client / unverified research / unsupported | Provenance test (14-point) | Time / cost, vs before P2 |
|---|---|---|---|---|
| mamaliga | 2 | 22 / 0 / 0 | fail | 182 s / $0.58 (164 s / $0.47) |
| betfair | 0 | 26 / 0 / 1 | **pass** | 122 s / $0.47 |
| employer | 2 | 16 / 0 / 3 | **pass** | 190 s / $0.59 |
| Barry's Tea | 15 | 4 / 3 / 2 | **pass** | 220 s / $0.53 (212 s / $0.56) |
| Oatly | 12 | 4 / 8 / 1 | **pass** | 205 s / $0.66 (176 s / $0.61) |

- **Provenance passes on 4 of 5.** Before P2 it failed on every brief.
- **Blind head-to-head against before P2:** Oatly won (the judge cited "an evidence list and a 'will
  not claim' section"); mamaliga and Barry's Tea lost on their insights, which are rewritten every run.
  The 14-point scores also move both ways. Both are read as run-to-run noise.
- **A flaw found and fixed:** the first check listed insights as unsupported claims; Barry's Tea's own
  insight landed under "What we will not claim". The check now leaves the argument (insight,
  proposition, desired response) out, apart from figures or named facts inside it.
- **Re-checking the 5 finished briefs with the fix** ($0.15): argument lines dropped to 2 in total, both
  factual statements. What stays flagged is genuinely factual: a government scheme and a social handle
  not in the employer's documents, and a human decision cited as proof (decisions are not evidence,
  ADR 0015).
- **Process note:** a duplicate test run (my mistake) cost about $5 more; the files were re-scored from
  the final run.
