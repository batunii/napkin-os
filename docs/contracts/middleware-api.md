# Middleware API — `napkin.middleware/1`

Status: **binding** for the host, the templates, the stand-in middleware and the
real one. Reads with: `docs/contracts/campaign-clan.md` (Contract 3 — what a
`change` may contain), `docs/contracts/os-layer.md` (Contract 4 — decisions,
addresses, M3/M4).

The one HTTP contract between the Napkin host and the middleware. The host and
the templates depend on this document only — never on an implementation.

**The swap guarantee.** Two implementations exist: the dependency-free stand-in
in `mock-middleware/`, and the FastAPI middleware in `server/`. Both must pass
`mock-middleware/contract_test.py` **unchanged**. The suite names no
implementation, no port and no backend; if the real middleware needs the suite
edited to pass, the middleware is wrong or this contract is, and the fix is
made here first. Moving from one to the other is changing one value, the
`middleware` endpoint in `workspace.yaml`. No template, host or test code may
name the stand-in, its port, or branch on which implementation answered.

---

## 1. Request — host → middleware

`POST <endpoint>` where `<endpoint>` is `proxies.middleware.endpoint` in
`workspace.yaml` (path `/v1/tasks`). The app sends it through the host's
`clan://api-proxy` with `request_kind: "middleware"`; the host adds auth and the
`clan` context. The app never talks to the middleware directly.

```json
{
  "request_kind": "middleware",
  "payload": { "task": "extract_ask | research_lens | synthesise_findings | job_status",
               "input": { "...": "task-specific, below" } },
  "clan": { "id": "<manifest id>", "version": "<doc version the host holds>",
            "app": { "app_id": "...", "version": "..." },
            "schema": {}, "data": {}, "facts": [], "findings": [], "pipeline": {},
            "decision_chain": {}, "context": "...", "lineage": {} }
}
```

- `clan.id` is required for every task, `clan.version` for every task except
  `job_status`. `facts` / `findings` are the members' lists (a
  `{facts: [...]}` / `{findings: [...]}` object is also accepted);
  `decision_chain` is `{decisions: [...]}`; `pipeline` is the parsed
  `app/pipeline.yaml`.
- **Task → handler.** `payload.task` is resolved against `clan.pipeline.tasks.<task>.handler`
  (`name@major`). When the document carries no pipeline, the middleware's
  declared built-in map is used (`extract_ask@1`, `research_lens@1`,
  `synthesise_findings@1`). A task the pipeline does not declare, an
  unregistered handler, a handler registered for a different task, or a major
  the middleware does not implement is a hard error (M4) — never a
  fall-through to a default. `job_status` is a transport verb and is not
  resolved against the pipeline.
- **Scope (org/brand) is never in the request** (M3). The middleware derives it
  from auth. A request that carries `scope`, `tenant`, `org`, `org_id` or
  `tenant_id` at the top level of the body, `payload`, `payload.input` or `clan`
  is refused with `400 invalid_input` — a caller must not be able to widen what
  it reads. In development the tenant is a fixed dev tenant from middleware
  configuration, reported back in `trace.scope`.

### Task inputs

| Task | `input` | Notes |
|---|---|---|
| `extract_ask` | `{ "prompt": "...", "attachments": [{ "name", "sha256", "text"? }] }` | `text` is the host-extracted text. An attachment without it is recorded as unread and grounds nothing. Nothing to read at all is `400 invalid_input` |
| `research_lens` | `{ "lenses"?: [lens ids], "markets"?: [ISO 3166-1 alpha-2] }` | Defaults: all eight lenses × `campaign.markets`. One run per lens × market (N4), merged by entity + key + market. `UK` is not a code (the UK is `GB`). Research with no markets or no categories is `400 invalid_input` (gate `research`) |
| `synthesise_findings` | `{ "lenses"?: [lens ids] }` | Reads `clan.facts` (pins only) |
| `job_status` | `{ "job_id": "..." }` | |

Lens ids, in taxonomy order: `market_structure`, `brands_positioning`,
`consumer_culture`, `category_codes`, `rhythm_moments`, `media_spend`,
`regulation_clearance`, `effectiveness_evidence`.

## 2. Response — middleware → host (HTTP 200)

```json
{
  "api": "napkin.middleware/1",
  "task": "research_lens",
  "handler": "research_lens@1.0",
  "job": { "id": "job_...", "state": "queued | running | done | failed",
           "progress": { "done": 3, "total": 16 },
           "started_at": "...", "finished_at": null, "error": null },
  "result": { "summary": "human-readable one-liner", "...": "task-specific, display only" },
  "change": null,
  "trace": { "scope": { "org": "...", "brand": "..." }, "backend": "...", "model": null,
             "hits": [{ "id": "...", "scope": "...", "source": "..." }],
             "usage": { "input_tokens": 0, "output_tokens": 0 } }
}
```

- `handler` is `name@major.minor` — the form `findings[].derived_by` and
  `selection.lenses_run[].handler` must match (`^[a-z_]+@\d+(\.\d+)?$`). Its
  name and major are the ones the pipeline declared.
- `result` is display only. Nothing in it is written to the document.
- `trace.usage` is what was actually spent; an implementation that ran no model
  reports zeros and never an estimate. `trace.model` is `null` when no model ran.
  `trace.hits` lists what was read: facts (`scope` = layer, `source` = origin
  URI) or sources (`source` = the source URI).
- A 200 body never has a top-level `error` key.

### Jobs

- **Short tasks** (`extract_ask`) answer `job.state: done` and a `change` in one
  response.
- **Long tasks** (`research_lens`, `synthesise_findings`) answer
  `job.state: queued | running` with `change: null`; the caller polls
  `job_status`. The `done` poll carries the `change`; later polls of a done job
  return the same change again. Real research runs minutes and must survive the
  laptop closing (N2), so a job outlives the request that started it.
- `progress.total` is the number of units of work: for `research_lens`, one per
  lens × market. `progress.done` never goes backwards.
- A `job_status` response describes the job: its `task` and `handler` are the
  job's (`research_lens`, `research_lens@1.0`), because the host copies the
  handler onto the decisions it applies.
- A job belongs to the tenant (from auth) and the document it was started on.
  Polling it from another tenant or another `clan.id` is `404`, the same as an
  id that never existed.
- `failed` carries `job.error: { type, message }` and no change.

## 3. `change`

```json
{
  "doc": "<manifest id it was computed for>",
  "base_version": "<the clan.version it read>",
  "data_patch": { "campaign": { "<field>": { "value": "...", "origin": "extracted", "gate": "...",
                                              "source": {}, "decision": "d_..." } },
                  "selection": {}, "materials": {} },
  "facts_append": [ "entries per facts.schema.json" ],
  "findings_append": [ "entries per findings.schema.json" ],
  "decisions": [ { "id": "d_...", "kind": "edit | finding | contest | pin", "agent": "<handler>",
                   "action": "...", "rationale": "...", "targets": ["<doc-id>#<path>"],
                   "cites": ["f_...", "src_...", "mat_..."], "handler": "...", "backend": "...",
                   "timestamp": "..." } ]
}
```

Every change must leave the document valid against the campaign schemas
(`app/templates/campaign-research/{schema,facts.schema,findings.schema}.json`):

- **`data_patch`** is a JSON merge patch (RFC 7396) over `shared/data.yaml`. It
  never touches `projection` (host-owned). Because a merge patch replaces arrays
  whole, a patch to an array (`selection.lenses_run`, `gaps`, `contested`, a
  list field) carries the full array: what the document held plus this run's
  entries. Objects (`selection.coverage`, `coverage_by_market`, `materials`)
  merge key-wise. The merged data must validate against `schema.json`.
- **Campaign fields** are envelopes per Contract 3 §2. `extracted` carries a
  `source` span whose `quote` is verbatim in the cited material; `proposed`
  carries `fact_ids` that are pins in the document. A field the material does
  not support is **absent** — the extraction decision lists it in `abstained`.
  `budget_band` is a band, never the figure; its span has no quote. A field
  whose origin is `confirmed` or `stated`, or that carries an unanswered bad
  verdict, is never written (Contract 3 §2.2).
- **Materials.** Spans cite material ids. An attachment is matched to
  `data.materials` by `sha256`; material the document has not indexed yet is
  added to `data_patch.materials` with licence `client-confidential` (the
  strictest class) until a human reclassifies it.
- **`facts_append`**: each entry validates against `facts.schema.json`, and so
  does the document's facts list after the append. Every fact carries sources
  (source ids), confidence **derived** from source tier + corroboration (never
  self-reported), licence (the strictest of its sources), `as_of` (when true) ≤
  `retrieved_at` (when learned), `origin fact://<layer>/<entity path>/<key>@<version>`,
  and the `decision` that pinned it. No two pins share entity + key + market
  with different values.
- **Contests.** Two runs disagreeing on one entity + key (+ market) produce a
  `selection.contested` entry, `status: open`, carrying every value with its
  `fact_id`, `from` and `sources`, and a `contest` decision targeting it. None
  of the contested values is pinned — nothing is silently picked.
- **`findings_append`**: each validates against `findings.schema.json`; always
  `status: proposed` (only a human verifies — D1 amended); `derived_by` = the
  response's `handler`; every `cites` id is a pin in the document; confidence is
  the lowest cited confidence, one lower for a single or any stale citation
  (Contract 3 §6.1).
- **`decisions`**: ids `d_…`, kinds from Contract 4, `targets` are
  `<doc-id>#<entity-keyed path>` (never positional), `handler` and `backend`
  equal the response's. Every envelope's and fact's `decision` names one of
  them.
- **Source records.** Facts cite source *ids*; the source records (URI, tier,
  licence) live in the layer. An implementation may echo them in
  `result.sources` for display; the document does not store them.

## 4. Errors

| Status | Body | When |
|---|---|---|
| 400 | `{"error":{"type":"unknown_task","message"}}` | task not in the contract, or not declared by the document's pipeline |
| 400 | `{"error":{"type":"unknown_handler","message"}}` | handler unregistered, for another task, malformed, or an unimplemented major |
| 400 | `{"error":{"type":"invalid_input","message"}}` | body not JSON; `request_kind` not `middleware`; no task; no `clan.id` / `clan.version`; scope in the request; bad lens, market or attachment; nothing to read; gate not met |
| 401 | `{"error":{"type":"unauthenticated","message"}}` | credentials missing or wrong (when the deployment requires them) |
| 404 | `{"error":{"type":"unknown_job","message"}}` | job id unknown to this tenant and document |
| 409 | `{"error":{"type":"version_conflict","message"}}` | `base_version` is stale — only an implementation that holds the document (the web product) can know this |
| 500 | `{"error":{"type":"internal","message"}}` | anything else. The message never carries request content |

Never a 200 with a fallback. The desktop host strips upstream error bodies
before they reach the app; the status survives.

**Stale base.** A job reads the document once, when it starts. If the document
moved on before the job finished, the `done` change still names the version it
read in `base_version`. An implementation that does not hold the document
(the desktop case) returns it anyway and the **host** decides — apply, merge
under the field policies, or refuse. One that does hold it may answer `409`.

## 5. Who applies a change — the host, never the app

The template never writes middleware output. When a `clan://api-proxy` reply for
`request_kind: middleware` carries a `change`:

1. the host checks `change.doc` equals the open document, else refuses;
2. applies it through the single write funnel as ONE Change: `data_patch` →
   `shared/data.yaml`, `facts_append` → `shared/facts.yaml`, `findings_append` →
   `shared/findings.yaml`, decisions appended with actor `process:middleware` and
   the response's handler and backend;
3. rebuilds `projection` (pins by fact id, findings by id, `built_from` hashes)
   per Contract 3 §5;
4. emits the usual patch event so the view re-renders, and returns the envelope
   to the app with `change` replaced by `{ "applied": true, "version": "<new>" }`
   or `{ "applied": false, "reason": "..." }`.

In the web product the same apply runs server-side in-process (P2); the envelope
does not change.

## 6. Auth

The host holds the secret (`secrets.yaml`, `proxies.middleware.secret_ref`) and
sends it as `Authorization: Bearer <secret>` (`auth_kind: bearer`) or
`x-api-key` (the default). The sandboxed app never sees it. The middleware maps
the credential to a tenant — that mapping is the only source of scope.

## 7. Dev wiring

```yaml
# <app-config-dir>/workspace.yaml
proxies:
  middleware:
    endpoint: http://localhost:8790/v1/tasks    # the stand-in
    # endpoint: https://middleware.example/v1/tasks   # the real one — the only line that changes
    auth_kind: bearer
    secret_ref: middleware_api                  # key in secrets.yaml
```

Configure `proxies.middleware` explicitly: an unconfigured kind falls back to
`agent_url`, which is the briefing agent and does not speak this contract.

- Stand-in: `python3 mock-middleware/server.py` (stdlib only), `:8790`,
  `POST /v1/tasks`, `GET /healthz`. See `mock-middleware/README.md`.
- Contract suite: `python3 mock-middleware/contract_test.py --base-url <url>
  [--schema-dir app/templates/campaign-research] [--token <secret>]`. The real
  middleware must pass it unchanged.
