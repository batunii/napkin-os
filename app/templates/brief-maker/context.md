# Brief Maker — Creative Brief

We are making a **creative brief** for an advertising campaign.

The human drops in a messy brief — notes, a client PDF, mood images, audio — and the middleware drafts the structured brief from it (`draft_brief`, `regenerate_field`: Extract, one Drafter per field, then the Judge; docs/contracts/middleware-api.md §10). The host applies what it returns; the view never writes it. The presentation (`human/index.html`) renders from `shared/data.yaml`; do not generate or edit HTML. Every write is attributed and appended to the decision chain.

Fields to fill:
- `project_name`, `client`, `background`
- `objectives.{commercial, behavioural, attitudinal}`
- `audience`, `competitor_context`, `insight`, `single_minded_proposition`
- `reasons_to_believe[]`
- `desired_response.{think, feel, do}`
- `tone_and_world[]`, `budget_and_scope`, `mandatories[]`, `open_questions[]`

The client's raw input is kept in `brief_input`, and attached files live in `reference_assets` (inside this `.clan`). This context is intentionally minimal and grows as the brief is filled.

## A brief started from research

A brief may be spun off from a Research Tool document (docs/contracts/os-layer.md §5). It then carries the research whole:

- `upstream.<research document id>` holds the research's data, frozen. The host writes it and nothing patches it; no address in it is ever rewritten.
- `shared/facts.yaml` and `shared/findings.yaml` hold the research's pins and findings, merged into this brief's own. The brief verifies, rejects and corrects **its copies**; the research itself never changes.
- The research's decisions — contests, verdicts, classify marks, its approval — are in this brief's chain, and what was open is still open.

What that evidence may ground (docs/contracts/middleware-api.md §10.13):

- **Capture never reads research.** The captured fields — everything but `insight`, `single_minded_proposition`, `reasons_to_believe`, `desired_response` and `open_questions` — come from the client's own material only, and never cite a pin (`f_…`) or a finding (`fi_…`).
- The drafted fields may cite pins and findings, each drafter only the selection it was sent.
- A finding not yet verified is **derived by the agent**: a field resting on it is at most medium certainty and says so. A rejected finding is never cited.
- A pin that is a value of an open contest is not used until a person resolves it. Nothing marked `model: false` goes into any model call.

The brief is locked by a person's `approve` on this document (os-layer.md §7), and only when nothing is unresolved, what it carried from the research included. An `approve` carried from the research does not lock the brief.
