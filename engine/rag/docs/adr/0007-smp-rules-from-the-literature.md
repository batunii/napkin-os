# ADR 0007 — The SMP rules come from the literature, not from a template

Status: accepted (Sai, 2026-09-26: "okay with SMP"; examples, audience rule and the
visible marks parked) · Research: `engine/outputs/research_2026_09_24/R1_smp_report.md`
(R1, 171 citations checked; spot-checked 2026-09-25) · Code: `engine/golden-brief/golden_brief.schema.json`
(field `smp`), `engine/golden_critic.py`, `engine/parse_brief.py` (`_rubric_flags`,
`_judge_and_gate`, `SMP_ANGLE_SEEDS`, `_gen_field_system`), `engine/golden-brief/Golden-Brief_Agent-Spec.md` ·
Tests: `engine/rag/test_smp_rules.py`.

## Context

The single-minded proposition (SMP) checks were written from a playbook digest and an
invented example, and their explanations to Sai on 2026-09-24 carried no citations
(project_plan `research_plan_2026_09_24`). R1 read the primary sources (Steel 1998, APG
1997, Excellence 1997, BBG/BetterBriefs, Reeves 1961, Ogilvy, BBH's 1992 review and 1994-95
briefs, Weichselbaum, Binet & Field 2007, Sharp 2010, Feldwick 2015) and compared them
with the rules in code. Findings that matter here: the "one idea, not a list" regex is
unsourced and wrong on sourced propositions (it passes "A range of thick flavours" and
flags Levi's and Corona); "must contain a benefit" is contested by the later formats and
by the effectiveness data; ownability as a hard rule is disputed (RSE 2007, Sharp 2010);
"not a tagline" is real but had no criterion; "never restate the brand line" is
contradicted by BBH's own Levi's and Forte Posthouse briefs; the tournament's angle seeds
pushed every draft toward social-judgement lines; no source gives a word count.

## Decision

**Definition (schema `prompt`).** The one thing we want the audience to take away; it
follows from the problem and the insight; true and backed by the reasons to believe; a
reason for this audience to care; specific to this brand, not a category generic; written
for the creative team, not the public. One sentence; not a tagline, not "creative", not a
list; if you have two, choose. The benefit clause is gone (D1). Sources per part: R1 §1.2.

**Checks (schema `rubric`), in this order.**

| check | method | tolerance | source |
|---|---|---|---|
| single_sentence | code | hard | Steel p.169, APG p.56, Dru p.156 |
| within_limit 3–20 | code | hard, our convention | unsourced as a number (C13) |
| single_minded: one strategic choice, not a menu; an "and" or comma is not a fail | judge | **hard** | Excellence p.158, Steel p.169, BBG p.17 |
| derives_from: seen why from the problem and the insight alone | judge | **hard** | APG p.57, Steel p.169, BBG p.14 |
| not_a_tagline: no pun, no hype for a thought, not the sign-off line; short or headline-able is not a fail | judge | soft | BBG p.17, BBH 1992 slide 9, Steel p.149 |
| ownable: not a category generic; a pre-empted claim counts | judge | soft | Reeves p.47, Trott 2022; contested by RSE 2007, Sharp 2010 |
| reason_to_care (new) | judge | soft | Reeves p.48, Weichselbaum p.305, Binet & Field 2007 |
| room_for_many_ads (new) | judge | soft | Excellence p.158, Steel p.173 |

`tolerance: "hard"` on a judge test means a fail is final regardless of the gate's
one-fail tolerance for fields with three or more judge tests (ADR 0006). The SMP now has
six judge tests, so one soft failure is tolerated and kept on the entry as `gate_notes`.

**Soft and flagged, no longer fatal.** The territory tests (`own_territory`,
`brand_only`) count as one soft failure with the judge's reason (D6). An SMP that echoes
the brand's standing vision / claim / tagline is a flag on the entry (`flags`), not a
fail. `ownable` without competitor context is left for review, not failed, and the
`ownable_needs_competitors` dependency likewise.

**Prompts.** The judge's ranking guidance no longer says "never a list or an 'and'" and
names copy by its devices instead of "restated taglines". The generator's ownable-tension
block says "do not simply restate the brand's standing line unless the brief asks for
continuity" and notes that a split audience may need two briefs. The territory block and
the rescue note say "not written as copy: no puns, slogans or sign-off lines; short is
fine" instead of banning headlines. `SMP_ANGLE_SEEDS` are the proposition types the
sources name: a killer fact, a promise (practical benefit), an emotional benefit, a plain
big idea (Ogilvy DO; Weichselbaum p.305).

**Kept.** The 20-word cap (Sai may raise it to 25: a real BBH proposition sits at 20/20
under our counter). The good and bad examples, until the sourced replacements are approved.
`_HARD_RULE_TEXT`, the category-truth half of the ownable block, the "reconcile the whole
audience" rule.

## Consequences

- The critic scores the SMP on eight checks instead of six (two new soft ones), each at
  hero weight, and the comma regex no longer gives half credit for free. Health on SMP-
  heavy briefs moves again; re-baseline (as ADR 0006 already requires).
- A draft that fails `single_minded` or `derives_from` is rejected outright and the
  code-rule repair does not run for it (it is not a code rule). On the recorded runs
  `derives_from` failed 0 of 17, so the immediate cost is nil; watch it after the
  rewording, since a check that never fails may be too lenient.
- Lines on the rival's ground are no longer thrown away by themselves. Expect fewer
  territory rescues and, until the A/B says otherwise, occasionally a more generic SMP
  that the `ownable` soft check and the human flag must catch.
- Parked, with Sai (2026-09-26): the sourced example pairs (Motel 6, Cuervo, Granada, BBH
  NatWest, BBH Chocolate Trifle), the audience rule (IPA vividness + "who it is not"), the
  visible "(our assumption)" / "(proposed)" marks, and the A/B size. The example swap
  matters for `not_a_tagline`: its only calibration example today is the ambiguous lager
  line (R1 §5.1).
