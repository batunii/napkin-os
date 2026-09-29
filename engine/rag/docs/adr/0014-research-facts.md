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

## C1b (built)

The writers' `[F:id vN]` citations are checked in code, in the one gate
(`research_facts.citation_failures`): a cited id the run was not given, a figure next to a
citation that is not in the cited fact (a misquote), and a figure that exists only in the
research but is used without a citation, each fail the draft. They start with
`fact citation:`, which counts as an invention, so such a draft is never kept for review. The
markers are then moved out of the prose into the field's `fact_refs`
(`[{item, id, version, scope, source_ids}]`, item = list index or think/feel/do key), and the
app mapping passes them on as a `fact_refs` key by app field name until the campaign CLAN's
source field is confirmed (question with Shrey).

## C1c (built)

The grounding count (ADR 0013) accepts current facts as support. `meta.research_facts.used`
now keeps each fact's line, so a later check sees what the writers saw without the database.
Given those lines, jev answers supported (by the brief), supported_by_research, contradicted
or not_in_brief; the report shows e.g. "0 of 5 (+1 to confirm, 2 from research)". A brief
written without facts is asked exactly the old question, so earlier checkpoints stay
comparable. The grader (golden_critic) was left alone: it never sees the client brief and
none of its rubric tests compares a field with the brief, so a research-backed claim is not
marked down there.

## C1d (built)

Where the client brief and a current fact disagree (jev, one yes/no question per fact,
p >= 0.9), the engine picks no winner (Sai). The fact goes to `meta.research_facts.conflicts`
({id, version, line, p}), a high-priority open question asks which is current, the app reply
carries `fact_conflicts` for CLAN's merge report to record the contest, and the writers get
the fact only under CONTESTED: never to be stated as fact, at most asked as TO CONFIRM
(option a, Sai "go on" 2026-09-29). With jev unavailable, no conflict is claimed and every
fact stays usable. Sai notes the research tool may detect such conflicts too; if it emits
them, the engine can take its records and skip this check (question with Shrey and the
research team).

## Next

- C2: the territory check's rival from the brand's competitor facts.
