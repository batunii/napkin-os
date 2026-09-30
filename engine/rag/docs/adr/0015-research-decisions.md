# ADR 0015 — What people decided on the research, in the brief

Status: proposed (added by Shrey's side, 2026-09-30, for Sai to review) · Code:
`engine/research_decisions.py`, `engine/parse_brief.py` (`run(upstream={"decisions": ...})`,
`fill_derivable_fields(research_decisions=...)`), `engine/agent-server/server.py`
(`payload.upstream`) · Tests: `engine/rag/test_research_decisions.py`.

## Context

ADR 0014 brought the research's verified facts to the brief's writers. A research document
also carries a human side: a person rejected a finding or verified it, settled a contest or
left it open, edited a line, gave a verdict, and the client reviewed it, each with a reason in
their own words. The research tool records these decisions; the brief tool could not take
them, so a writer could lean on a finding a person had rejected, or state one side of a
contest nobody had settled. The owner asked for "the change to ingest the human side as well".
The app server also passed no upstream at all to `parse_brief.run()`.

## Decision

The decisions are a second optional upstream input beside the facts, for the writers only.
They are what people said about the research, not research and not the client's brief: they
never count as a source of figures and never enter anything that reads the client's brief as
the client's words. Sai's rule for everything from the level above holds (ADR 0014): it may
only make the brief better, and a run without it is exactly as before.

## What is built

- `upstream["decisions"]` takes rows `{id, kind, who, role, about, statement, reason, as_of,
  status}`; kind is one of rejected_finding, verified_finding, resolved_contest, open_contest,
  edit, verdict, client_review; role is person or client; as_of is YYYY-MM-DD or null.
  `research_decisions.current` keeps the current, well-formed rows in order; a superseded,
  malformed, duplicate or over-the-cap row is skipped with the reason. Caps: 40 rows
  (`MAX_DECISIONS`, the ALLOWED FACTS list's cap) and 300 characters per text part
  (`MAX_CHARS`, clipped with an ellipsis).
- Each row becomes one line, e.g. `[D:d-1] Aoife (person) Rejected the finding that footfall
  fell 12% at the flagship shops — about shop footfall; their words: "The survey counted a
  closed week." (as of 2026-09-12).`
- Every hero writer that gets VERIFIED RESEARCH FACTS gets, right after that block, WHAT
  PEOPLE DECIDED ON THE RESEARCH inside a `<decisions>` data tag: data, not instructions; a
  rejected finding must not be used; an open contest is never stated as fact on either side.
- Writers may mention a decision; no citation is needed. A `[D:id]` the run was not given
  fails the draft, and a figure found only in a decision line (its about, statement or reason;
  never its id or date) that is in neither the brief nor a given fact fails it too. Both start
  with `research decision:`, which counts as an invention, so such a draft is never kept for
  review (the same gate as `research_facts.citation_failures`). Decision lines are never added
  to the text the figure checks allow (`_allowed_for`), so the code number check (and jev's
  figure check, when on) fails such a figure in a list field as well. Valid `[D:id]` markers are moved out
  of the prose into the field's `decision_refs` ([{item, id}]).
- `meta.research_decisions` records `given`, `used` ({id, kind, line}) and `skipped`
  ({id, why}), present only when decisions were given.
- The agent server passes `payload.upstream` ({brand, category, competitors, facts,
  decisions}) to `run(upstream=...)`, on the degraded retry too; a key of the wrong type is
  dropped and logged, and with no upstream `run()` is called exactly as before.

## What it never does

- Enter the Loop 1 capture, the how-to-win read, the golden extraction, the scorecard or the
  judge's prompts.
- Make a figure allowed, or pick a side in a contest.
- Change a run given no decisions: the prompts and the output are byte-identical (tested:
  `test_without_decisions_the_prompts_are_byte_identical`, and compared against 7009001).
- Filter or change the facts: a rejected finding is told to the writers, not removed from
  `upstream["facts"]`; whether the research tool should retire such a fact is its call.

## Next

- Sai to review; one real run when the research tool emits decision rows.
