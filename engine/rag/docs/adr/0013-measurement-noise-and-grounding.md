# ADR 0013 — Every checkpoint report says what is noise and what is invented

Status: accepted (Sai, 2026-09-28; phase A item 5, reduced form) · Findings: BW9,
critic-G3 (partly) · Code: `engine/rag/checkpoint_run.py` (`load_sets`,
`resolve_briefs`, `noise_estimate`, `preflight`, `score_row`, `ground_row`, `render`),
`engine/rag/grounding.py`, `engine/rag/jev_checks.py` (`claims_supported`),
`engine/rag/eval_checkpoints.json` (`repeat_of`) · Tests:
`engine/rag/test_checkpoint_tools.py`.

## Context

Three things made checkpoint results hard to read:

1. **No noise line.** Reports said "treat differences under ~13 points as noise", a number
   nobody had measured. Two checkpoints in the registry changed nothing a brief can see
   (9b06017 left the evidence identical; 2d9370c was a refactor), so their per-brief
   differences from the checkpoint before them are pure run-to-run noise: -12, +10, -1,
   -7, +7, -5. The spread of a two-run difference is 7.8, so a single brief moves about
   ±16 with no change at all.
2. **Health rewards invention.** An empty field costs health; a field filled with facts the
   client never gave mostly does not. On bord-gais (2026-09-28) ragAdded scored 66 against
   the current code's 45, but its SMP and all four reasons to believe rested on a
   smart-meter rollout the document never mentions.
3. **The runner could waste a run.** The critic runs in the runner's own process; without
   a Claude transport there it failed only after the briefs were written, stamped the
   brief as graded with no score, never retried, and the totals printed 0.

## Decisions

- **Brief lists as data.** `--set NAME` reads `golden/labels/client/eval_sets.json`
  (git-ignored, since brief names are client material). `--briefs` still works; the
  default is the three test briefs. Widening the set is an edit to that file.
- **Noise line from repeat pairs.** A registry entry with `repeat_of` marks a checkpoint
  whose brief behaviour did not change from the named one. `noise_estimate` pools their
  per-brief differences; the line is 2 x their spread (per brief), and x sqrt(n) for a
  sum over n briefs. Each difference from the first arm is marked real or noise. The pairs
  were scored by the old one-sample critic, so the line is an upper bound for Fable x3.
  Running every brief twice would measure it directly but doubles every checkpoint; Sai
  chose not to (token cost).
- **Grounding count beside health.** `grounding.check` asks jev whether each reason to
  believe is supported by the client brief; not supported at p >= 0.9 counts as invented. "TO CONFIRM" lines are the brief asking for evidence and are counted
  apart (never sent to jev). The SMP, insight and desired response are meant to go beyond
  the document and are not counted: the first trial counted SMPs too, and jev called three
  purely creative SMPs "not in brief". One jev request per brief, no Claude calls. Trial
  (reasons to believe only): bord-gais ragAdded 4 of 4 invented, current 0 of 5; friskies 4
  of 4 against 0 of 3 (+2 TO CONFIRM); mamaliga 0 and 0; the four random briefs 5 against 0 in total.
- **The runner checks before it spends.** `preflight` gives the critic the arms' transport
  (CLI unless set) and stops at once when Claude cannot be reached. A grading with no
  answer is tried once more after 30 s and is never stamped as graded; an ungraded brief
  reads "not scored" and is left out of the totals, which say how many briefs they cover.

## Consequences

- No extra Claude tokens in a normal run; a failed grading costs one retry, a missing login
  costs nothing.
- The noise line tightens as repeat pairs are added to the registry (mark any
  behaviour-neutral commit's checkpoint with `repeat_of`).
- The grounding count is jev's judgement, unmeasured against people; it is a flag beside
  health, not a gate. The CD/planner labels would calibrate it.
