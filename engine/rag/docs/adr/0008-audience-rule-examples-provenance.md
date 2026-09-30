# ADR 0008 — The audience rule, sourced examples, and where provenance marks go

Status: accepted (Sai, 2026-09-26 pm, decisions 2–4 of five) · Research:
`engine/outputs/research_2026_09_24/R2_claims_report.md` (audience, §5–6) and
`R1_smp_report.md` (examples, §4) · Code: `engine/golden-brief/golden_brief.schema.json`,
`engine/golden-brief/Golden-Brief_Agent-Spec.md`, `engine/parse_brief.py`
(`_mark_provenance`, `_good_examples_block`, `_judge_and_gate`, `render_golden_provenance`),
`engine/golden_critic.py` (`critic_prompts_batched`) · Tests: `engine/rag/test_decisions_2026_09_26.py`.

## 1. The audience rule

**Context.** The golden extractor was told "Extract ONLY what the brief contains" and, in
the same prompt, "Describe one real human … Good: Conor, 29, Dublin". The `is_human` check
then rewarded the invention: it passed only when the extractor turned "Urban Heritage
Seekers (25-45)" into "Ana, 32, Bucharest, orders Bolt Food", and failed every faithful
extraction, five of five on the B2B employer brief (R2 §3.2, audit H4). R2 found no
primary advertising source for the one-person portrait; BBG p.16 asks for "a vivid
picture of demographics, psychographics and needs or wants", BBG p.10 for "equally clear
on who it is not", CB p.30 ties pen portraits to research, and all 16 legible audience
boxes in BBH's 1994–95 briefs describe a group.

**Decision.** The schema's audience `prompt` asks for a vivid picture built only from the
brief's data (demographics, psychographics, needs, use), who the brand is not for, every
named audience in priority order, a B2B audience as a buying group, and never an invented
person, age, city or habits. `is_human` is replaced by `is_vivid` (BBG p.16, and not an
invented individual) and `says_who_not` (BBG p.10). The examples are two BBH audience boxes
(Cadbury's Chocolate Trifle, 24 Jan 1995; NatWest Mid Corporates, 10 Feb 1995). The
extractor prompt is built from the schema, so it carries the rule without a code change.

**Consequences.** The invented persona loses its reward; B2B and two-audience briefs stop
failing a test they could never pass; the audience field is scored on three checks instead
of two, so health moves (re-baseline, as ADRs 0006/0007 already require). The BBH slides
are a third-party upload, not independently authenticated.

## 2. Sourced examples

**Context.** The generator copies the shape of its style reference (R1 §5.4), and the
reference for the SMP was an invented lager line that carries copy markers itself; the
RTB reference was an invented statistic, the exact fault the RTB writer is being cured of
(audit H1).

**Decision.** SMP: good examples Motel 6 "A smart choice because you don't pay for what
you don't need" (Weichselbaum p.305), Cuervo "A party waiting to happen" and Häagen-Dazs
"the ultimate sensual intimate pleasure" (BBH brief, 10 Feb 1995); bad example Cuervo's
"Good drinks, fun times, real people" with Steel's reason (p.169). Contrast pairs for the
not-a-tagline judgement: Granada proposition vs copy (Excellence 1997 p.159), Polaroid
proposition vs "See What Develops" (Steel pp.174–175), and Levi's as the allowed
convergence case. RTB: Granada's support (Excellence p.159). Insight: built from Fallon's
Skoda Fabia account (Fallon 2006 pp.77–80), wording ours, facts the source's. The generator
and the batched judge show every `good_examples` entry; the critic prompt for the SMP
carries the contrast pairs. Desired response keeps its example: R1/R2 supplied no sourced
replacement. Every example carries `_example_sources` in the schema.

**Consequences.** Output shape should get less templated; unmeasured until the next
checkpoint run. Page numbers for Steel and Excellence were verified by wording, not by
page (T8).

## 3. Provenance marks: review and lineage, not the client page

**Context.** Audit H2/F4c proposed marking inferred values "(our assumption, to confirm)"
and generated ones "(proposed)" on the client brief. Sai's decision: not on the client
page; the marks go to the review file and into the brief object, so they can ride into the
CLAN context or lineage when the RAG module joins the rest of the system.

**Decision.** `_mark_provenance` writes `loop2_golden.provenance = {field: {kind, mark,
confidence}}` with kinds client_stated / generated / inferred / missing, and appends an
open question for an inferred value below the confidence floor ("Confirm the audience: it
is our assumption at confidence 0.50"). `review.md` lists the provenance of every field
under the RAG-provenance section. `render_client_brief` is untouched.

**Consequences.** The planner and the lineage see which lines are the client's, which are
our reading and which we wrote; the client sees the same page as before. A low-confidence
assumption is now a question on the page, not a fact.
