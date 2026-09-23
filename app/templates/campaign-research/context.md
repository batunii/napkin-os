# Campaign Research — the campaign document

We are researching a **campaign**: what the client has asked for, what the
agency already knows about the brand and its category, and what research found,
missed and could not agree on. This document is the ask and the evidence. It is
not the brief — a brief is spun off from it and carries it upstream.

The presentation (`human/index.html`) renders from `shared/data.yaml`; do not
generate or edit HTML. Write structured fields via `patch-data` matching
`agent/output-schema.json`. Every write is attributed and appended to the
decision chain. The contract is `docs/contracts/campaign-clan.md` (Contract 3).

## The five members

- `shared/data.yaml` — `campaign.*` (the ask, nineteen fields), `selection.*`
  (what research drew on), `materials` (the prompt and attachments), and a
  `projection` the host writes. **Never write `projection`.**
- `shared/facts.yaml` — pinned facts. Frozen copies of knowledge-layer rows.
  This document owns no facts; it pins them.
- `shared/findings.yaml` — agent synthesis, labelled *derived by the agent*.
- `agent/decision-chain.yaml` — every decision, addressed
  `<doc-id>#<entity-keyed path>`.

## Rules that are not negotiable

- **No form, no defaults.** The ask is filled by one grounded extraction call
  over the user's prompt, the attached material and what the brand and category
  layers already know. Nobody types eighteen fields, and no field is ever given
  a value because it was empty.
- **Abstain, never guess.** Where nothing in the material or the layers supports
  a value, leave the field out and record the abstention in the extraction
  decision. A field that is present but wrong is worse than a missing one.
- **Say how every field got there.** `extracted` (read from supplied material —
  cite the span), `proposed` (derived from layer facts — cite the fact ids),
  `confirmed` (a human accepted or corrected it), `stated` (a human supplied it).
  Never write `extracted` for something you inferred: "you said this" and "we
  inferred this" must look different to the reviewer.
- **Confirmed and stated fields are the human's.** Re-extraction never overwrites
  them, and never overwrites a field carrying a bad verdict — propose instead.
- **Subject vs comparator.** `campaign.brand` is the subject. Every entry in
  `campaign.competitor_set` is a comparator — public material only. If the
  material does not make clear which brand is the subject, block and ask; never
  default.
- **Do not copy a fact's value into a field.** Cite the pin. A copied value goes
  stale silently and nobody can tell which copy was authoritative. The audience
  field holds statements and fact ids, not figures.
- **Confidence is derived, not asserted.** A fact's confidence comes from its
  sources' tier and corroboration; a finding's from the pins it cites. Never
  report or gate on the model's own confidence.
- **Research runs per lens × market** — one run for each of the eight lenses in
  each market, in parallel, each on its own branch, merged by entity + key +
  market. A difference between markets is two facts. A disagreement is a contest:
  leave it open, pick nothing.
- **A synthesis is a finding** until a human verifies it. The brief may use a
  proposed finding only visibly labelled. Nothing unverified enters the layers.
- **Budget is a band, never the figure.**

## Example

`example/` holds a filled document for the invented Lúnasa 0.0 launch. It is
**EXAMPLE — fabricated for testing**: the brand, client, people, figures and
sources are all invented. Never cite it as research.
