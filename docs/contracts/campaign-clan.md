# Contract 3 — the campaign CLAN shape

Status: **draft**, pending the W1-C4 contract gate. Owner: Shrey.
Implements foundation decisions D1, D2, D3, D4 (and D1 as amended).
Task: `W1-C3` in `napkin-build-plan.clan`. Source: the owner's *Campaign CLAN
Contract* (draft 2026-09-18) with the amendments decided 2026-09-23.
Reads with: Contract 4, `docs/contracts/os-layer.md` (addresses, decision
kinds, spin-off, lock). Where this document restates Contract 4, Contract 4 wins.

The campaign document is the ask and the evidence behind it: what the client
wants done, the facts the agency pinned, what research found, missed and could
not agree on, and every decision about it. The Research Tool app
(`app/templates/campaign-research/`) is its view. A brief is spun off from it.

This document is what the view, packaging, research and brief tracks code
against. The machine-checkable half is:

| File | Checks |
|---|---|
| `app/templates/campaign-research/schema.json` | `shared/data.yaml` — packaged as `agent/output-schema.json` |
| `app/templates/campaign-research/facts.schema.json` | `shared/facts.yaml` |
| `app/templates/campaign-research/findings.schema.json` | `shared/findings.yaml` |
| `app/templates/campaign-research/example/` | A filled document that passes all three |

All three are JSON Schema draft-07, the same dialect as the other templates.
No property in any of them has a `default`.

---

## 1. Five members

| Member | Holds | Written by |
|---|---|---|
| `shared/data.yaml` → `campaign.*` | Region A — the ask, 19 fields | extraction, humans |
| `shared/data.yaml` → `selection.*` | Region C — lenses run, coverage, gaps, contests, exclusions | research merges, humans |
| `shared/facts.yaml` | Region B — pinned facts | the lease holder, or a merge |
| `shared/findings.yaml` | Agent synthesis, "derived by the agent" | `synthesise_findings`; status changes by humans |
| `agent/decision-chain.yaml` | Region D — every decision | the OS layer, append-only |

`shared/data.yaml` also holds two supporting blocks that are not regions:

- `materials` — the index of supplied material (the prompt and each attachment),
  keyed by material id. Spans cite it. Bytes live as assets, by hash.
- `projection` — the host's read-only scalar copy of the facts and findings
  members, for view bindings (§5).
- `intake` — the chat that started the campaign: `intake.messages`, a map
  keyed by message id (§16).
- `report` — the structured report the middleware composes and the view
  renders read-only (§17).

`shared/facts.yaml` and `shared/findings.yaml` are their own registered members
with their own roles (D2): a data-update pack replaces `shared/data.yaml` whole
and would otherwise delete every pin silently. Proposed roles:
`pinned-facts` and `findings`. Registering them is packaging work.

The document **owns no facts** (D1). Every figure it relies on is a pin into
the brand or category layer. Nothing in `campaign.*` holds a fact's value.

---

## 2. Region A — the ask

### 2.1 The field envelope

Every campaign field that is present is an envelope:

```yaml
campaign:
  problem:
    value: "people who love Lúnasa are drinking less of it midweek, …"
    origin: extracted            # extracted | proposed | confirmed | stated
    gate: brief                  # constant per field; the schema pins it
    source: { material_id: mat_email01, locator: "¶2", quote: "people who love …" }
    decision: d_01JA0D02EXT      # the decision that set the current value
```

| Key | When | Meaning |
|---|---|---|
| `value` | always | The field's value, typed per field (§2.3) |
| `origin` | always | How it got here (§2.2) |
| `gate` | always | The field's gate. `const` in the schema: a writer cannot change it |
| `decision` | always | Id of the chain entry that set the current value. The rest of the history is `GET /history?address=` |
| `source` | required for `extracted` | The span: `{material_id, locator, quote?}` |
| `fact_ids` | required for `proposed` | The pins it was derived from — ids in `shared/facts.yaml` |
| `finding_ids` | optional | Findings it rests on. A field citing a rejected finding is flagged (§6.3) |
| `by` | required for `confirmed` and `stated` | The human, `human:<id>` |
| `confirmed_from` | required for `confirmed` | What the human accepted: `extracted`, `proposed` or `stated`. The earlier `source` / `fact_ids` stay |
| `item_provenance` | list fields only, optional | Per-item provenance, keyed by item address key (§2.4) |

**Why an envelope and not a parallel `provenance` map.** One path answers both
"what is it" and "how do we know": the view binds `campaign.problem.value` and
`campaign.problem.origin` side by side, and a `patch-data` that changes a value
must also say where it came from, in the same object, or the schema rejects it.
A parallel map lets the two drift — a value rewritten without its provenance
still validates — and every view binding would need two lookups keyed the same
way. The envelope's cost is one extra `.value` in every binding. The address of
a field is the envelope (`#campaign.problem`), not `.value`.

**Why the gate is in the data as well as the schema.** The view receives
`window.__CLAN__.data`, not the schema. Each field schema carries `x-gate` and
pins `gate` with `const`, so the copy in the data cannot lie. For a field that
is absent the view uses the table in §2.3 — it must lay out all 19 anyway.

**Absent means not filled.** There is no `null`, no empty string and no
placeholder. The extraction decision lists what it abstained on. No campaign
field is `required` in the schema: a gate is not a required flag (D4), and the
empty template `{}` must validate. The schema is enforced on every write, over
the fully merged data (`pack.rs`), so this matters.

### 2.2 Origin

| Origin | Means | Must carry | The view says |
|---|---|---|---|
| `extracted` | Read from supplied material | `source` (the span) | "you said this" — show the quote |
| `proposed` | Derived from layer facts | `fact_ids` (≥1) | "we inferred this" — show the pins |
| `confirmed` | A human accepted or corrected it | `by`, `confirmed_from` | who confirmed it, and from what |
| `stated` | A human supplied it directly | `by` | who said it |

`extracted` and `proposed` must look different on screen (D3); so must
`extracted` and `stated`. Collapsing them dresses an inference in the client's
words.

**Confirmed and stated fields belong to the human.** Re-extraction never
overwrites them without asking: it writes a proposal the lease holder accepts
or rejects. The same holds for any field with an unanswered bad verdict — a
rejected field is not silently re-extracted on the next run. The extraction
handler reads `/open` before writing.

**Mixed lists.** A list whose items came from different origins records the
*least certain* origin at field level — `proposed` if any item is proposed,
else `extracted` if any item is, else `stated`/`confirmed` — with the union of
citations, and gives each item its own entry in `item_provenance`. The view
renders per item when `item_provenance` is present.

**Created-at-open fields.** `campaign.id` and `campaign.ask_source` are minted
when a human opens the document from a piece of material. They are `stated`,
`by` that human: opening the document on that email is the human saying "this
is the ask".

### 2.3 The nineteen fields

Gate = the state the document cannot reach without the field (D4):
`created` (the document cannot exist), `research` (research cannot run),
`brief` (the document is not brief-ready), `none` (no state waits on it).

| Field | Value type | Gate | Meaning |
|---|---|---|---|
| `campaign.id` | string `cmp_<ULID>` | created | Stable across spin-off. Not the manifest id |
| `campaign.name` | string | created | Working name |
| `campaign.brand` | `{ref: brand/<slug>, name}` | created | The **subject** brand. Ambiguity blocks; never defaults |
| `campaign.client_org` | `{ref: org/<slug>, name}` | created | The agency's client. One client can hold several brands |
| `campaign.categories` | list of leaf codes `<vertical>.<leaf>`, ranked, ≥1 | research | Category leaves. **A list** (answered §11). Contract values from the brand roster row. Left out rather than guessed |
| `campaign.markets` | list of ISO 3166-1 alpha-2, ≥1 | research | Markets. Research runs once per market (§8). The UK is `GB` |
| `campaign.campaign_type` | `launch` \| `always_on` \| `seasonal` \| `rebrand` | none | |
| `campaign.problem` | text | brief | The client's problem, **verbatim** |
| `campaign.objective` | text | brief | The business outcome |
| `campaign.audience_stated` | text | brief | Who the client says it is for |
| `campaign.audience` | object, see below | brief | The **researched** audience. A field backed by pins (answered §11). Origin `proposed` or `confirmed` only |
| `campaign.competitor_set` | list of `{ref: brand/<slug>, name}` | none | Comparators. Every one is a comparator — public material only |
| `campaign.success_measures` | list of `{id, text}` | none | How the client will judge success |
| `campaign.budget_band` | `under_50k` \| `50k_250k` \| `250k_1m` \| `1m_5m` \| `over_5m` (EUR) | brief | A band, never the figure (kept, gate brief — answered §11). Its `source` has a locator and **no quote**: the quote would carry the figure |
| `campaign.in_market` | `{from, to}` ISO dates | none | Drives the rhythm-and-moments lens |
| `campaign.channels_mandated` | list of slugs | none | Channels the client requires (`tv`, `bvod`, `ooh`, …) |
| `campaign.deliverables` | list of slugs | none | What the agency hands over (`tvc_30`, `social_cutdowns`, …) |
| `campaign.constraints` | list of `{id, kind: legal\|brand\|mandatory, text}` | none | Legal, brand and mandatory constraints |
| `campaign.ask_source` | `{material_id, sha256}` | created | The client's own brief or email, by hash. Source of record |

`campaign.audience.value`:

```yaml
definition: "Irish adults who are cutting back on alcohol midweek but …"
size: { fact_ids: [f_…], note: "…" }                  # never a number — the pins that measure it
behaviours: [{ id, statement, fact_ids: [f_…], finding_ids?: [fi_…] }]
attitudes:  [{ id, statement, fact_ids: [f_…], finding_ids?: [fi_…] }]
synthesis_finding_ids: [fi_…]                          # audience syntheses; findings until verified
```

A statement describes; it never restates a pinned figure. The view renders the
figure from the pin.

**Cold start.** A brand with no facts must still reach `created` and
`research`. Every `created` field is satisfiable from the prompt and the
attachment alone (`brand` and `client_org` extracted or stated, `ask_source`
and `id` minted at open); `categories` and `markets` are then extracted or
stated when there is no roster row. A document with zero pins validates.

**Categories are a closed enum**, taxonomy-versioned (18 verticals, 108 leaves).
Until the taxonomy file lands the schema checks the `<vertical>.<leaf>` shape
and the category layer rejects an unknown leaf; the enum then replaces the
pattern in a minor version.

### 2.4 Entity-keyed paths inside fields

Addresses are `<doc-id>#<entity-keyed path>` (Contract 4 §3); a positional
index is invalid. Each list has a natural key:

| List | Item address key | Example |
|---|---|---|
| `categories`, `markets`, `channels_mandated`, `deliverables` | the item itself | `#campaign.markets[GB]` |
| `competitor_set` | the item's `ref` | `#campaign.competitor_set[brand/orchard-hill]` |
| `success_measures`, `constraints`, `audience.behaviours`, `audience.attitudes` | the item's `id` | `#campaign.constraints[c_parent_livery]` |
| `shared/facts.yaml` | fact `id` | `#facts[f_01JA0B3P4Q]` |
| `shared/findings.yaml` | finding `id` | `#findings[fi_01JA0F5E]` |
| `selection.contested`, `selection.gaps` | `id` | `#selection.contested[ct_orchard_hill_abv]` |
| `selection.lenses_run` | `<lens>/<market>` | `#selection.lenses_run[media_spend/GB]` |
| `selection.excluded` | `fact_id` | `#selection.excluded[f_01J7X3Q2MN]` |
| `selection.lenses_skipped` | `<lens>/<market>`, or `<lens>` when skipped in every market | `#selection.lenses_skipped[effectiveness_evidence/GB]` |
| `materials`, `intake.messages` (maps) | the key | `#intake.messages[msg_01JA0A04U]` — the patch path `intake.messages.msg_01JA0A04U` |
| `report` | the whole object | `#report` |

Every list is `uniqueItems`, and item ids are stable: an item keeps its id
when reworded.

---

## 3. How Region A is filled: one grounded extraction, never a form

Handler `extract_ask@1` (declared in `app/pipeline.yaml`; no code yet).

1. **Deterministic lookups** in the layers first — the brand roster row gives
   the category array, markets and client. Each value found is pinned and the
   field written `proposed`, citing the pin.
2. **One structured-output extraction call**, enum-constrained, over the user's
   prompt, the attached material (the client's brief, deck or email is the
   source of record), and what the brand and category layers hold. It abstains
   where nothing supports a value. Structured output is enforced: the no-signal
   case returned prose 5 times out of 5. Until W2-M5 lands, validate every field
   against the schema before writing.
3. **Confirm a short list only**: the category pick, the markets, and subject
   vs comparator brands. Subject vs comparator is reliable when it resolves; when
   it is ambiguous it blocks — it never defaults.
4. **Write back** roster corrections to the brand layer.

The model's self-reported confidence is never read (it reported 92–95% on
answers that varied). Ranked arrays of at most two are stable, so extraction
proposes at most two categories; a human may add more. Do not classify by
retrieving.

The extraction is itself a decision: `kind: edit`, `handler`, `backend`,
`cites` (the material ids and pins), plus `material_read` and `abstained`
(field names) in the flatten tail.

Attached material marked `client-confidential` may ground the extraction and
appear in the brief; the facts derived from it carry the brand licence class
and never promote (C3). `materials.<id>.licence` records it.

---

## 4. Region B — pinned facts (`shared/facts.yaml`)

```yaml
facts:
  - id: f_01J8XZ4K2              # opaque; survives spinoff
    entity: brand/bulmers
    key: awareness.prompted
    value: 0.61
    unit: proportion
    as_of: 2026-06-30            # when true
    retrieved_at: 2026-09-12     # when learned
    sources: [src_4f2a, src_9c11]
    confidence: high             # derived from tier + corroboration, never asserted
    licence: client-confidential # open | licensed-internal | client-confidential
    status: active
    version: 12
    supersedes: f_01J7Q0B8H
    origin: fact://brand/bulmers/awareness.prompted@12
    decision: d_01J8XZ4M7
    pinned_at: 2026-09-18T09:14:02Z
    pin_reason: "Cited in the problem statement"
    # optional, added by this contract:
    layer: brand                 # brand | category — so nothing parses the URI
    market: IE                   # ISO 3166; absent = market-independent
    method: measurement          # observation | report | measurement | synthesis
    stale: { detected_at, current_version, current_fact_id, current_value? }
```

Every key of the source example is required except `supersedes` (null on a
first version). `value` is a scalar — number, string or boolean — because the
layer types its value columns (W1-C1). `confidence` is an ordinal,
`high | medium | low`; never averaged. `status` is the layer row's status at
pin time, `active` or `contested`, frozen with the value. `sources` holds source
ids, a `human:<id>` verification, or — for `method: synthesis` — the fact ids
the synthesis rests on.

**Identity** is `entity + key + market`. Two markets are two facts.

**Origin URI.** `fact://<layer>/<entity path>/<key>@<version>`. The entity path
is the entity ref with its type segment dropped when the type equals the layer,
which is how the source's example reads: `brand/bulmers` in the brand layer is
`fact://brand/bulmers/awareness.prompted@12`. A category leaf in the category
layer is `fact://category/drinks.cider/rhythm.peak_months@3`. A comparator brand
lives in the category layer (D1: "the competitor launched in March is a category
fact"), so it keeps its type: `fact://category/brand/orchard-hill/launch.date@1`.
`layer` is carried as a field so no consumer has to parse this.

**Ids are opaque, never paths.** Every pin names the decision that pinned it.
Licence travels with the pin.

### 4.1 Staleness

A pin is frozen at the version it was taken at and never follows supersession.
When the layer row is superseded — once or several times — the host sets
`stale` on the pin: `current_version`, `current_fact_id`, optionally
`current_value`. The pinned `value`, `version` and `origin` do not change. The
view shows the pinned value, flags it stale, and **offers** the current one;
taking it is a new pin (a `pin` decision) that the lease holder makes. It never
updates silently.

Staleness is detected by the host (`/stale`, Contract 4 §8) and recorded as a
`pin` decision. A pin into the category layer from a brand document is a read
across a database boundary, resolved at render. If the category database is
unreachable the view shows the pin's frozen value and marks its currency
**unavailable** — never an error, never a guess that it is current.

Stale pins do not block the lock; the lock review lists them and accepting the
lock accepts them as frozen.

---

## 5. The projection (D2)

The view receives `shared/data.yaml` only. D2 puts the pins in their own member
*with a scalar projection into `shared/data.yaml` for view bindings*:

```yaml
projection:                       # HOST-WRITTEN, READ-ONLY
  built_from: { facts_sha256, findings_sha256, built_at }
  pins:
    f_01JA0B3P4Q: { layer, entity, key, market?, value, unit, as_of, confidence, licence, method?, stale: true, current_version: 3 }
  findings:
    fi_01JA0F5E: { statement, status, confidence, cites: [f_…], fact_id? }
```

- The host rebuilds it after every change to either member, and after any
  data-update pack (which would otherwise drop it). No agent or human patch
  writes it; the merge policy table omits it on purpose.
- The members are the authority. If `built_from` does not match the members'
  hashes, the view says so rather than rendering the projection as current.
- This is not "a fact's value in the campaign document for convenience": no
  campaign field holds a value, and the projection is regenerated, never
  edited, and self-checking by hash.

View bindings: `projection.pins.<fact_id>.value`,
`projection.findings.<finding_id>.status`.

---

## 6. Findings (`shared/findings.yaml`)

```yaml
findings:
  - id: fi_01JA0F2B
    statement: "Lúnasa arrives in Ireland with permission and in GB as a stranger: …"
    cites: [f_01JA0B2K7M, f_01JA0B2K8N]   # pins it rests on; each must be in facts.yaml
    method: synthesis
    status: proposed                      # proposed | verified | rejected
    derived_by: synthesise_findings@1.0   # handler@version
    confidence: medium                    # derived, never asserted
    derived_at: 2026-09-21T12:10:00Z
    decision: d_01JA0D06SYN
    lens: brands_positioning              # optional
    markets: [IE, GB]                     # optional
    verification: { decision, by: human:<id>, at, fact_id }   # iff verified
    rejection:    { decision, by: human:<id>, at, reason }    # iff rejected
```

### 6.1 Confidence is derived, not asserted

A finding's confidence is the **lowest** confidence among the pins it cites,
**one level lower** if it cites a single pin or any stale pin (floor `low`).
The handler computes it; the model is never asked. Ordinal, never averaged.

### 6.2 Use and verification

The view labels every finding **derived by the agent**. The brief may use a
`proposed` finding only visibly labelled. Nothing unverified enters the layers.

`verify` (a human, under the lease) creates a `human:<id>` source at tier
`reviewer-verified`, writes the finding to the layer as a fact with
`method: synthesis` whose sources are that human source and the cited facts,
and pins it like any other fact. `verification.fact_id` names the pin. The
layer fact inherits the strictest licence among its citations, so a synthesis
over a client-confidential fact stays at brand scope and never promotes (C3).

### 6.3 Rejection

Rejecting a finding sets `status: rejected` with `rejection.reason`, and
**flags every field that cites it** — any campaign field or audience item whose
`finding_ids` contains it. The flag is derived, not stored: the view computes it
from `finding_ids` and `projection.findings.<id>.status`. A flagged field is an
unresolved item until it is revised to stop citing the finding.

The rejection is a `verdict` decision, polarity `bad`, targeting
`#findings[<id>]`, rationale required.

---

## 7. Region C — selection

```yaml
selection:
  lenses_run:  [{ lens, market, ran_at, handler, decision? }]      # one per lens x market
  coverage:    { <lens>: filled | thin | empty }                   # merged across markets
  coverage_by_market: { <market>: { <lens>: filled | thin | empty } }
  gaps:        [{ id, key, lens?, market?, searched, sources_tried[], note? }]
  contested:   [{ id, key, status: open | resolved, values[], chosen?, reason?, decided_by?, opened_by? }]
  excluded:    [{ fact_id, reason, decision? }]
  lenses_skipped: [{ lens, market?, reason, decision? }]           # judged not relevant to this ask
```

- **Lenses** (the Planner Research Taxonomy, in order): `market_structure`
  (quarterly), `brands_positioning` (weekly), `consumer_culture` (weekly),
  `category_codes` (quarterly), `rhythm_moments` (monthly), `media_spend`
  (monthly), `regulation_clearance` (monthly; monthly minimum on regulated
  leaves), `effectiveness_evidence` (quarterly). Coverage groups are lenses.
- **Coverage**: `filled` = corroborated, `thin` = single-source, `empty` =
  nothing found.
- **Gaps**: `key` is `<entity>:<fact key>`, the fact that was wanted;
  `searched` says in words what was looked for.
- **Contested**: `key` is `<entity>:<fact key>[@<market>]`. Each value carries
  `{value, unit?, fact_id, from, sources?}`, where `from` is the writer
  (`<lens>/<market>`, a source, or an actor). `open` entries have no `chosen`
  and no `decided_by`; `resolved` entries must have `chosen`, `reason`,
  `decided_by`. Resolved entries stay, and the losing values stay with the
  written reason. No `open` entry may remain at lock.
- **Excluded**: facts research found and deliberately did not pin, with why.
- **Lenses skipped**: lens × market pairs the `select` stage of
  `start_campaign` judged not relevant to the prompt, with the reason.
  `market` absent means every market. **Skipped is not empty**: a skipped pair
  has no `lenses_run` entry and no `coverage_by_market` entry, merged coverage
  is computed over the markets that ran the lens (a lens skipped everywhere
  has no `coverage` entry), and the report lists it under `not_researched`.
  A later `research_lens` run of a skipped pair removes its entry.

The values inside a contest are the adjudication record, not pins: they may
point at layer rows that are not pinned. Only the chosen one is pinned.

---

## 8. Research runs per market, then merges

Handler `research_lens@1` fans out **one run per lens × market** — eight lenses
times `campaign.markets` — in parallel, each on its own branch
`agents/<user>.research_lens.<lens>-<market>/` (user from `Ctx`). A run reads
the retrieval pairs for its one market. It writes fact rows to the layers (no
lease needed) and, on its branch, proposed pins and selection entries.

Merging, under the lease, is by **entity + key + market**:

| Case | Result |
|---|---|
| Same identity, same value | One pin; sources unioned (corroboration) |
| Same entity + key, different market | Two facts, two pins — a market difference is not a conflict |
| Same identity, different values — including a market-independent key two market runs disagree on | A contest in `selection.contested`, `open`, nothing picked |

Merged coverage per lens: `filled` only if filled in every market, `empty` only
if empty in every market, otherwise `thin`. Merge policies are declared in
`app/pipeline.yaml`: `ask` for every `campaign.*` field and for contests and
coverage, `append` for `lenses_run`, `gaps`, `excluded`, `lenses_skipped`
and `intake.messages` (a key-wise union of the map), `last-write` for
`report` (§17).

---

## 9. Handoff mapping — campaign to retrieval pairs

Stored in the document, in `app/pipeline.yaml` → `retrieval_pairs`, so it
travels with every instance:

| Retrieval pair | From |
|---|---|
| `brand` | `campaign.brand.value.name` |
| `client` | `campaign.client_org.value.name` |
| `category` | each of `campaign.categories.value` — one pair per category |
| `campaign_type` | `campaign.campaign_type.value` |
| `problem` | `campaign.problem.value` |
| `objective` | `campaign.objective.value` |
| `audience` | `campaign.audience_stated.value` |
| `competitors` | `campaign.competitor_set.value[].name` |
| `market` | each of `campaign.markets.value` — one pair per market, one research run each |

**Scope (tenant, brand) is never in this mapping.** The middleware injects it
from `Ctx` (M3). A handler that could read scope from the document could widen it.

---

## 10. Review marks

Every mark is a typed decision on an address `<doc-id>#<entity-keyed path>`
(§2.4). The kinds, their required fields and the lease rules are Contract 4 §3–§4
and §6; this app uses them as follows.

| The reviewer… | Decision kind | Target |
|---|---|---|
| validates a field | `verdict`, polarity good | `#campaign.<field>` |
| rejects a field | `verdict`, polarity bad, `reason_code` + rationale | `#campaign.<field>` (the field is then not re-extracted over — §2.2) |
| asks more about a field | `/history?address=` (a read — no decision) | `#campaign.<field>` |
| sees provenance | the envelope + `/history` | `#campaign.<field>` |
| accepts / corrects an extracted or proposed field | `edit`; origin becomes `confirmed` | `#campaign.<field>` |
| marks something confidential | `classify` `{model, export, corpus}` | the field; on a fact-backed field it supersedes the fact's licence in the layer |
| resolves a contest | `resolve`, rationale required | `#selection.contested[<id>]` |
| verifies a finding | `verify` | `#findings[<id>]` |
| rejects a finding | `verdict`, polarity bad, rationale required | `#findings[<id>]` |
| takes the current version of a stale pin | `pin` | `#facts[<id>]` |
| locks | `approve` | the document |

Research merges open contests (`contest`) and record pins (`pin`); synthesis
records `finding`. Every decision names its `targets[]` and what it `cites[]`.

Every agent decision also says why in `reasoning` (Contract 4 §3; required on
the kinds middleware-api.md §3 lists): what was decided, the evidence point by
point with the pins, sources and materials each rests on, the alternatives
that lost, the certainty — for pins and findings their derived confidence
(§4, §6.1), never the model's — what would reverse it, and, when a person
should look, why. A reviewer's mark may carry one too (`patch-data` takes an
optional `reasoning`); it is never required of a person. The view's "why is
this here" shows it compactly, falling back to `rationale`.

---

## 11. Lock prerequisites

Lock = accept = seal, file-wide (Contract 4 §7). This document cannot lock while:

1. any entry in `selection.contested` is `open`, or any `contest` decision is
   unresolved — including ones carried from upstream;
2. an agent branch is unmerged;
3. any finding is `proposed`;
4. any field cites a rejected finding (§6.3);
5. any bad verdict is unanswered — the field revised, or the verdict overridden
   with a written reason;
6. any `created`, `research` or `brief` gated field is absent. A locked campaign
   is brief-ready. (App rule; the OS lock list is 1–5.)

At lock the reviewer is shown the findings list; confirming verifies each
(`verify`, written to the layer); rejecting one flags its fields and the lock
waits. Stale pins are listed and accepted as frozen. One `approve` records the
version and content hash.

Spin-off to a brief is allowed from an unlocked campaign (Contract 4 §5); the
brief carries this document whole, open items and all.

---

## 12. The four open calls — answered

Answered by the owner, 2026-09-23.

| Call | Answer |
|---|---|
| Can `campaign.category` hold more than one value? | **Yes. `campaign.categories` is a list.** A campaign spanning two leaves pins facts from both; one retrieval pair per category |
| How do multiple markets interact with single-market retrieval? | **Research runs once per market, then merges** by entity + key + market. A market difference is two facts; a conflict is a contest (§8) |
| Is the researched audience a fact or a field? | **A field**, `campaign.audience`, origin `proposed` or `confirmed`, backed by pinned facts. An audience synthesis is a finding until verified |
| Does `budget_band` earn its place? | **Kept, gate `brief`.** A band, never the figure |

---

## 13. Calls this contract made that the source did not settle

For the W1-C4 gate to confirm or overturn.

1. **Field shape**: an envelope per field, not a parallel provenance map (§2.1).
2. **Gate in the data** as a schema-pinned constant, plus `x-gate` in the schema.
3. **Entity refs are `{ref, name}`**, because the handoff mapping sends names.
4. **Prose lists carry item ids** (`success_measures`, `constraints`, audience
   items) so they are addressable without positions; `constraints` items carry
   `kind`.
5. **Mixed-origin lists**: least-certain origin at field level plus
   `item_provenance`.
6. **`campaign.id` and `ask_source` are `stated`** by the human who opened the
   document.
7. **`budget_band` values** are five EUR bands; its span carries no quote.
8. **`market` on pins**, and fact identity = entity + key + market. W1-C1's
   envelope has no market field; it needs one, or market goes into the key.
9. **Origin URI grammar** drops the entity's type segment when it equals the
   layer (to match the source's example); comparator brands keep theirs. Plus
   an explicit `layer` field.
10. **Staleness is a `stale` object on the pin**, set by the host; the value is
    untouched.
11. **The projection** implements D2's scalar projection, host-written and
    hash-checked (§5). The alternative — the host exposing
    `window.__CLAN__.facts` and `.findings` — needs host work and a second
    binding surface.
12. **Finding confidence**: minimum of the cited pins, one lower for a single or
    stale citation.
13. **Rejecting a finding is a `verdict`** (bad) on `#findings[<id>]`; Contract 4
    lists `verify` but not a reject kind.
14. **The rejected-finding flag is derived**, not stored.
15. **Category codes are `<vertical>.<leaf>`**, pattern-checked until the
    taxonomy enum lands.
16. **Lock also requires every gated field present** (§11 item 6).
17. **Coverage merge rule** (§8) and coverage groups = lenses.
18. **Strictness**: `campaign.*`, envelopes and selection items reject unknown
    keys; the top level of `shared/data.yaml` does not, so other blocks
    (`example_notice`, future host keys) survive.
19. **`materials`** is a block in data, keyed by material id, holding each
    source's hash and licence.
20. **Handoff**: `brand` sends the brand's name and `client` the client org's.

---

## 14. Binding cheat sheet for the view

```
campaign.<field>.value / .origin / .gate / .decision
campaign.<field>.source.{material_id, locator, quote}     extracted
campaign.<field>.fact_ids[]  -> projection.pins.<id>      proposed
campaign.<field>.finding_ids[] -> projection.findings.<id>  flagged if .status == rejected
campaign.<field>.by, .confirmed_from                       confirmed / stated
campaign.<list>.item_provenance.<item key>.origin …        per item
campaign.audience.value.{definition, size.fact_ids, behaviours[], attitudes[], synthesis_finding_ids}
selection.{lenses_run[], coverage.<lens>, coverage_by_market.<m>.<lens>, gaps[], contested[], excluded[], lenses_skipped[]}
intake.messages.<msg_id>.{role, text, at, by, material_id, attachments[], job_id, stage, question, answer}   §16
report.{built_at, handler, based_on, headline, summary[], sections[].blocks[], confirm[], not_researched[]}  §17
materials.<material_id>.{name, kind, licence, asset}
projection.pins.<fact_id>.{value, unit, as_of, confidence, licence, stale, current_version}
projection.findings.<finding_id>.{statement, status, confidence, cites[]}
projection.built_from.{facts_sha256, findings_sha256}
```

States the view must be able to show, present in `example/`: each of the four
origins; mixed-origin lists; a stale pin superseded once and one superseded
twice; brand-layer and category-layer pins; a pin taken while its layer row was
contested; a verified finding and the synthesis fact it became; proposed
findings; a rejected finding and the field it flags (`campaign.in_market`); a
good and a bad verdict (in the chain); coverage filled, thin and empty; two
gaps; an open and a resolved contest; an exclusion; a classify mark; a chat
thread (the prompt with an attachment, stage messages, a question the bot
asked about the subject brand and the person's answer, the confirmed field it
wrote); a report whose every claim cites real pins and findings; a skipped
lens.

Not in the example, because they are not stored: an absent gated field (render
the empty template `{}` — every field shows as missing with its gate), and a pin
whose currency is **unavailable** (fail the category-layer read at render).

---

## 15. Build order

| Task | What |
|---|---|
| W1-C3 (this part) | This contract, the three schemas, `context.md`, `pipeline.yaml`, the example |
| W1-C3 (view) | `app/templates/campaign-research/index.html` — the four affordances against §14 |
| W1-C3 (packaging) | Package the template and the example `.clan`; register the two members; `clan validate --strict` |
| W1-C1 | Carry `market` in the fact envelope (§13 item 8) |
| W1P-I5 | Decision flatten tail — until then the SDK drops the §3 fields on rewrite |
| W3-J1 | Fill it from real research |

---

## 16. The chat intake (`intake.messages`)

A new campaign starts as a chat: the person types a prompt, attaches
material, and the middleware's `start_campaign` job extracts the ask,
identifies the brand and categories, researches and reports
(`docs/contracts/middleware-api.md` §8). The thread is kept in the document.

```yaml
intake:
  messages:                               # a MAP keyed by message id — never an array
    msg_01JA0A01U:
      role: user                          # user | agent
      by: human:u_aoife                   # user messages: who
      at: 2026-09-21T08:58:40Z
      text: "Brief from Glenmore attached — …"
      material_id: mat_prompt01           # this text, indexed as a kind: prompt material
      attachments: [mat_email01]          # each a key of materials
    msg_01JA0A03A:
      role: agent
      job_id: job_01JA0C2288              # agent messages: the job and the stage
      stage: identify                     # extract | identify | select | research | synthesise | report
      at: 2026-09-21T09:00:06Z
      text: "The email names two brands … Which one is your client's?"
      question:                           # = job.question when the job entered needs_input
        id: q_01JA0A03SUBJ
        text: "Which brand is the client's?"
        address: "<doc-id>#campaign.brand"
        allow_text: true
        options:
          - { id: lunasa, label: "Lúnasa", value: { ref: brand/lunasa, name: "Lúnasa" },
              origin: extracted, source: { material_id: mat_email01, locator: "¶1", quote: "…" } }
          - { id: none, label: "None of these" }      # the escape: no value, answered with text
    msg_01JA0A04U:
      role: user
      by: human:u_aoife
      at: 2026-09-21T09:05:00Z
      text: "Lúnasa"
      job_id: job_01JA0C2288              # the job it answers
      answer: { question_id: q_01JA0A03SUBJ, option_id: lunasa }   # option_id or text, exactly one
```

### 16.1 Why a map

Two writers add to the thread: the person, through the view (a human write),
and the middleware, through a job's `data_patch`. A JSON merge patch replaces
an array whole, so an array would make every write carry the whole thread —
and a job computed before the person's latest message would, on landing,
either drop it or be judged a stale write over it and open a contest. A map
merges key-wise: each writer adds its own keys and never touches the other's,
so there is no stale-base collision. Message ids are opaque, ULID-style
(`msg_…`), unique across writers; display order is `at`, then the key.

### 16.2 Who writes what

| Message | Written by | Carries |
|---|---|---|
| `role: user` | the view, as a human write (a decision with `actor: human:<id>`) | `by`; the first carries `material_id` (the prompt, `kind: prompt`, sha256 of the text's UTF-8 bytes) and `attachments`; an answer carries `job_id` and `answer` |
| `role: agent` | the middleware's `data_patch`, each with a decision targeting `#intake.messages[<id>]` | `job_id`, `stage`; a question carries `question` |

The middleware never writes a `role: user` message; the view never writes a
`role: agent` one. Messages are display and record: no field's value is read
from one. The person's answers settle fields by the view's own write of the
field (§16.3); the thread records that they did.

### 16.3 The chat is D3's confirmation step, split in two

D3 asks for a short confirmation list — the category pick, the markets,
subject vs comparator — and §3 says an ambiguous subject brand blocks and
never defaults. The chat splits that list by whether it blocks:

- **Blocking**, asked in the chat while research waits: what `identify`
  cannot decide (`docs/contracts/middleware-api.md` §8.6) — the subject brand
  when several brands are named or none clearly is the client's, the
  categories when the brand has no roster row, the markets when the material
  names none.
- **Non-blocking**, listed under the report (`report.confirm`, §17): every
  other value the person has not yet accepted.

**A person's answer is a human write and becomes `confirmed`.** The view
writes the answered field as the person: a picked candidate that was
`extracted` or `proposed` becomes `origin: confirmed`, `confirmed_from:
<that origin>`, `by: human:<id>`, keeping its `source` / `fact_ids`; a
candidate built from the person's own free text becomes `origin: stated`.
Free text itself never becomes a value: the middleware turns it into
candidates and asks again. Confirmed and stated fields belong to the human
(§2.2), so the job never writes them.

The origins stay honest: `extracted` (the chat read it from the material,
quoted), `proposed` (the roster pin), `confirmed` (the person picked it in
the chat, or from the confirm list), `stated` (the person typed it).

## 17. The report (`report`)

```yaml
report:                                  # MIDDLEWARE-WRITTEN, rendered read-only
  built_at: 2026-09-23T08:10:00Z
  handler: compose_report@1.0            # the response's handler (start_campaign@1.x for the report stage)
  based_on: { version, facts_sha256, findings_sha256 }
  headline: { text, cites: [f_… | fi_…] }
  summary:  [{ text, cites: [f_… | fi_…] }]           # ≥1 line
  sections:
    - id: s_market                       # item key, unique
      title: "The market"
      lens: market_structure             # optional
      market: IE                         # optional
      blocks:                            # ≥1, in reading order
        - { kind: claim,   text: "…", cites: [f_…, fi_…] }
        - { kind: pins,    fact_ids: [f_…] }
        - { kind: finding, finding_id: fi_… }
        - { kind: gap,     gap_id: g_… }
        - { kind: contest, contest_id: ct_… }
  confirm:        [{ address: "<doc-id>#campaign.<field>", label, why }]
  not_researched: [{ lens, market?, reason }]
```

| Block | The view renders |
|---|---|
| `claim` | the sentence, with its cites as chips (pins from `projection.pins`, findings from `projection.findings`) |
| `pins` | each pin's value, unit, market, as-of, confidence and stale flag, from `projection.pins` |
| `finding` | the finding, labelled **derived by the agent** unless verified |
| `gap` | the `selection.gaps` entry: what was looked for and not found |
| `contest` | the `selection.contested` entry, open or resolved, with every value |

**The cite rule.** The headline, every summary line and every `claim` block
cite at least one id; each is a pin in `shared/facts.yaml` or a finding in
`shared/findings.yaml` — never a source, a material or a URL. No cite and no
`finding` block names a rejected finding. A claim states no figure that is
not in a cited pin (or verbatim in a cited finding); figures are shown by
`pins` blocks, which render the pinned value. Entity names that contain
digits ("Lúnasa 0.0") are names, not figures. `gap_id` and `contest_id` name
entries in `selection`.

**`based_on`** is the document the report was composed from: its
`clan.version` and the host's member hashes, `projection.built_from.
{facts_sha256, findings_sha256}`, as read. The view shows the report as out
of date — and offers **Refresh report** (`compose_report`) — when those
hashes differ from `projection.built_from`, or when a decision newer than
`built_at` targets `campaign.*` or `selection.*`.

**`confirm`** is D3's short list, non-blocking (§16.3): `brand`,
`categories`, `markets`, `competitor_set` — each while its origin is
`extracted` or `proposed` — then every other campaign field whose origin is
`proposed`. `label` says what is being confirmed, `why` why it needs a
person. The view renders each with the field's confirm action (§10).

**`not_researched`** lists every lens × market that was not researched, with
why: at least every `selection.lenses_skipped` entry, with its reason.
Researched-and-empty is not here: that is a `gap` block and `coverage: empty`.

**Who writes it.** The middleware only — the `report` stage of
`start_campaign` and `compose_report` — replacing it whole on every write,
with a decision targeting `#report`. It is derived, never edited: the view
renders it read-only and no human patch writes it; a person changes the
report by changing the document and refreshing. Merge policy `last-write`:
the newest composition wins, and `based_on` says what it describes. Never
HTML, never markdown to be rendered as markup: the view escapes every string.

## 18. Calls the chat intake made

Decided while writing the intake contract (2026-09-24), for the owner to
confirm or overturn.

1. **The answered field is the view's human write**, not the middleware's:
   the middleware never writes a confirmed or stated field (§2.2), and only
   a human write carries the person as actor.
2. **Question options carry typed values** (the whole field value) with the
   candidate's origin and evidence, so the view can write the envelope
   without guessing; an option without a value is the escape.
3. **Free text becomes candidates, then a pick**: text never becomes a value
   directly, so the view never mints a brand ref or a leaf code.
4. **A brand read from the material is `extracted`**, not `proposed`: the
   brief's "proposed with the evidence span" is Contract 3's `extracted`
   (`proposed` requires pins).
5. **Markets are asked when absent** — the `research` gate needs them.
6. **The prompt is a material** (`kind: prompt`, hashed), linked from the
   first message by `material_id`, so spans can cite it.
7. **User messages carry `by`**; agent messages require `job_id` and `stage`.
8. **Addresses for maps use the key in brackets**: `#intake.messages[<id>]`,
   `#materials[<id>]`.
9. **`selection.lenses_skipped` items carry an optional `decision`** like the
   other selection lists, and skipped pairs have no coverage.
10. **`report.confirm` is computed** (the D3 four while unconfirmed, then
    every proposed field) — a rule the example checker enforces.
11. **Merge policies**: `append` for `intake.messages` and `lenses_skipped`,
    `last-write` for `report` — names the SDK already knows.
12. **`based_on` hashes are the host's**, copied from the
    `projection.built_from` the composer read; the report stage therefore
    composes once the earlier stages have landed.
