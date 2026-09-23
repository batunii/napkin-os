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
| `MOCK_MIDDLEWARE_JOB_SECONDS` | `2.0` | How long a long job takes. `0` finishes on the first poll |
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

**Stale base** — a job reads the document when it starts; its `done` change
names that version in `base_version` even if later polls carry a newer
`clan.version`, and carries `read` — what it read of every field its
`data_patch` writes (`campaign.<field>`, `materials.<id>`, `selection.<key>`).
The host decides field by field. The stand-in never answers 409: it does not
hold the document.

## What it deliberately does not do

- No LLM, no retrieval, no knowledge layer: every fact is a fixture from a
  `mock-source://` URI at tier `mock`. Never quote a figure it returns.
- No usage estimates — token counts are zero because no model ran.
- No persistence: jobs live in memory and die with the process (the real one
  must survive restarts, N2). Never a `failed` job, never a 409.
- No layer writes, no branch per run, no roster write-back, no
  `campaign.audience` proposal.
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
