# mock-middleware

A stand-in for the Napkin middleware. It answers `napkin.middleware/1`
(`docs/contracts/middleware-api.md`) exactly as the real middleware in `server/`
will, so the app can be built and demoed now and pointed at the real one later
by changing one endpoint. No model, no network, no dependencies: Python 3.11+
standard library only.

## Run it

```sh
python3 mock-middleware/server.py
# [mock-middleware] napkin.middleware/1 on http://127.0.0.1:8790/v1/tasks ...
curl -s localhost:8790/healthz
```

| Env | Default | |
|---|---|---|
| `MOCK_MIDDLEWARE_PORT` | `8790` | |
| `MOCK_MIDDLEWARE_HOST` | `127.0.0.1` | |
| `MOCK_MIDDLEWARE_ORG` / `MOCK_MIDDLEWARE_BRAND` | `org/dev-agency` / `brand/dev-brand` | The fixed dev tenant, reported in `trace.scope`. Scope never comes from the request |
| `MOCK_MIDDLEWARE_TOKEN` | unset (open) | When set, requests need `Authorization: Bearer <token>` or `x-api-key: <token>`, else 401 |
| `MOCK_MIDDLEWARE_JOB_SECONDS` | `2.0` | How long a long job takes. `0` finishes on the first poll. A `start_campaign` job spreads it over its six stages; every poll advances at least one stage |
| `MOCK_MIDDLEWARE_CLOCK` | unset | Freeze content timestamps (`2026-09-23T10:00:00Z`) for byte-identical output |
| `MOCK_MIDDLEWARE_DUMP_DIR` | unset | Write each request body to this directory. Off by default: bodies carry client-confidential material |

## Point the app at it

In `<app-config-dir>/workspace.yaml` (template: `docs/workspace.example.yaml`):

```yaml
proxies:
  middleware:
    endpoint: http://localhost:8790/v1/tasks
    # with MOCK_MIDDLEWARE_TOKEN set:
    # auth_kind: bearer
    # secret_ref: middleware_api     # middleware_api: <token> in secrets.yaml
```

Configure the `middleware` kind explicitly: an unconfigured kind falls back to
`agent_url`, the briefing agent, which does not speak this contract.

## Swap to the real middleware

Change `endpoint` (and the secret) to the real service. Nothing else changes —
no template, host or test code knows which one answered. Before swapping, run
the contract suite against the real one:

```sh
python3 mock-middleware/contract_test.py --base-url https://middleware.example \
    --schema-dir app/templates/campaign-research --token "$MIDDLEWARE_TOKEN"
```

It must pass unchanged. The suite validates every `change` against the
campaign schemas (with `jsonschema` if installed, else a built-in draft-07
validator) and checks the job lifecycle and every error case.

## What it returns, and by which rules

Deterministic: ids and values are seeded from the dev scope, the document id
and the inputs, so the same request gives the same change (timestamps aside —
freeze them with `MOCK_MIDDLEWARE_CLOCK`). Job ids are per submission.

**`extract_ask`** — regex heuristics over the prompt and each attachment's
`text`, never a guess. Each value is an `extracted` envelope whose `source`
cites the material id, a `¶n` locator and the verbatim sentence:

- `markets` from country names (Northern Ireland, UK, Britain → `GB`); an
  `item_provenance` entry per item when they come from different sentences.
- `in_market` only from explicit dates (ISO pairs, `May to August 2027`,
  `Q2 2027`, `summer 2027`, `in June 2027`). `next spring` is abstained on —
  there is no reference date to resolve it against.
- `budget_band` from an amount in a sentence about budget/spend. EUR maps
  straight to a band; GBP/USD convert at a static rate only when the band holds
  under a ±15% rate move, otherwise it abstains. Locator only, no quote.
- `deliverables`, `channels_mandated` (only in a sentence that mandates:
  must/required/…), `competitor_set` (`X as the one to beat`, `competitors
  include X and Y`, `vs X`), `problem` / `objective` / `audience_stated` (a
  labelled cue — `The problem …:` — or `our objective is to …`; verbatim),
  `campaign_type` (only when exactly one type is signalled), `success_measures`,
  `constraints`, `client_org` (a signature line `<role>, <Org>`), `brand` (only
  from an explicit `Brand:` label — the subject brand never defaults).
- `proposed` with `fact_ids` from roster pins in `clan.facts`
  (`roster.categories.primary|secondary` → `categories`, at most two;
  `roster.client_org` → `client_org`). Categories are never read from text:
  the taxonomy is a closed enum.
- Anything else is absent and listed in the decision's `abstained`. Fields that
  are `confirmed`/`stated` or carry an unanswered bad verdict are left alone and
  reported in `result.withheld`.
- Material not yet in `data.materials` is added with licence
  `client-confidential`.

**`research_lens`** — a job, one unit per lens × market. Each lens asks for a
few keys (for example `market_structure`: `market.value_growth_yoy`,
`market.size_eur` per category and market; `brands_positioning`: subject
`awareness.prompted` per market and each comparator's market-independent
`launch.date`; `category_codes`: market-independent `codes.dominant_colour`).
Values come from a small fixture bank (`drinks.cider`, `drinks.no_low_alcohol`)
or a hash of scope + entity + key + market.

- **Sources.** Each (entity, key, market) finds 0–3 sources by hash (0: ⅛, 1: ⅜,
  2: ⅜, 3: ⅛), each `src_…` with `uri mock-source://<lens>/<market>/<entity>/<key>/<n>`,
  tier `mock`, a licence (measurement → licensed-internal, observation → open,
  report → either). Records are in `result.sources`; pins cite the ids. Zero
  sources is a `selection.gaps` entry, not a fact.
- **Confidence** is derived, never asserted: start at the best source's tier
  (`primary` = medium; `secondary` and `mock` = low), then one step up per
  independent corroborating source — 1 source low, 2 medium, 3+ high for mock
  sources, capped at high.
- **Merge** by entity + key + market. A market-independent key found by both
  markets' runs with the same value is one pin with the sources unioned (so its
  confidence rises). **Contest rule:** the first comparator in
  `campaign.competitor_set` has its `launch.date` read differently by each
  market's run (each market saw its own launch), so with two or more markets it
  always opens a `selection.contested` entry, `status: open`, with both values,
  their fact ids, `from` (`<lens>/<market>`) and sources, plus a `contest`
  decision; neither value is pinned. A research value that differs from an
  existing pin opens a contest against the pin the same way.
- **Coverage** per lens × market: `empty` if nothing found, `filled` if every
  fact found has ≥2 sources, else `thin`; merged per Contract 3 §8.
- Pins carry `origin fact://<layer>/<path>/<key>@1`, `as_of` 10–210 days
  before `retrieved_at`, and a `pin_reason` that says they are mock-source
  fixtures. Comparators live in the category layer.

**`synthesise_findings`** — a job over `clan.facts` (excluded pins skipped).
Pins are grouped by lens (from the key prefix); one finding per lens, citing a
key read in two markets when there is one (a cross-market comparison), else up
to three pins. `status: proposed`, `derived_by synthesise_findings@1.0`,
confidence per Contract 3 §6.1, one `finding` decision each. No pins is
`400 invalid_input` — a finding must cite something.

**Reasoning** — every decision carries `reasoning` (middleware-api.md §3,
Contract 4 §3), written deterministically since no model runs: `because`
points built from what the decision cites (a pin's entity, key and value with
its sources; the materials, findings, sources and decisions it rests on),
`rejected` from a fixed table per step, `certainty` derived (a pin decision's
is the lowest confidence of its pins, a finding's is the finding's), and
`attention` on contests, low-confidence pins and findings, thin runs and
questions. The contract suite checks the shape and that every cite resolves,
against this stand-in and the real middleware alike.

**Stale base** — a job reads the document when it starts; its `done` change
names that version in `base_version` even if later polls carry a newer
`clan.version`, and carries `read` — what it read of every field its
`data_patch` writes (`campaign.<field>`, `materials.<id>`, `selection.<key>`).
The host decides field by field. The stand-in never answers 409: it does not
hold the document.

## The chat intake: `start_campaign`, `answer_question`, `compose_report`

Contract: `docs/contracts/middleware-api.md` §8 and Contract 3 §16–18. One
`start_campaign` job runs six stages — `extract`, `identify`, `select`,
`research`, `synthesise`, `report` — and `progress` counts them (`total: 6`).
The reply to `start_campaign` is `queued`; each `job_status` poll advances the
job (at least one stage per poll, more if `MOCK_MIDDLEWARE_JOB_SECONDS` has
elapsed) and carries the change for the stages the request's
`clan.decision_chain` does not hold yet. So a stage whose change did not land
is sent again, byte-identical, with the same decision ids — the host skips it
if it did land. Every staged field write is named by a decision's `targets`
(`#campaign.<f>`, `#selection.<k>`, `#materials[<id>]`,
`#intake.messages[<id>]`, `#report`), and every stage writes one agent message
under `intake.messages.<id>` (a ULID-style key, so `at`-then-key order is
creation order), also listed in `result.messages`.

- **`extract`** — `extract_ask` over the prompt (a `kind: prompt` material,
  matched to `data.materials` by sha256) and the attachments, minus `brand`,
  `client_org` and `categories`, which `identify` owns.
- **`identify`** (§8.6), in order: subject brand, categories, markets, client.
  - *Brand*: clear when a `Brand:` label names it, the prompt names it as the
    client's (`our client is X`, `X is our client`, `our brand X`), or it is the
    only brand the material names and is not named as a comparator → written
    `extracted` with its span. Brands are found from the roster names below,
    comparator cues (`X as the one to beat`, `competitors include X`),
    `brands: X and Y`, and `launching X`. Several, or a lone comparator →
    `needs_input` "Which brand is the client's?": one `extracted` option per
    brand with its span, plus "None of these". None → a free-text question.
  - *Categories*: a roster pin already in `clan.facts` for the subject, else
    the fixture roster below (pinned now: `roster.categories.primary|secondary`,
    `roster.client_org`, brand layer, one `pin` decision) → `proposed` citing
    the pins. No row → the material's words ranked into at most two leaves of a
    small fixture taxonomy → `needs_input` with each leaf, "both", and
    "Something else" (never written without a pick). Nothing matches → free text.
  - *Markets*: none in the material → `needs_input`, free text.
  - *Client*: the roster row → `proposed`; else a signature line → `extracted`.
  - If the subject was listed as a comparator by `extract`, `identify` rewrites
    `competitor_set` without it: a new decision, `read` = extract's value.
  - *Free text* is resolved and asked again (new question id, same
    `address`): brands by name against the roster and the brands found, plus the
    text itself as a new brand, all `origin: stated`; category leaves by the
    same keywords or a typed leaf code; markets by country names or ISO codes.
    Nothing resolves → the question says so and allows text again.
  - An answered field is never written by the job: the view writes it.
- **`select`** — every lens × `campaign.markets`, except what the prompt rules
  out: a sentence with `only` naming a lens and a market (`Effectiveness cases
  only matter for Ireland` → skipped in the other markets); `skip / no need for
  / don't need <lens>` → skipped everywhere; `just / only <lenses>` → the
  others skipped. `regulation_clearance` is skipped when no category is in a
  regulated vertical (drinks, food, finance, health, gambling). Everything
  skipped is a `selection.lenses_skipped` entry with the reason (quoting the
  prompt) and the select decision.
- **`research`** / **`synthesise`** — `research_lens` over the selected pairs
  only (a skipped pair has no run and no coverage), then `synthesise_findings`.
  Handler on everything: `start_campaign@1.0`.
- **`report`** — waits (`running`, `stage: report`) for a poll whose
  `clan.decision_chain` holds every earlier decision, then composes from that
  `clan`: a "Brand and category" section over the roster pins, then one section
  per lens with a claim (a finding's own statement citing it and its pins, or a
  sentence with no figure citing the lens's pins), a `pins` block, `finding`,
  `contest` and `gap` blocks. Headline and up to four summary lines, all cited.
  `based_on` copies `clan.version` and `projection.built_from`'s hashes (a
  document with no projection gets a hash of the members as sent). `confirm`:
  brand, categories, markets, comparators while `extracted`/`proposed`, then
  every other `proposed` field. `not_researched`: every skipped entry, then any
  lens × market with no run.

**`answer_question`** — `{job_id, question_id, option_id | text}`. `409
job_state` unless the job is `needs_input`; `400` for another question id,
both or neither of option/text, the escape's id or an unknown id, text where
not allowed. The request's `clan` (holding the view's write) becomes the base;
the reply continues `identify` and is `running`, or `needs_input` with the next
question. Its `task`/`handler` are the job's (`start_campaign@1.0`).

**`compose_report`** — short; the same composer over the request's `clan`,
`handler: compose_report@1.0`, one agent message. `409 job_state` while a
`start_campaign` job on the document is unfinished (so is a second
`start_campaign`); `400` when there is no pin and no finding to cite.

**The fixture roster** (the brand layer; invented, like everything here):

| Brand | Ref | Categories | Client |
|---|---|---|---|
| Lúnasa | `brand/lunasa` | `drinks.cider`, `drinks.no_low_alcohol` | Glenmore Drinks |
| Brightwater 0.0 | `brand/brightwater` | `drinks.no_low_alcohol`, `drinks.beer` | Brightwater Brewing |
| Kestrel Press | `brand/kestrel-press` | `drinks.cider` | Kestrel Cider Co |
| Oakfield Dairy | `brand/oakfield` | `food.dairy` | Oakfield Foods |
| Crunchwell | `brand/crunchwell` | `food.snacks` | Oakfield Foods |
| Tidewater Bank | `brand/tidewater-bank` | `finance.banking` | Tidewater Financial |
| Tidewater Cover | `brand/tidewater-cover` | `finance.insurance` | Tidewater Financial |

### Try an ambiguous prompt

In the Research Tool's chat, attach the example email
(`app/templates/campaign-research/example/assets/client-email.txt`) and send
*"Brief from Glenmore attached — can you pull together the landscape? Effectiveness
cases only matter for Ireland."* The email names Lúnasa and Brightwater 0.0 and
says neither is the client's, so the bot asks **"Which brand is the client's?"**
with Lúnasa, Brightwater 0.0 and "None of these". Pick Lúnasa: its roster row
proposes cider + no/low and Glenmore Drinks, research runs 15 pairs
(effectiveness skipped in GB) and the report lands. Pick "None of these" and
type *harbour tonic* instead: the bot asks again with "harbour tonic (new
brand)"; pick it and, with no roster row, it asks **"Which category is it?"**
with no/low alcohol, cider, both, and "Something else" — the leaves the email's
words point at.

Without the app, the contract suite drives the same flow (it plays the host
and the view): `python3 mock-middleware/contract_test.py --base-url
http://127.0.0.1:8790`.

## What it deliberately does not do

- No LLM, no retrieval, no knowledge layer: every fact is a fixture from a
  `mock-source://` URI at tier `mock`. Never quote a figure it returns.
- No usage estimates — token counts are zero because no model ran.
- No persistence: jobs live in memory and die with the process (the real one
  must survive restarts, N2). Never a `failed` job except a `start_campaign`
  stage that cannot run; never a `version_conflict`.
- No layer writes, no branch per run, no roster write-back, no
  `campaign.audience` / `in_market` proposal from `synthesise`.
- No model in `identify`/`select`/`report`: regexes over the prompt and the
  material, a fixture roster and a keyword taxonomy stand in for them.
- No request body on disk unless `MOCK_MIDDLEWARE_DUMP_DIR` is set.

## Removability test

The stand-in is removable when nothing outside `mock-middleware/`, this
contract and `docs/workspace.example.yaml` knows it exists:

```sh
git diff --stat feature/studio-start -- app/templates app/src crates   # empty
grep -rniE 'mock|8790' app/templates app/src crates                    # no line added by this work
```

(The grep has 8 pre-existing hits — `advertising-studio`'s `mock` asset kind
and a Brief Maker status string — none from here.) Removing the stand-in is
deleting this directory and changing one endpoint; there is no code to unpick.
