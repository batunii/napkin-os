# ADR 0014 — Verified research facts in the brief

Status: accepted (Sai, 2026-09-28/29); C1a built, C1b-C1d to follow · Code:
`engine/research_facts.py`, `engine/parse_brief.py` (`run(upstream={"facts": ...})`,
`fill_derivable_fields(research_facts=...)`) · Tests: `engine/rag/test_research_facts.py`.

## Context

Napkin OS runs brand and category research before the brief. Its output is verified facts in
the knowledge-layer databases (foundation-spec.clan storage_model: fact rows with id, version,
status, supersedes, entity, key, value, unit, as_of, and sources through fact_source). Until
now the brief tool could take only three facets from it (brand, category, competitors), the
writers could use only the client brief's own facts, and every check compared claims with the
client brief alone, so a true research fact would have read as invented.

## Decisions (Sai's answers, 2026-09-28)

1. Research facts may back the insight and SMP as well as the RTB and desired response.
2. The campaign CLAN shows sources inline through its fields: the engine carries fact ids and
   versions per field and writes no source text into the prose.
3. The brief is written after the research; the brief and the research are separate entities.
   Where two agents disagree, CLAN's merge report records the contest (CLAN-SPEC sections 24-25)
   and a person adjudicates. The engine picks no winner.
4. The source of truth is the verified-facts databases, versioned, human in the loop. The app
   server's own research helper is not a fact source.
5. Tests use fixture facts; one real run follows when Sai's sample arrives.

## C1a (built)

`upstream["facts"]` takes fact rows. `research_facts.current` keeps the current, well-formed
ones (a superseded, retired, withdrawn or rejected fact is skipped, with the reason). Each
becomes a line such as `[F:f-1 v2] brand shops: 40 shops (brand research, as of 2026-06;
Annual report 2025)`, given to every hero writer as VERIFIED RESEARCH FACTS inside a data tag,
with the instruction to cite the `[F:id]` used. The figure checks treat the fact lines as
allowed text, so a figure taken from a fact is not an invention. `meta.research_facts`
records the ids and versions used and those skipped. The Loop 1 capture and the golden
extraction still read the client brief only. A run given no facts is unchanged.

## Next

- C1b: the writers' cited `[F:id]` are checked in code (the id exists; a figure matches the
  fact's value).
- C1c: the grounding count and the grader treat a claim as supported by the brief or a current
  fact, and say which.
- C1d: each generated field lists the facts it used (id, version) for the campaign CLAN; where
  the brief and a fact differ, both values are kept with their origin.
- C2: the territory check's rival from the brand's competitor facts.
