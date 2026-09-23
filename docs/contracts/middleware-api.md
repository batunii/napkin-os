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
  "payload": { "task": "extract_ask | research_lens | synthesise_findings | start_campaign | answer_question | compose_report | job_status",
               "input": { "...": "task-specific, below" } },
  "clan": { "id": "<document_id>", "revision": "<manifest id>",
            "version": "<doc version the host holds>",
            "app": { "app_id": "...", "version": "..." },
            "schema": {}, "data": {}, "facts": [], "findings": [], "pipeline": {},
            "decision_chain": {}, "context": "...", "lineage": {} }
}
```

- `clan.id` is the document's identity — the manifest's `document_id`, which
  every revision carries unchanged (a file written before it existed uses its
  manifest `id`). It is the `<doc-id>` prefix of every address. `clan.revision`
  is the manifest `id` of the revision the host read, fresh on every write;
  informational. `clan.version` is the host's version of the archive (for the
  desktop, `sha256:` of its bytes). `clan.id` is required for every task,
  `clan.version` for every task except `job_status`. `facts` / `findings` are the members' lists (a
  `{facts: [...]}` / `{findings: [...]}` object is also accepted);
  `decision_chain` is `{decisions: [...]}`; `pipeline` is the parsed
  `app/pipeline.yaml`. The host sends the whole `clan` — the document as it
  holds it now — on **every** request, `job_status` included: a
  `start_campaign` job reads it on each poll (§8.4).
- **Task → handler.** `payload.task` is resolved against `clan.pipeline.tasks.<task>.handler`
  (`name@major`). When the document carries no pipeline, the middleware's
  declared built-in map is used (`extract_ask@1`, `research_lens@1`,
  `synthesise_findings@1`, `start_campaign@1`, `answer_question@1`,
  `compose_report@1`). A task the pipeline does not declare, an
  unregistered handler, a handler registered for a different task, or a major
  the middleware does not implement is a hard error (M4) — never a
  fall-through to a default. `job_status` is a transport verb and is not
  resolved against the pipeline. `answer_question` **is** resolved (the
  pipeline must declare it), but like `job_status` its reply describes the job
  it answers (§2, Jobs).
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
| `start_campaign` | `{ "prompt": "...", "attachments": [{ "material_id", "name", "sha256", "text"? }] }` | The chat intake (§8). Long. Each `material_id` must be a key of `clan.data.materials` with that `sha256` (the view indexes the file first). Nothing to read at all is `400 invalid_input`, as for `extract_ask` |
| `answer_question` | `{ "job_id": "...", "question_id": "...", "option_id"?: "...", "text"?: "..." }` | Exactly one of `option_id` / `text` (§8.3) |
| `compose_report` | `{}` | Short. Re-composes `data.report` from the document as it stands (§8.5) |
| `job_status` | `{ "job_id": "..." }` | Free: never charged to quota (§9) |

Lens ids, in taxonomy order: `market_structure`, `brands_positioning`,
`consumer_culture`, `category_codes`, `rhythm_moments`, `media_spend`,
`regulation_clearance`, `effectiveness_evidence`.

## 2. Response — middleware → host (HTTP 200)

```json
{
  "api": "napkin.middleware/1",
  "task": "research_lens",
  "handler": "research_lens@1.0",
  "job": { "id": "job_...", "state": "queued | running | needs_input | done | failed",
           "progress": { "done": 3, "total": 16 },
           "stage": "research", "question": null,
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
  `result.messages` (`start_campaign`, `answer_question`, `compose_report`)
  is `[{ "id": "msg_...", "text": "...", "stage": "..." }]`: the job's chat
  narration so far, in order. Each id is also the key the middleware writes
  that message under in `intake.messages` (§8.2), so the view shows the list
  while the change is in flight and de-duplicates by id once it lands.
- `job.stage` and `job.question` are present on `start_campaign` jobs (and
  the replies of `answer_question`, which describe one) and on
  `compose_report` replies (`stage: report`, `question: null`, §8.2); other
  tasks omit them. `job.question` is non-null exactly when `job.state` is
  `needs_input`.
- `trace.usage` is what was actually spent; an implementation that ran no model
  reports zeros and never an estimate. `trace.model` is `null` when no model ran.
  `trace.hits` lists what was read: facts (`scope` = layer, `source` = origin
  URI) or sources (`source` = the source URI).
- A 200 body never has a top-level `error` key.

### Jobs

- **Short tasks** (`extract_ask`, `compose_report`) answer `job.state: done`
  and a `change` in one response.
- **Long tasks** (`research_lens`, `synthesise_findings`, `start_campaign`)
  answer `job.state: queued | running`; the caller polls `job_status`. A
  `queued`, `running` or `needs_input` reply **may** carry a `change` for work
  finished since the previous reply — `start_campaign` does, stage by stage
  (§8.4); `research_lens` and `synthesise_findings` send `change: null` until
  `done`. The `done` poll carries the (last) change; later polls of a done job
  return the same change again. **The host applies any reply's `change`,
  whatever the job's state.** Real research runs minutes and must survive the
  laptop closing (N2), so a job outlives the request that started it.
- **`needs_input`** (`start_campaign` only): the job is waiting for the person.
  It carries `job.question` (§8.3) and stays so — polls are free and change
  nothing — until an `answer_question` for it is accepted. It never times out
  in the contract.
- `progress.total` is the number of units of work: for `research_lens`, one per
  lens × market. `progress.done` never goes backwards.
- A `job_status` response describes the job: its `task` and `handler` are the
  job's own (`research_lens`, `research_lens@1.0`) — never `job_status` —
  because the host copies the handler onto the decisions it applies. The same
  holds for `answer_question`: its reply is `task: start_campaign`,
  `handler: start_campaign@1.x`.
- A job belongs to the tenant (from auth) and the document it was started on.
  Polling it from another tenant or another `clan.id` is `404`, the same as an
  id that never existed.
- `failed` carries `job.error: { type, message }`. For the tasks that send no
  staged changes it carries no change; a failed `start_campaign` carries the
  change for any stages finished since the previous reply, plus an agent
  message saying which stage failed. What already landed stays.

## 3. `change`

```json
{
  "doc": "<the clan.id it was computed for — the document_id>",
  "base_version": "<the clan.version it read>",
  "read": { "campaign.problem": { "value": "...", "origin": "stated", "...": "..." },
            "materials.mat_email01": null },
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

- **`read`** is the read-set: for each field the `data_patch` writes, the value
  the job read there, keyed by dotted path into `shared/data.yaml`
  (`campaign.<field>`, `materials.<id>`, `selection.<key>`,
  `intake.messages.<id>`, `report`); a field that was
  absent was read as `null`. Every path the patch sets (every leaf of the
  merge patch — a non-object, `null` included, or an empty object) must lie at
  or under one of its keys. It is what the host judges a stale base by (§4);
  required whenever `data_patch` sets anything, `{}` or absent when it sets
  nothing.
- **`data_patch`** is a JSON merge patch (RFC 7396) over `shared/data.yaml`. It
  never touches `projection` (host-owned). Because a merge patch replaces arrays
  whole, a patch to an array (`selection.lenses_run`, `gaps`, `contested`, a
  list field) carries the full array: what the document held plus this run's
  entries. Objects (`selection.coverage`, `coverage_by_market`, `materials`)
  merge key-wise. The merged data must validate against `schema.json`.
  `intake.messages` is a map for exactly this reason: a patch adds its keys
  and never replaces the person's. `report` is written whole — the patch
  carries the entire object (§8.5).
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
- **Staged writes** (any change sent on a reply before `done`, and every
  `start_campaign` change). A stage may be delivered more than once, so each
  must be recognisable as a repeat:
  - every field the `data_patch` writes is named by the `targets` of a
    decision in the same change (`<doc-id>#campaign.problem`,
    `<doc-id>#intake.messages[msg_…]` for the patch path
    `intake.messages.msg_…`, `<doc-id>#report`), and a repeat sends the same
    decision ids — the host then skips the field (§5);
  - agent chat messages therefore carry a decision too (one decision may
    target a stage's fields and its message together);
  - a later stage that deliberately rewrites a field an earlier stage wrote
    uses a **new** decision id and a `read` holding the value the earlier stage
    wrote (or takes `base_version` from the `clan.version` of the request it
    was computed on) — otherwise it reads as a stale write over its own
    earlier value and contests itself.
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
| 400 | `{"error":{"type":"invalid_input","message"}}` | also: `start_campaign` attachment whose `material_id` is not in `clan.data.materials` with that `sha256`; `answer_question` with a `question_id` that is not the job's open question, neither or both of `option_id` / `text`, an `option_id` not among the question's candidates (an escape option is answered with `text`), `text` when `allow_text` is false; `compose_report` with no pin and no finding to cite |
| 404 | `{"error":{"type":"unknown_job","message"}}` | job id unknown to this tenant and document |
| 409 | `{"error":{"type":"job_state","message"}}` | `answer_question` for a job that is not `needs_input` (already answered, done or failed); `start_campaign` or `compose_report` while a `start_campaign` job on the same document is `queued`, `running` or `needs_input` — one composition of a document at a time |
| 409 | `{"error":{"type":"version_conflict","message"}}` | `base_version` is stale — only an implementation that holds the document (the web product) can know this |
| 500 | `{"error":{"type":"internal","message"}}` | anything else. The message never carries request content |

Never a 200 with a fallback. For `request_kind: middleware` the host passes
the body's `error` through to the app as the proxy envelope's `error` —
`{ "type", "message" }`, those two strings and nothing else — with `data:
null` and the status. (For every other request kind it strips upstream error
bodies and says only `upstream returned <status>`.) That is why a message
never carries request content. `401 unauthenticated` reaches the app the same
way: the host's configured secret is missing or wrong, which the app can
report but not fix.

**Stale base.** A job reads the document once, when it starts. If the document
moved on before the job finished, the `done` change still names the version it
read in `base_version`, and `read` says what it read. An implementation that
does not hold the document (the desktop case) returns it anyway and the
**host** decides, field by field (N2: "if a human edited a different fact
meanwhile, nothing conflicts; if they edited the same one, the job's write
becomes a contested value"):

- each `read` key the patch writes under is judged as one field: if the
  document still holds there what the job read — or already holds what the job
  would write — the patch applies there;
- if it holds something else, nothing is written there. The host records an
  open `contest` decision targeting `<doc-id>#<path>`, with `values`: the
  document's current value (`from: document`) and the job's (`from: <handler>`,
  with `base_version` and what it read). Job decisions that target only
  contested fields are held inside the contest (`withheld`, cited by id), not
  appended — they describe a write that did not happen;
- `facts_append` and `findings_append` are append-only and always apply;
- a stale base with a `data_patch` and no `read` is refused:
  `{ "applied": false, "reason": "stale base and no read-set; rerun" }`, and
  so is a `read` that does not cover every path the patch sets.

The store's own expected-version check (the document moving between the
host's read and its write) is separate and not yet implemented (W2-A4). One
that does hold the document may answer `409`.

## 5. Who applies a change — the host, never the app

The template never writes middleware output. When a `clan://api-proxy` reply for
`request_kind: middleware` carries a `change`:

1. the host checks `change.doc` equals the open document's `document_id`, else
   refuses;
2. applies it through the single write funnel as ONE Change: `data_patch` →
   `shared/data.yaml`, `facts_append` → `shared/facts.yaml`, `findings_append` →
   `shared/findings.yaml`, decisions appended with `actor: process:middleware`,
   `handler` and `backend` the response's, `scope` the one the host resolved,
   and the decision's own `id`, `kind`, `targets`, `cites` and `rationale` as
   fields (`claimed_agent` = its `agent` when that is not the actor);
3. rebuilds `projection` (pins by fact id, findings by id, `built_from` hashes)
   per Contract 3 §5;
4. emits the usual patch event so the view re-renders, and returns the envelope
   to the app with `change` replaced by

   ```json
   { "applied": true, "version": "<new>", "base_stale": false,
     "applied_fields": ["campaign.problem"], "contested_fields": [], "contests": [] }
   ```

   (`contests` the ids of the `contest` decisions it opened), or
   `{ "applied": false, "reason": "..." }`. When a change landed the envelope
   also carries `clan: { id, revision, version, data }` — the document as it
   now stands — beside `data`.

Applying is idempotent. A field whose decisions — every decision in the
change whose `targets` name it — are all already in the chain is a repeated
stage: it is skipped entirely, neither re-applied nor contested, and not
listed in `applied_fields` or `contested_fields`. Other entries already present are skipped: a fact or finding
with the same id and the same content (the same id with different content is
refused — a pin is frozen), a decision whose `id` is already in the chain
(or held in a contest), a contest already open over the same field and value.
A change of which nothing is new — every fact, finding and decision present,
the patch a no-op — is `{ "applied": false, "reason": "already applied" }`, so
a re-poll of a `done` job changes nothing.

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

**Configure `proxies.middleware` explicitly.** An unconfigured request kind
falls back to `agent_url`, which is the briefing agent and does not speak this
contract. The host refuses any `request_kind: middleware` reply that does not
carry `"api": "napkin.middleware/1"` — the app sees an error naming the API,
not the agent's answer, and nothing is applied — but every task will fail
until the endpoint is set.

- Stand-in: `python3 mock-middleware/server.py` (stdlib only), `:8790`,
  `POST /v1/tasks`, `GET /healthz`. See `mock-middleware/README.md`.
- Contract suite: `python3 mock-middleware/contract_test.py --base-url <url>
  [--schema-dir app/templates/campaign-research] [--token <secret>]`. The real
  middleware must pass it unchanged.

---

## 8. The chat intake — `start_campaign`, `answer_question`, `compose_report`

The owner, 2026-09-23: *"The start campaign should work like a chat bot input.
Allow me to type prompt, attach any relevant material and return the campaign
to me, the middleware should be able to figure out the brand and category and
then give me the relevant research using the prompt and the model can then
send the relevant report to render."* Three decisions bind it: the report is
**structured blocks the app renders**, never model-written HTML, and every
claim cites pins or findings; when brand or category is ambiguous the bot
**asks back in the chat** and research waits — it never guesses the subject
brand; **only task submissions spend quota** (§9). What the document holds —
`intake.messages`, `report`, `selection.lenses_skipped` — is Contract 3
§16–17.

### 8.1 The flow

1. A new Research Tool document opens on a chat composer: prompt, attach
   files, Send.
2. The view uploads each file (`clan.uploadAsset`) and indexes it under
   `data.materials.<mat_id>`, indexes the prompt as a `kind: prompt` material
   (sha256 of the text's UTF-8 bytes), and writes the person's message into
   `intake.messages` — one human write. Then it sends `start_campaign`.
3. `start_campaign` is **one** middleware job (M1: orchestration lives in the
   middleware), in six stages, in order:

   | Stage | Does | Writes |
   |---|---|---|
   | `extract` | `extract_ask` (Contract 3 §3): deterministic layer lookups, then one structured-output call — every field except `brand`, `client_org`, `categories` | `campaign.*` (extracted / proposed), `materials`, pins |
   | `identify` | the subject brand, the client and the categories, by §8.6; may enter `needs_input` | `campaign.brand` (extracted), `client_org`, `categories` (proposed) — or a question |
   | `select` | which lenses × `campaign.markets` this prompt needs | `selection.lenses_skipped` |
   | `research` | `research_lens` over the selected pairs only | pins, `selection.*` |
   | `synthesise` | `synthesise_findings` | findings, `campaign.audience` / `in_market` (proposed) |
   | `report` | composes the report (§8.5) | `report` |

   Every stage writes at least one short agent message into
   `intake.messages` (`role: agent`, its `job_id` and `stage`).
4. Each reply may carry a `change` for the stages finished since the previous
   one (§8.4); the host applies it. The chat shows `job.stage`,
   `job.progress` and the messages.
5. When `identify` cannot decide, the job enters `needs_input` with
   `job.question`; the view shows it as a bot message with buttons; the
   person's answer goes back with `answer_question`; the job continues.
6. When `done`, the view renders `data.report` on top of the campaign, then its
   `confirm` list (D3's confirmation step, Contract 3 §16.3).
7. **Refresh report** sends `compose_report`, which re-composes the report
   from the document as it now stands.

For `start_campaign`, `progress` counts stages: `{ "done": <stages
finished>, "total": 6 }`. The research fan-out is narrated in messages, not in
`progress`.

### 8.2 Replies

Every reply of a `start_campaign` job — to `start_campaign`, `job_status` or
`answer_question` — is the §2 envelope with `task: start_campaign`,
`handler: start_campaign@1.x`, and:

```json
{ "job": { "id": "job_…", "state": "needs_input", "stage": "identify",
           "progress": { "done": 1, "total": 6 },
           "question": { "id": "q_…", "text": "Which brand is the client's?",
                         "address": "<doc-id>#campaign.brand", "allow_text": true,
                         "options": [
                           { "id": "lunasa", "label": "Lúnasa",
                             "value": { "ref": "brand/lunasa", "name": "Lúnasa" },
                             "origin": "extracted",
                             "source": { "material_id": "mat_…", "locator": "¶1", "quote": "…" } },
                           { "id": "none", "label": "None of these" } ] },
           "started_at": "…", "finished_at": null, "error": null },
  "result": { "summary": "Waiting for you: which brand is the client's?",
              "messages": [{ "id": "msg_…", "text": "…", "stage": "extract" }, "…"] },
  "change": { "…": "the stages finished since the previous reply" } }
```

`compose_report` replies `task: compose_report`, `handler:
compose_report@1.x`, `job.state: done`, `job.stage: report`.

### 8.3 Questions and answers

`job.question` is `{ id, text, options, allow_text, address? }`, the same
object the middleware writes into the thread as the question message's
`question` (schema `definitions/question`):

- `address` is `<doc-id>#campaign.<field>`, the field the answer settles.
  Every `identify` question has one.
- An **option with a `value`** is a candidate. `value` is the complete value
  for that field, typed as Contract 3 §2.3 types it (a `{ref, name}` for
  `brand`, a ranked leaf list for `categories`, a code list for `markets`).
  `origin` says where the candidate came from and it carries what that origin
  carries: `extracted` + `source` (a span in the material), `proposed` +
  `fact_ids` (pins), or `stated` (the person's own words, from an earlier
  free-text answer).
- An **option without a `value`** is the escape ("None of these", "Something
  else"). A question with an escape, or with no options, has `allow_text:
  true`. Choosing the escape opens the text box; the answer is sent as
  `text`, never as the escape's id.

**The answer is the person's write, not the middleware's.** Before sending
`answer_question` the view writes, as a human, the person's message
(`role: user`, `job_id`, `answer: { question_id, option_id | text }`) and,
for an option answer, the field at `address`:

| Picked option's `origin` | The view writes the field as |
|---|---|
| `extracted` / `proposed` | `origin: confirmed`, `confirmed_from: <origin>`, `by: human:<id>`, keeping the option's `source` / `fact_ids` |
| `stated` | `origin: stated`, `by: human:<id>` |

That is D3's confirmation made in the chat: the person's answer becomes a
`confirmed` (or `stated`) field with the person as `by`, and the middleware
never writes a confirmed or stated field (Contract 3 §2.2). The middleware
continues with the answered value and never writes the answered field.

A **`text` answer** writes only the message. The middleware resolves the text
— to the brands, leaves or market codes it names — and asks again: a new
question (new id) whose candidates are what the text resolved to, the
last-resort candidate being the text itself as a new entity (`origin:
stated`, a brand `{ref: brand/<slug>, name: <text>}`). The field is then
written from that pick. Free text steers a question; it never becomes a field
value without the person picking it. When nothing resolves (a leaf or market
the taxonomy does not know), the new question says so and allows text again.

`answer_question` is accepted only when the job is `needs_input` and
`question_id` is `job.question.id` (else `409 job_state` / `400
invalid_input`, §4). Its request's `clan` — which now holds the person's write
— becomes the job's base for the stages that follow. The reply describes the
job: `running` (or `needs_input` again, with the next question).

### 8.4 Staged changes

- A reply carries the change for every stage finished since the previous reply
  (none: `change: null`). It **may** repeat stages already sent — every
  staged write carries its decision (§3, *Staged writes*), and the host skips
  a field whose decisions are all in the chain (§5), so a repeat is harmless.
  A middleware that cannot know whether its previous reply arrived should
  repeat every finished stage whose decisions are not yet in the
  `clan.decision_chain` the request carried.
- One change may span several stages. `base_version` is the version the
  earliest of them read; `read` holds, per path, what the stage that wrote it
  read.
- The host applies each reply's change regardless of `job.state` and returns
  its outcome in place of `change`, as for `done` (§5).
- The **`report` stage** composes from the document as the host holds it: from
  the `clan` of the first request (a poll) whose `clan.decision_chain` holds
  every decision of the earlier stages — i.e. once they have landed. Until
  then the job stays `running`, `stage: report`. If an earlier stage was
  refused, the job stays there; **Refresh report** composes from what landed.

### 8.5 The report

`data.report` (Contract 3 §17) is written by the `report` stage and by
`compose_report`, and by nothing else. The patch sets `report` whole, with a
decision targeting `<doc-id>#report`, `read: { "report": <what the request's
document held> }`, and one agent message. It is composed from the request's
`clan`:

- `based_on`: `{ version: clan.version, facts_sha256, findings_sha256 }`, the
  two hashes copied from `clan.data.projection.built_from` — the host's hashes
  of the members the report describes.
- Every `claim` — the headline, each summary line, each `claim` block — cites
  at least one pin or finding present in `clan.facts` / `clan.findings`. None
  cites a rejected finding; no `finding` block shows one. A claim states no
  figure that is not in a cited pin (or verbatim in a cited finding); the
  view renders figures from `pins` blocks. `gap` and `contest` blocks name
  `selection.gaps` / `selection.contested` ids.
- `confirm` and `not_researched` follow Contract 3 §17.
- A model may compose it (structured output, the block schema enforced);
  every cite is checked against the document before the change is sent. The
  model's own confidence is never read.

### 8.6 Identify rules

The stand-in and the real middleware both follow these.

- **Subject brand.** Exactly one brand clearly the client — named in the
  prompt as ours or the client's, a `Brand:` label, or the only brand in the
  material — is written `campaign.brand`, `origin: extracted`, with the
  evidence span. (Contract 3's `proposed` is reserved for values derived from
  pins; a brand read from the material is `extracted`.) Several brands, or
  subject vs comparator unclear → `needs_input`: "which is the client's?",
  options the brands found (`extracted`, each with its span) plus "None of
  these". No brand → `needs_input`, free text.
- **Categories.** From the subject brand's roster row when one exists (a
  brand-layer fact `roster.categories…`, pinned) → `campaign.categories`,
  `origin: proposed`, citing the pin. Otherwise a ranked array of at most two
  candidates → `needs_input`: options the candidates (and, when there are
  two, both together — each option's value is a whole list) plus "Something
  else". Never classify by retrieving.
- **Client.** From the roster row (`roster.client_org`) → `proposed`; else as
  `extract_ask` would.
- **Markets.** None in the material → `needs_input`, free text; research
  cannot run without them (gate `research`).
- The answer becomes a `confirmed` field, by the person (§8.3), and the job
  continues. The subject brand is asked before categories: the roster row
  depends on it.

## 9. Quota

Only **task submissions** spend agent quota: the host (the web product's
meter) charges one unit for each `request_kind: middleware` request whose
task is not `job_status` — `start_campaign`, `answer_question`,
`compose_report` and the older tasks alike — before sending it, and refuses
over the cap with its own `429` (the middleware never sees the request).
`job_status` polls are free: a job is polled for minutes, and a poll spends
nothing upstream.
