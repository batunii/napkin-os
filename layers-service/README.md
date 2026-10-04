# layers-service: the knowledge layers on Postgres

The schema behind `napkin.layers/1` (`docs/contracts/peripherals.md` §4), laid
out per foundation-spec S3: one shared `napkin_category` database, and one
`napkin_agency_<slug>` per agency. The HTTP service that speaks the contract
over this schema is the next step. This directory holds the schema, its
migrations and the append rule, tested against a real PostgreSQL 16.

## What a fact is stored as

```
sources    one row per URL                    uri, publisher, tier, licence
captures   one row per reading of it          retrieved_at, published_at + basis, content hash
excerpts   verbatim passages from a reading   text (<= 1,500 chars), sha256 computed here
facts      one row per fact version           typed value, unit, market + market_basis,
                                              period_start..period_end, date_basis,
                                              period_basis (stated | inferred), status,
                                              supersedes / superseded_by
evidence   fact <- the exact span of an excerpt that says it (quote, start, end)
decisions  the decision behind every write, whole
```

So for any fact you can answer: **what was said** (the quote, and the passage
around it), **where** (URL, publisher), **when it was published** and **when
we read it** (the capture), **what period it describes**, and **why it
entered the layer** (the decision).

The database enforces this itself; it trusts no caller:

- **The quote is checked.** A quote must be exactly the characters of its
  excerpt at the offsets given, on every insert.
- **Append-only.** Nothing is updated or deleted, except a fact's `status`
  and `superseded_by`. Triggers refuse everything else, the owner included.
- **Scope never comes from the body.** The service sets
  `SET LOCAL napkin.org / napkin.brand` per transaction, and missing scope is
  an error, never "everything".
- **Isolation.**
  - Agency databases force RLS on (org, brand), and a fact's org must match
    the agency the database belongs to.
  - The category database shares facts but keeps each agency's decisions
    private: a research rationale can name a client.
  - Client-confidential sources are refused there.
- **No silent declassification.** A fact's licence is never less restricted
  than its sources.

## Markets: every fact names its place

`market` is required: a country (ISO 3166-1 alpha-2; the UK is `GB`), `EU`,
`GLOBAL`, or `UNKNOWN` (with `market_basis = unknown`) when the place could
not be established. `market_basis` says whether the page **stated** it or we
**inferred** it (e.g. from the publisher).

A campaign's market decides which facts apply, via `layers.applicable_markets`:
IE sees IE, then EU, then GLOBAL; GB sees GB and GLOBAL (not the EU); AU sees
AU and GLOBAL. UNKNOWN applies to nothing. A fact is stored once, for the place
it describes, and an Irish and an EU figure never contest: they are different
places. Countries and EU membership come from `data/geographies.json`.

## Cells: the living dossier (`0004_cells.sql`, category database)

The unit a planner reads (Planner Research Taxonomy, panels 3 and 4): one leaf
× one lens × one market.

| Table | Holds |
|---|---|
| `lenses` | the eight standing questions, each with its cadence; `amber_cadence` is the stricter clock for a regulated leaf |
| `cells` | opened per leaf and market by the job that researches them (`layers.open_cells(leaf, market)` makes its eight) |
| `cell_versions` | every confirmation, never overwritten: summary in our words, what changed since last time, confidence, last-confirmed date, author (`agent:…` or `human:…`) |
| `cell_citations` | what a version stands on: facts, or verbatim excerpts for qualitative claims (codes, culture) |
| `cell_verifications` | a person confirming an agent-written version |
| `cell_status` (view) | per cell: `fresh` · `stale` · `empty` from the lens's clock, due date, source count, `thin` (under 3 sources), verified |

`layers.confirm_cell(...)` adds a version. A cited fact must answer the cell:
its key is under the lens's prefix, it is about this leaf (or a named
competitor), it is not superseded, and its market applies (an IE cell may cite
IE, EU and GLOBAL facts, never GB). A cell with fewer than 3 sources is kept
and shown as thin, not refused.

## Measures: the category layer's vocabulary (`0007_measures.sql`)

The category layer speaks only the measure list, `data/measures.json` (a
draft, for a planner to approve), loaded into `layers.measures` by the runner:
65 measures across the 8 lenses, each with its units, a cardinality, the kind
of qualifier it takes, a definition, and the search group that finds it.

- **Qualifier:** what tells rows of a measure apart. `market.player_share`
  for "Aldi" and for "Lidl" are two facts, not a contest. Compared normalised
  (case and spacing).
- **Cardinality `many`:** a list (claims, moments, codes, restrictions). A new
  item is **added** beside the others and never contests or supersedes; the
  same item again corroborates. Single-valued measures keep the dating rules.
- **Unlisted measures** are refused by `append_fact` and go to
  `layers.measure_proposals`, with the passage that states them, for a
  planner to add to the list or refuse.

## The append rule (`layers.append_fact`, owner's decisions 2026-09-29)

One call writes the fact, its evidence and its decision in one transaction,
serialised per entity + key by an advisory lock.

| The identity's current rows | New fact | Outcome |
|---|---|---|
| a row with the same value and the same period | same value (or it is undated) | **corroborated**: evidence added |
| no dated row | dated | **created**, active; undated rows step back to `undated` |
| dated rows | dated, **later** period | **superseded** |
| dated rows | dated, **same** period, another value | **contested**, always |
| dated rows | dated, any other period | **history**: kept, never current |
| a dated row | undated | **undated**: kept, flagged, never supersedes |
| only undated rows | undated, another value | **contested** |
| nothing | undated | **created**, active, date unknown |

A period is a range: "2025" is 2025-01-01..2025-12-31, and a Q4 figure is a
different period from an annual one. `period_basis` says whether the passage
**stated** the period or it was **inferred** from context (a comparison, the
publication date). "Stated" is checked: the year the period ends in must
appear in one of the fact's own passages, or the fact is refused (0006). The date we read the page never decides
anything; it only says how fresh our reading is.

## Running

```sh
uv sync                     # psycopg; dev: pytest + pgserver (a real Postgres 16)
uv run pytest -q            # 73 tests, no Postgres to install

# apply to a database (as an admin who is a member of the owner role)
uv run python -m napkin_layers.migrate --dsn "$DSN/napkin_category" --kind category \
  --owner-role napkin_category_owner --app-role napkin_category_app
uv run python -m napkin_layers.migrate --dsn "$DSN/napkin_agency_acme" --kind agency \
  --agency org/acme --owner-role napkin_agency_acme_owner --app-role napkin_agency_acme_app
```

On AWS, run it from the admin host after `provision-databases.sh` has
created the databases and roles. Re-running applies only what is missing.
