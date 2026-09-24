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
