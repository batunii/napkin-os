# Making the briefing tool better with BetterBriefs

Synthesis of the four BetterBriefs PDFs in this folder (best-practice guide,
booklet, "the issues with briefs", Better Ideas global report) mapped onto
concrete changes to `parse_brief.py`. The PDFs are the quality bar Laurence
set for the brief system (Slack, #wiki, 2026-06-05).

## The core insight

The tool's current checks are **presence checks** (is there an objective? an
audience? a budget?). BetterBriefs says the dominant failure mode is
**present-but-vague**: 78% of marketers think their briefs give clear
strategic direction, only 5% of agencies agree. 65% of agencies can't picture
the target audience from the brief. 88% aren't clear how the work will be
evaluated. So the upgrade is from "did the client fill the slot?" to "does
what they wrote pass the quality test for that slot?"

## 1. Schema additions (brief_object.schema.json + EXTRACTION_SYSTEM)

| New field | Why (per BetterBriefs) |
|---|---|
| `key_message` (+ `proof_points[]`) | A good brief has ONE single-minded message backed by proof points. Currently not captured at all. |
| `evaluation_criteria` (first-class, not buried in how_to_win) | "On brief" is the #2 evaluation criterion industry-wide; only 30% of clients provide criteria; 88% of agencies unclear how work is judged. The brief should be the evaluation tool. |
| `strategic_angle` | "Beginnings of a strategic angle" is one of the 3-4 essentials of a brief (problem, timed objective, angle, budget). |
| `anti_target` | Good targeting states who it's NOT for ("two thirds of brands cannot communicate who they are for and who they are not for" — Ritson). |
| objective subtype tag: `commercial / behavioural / attitudinal` | The three types must coexist and link. Lets the review check the chain: attitude shift → behaviour change → commercial outcome. |

## 2. Per-field quality tests (new REVIEW step — "BetterBriefs scorecard")

Score each captured brief against the rubric, not just for gaps. Run as an
LLM judge with this checklist (heuristic fallback: regex/count checks):

- **Objectives**: ≤ a handful? Hierarchical (commercial at top)? Benchmarked,
  realistic, time-stamped? All three types present and linked, or wishful?
  ("Objectives are the most critical yet most poorly defined element.")
- **Audience**: vivid (demographics + psychographics + needs)? Cliché
  detector — flag "millennials", "everyone", "adults 18–65", pasted segment
  labels. Is anti-target stated? Big enough for the objectives?
- **Single-mindedness**: ONE key message / ONE strategy? If the brief bundles
  mutually exclusive strategies (acquisition vs upsell vs frequency) or
  multiple exercises, flag **"split into N briefs"** — one brief = one
  strategy. (One of our BTL samples is literally 3 exercises = 3 briefs.)
- **Interlock**: budget ↔ objectives ↔ audience must be mutually feasible;
  flag mismatch (e.g. mass-market awareness objective on activation-sized
  budget).
- **Evaluation criteria**: stated? agreed? If absent, auto-generate the open
  question with Orlando Wood's three tests (connect to business outcomes /
  give oxygen to the idea / build mental availability or fame).
- **Language**: jargon/category-speak/flowery flag; length flag (54% of
  agencies say briefs are too long; everything extra → appendix).
- **Problem**: is it a business problem communications can actually solve,
  stated as a choice (what we will NOT do), not a shopping list?

Output: a scored scorecard section in `structured_brief.md` (per dimension:
pass / vague / missing + evidence quote). This directly feeds Laurence's
"Client Briefs Sample — Quality Ratings" work.

## 3. Loop 2 (first-round agency brief) shape changes

Current shape: problem / objective / audience / scope. Add:

- **Why this brief exists** ("define the need for advertising" — the compass)
- **Key message + proof points** (single-minded)
- **Evaluation criteria** (proposed if client gave none — to be agreed, since
  criteria should be co-created, and "walking into a creative presentation
  with different checklists in our heads" is the chaos to prevent)
- **What we are NOT doing** (strategy is sacrifice — Porter)

## 4. Open-question prioritisation (data-backed)

Rank generated open questions by the industry-criticality the reports give:
1. Objectives (61% of marketers / 71% of agencies: most critical element)
2. Evaluation criteria (only 30% of clients have them; #2 quality proxy)
3. Audience vividness (65% of agencies can't picture the target)
4. Single key message / strategic angle
5. Budget–objective interlock

`shape_loop2`'s CORE_FIELDS questions should carry these stats in
`why_it_matters` — that's persuasive ammunition when the questions go back to
the client.

## 5. Prerequisites from the 13-brief test run (2026-06-10)

Quality grading only works if extraction is reliable:
- Fix the ~40% NIM runs that fall back to heuristic ("LLM did not return
  clean JSON") — likely fences/reasoning preamble around the JSON; strip
  more aggressively or use guided/JSON mode.
- Loosen the no-loss ledger quote matching (near-verbatim substring is too
  strict; coverage capped ~49% even on good runs).

## Quick wins, in order

1. Add `key_message`, `evaluation_criteria`, `strategic_angle`, `anti_target`
   + objective subtypes to EXTRACTION_SYSTEM and the schema.
2. Add the BetterBriefs scorecard as a third REVIEW step with the rubric
   above; render it in `structured_brief.md`.
3. Add the "split into N briefs" single-mindedness detector.
4. Reword open questions with the stats; add Wood's three evaluation tests.
5. Fix JSON-mode robustness + ledger matching so the scorecard sits on
   reliable extraction.
