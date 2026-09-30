# Middleware API — `napkin.middleware/1`

Status: **binding** for the host, the templates, the stand-in middleware and the
real one. Reads with: `docs/contracts/campaign-clan.md` (Contract 3 — what a
`change` may contain), `docs/contracts/os-layer.md` (Contract 4 — decisions,
addresses, M3/M4), `docs/contracts/peripherals.md` (Contract 5 — the
model, research, retrieval and layers ports behind the middleware).

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
  "payload": { "task": "extract_ask | research_lens | synthesise_findings | start_campaign | answer_question | compose_report | draft_brief | regenerate_field | find_client_parts | job_status",
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
- **Upstream is sent as an index** (2026-09-29). A spun-off document holds
  its parent's data frozen at `data.upstream.<id>` (Contract 4 §5), often
  larger than the document itself. The host replaces it in `clan.data` with

  ```json
  "upstream": { "<id>": { "direct": true, "keys": ["campaign", "selection", "materials", "intake", "report"],
                          "open_contests": [{ "id": "ct_…", "key": "…", "fact_ids": ["f_…", "f_…"] }] } }
  ```

  one entry per key: `direct` whether it is `lineage.carried.document_id`,
  `keys` the frozen copy's top-level keys, `open_contests` its
  `selection.contested` entries still `open` and not resolved in the chain
  (Contract 4 §7.2), each with the `fact_id` of every value.
  The carried pins and findings are not here: they are merged into the
  document's own members and arrive in `clan.facts` and `clan.findings`. The
  frozen data reaches a model only as §10.13 says. `upstream` is read-only:
  a `data_patch` that names it is refused (Contract 4 §8.1).
- **Task → handler.** `payload.task` is resolved against `clan.pipeline.tasks.<task>.handler`
  (`name@major`). When the document carries no pipeline, the middleware's
  declared built-in map is used (`extract_ask@1`, `research_lens@1`,
  `synthesise_findings@1`, `start_campaign@1`, `answer_question@1`,
  `compose_report@1`, `draft_brief@1`, `regenerate_field@1`, and the review
  tasks `verify_finding@1`, `correct_fact@1` and `find_client_parts@1`, which
  resolve from this map even for a pipeline that does not declare them, §11).
  Any other task the pipeline does not declare, an
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
| `synthesise_findings` | `{ "lenses"?: [lens ids], "redo"?: "audience" }` | Reads `clan.facts` (pins only). Never proposes a finding the document already holds, a rejected one included, and sends the rejected ones (statement, reason) to the model. `redo: "audience"` proposes `campaign.audience` again without rejected findings, replacing only an audience the agent proposed, and writes nothing else |
| `start_campaign` | `{ "prompt": "...", "attachments": [{ "material_id", "name", "sha256", "text"? }] }` | The chat intake (§8). Long. Each `material_id` must be a key of `clan.data.materials` with that `sha256` (the view indexes the file first). Nothing to read at all is `400 invalid_input`, as for `extract_ask` |
| `answer_question` | `{ "job_id": "...", "question_id": "...", "option_id"?: "...", "text"?: "..." }` | Exactly one of `option_id` / `text` (§8.3) |
| `compose_report` | `{}` | Short. Re-composes `data.report` from the document as it stands (§8.5) |
| `draft_brief` | `{ "prompt"?: "...", "attachments": [{ "name", "sha256", "media_type"?, "asset"?, "text"?, "image"?: { "media_type", "data" } }] }` | Brief Maker (§10). Long. `image` is base64 bytes of a picture attachment (§10.1). Nothing to read at all is `400 invalid_input` |
| `regenerate_field` | `{ "field": "<dotted field key>", "guidance"?: "..." }` | Brief Maker (§10.10). Long. `field` one of the eighteen keys of §10.4 |
| `find_client_parts` | `{ "answer": "accepted_with_changes \| rejected", "proof": "...", "parts": [{ "address", "label", "value" }] }` | Client review (§11). Short. Asked by the host, never by an app. Output: suggestions `[{address, answer, quote}]`, each quote a verbatim substring of `proof` |
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
  the replies of `answer_question`, which describe one), on
  `compose_report` replies (`stage: report`, `question: null`, §8.2), and on
  `draft_brief` and `regenerate_field` jobs (`question: null` always, §10.6);
  other tasks omit them. `job.question` is non-null exactly when `job.state` is
  `needs_input`.
- `trace.usage` is what was actually spent; an implementation that ran no model
  reports zeros and never an estimate. `trace.model` is `null` when no model ran.
  `trace.hits` lists what was read: facts (`scope` = layer, `source` = origin
  URI) or sources (`source` = the source URI).
- A 200 body never has a top-level `error` key.

### Jobs

- **Short tasks** (`extract_ask`, `compose_report`, `find_client_parts`) answer `job.state: done`
  and a `change` in one response.
- **Long tasks** (`research_lens`, `synthesise_findings`, `start_campaign`,
  `draft_brief`, `regenerate_field`)
  answer `job.state: queued | running`; the caller polls `job_status`. A
  `queued`, `running` or `needs_input` reply **may** carry a `change` for work
  finished since the previous reply — `start_campaign` does, stage by stage
  (§8.4), and so does `draft_brief` (§10.9); `research_lens` and `synthesise_findings` send `change: null` until
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
  "sources_append": [ { "id": "src_...", "uri": "https://...", "title": "...", "publisher": "...",
                        "published_at": "YYYY-MM-DD | null", "retrieved_at": "YYYY-MM-DD",
                        "tier": "...", "domain": "...", "licence": "open" } ],
  "decisions": [ { "id": "d_...", "kind": "edit | finding | contest | pin | verdict (§10.7)", "agent": "<handler>",
                   "action": "...", "rationale": "...", "targets": ["<doc-id>#<path>"],
                   "cites": ["f_...", "src_...", "mat_..."], "handler": "...", "backend": "...",
                   "timestamp": "...",
                   "reasoning": { "decided": "...", "because": [ { "point": "...", "cites": ["f_..."] } ],
                                  "rejected": [ { "option": "...", "why": "..." } ],
                                  "certainty": { "level": "high | medium | low", "why": "..." },
                                  "would_change_if": "...", "attention": "..." } } ]
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
  with different values. A pin carries `quotes`: for each of its `src_`
  sources, the verbatim passage that source gave for the value.
- **`sources_append`** (optional): a record for every `src_` source a new pin
  or contest value cites — `id` and `uri` required, with its title, publisher,
  dates, tier, domain and licence — so a citation in the document leads to
  where it was read without a call to the layers. The host keeps the first
  record it is given for an id; a later one for the same id is dropped, never
  a conflict (two lenses citing one page under different titles).
- **Contests.** Two runs disagreeing on one entity + key (+ market) produce a
  `selection.contested` entry, `status: open`, carrying every value with its
  `fact_id`, `from`, `sources` and `quotes`, and a `contest` decision targeting it. None
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
- **`reasoning`** (Contract 4 §3, the shape and its rules). **Required** on
  every decision of kind `pin`, `contest`, `finding` or `verdict`, on every
  `edit` whose action is `propose`, and on whatever the document's app
  declares in its `app/pipeline.yaml` (R1 is app knowledge, so the app
  declares it and the host applies it — Contract 4 §3):

  ```yaml
  reasoning:
    kinds: [...]      # decision kinds required on top of the floor above
    edits: [...]      # dotted data paths: an edit whose targets lie at or under one must say why
  ```

  A declaration the host cannot read refuses the change (M4). The Research
  Tool declares `edits: [campaign, selection, report]` — the agent-written
  fields; Brief Maker declares its eighteen fields' top-level keys plus
  `capture` and `review` (§10.2). For the Research Tool that is: extract (`extract`,
  `extract_ask`), identify (`identify` when it writes a field), the roster
  `lookup` pin, `select`, every `research_run`, the `research_merge` pin,
  every `open_contest`, every `synthesise_finding`, `propose_audience`, and
  `report` / `compose_report`. An `edit` that only posts a chat message or
  indexes a material (`narrate`, an identify question, `stage_failed`) may
  omit it; an implementation should still send it. Its rules:
  - every id a `because` point cites resolves in the document as it stands
    after the change — a pin, a finding, a decision, a material, a contest or
    a gap, a value a contest holds, a source a pin or a contested value rests
    on — or is an address on it (`<doc-id>#…`), or is a source in
    `result.sources`;
  - `certainty` is derived, never the model's: for a `pin` it is the lowest
    derived confidence of the pins it targets; for a `finding` it is the
    finding's derived confidence; for the rest, the implementation's stated
    rule from the evidence (coverage for a run, verified quotes for an
    extraction, how the brand was settled for identify);
  - where a model call decided (extract, identify, select, synthesise,
    report), the model may write `because`, `rejected`, `only_option`,
    `would_change_if` and `attention`; the implementation checks every cite
    against the ids the model was given, drops a point whose cites do not all
    resolve or that states a figure without a cite, and, when no point is left,
    writes its own from the evidence and sets `attention`;
  - `rationale` is its one-line summary (`decided` + the first point) unless
    the implementation sends its own; a host fills it from the reasoning when
    it is empty.
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
| 409 | `{"error":{"type":"job_state","message"}}` | `answer_question` for a job that is not `needs_input` (already answered, done or failed); `start_campaign` or `compose_report` while a `start_campaign` job on the same document is `queued`, `running` or `needs_input` — one composition of a document at a time; `draft_brief` while a `draft_brief` or `regenerate_field` job on the same document is `queued` or `running`, and `regenerate_field` while a `draft_brief` job, or a `regenerate_field` job for the same field, is (§10.11) |
| 400 | `{"error":{"type":"invalid_input","message"}}` | also: `draft_brief` or `regenerate_field` on a brief whose `data.locked` is true; `regenerate_field` for a field not in §10.4 or in `data.locked_fields`; an `image` over 5 MB or of another media type (§10.1) |
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
   `shared/findings.yaml`, `sources_append` → `shared/sources.yaml`, decisions appended with `actor: process:middleware`,
   `handler` and `backend` the response's, `scope` the one the host resolved,
   and the decision's own `id`, `kind`, `targets`, `cites`, `rationale` and
   `reasoning` as fields (`claimed_agent` = its `agent` when that is not the
   actor). A change carrying a decision whose `reasoning` is required (§3) and
   absent, or present and malformed, is refused whole —
   `{ "applied": false, "reason": "decision d_… (pin) carries no reasoning; …" }`
   — rather than accepted with a flag: reasoning is what the decider knew when
   it decided and cannot be added later without being invented, and the
   refusal names the decision so the fix lands at the source on a rerun. A
   contest the host opens over a stale write (§4) carries the host's own
   reasoning in the same shape;
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

**Configure `proxies.middleware` explicitly.** Other request kinds fall back
to `agent_url`; `middleware` never does. With no `proxies.middleware` the host
sends nothing and answers the task itself — the proxy envelope with `ok:
false`, `data: null` and `error: { "type": "no_middleware", "message" }` —
and a browser build with no network answers every task the same way. `GET
clan://middleware` answers `{ "api": "napkin.middleware/1", "configured":
bool }` — presence only, never the endpoint — so an app can say so on open.
The host still refuses any reply that does not carry `"api":
"napkin.middleware/1"` (an endpoint pointed at the wrong service): the app
sees an error naming the API and nothing is applied.

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

## 10. Brief Maker — `draft_brief`, `regenerate_field`

Brief Maker moves off `engine/agent-server/` (a bare-JSON backend whose output
the view wrote itself) onto the middleware. The engine's semantics port; its
transport does not. Every rule in §1–§5 holds: the reply is the §2 envelope,
the output is a job whose `change` the **host** applies — never bare JSON —
and nothing the view receives is written by the view. The peripherals each
stage uses are Contract 5 §6.

*Changed 2026-09-29*, for briefs spun off from research (Contract 4 §5): the
drafters read `clan.findings` and may cite `fi_` (§10.1, §10.3, §10.7), capture
stays free of research as well as of retrieval (§10.3), a brief is locked by
an `approve` on it rather than by a data flag (§10.5), and §10.13 says which
research evidence each loop gets and that nothing marked `model: false`
reaches a model. The drafters never read findings before, so research could
not reach a brief; and the data flag let a view lock around the lock list.

### 10.1 Input

`draft_brief`: `{ "prompt"?, "attachments": [...] }`.

- `prompt` — the planner's typed ask (`data.brief_input`). It is a material
  (`kind: prompt`, sha256 of its UTF-8 bytes).
- Each attachment — `name`, `sha256` of its bytes, and what can be read:
  `text` (host-extracted, as for `extract_ask`), or `image: {media_type,
  data}` (base64 bytes; `image/png | image/jpeg | image/gif | image/webp`, at
  most 5 MB decoded, else `400 invalid_input`). `media_type` and `asset` (the
  document path of the bytes, from `reference_assets[].path`) are recorded on
  the material. An attachment with neither `text` nor `image` is recorded
  **unread** and grounds nothing.
- At least a non-empty `prompt`, a `text` or an `image`; else `400
  invalid_input`.

The drafters also read `clan.facts` and `clan.findings` (§1) — in a brief
spun off from research, the pins and findings it carried (§10.13); capture
never does.

The middleware indexes every material itself, in `data_patch.materials`
(§10.4); the view does not index first. Material ids are deterministic —
`mat_` + the first 16 hex of the sha256 — so a repeat, or a second draft over
the same file, names the same material.

### 10.2 The pipeline declaration

Brief Maker's `app/pipeline.yaml` becomes a declaration like the Research
Tool's (M2):

```yaml
pipeline: napkin-briefing
version: 2
request_kind: middleware
contract: middleware-api/1#10

tasks:
  draft_brief:
    handler: draft_brief@1
    job: long
    stages: [extract, draft, judge]
  regenerate_field:
    handler: regenerate_field@1
    job: long
    stages: [draft, judge]

fields:                      # app key -> how it is filled; the handler reads this map
  project_name:              { class: captured }
  client:                    { class: captured }
  background:                { class: captured, rubric: background }
  objectives.commercial:     { class: captured, rubric: objectives }
  objectives.behavioural:    { class: captured, rubric: objectives }
  objectives.attitudinal:    { class: captured, rubric: objectives }
  audience:                  { class: captured, rubric: audience }
  competitor_context:        { class: captured, rubric: competitor_context }
  budget_and_scope:          { class: captured, rubric: budget_scope }
  mandatories:               { class: captured, rubric: mandatories }
  tone_and_world:            { class: captured, rubric: tone_world_assets }
  insight:                   { class: drafted, rubric: insight, loop: loop4_insight }
  single_minded_proposition: { class: drafted, rubric: smp, loop: loop5_proposition }
  reasons_to_believe:        { class: drafted, rubric: reasons_to_believe, loop: loop6_substantiation }
  desired_response.think:    { class: drafted, rubric: desired_response, loop: loop5_proposition, drafter: desired_response }
  desired_response.feel:     { class: drafted, rubric: desired_response, loop: loop5_proposition, drafter: desired_response }
  desired_response.do:       { class: drafted, rubric: desired_response, loop: loop5_proposition, drafter: desired_response }
  open_questions:            { class: composed }

merge_policies:
  materials: append
  passages: append
  capture: last-write
  review: last-write
  "*": ask
```

`rubric` names a field of the golden-brief rubric
(`engine/golden-brief/golden_brief.schema.json`), bundled with the handler
and versioned with it (Contract 5 §9, O9). The three `desired_response`
leaves share one drafter: they are a ladder.

### 10.3 The flow

One job, three stages, in order. `progress` counts stages: `{done, total: 3}`.

| Stage | Does | Writes (on the reply after it finishes) |
|---|---|---|
| `extract` | Transcribe each image (Contract 5 §1.6). **Loop 1 — no-loss capture:** one structured-output call over the materials returning the eighteen Loop-1 keys (the engine's `EXTRACTION_SYSTEM`: each item `{value, status: fact \| assumption, quote, material_id}`, plus `how_to_win` and `open_questions`); every `fact` quote checked verbatim in its material — an item whose quote is not found is dropped and its words stay unmapped; the no-loss ledger over the materials' segments. **Loop 2 — the working brief:** the captured fields, derived from the capture by the engine's `map_brief` rules (a Loop-2 client fact is never an insight or an SMP). The **BetterBriefs scorecard** (`SCORECARD_SYSTEM`: seven dimensions and single-mindedness, evidence quoted verbatim) | `materials`, `capture`, `review` (scorecard), every captured field it can support |
| `draft` | For each drafted field not locked, **one drafter, all in parallel** (N4). A drafter builds its loop's query from the working brief (the captured fields' values — never raw material, never the capture itself), asks the retrieval port for the packs whose `loops` include its loop (and, for insight and substantiation, each case pack at its own `k`), and drafts: insight and SMP by tournament (N candidates in one call, ranked, auto-gated, one sharpen pass), the rest once. It may cite passages (`psg_…`), capture items (`cap_…`), pins in `clan.facts` (`f_…`) and findings in `clan.findings` (`fi_…`, §10.13) — nothing else. Drafters never see each other's drafts | nothing yet: a draft lands only once judged |
| `judge` | **Serial** (N4). The Judge (§10.8) judges every field that holds or will hold a value, then the brief as a whole; one revision per failed drafted field, by its drafter with the Judge's `fix`, re-judged once. Composes `open_questions` | the drafted fields that passed, the `passages` they cite, verdicts, proposals, `review` (with the Judge), `open_questions` |

**The one hard rule: capture is RAG-free and research-free.** The `extract`
stage has no retrieval or research capability (Contract 5 §3.4): nothing
retrieved and nothing from research — no pin, finding, contest or report,
own or carried — is fed to Loop 1, and no captured field cites a passage, a
pin or a finding. The no-loss ledger measures
fidelity to the client's own words, and injected text craters it.

**Omitted, not blank.** No patch sets a brief field to `""`, `[]`, `{}` or
`null`, and no patch removes one. A field nothing supports is absent from the
patch and named in its stage decision's `abstained`. A second draft never
blanks a box.

**Parallel drafting in v1** runs in-process: each drafter owns one field, so
the fan-out cannot collide (N4's reason), and the job's staged change carries
the result. Branches (`agents/<user>.<agent>.<task>/`) are the web product's
mechanism for the same thing and change nothing here.

### 10.4 What the document holds

Brief Maker's `shared/data.yaml`. The eighteen fields keep their bare shapes
(`schema.json`: strings, or arrays of strings for `reasons_to_believe`,
`tone_and_world`, `mandatories`, `open_questions`); provenance lives in the
decisions that target them, not in an envelope. Addresses are
`<doc-id>#<dotted key>` — `#insight`, `#objectives.commercial` — and a nested
patch merges key-wise (`{objectives: {commercial: …}}` leaves `behavioural`
alone).

Four blocks, written only by the middleware — additions to Brief Maker's
`schema.json` (shapes: `docs/contracts/peripherals/brief-maker.schema.json`;
Contract 5 §9, O5 and O12):

```yaml
materials:                        # as Contract 3 §1, plus two optional keys; merges key-wise
  mat_9f2c0a1b3d4e5f60:
    kind: prompt | client_brief | email | deck | image | other
    name: "Glenmore brief.pdf"
    sha256: "…"
    media_type: application/pdf
    asset: human/assets/…
    received_at: 2026-09-24T09:00:00Z
    licence: client-confidential  # the strictest class until a human reclassifies it
    unread: true                  # optional: nothing readable arrived
    transcribed: { model: claude-opus-5, backend: "…" }   # optional: its text is a vision transcription
capture:                          # Loop 1; replaced whole by each extract stage
  built_at: …
  handler: draft_brief@1.0
  items:
    cap_1a2b3c4d5e6f7a8b:
      key: business_problem       # one of the eighteen Loop-1 keys
      value: "…"
      status: fact | assumption
      quote: "…verbatim in the material…"   # required for fact
      material_id: mat_…
      objective_type: commercial | behavioural | attitudinal   # objective items only
  gaps: [budget, decision_makers] # Loop-1 keys the material does not answer
  how_to_win: [{ kind: stated_evaluation_criteria | unstated_needs | likely_landmines | winning_themes | proof_required, point, evidence, material_id }]
  ledger: { total_segments, mapped_segments, coverage_pct, unmapped: [{ segment, material_id }] }
review:                           # replaced whole by extract, then by judge
  built_at: …
  handler: draft_brief@1.0
  based_on: { version }           # the clan.version the job read
  scorecard:
    dimensions: [{ dimension, verdict: pass | vague | missing, evidence?, fix? }]   # the seven, in order
    single_mindedness: { verdict: single | multiple, split_into: [] }
    summary: "…"
  judge:                          # absent until the judge stage
    reason_codes_version: "1"
    fields: { <key>: { outcome: passed | revised | failed | kept | absent, checks: [{ check, method: auto | llm | human, status: pass | fail | review, note, fix? }] } }
    dependencies: [{ id, status: pass | fail | review, note }]
    definition_of_done: [{ id, status: pass | fail | review }]
    health: 0-100                 # golden_critic's score, computed by code
passages:                         # every passage a decision cites; merges key-wise
  psg_3b9f0c2e7a41d5c8e210: { uri, pack, scope, licence, source, section, citation, text, text_sha256, pack_version, retrieved_at }
```

`cap_` ids are `cap_` + the first 16 hex of `sha256(key + "\n" + value + "\n"
+ material_id)`; `psg_` ids are the retrieval port's (Contract 5 §3.2). A
scorecard `evidence` is a verbatim quote from a material (checked; one not
found is dropped), absent for `missing`. The view renders `review` in place of
the old context panel; the middleware no longer returns a `context`.

### 10.5 Fields the middleware never writes

- **Locked.** The brief is locked when `clan.decision_chain` holds an
  `approve` decision, not superseded, whose `targets` include `clan.id`
  (Contract 4 §7.1) — or, in a brief made before it locked through
  `/approve`, when `data.locked` is `true`. A carried `approve` (it targets
  the parent) does not lock the brief. Locked, both tasks are `400
  invalid_input`. A key in `data.locked_fields` — or whose top-level key is
  there — is locked: not drafted, not written, not proposed; its
  `result.fields` state is `kept`.
- **Human-held.** A field is held by a person when the latest decision in
  `clan.decision_chain` that wrote it is a person's: its `actor` begins
  `human` (or, with no `actor`, its `agent` does). A decision *wrote* key `K`
  (top-level `T`) when its `targets` include `<doc-id>#K` or `<doc-id>#T`, or,
  with no `targets`, its `fields_changed` includes `T` and either `K` is `T`
  or its `action` names no sibling `T.<x>` other than `K` (the view's own
  rule). A field that holds a value no decision wrote is human-held.
- **An unanswered bad verdict** by a person on the field (Contract 3 §2.2)
  makes it human-held too: it is not silently re-drafted.

The middleware **proposes** instead of writing a human-held field: the value
goes in `result.proposals` (§10.6) and the change carries an `edit` decision
with `action: propose`, `targets: ["<doc-id>#K"]`, the value as
`proposed_value`, and reasoning — and no `data_patch` for `K`. Accepting is
the view's human write of `K` (as §8.3's answers are), naming the proposal's
decision id in its rationale; ignoring it changes nothing. Locked fields are
not even proposed. A human-held `open_questions` is proposed like any field.

### 10.6 Replies

The §2 envelope; `task` and `handler` are the job's own (`draft_brief@1.x`,
`regenerate_field@1.x`) on every reply, `job_status` included.

```json
{ "job": { "id": "job_…", "state": "running", "stage": "draft",
           "progress": { "done": 1, "total": 3 }, "question": null,
           "started_at": "…", "finished_at": null, "error": null },
  "result": { "summary": "Drafting 4 fields in parallel.",
              "fields": {
                "background":                { "state": "done",     "by": "extract" },
                "budget_and_scope":          { "state": "absent",   "by": "extract" },
                "insight":                   { "state": "drafting", "by": "drafter" },
                "single_minded_proposition": { "state": "drafting", "by": "drafter" },
                "mandatories":               { "state": "kept",     "by": null },
                "…": "every one of the eighteen keys" },
              "proposals": [ { "field": "audience", "value": "…", "decision": "d_…" } ] },
  "change": { "…": "the stages finished since the previous reply" } }
```

- `job.stage`: `extract | draft | judge` (`regenerate_field`: `draft |
  judge`). `job.question` is always `null`: Brief Maker never waits for the
  person mid-job.
- `result.fields` — all eighteen keys, each `{state, by}`. `state`: `waiting
  | extracting | drafting | judging | revising | done | proposed | absent |
  kept | failed`; `by`: the worker doing it or that last did it — `extract |
  drafter | judge` — or `null`. This is what lets the view show Extract, one
  Drafter per field and the Judge working for real. `done`: written by a change
  already sent or in this reply. `failed`: judged bad twice, not written.
  `absent`: nothing supports it.
- `result.proposals` — the proposals so far (§10.5). Display only.
- `result.withheld` — the ids kept out of every model call because a
  `classify` mark says `model: false` (§10.13, item 6). Display only; absent
  when nothing was withheld.
- `trace.hits` — each passage read (`id` its `psg_`, `scope` `house` or
  `agency:<org>`, `source` its `uri`) and each pin cited.

### 10.7 Decisions and reasoning

Every decision in a Brief Maker change carries `reasoning` (Contract 4 §3),
whatever the host currently requires (Contract 5 §10, item 6): each one writes
an agent field. `agent` is the worker — `draft_brief@1.x/extract`,
`…/drafter`, `…/judge` — so the chain says who did it; `handler` is the
reply's.

| Decision | Kind | Targets | Cites |
|---|---|---|---|
| one per captured field written | `edit`, action `extract` | `#K` | the `cap_` items and `mat_` it rests on |
| the capture | `edit`, action `capture` | `#capture`, each new `#materials[mat_…]` | the `mat_` ids; flatten tail `material_read`, `unread`, `abstained` (keys not written) |
| the scorecard, the review | `edit`, action `score` / `review` | `#review` | `mat_` ids / the verdict decision ids |
| one per drafted field written | `edit`, action `draft` (`regenerate` for §10.10) | `#K` | its `psg_`, `cap_`, `f_`, `fi_` grounds |
| one per judged field | `verdict`, `polarity: good \| bad`, `reason_code` when bad, `taxonomy_version: "reason-codes/1"` | `#K` | the edit decision it judges; loop-7 `psg_` |
| a coherence failure | `verdict`, polarity `bad` | every field it names | their edit decisions |
| a proposal | `edit`, action `propose`, flatten `proposed_value` | `#K` | as a draft or an extraction |
| the open questions | `edit`, action `questions` | `#open_questions` | the verdicts and capture gaps they come from |

Reasoning, by what decided:

- **Captured field.** `because`: the client's words, each point citing its
  `cap_` item. `only_option`: the value is what the client wrote. `certainty`
  (derived): `high` — every cited item is a `fact` whose quote was found
  verbatim in typed material; `medium` — an `assumption` among them, or a quote
  from a transcribed image; `low` — the value rests on assumptions alone.
  `would_change_if`: the client's material says otherwise, or a person edits
  it.
- **Drafted field.** `because`: the drafter's grounds, each citing passages,
  capture items, pins or findings; a point stating a figure cites the pin
  holding it, and a point that cites a finding and states a figure cites that
  finding's pins too (its `cites` that are in `clan.facts`).
  `rejected`: the losing tournament candidates, each with why it lost (the
  ranking's reason, a failed auto check, walking onto a competitor's ground).
  `certainty` (derived, never the model's): `high` — every rubric check passed
  first time and the grounds cite passages from at least two packs, or a pin
  (or a verified finding) and a passage; `medium` — passed with checks left at `review`, or grounded
  in one pack; `low` — passed only after revision, or grounded in no passage
  (retrieval unconfigured, failed or empty — `attention` then says so). A
  field whose kept points cite a finding not yet verified is at most
  `medium`, and its `attention` includes "rests on a finding not yet
  verified" (§10.13).
- **Verdict.** `decided`: the outcome; `because`: one point per check that
  decided it; `certainty`: `high` when auto checks alone decided, `medium` when
  a model check did; `attention` on every bad verdict and every check left at
  `review`.
- As §3: the model may write `because`, `rejected`, `would_change_if` and
  `attention`; the middleware checks every cite against the ids it gave the
  model, drops a point whose cites do not all resolve or that states a figure
  without a pin, and writes its own point from the evidence when none is left.
  The model's self-reported confidence is never asked for or read (the engine's
  `confidence_floor` does not port).

**Cites resolve in the document after the change** (§3), extended for these
tasks: a `psg_` resolves when `data.passages` holds it, a `cap_` when
`data.capture.items` does, and an `fi_` when `clan.findings` holds it with
status `proposed` or `verified` — a rejected finding never resolves, and a
point citing one is dropped. Every passage a decision cites is therefore written
into `passages` by the same change.

**What the host keeps.** The host reads a middleware decision whole into
the chain: `id`, `kind` (`verdict` included), `agent`, `action`, `targets`,
`cites`, `rationale`, `reasoning`, the verdict fields (`polarity`,
`reason_code`, `taxonomy_version`, `reviewer_role`), `licence`,
`claimed_agent`, `fields_changed`, `superseded_by`, and every other key as
sent — the flatten tail (`abstained`, `material_read`, `unread`,
`proposed_value`, …). `actor`, `scope`, `handler`, `backend` and `timestamp`
are the host's (§5); a decision that sent its own keeps it as
`claimed_<field>`. Brief Maker's view finds a field's decisions by `targets`
(what `clan://chain` returns), and by `fields_changed` for a person's
patch-data, which has no targets.

### 10.8 The Judge

The engine's `golden_critic.py`, ported into the middleware, with the rubric
bundled with the handler. It never sees a drafter's prompt, candidates or
grounds — only the brief as it would stand, the rubric and loop-7
decision-rule passages (UC-6).

1. **Auto checks** in code: `within_limit`, `single_sentence`,
   `single_minded`, `reveals_why`, `max_items`, `three_levels`, `all_three`,
   `has_constraint`, `has_deliverables`, `names_rivals`.
2. **Model checks**, one structured call per field, one field at a time,
   carrying all its pending `llm` checks with the rubric's good and bad examples
   (`critic_prompts_batched`), schema `{<check>: {verdict: pass | fail, reason,
   fix}}`.
3. **Coherence**, once every field is judged: the dependencies
   (`backbone_balance`, `smp_derivation`, `rtb_supports_smp`,
   `response_ladders`, `ownable_needs_competitors`) and the definition of done,
   including `one_strategy` — the brief pulling in several directions,
   per-field drafting's risk (N4).
4. **Gating.** A drafted field with a failed check is revised once and
   re-judged; failing again it is **not written** — state `failed`, a bad
   verdict with `attention`, an open question "Agree the <label>." A captured
   field is never rewritten (it is the client's words): a failed check on it
   is a bad verdict with `attention` and an open question to take back to the
   client. A coherence failure never removes a field: it is a bad verdict on
   every field it names, with `attention`; an unanswered bad verdict is on the
   lock list (Contract 4 §7), so a person answers it before the brief locks.
5. **Reason codes** for bad verdicts: `within_limit`, `single_sentence`,
   `single_minded`, `one_strategy`, `max_items` → `not_single_minded`;
   `ownable`, `not_a_tagline` → `cliche`; `smp_derivation`,
   `rtb_supports_smp`, `supports_smp`, `response_ladders`, `derives_from`,
   `linked`, `backbone_balance` → `off_strategy`; `has_constraint` (budget
   against ambition) → `unfeasible`; anything else → `other`, with the check's
   reason as the text. Pending the creative director's redline (Contract 5
   §9, O11).
6. `review.judge.health` is `golden_critic._health`, computed by code.
7. **Open questions** (`open_questions`, written whole): the capture's open
   questions formatted `[<priority>] <question> — <why>` (the engine's
   `_format_question`), then one per failed field, then one per bad verdict on
   a captured field. Never a question the material already answers.

### 10.9 Staged changes

As §8.4: a reply carries the change for every stage finished since the
previous one and may repeat them with the same decision ids; the host skips a
field whose decisions are all in the chain.

- **After `extract`**: `materials`, `capture`, `review` (scorecard), the
  captured fields, and their decisions. `read` holds, per path, what the job
  read (`null` for absent).
- **After `judge`**: the drafted fields that passed, `passages`, verdicts,
  proposals, `open_questions`, and `review` again — a deliberate rewrite, so a
  **new** decision id and a `read` holding the value the extract stage wrote
  (§3, *Staged writes*).
- `draft` sends no change. A failed stage fails the job (`job.error`); the
  reply carries the change for the stages that did finish, and what landed
  stays.
- The `done` poll carries the last change; later polls repeat it.

### 10.10 `regenerate_field`

`{ field, guidance? }` — redraft one field.

- `field` is one of the eighteen keys; any other, or a locked one (§10.5), is
  `400 invalid_input`.
- Stages `draft` — one drafter; a captured field is re-derived from the
  capture, never from retrieval; `guidance` goes to the drafter as the
  planner's steer — then `judge` — that field's checks, then the coherence
  checks that involve it. `desired_response.*` redrafts the named leaf only,
  with the other two as context. `progress`: `{done, total: 2}`.
- It writes the field (an `edit`, action `regenerate`, plus its verdict) — or,
  when the field is human-held, proposes it (§10.5): pressing Redraft asks for
  a draft, and the planner decides whether it replaces their words. A field
  that fails twice is not written; `result.summary` says why and the bad
  verdict is recorded.

### 10.11 Errors and concurrency

- `draft_brief` while a `draft_brief` or `regenerate_field` job on the same
  document is `queued` or `running` → `409 job_state`. `regenerate_field` while
  a `draft_brief`, or a `regenerate_field` for the same field, is → `409
  job_state`. `regenerate_field` jobs for different fields may run together.
- A model failure in `extract` fails the job: nothing was captured. A retrieval
  failure in `draft` does not: the drafter drafts without passages, certainty
  `low`, `attention` naming the failure. A model failure for one drafter makes
  that field `failed`; the others continue.
- Quota: each submission is one unit (§9); polls are free.

### 10.12 Contract suite additions

`mock-middleware/contract_test.py` gains, run against both middlewares:

- `draft_brief` with a prompt only → a long job whose replies carry `stage`
  and `result.fields` for all eighteen keys; the `done` change validates
  against Brief Maker's schema with the §10.4 blocks; no field is `""`, `[]`,
  `{}` or `null`; every written field is named by a decision's `targets`;
  every decision carries well-formed reasoning; every `cap_` and `psg_` cite
  resolves in the data after the change.
- Every captured field's decision cites only `cap_` and `mat_` ids; every
  `fact` capture item's quote is a substring of its material's text.
- A key in `locked_fields` is in no patch; a field last written by a human
  decision is in no patch and is in `result.proposals`.
- `regenerate_field` for an unknown or a locked field → `400`; a second
  `draft_brief` while one runs → `409`.
- Polling a `done` job again returns the same change.

### 10.13 Research-fed briefs

*Added 2026-09-29.* A brief can now start from a research document (Contract
4 §5): its pins, findings and sources are merged into the brief's members,
its data is frozen at `data.upstream.<research id>`, and its chain — contests,
verdicts, classify marks — comes with it. This section says which of that
reaches which loop, so research feeds the drafting without touching the
client's words, and each call carries only what it needs.

1. **Where it arrives.** Carried pins in `clan.facts`, carried findings in
   `clan.findings`, carried marks in `clan.decision_chain`, and the frozen data
   as the §1 index only. A brief is *research-fed* when `data.upstream` is not
   empty; one that is not keeps §10.3 exactly.
2. **Evidence by loop.** Chosen in code, per drafter, before any model call.

   | Stage · loop | Gets from the research | Never |
   |---|---|---|
   | `extract` · Loop 1, Loop 2, scorecard | Nothing | Any pin, finding, contest, report or `campaign.*` value |
   | `draft` · `loop4_insight` (insight) | Findings of lenses `consumer_culture`, `category_codes`, `rhythm_moments`, `brands_positioning`, and the pins they cite | |
   | `draft` · `loop5_proposition` (SMP, desired response) | Findings of `brands_positioning`, `consumer_culture`, `category_codes`, and their pins | |
   | `draft` · `loop6_substantiation` (reasons to believe) | Findings of `effectiveness_evidence`, `market_structure`, `brands_positioning`, `regulation_clearance`, and their pins | |
   | `judge` · loop 7 | Nothing: it judges the brief as it would stand (UC-6) | The drafters' findings and pins |

   The client's own material that travelled with the research — a frozen
   `materials` entry whose `asset` is in the brief's `human/assets/` — may be
   attached to `draft_brief` like any upload. It is the client's words, so it
   becomes a material of the brief (`mat_` from its bytes, §10.1) and is
   captured; it is never cited as research.
3. **The selection.** For each drafter:
   1. candidate findings: status `proposed` or `verified`, of the loop's
      lenses, not withheld (item 6);
   2. ordered verified first, then proposed; then `confidence` high, medium,
      low; then `derived_at`, newest first; **at most 12**;
   3. pins: those the chosen findings cite that `clan.facts` holds, not
      `replaced_by` another (the replacement is given instead), not a value of
      an open contest (item 5), not withheld; **at most 40**;
   4. a finding is sent as `{id, statement, status, confidence, lens,
      markets, cites}`, `cites` narrowed to the pins sent; a pin as today.
   The ids sent, with the passages and capture items, are the only ids the
   drafter may cite: a cite outside them is dropped (§10.7).
4. **Unchecked findings are cited only** (owner default, 2026-09-29; may be
   reversed). A `proposed` finding may ground insight, SMP, reasons to believe
   and desired response. It is shown there as **"derived by the agent"**;
   certainty is at most `medium`; `attention` includes "rests on a finding
   not yet verified". It never fills a captured field and never enters
   capture. A person verifies it in the brief (`/verify`, which also sends
   the research a `backref`) or upstream.
5. **Contests.** A pin that is a value of an open contest — the brief's own,
   or one the §1 index lists under `open_contests` (its `fact_ids`) — is not
   sent: nothing is picked yet. Once a person resolves it (Contract 4 §8.1), the chosen pin is
   in `clan.facts` like any other.
6. **`model: false` never goes into a payload.**
   - The mark that holds on an item is the newest `classify` decision in
     `clan.decision_chain`, not superseded, targeting it — own or carried.
     Ids are never remapped, so a mark on `<research id>#facts[f_…]` holds for
     `f_…` in the brief. A mark on a data path holds for everything under it.
   - An item whose mark has `model: false` is withheld from **every** model
     call of every stage: the drafters, the Judge, the capture call and a
     vision transcription. A finding that cites a withheld pin is withheld
     too — its statement may restate the figure. A material so marked is
     recorded `unread` and grounds nothing. A brief field so marked is left
     out of the working brief and the Judge's brief; its `result.fields` state
     is `kept`.
   - The withheld ids are listed in `result.withheld` (display only). An id
     never sent can never be cited, so none reaches a decision.
7. **Cost** (owner default, 2026-09-29; may be reversed). Each drafter gets
   only item 3's selection — never `clan.facts` or `clan.findings` whole, and
   never the frozen data. Prompt caching is not used until a measured run
   shows what it saves.
8. **The extract.** The research's own sections (its campaign fields and
   report) will reach the drafters as its **extract**
   (`docs/contracts/clan-extract.md`, in design), not as raw data. Until that
   grammar is agreed, a drafter gets findings and pins only, as above.

When built, §10.12 gains, for a research-fed brief: no captured field's
decision cites an `f_` or `fi_`; a drafted decision citing a `proposed`
finding has certainty at most `medium` and the `attention` of item 4; no
decision cites an id marked `model: false`; every `fi_` cite resolves in
`clan.findings` and is not rejected.

---

## 11. Client review — `find_client_parts`

*Added 2026-09-30.* Ellis (the extract agent) reads a client's answer and
suggests which parts of the document it was about (Contract 4 §7.5). It
writes nothing: the host records each suggestion as "derived by the agent",
and a person confirms or dismisses it. Only a confirmed part counts.

1. **Who asks.** The host, inside `POST /client-review` (Contract 4 §8.2),
   when the answer is `accepted_with_changes` or `rejected`, no part was
   marked, the app declared parts, and there is proof text. Like
   `verify_finding` and `correct_fact` it is a review task: it resolves for
   any document, whatever pipeline it was made with (`find_client_parts@1`
   from the built-in map when the pipeline does not declare it). A short task:
   `job.state: done` in one response.
2. **Input.**

   ```json
   { "task": "find_client_parts",
     "input": {
       "answer": "rejected",
       "proof": "Honestly this isn't the brief we talked about. The summer line doesn't feel like us.",
       "parts": [ { "address": "3f2a…#single_minded_proposition", "label": "Single-minded proposition",
                    "value": "Summer tastes better without the hangover." },
                  { "address": "3f2a…#audience", "label": "Audience", "value": null } ] } }
   ```

   - `answer`: the document-level answer, `accepted_with_changes` or
     `rejected`.
   - `proof`: the client's words — the review's `said` when it has one, else
     the attached file's extracted text — at most 24,000 characters (the
     host's extraction limit).
   - `parts`: every part the app declared, 1 to 100, each `{address, label,
     value}`. `value` is the part's value as the client saw it, as plain
     text (a string as it is, anything else as compact JSON; a field
     envelope's `value` only), clipped to 2,000 characters. It is `null` when
     the part is empty, or marked `model: false` (§10.13, item 6: that mark
     withholds a value from every model call; it is not a confidentiality
     filter on what the client sees).
3. **Output.** `result.suggestions: [{address, answer, quote}]`, plus
   `result.summary` and `result.dropped` (how many it dropped by item 4).
   `change` is `null`. **No other text**: no rationale, reasoning or
   comment, on the reply or on a suggestion; any other key on a suggestion
   is dropped. An empty list is a valid answer — the words may name no part.
   - `address`: one of the input's.
   - `answer`: `accepted`, `accepted_with_changes` or `rejected` — a part's
     answer may differ from the document's ("the proposition is right, the
     audience is wrong").
   - `quote`: the sentence of the proof that says so, **a verbatim substring
     of `proof`**, at most 500 characters.
4. **Checked, and dropped if not.** The middleware drops a suggestion whose
   address is not an input part, whose answer is not one of the three, whose
   quote is empty, too long or not in the proof, or whose address an earlier
   suggestion already took (the first one stays). "In the proof" is exact
   after one normalisation only: every run of whitespace, on both sides, is
   one space. No case folding, no quote-mark or dash normalisation, no
   ellipsis joining. The host checks the same again before it records
   anything (Contract 4 §8.2), and its check is the one that counts.
5. **Bounded.** One model call: no tools, no retrieval, no layer read, no
   second pass, a small output cap (1,024 tokens). The prompt is built from
   `input` alone. The handler reads nothing else from the document — not
   `clan.data`, the members, the chain or the context, which the host still
   sends for transport (§1). A call that fails is an error (§4), never a
   partial or invented list; the host then records the answer without
   suggestions.
6. **It writes nothing.** No `change`, no layer row, no source, no corpus
   entry. The client's words enter no index through this task.
7. **The stand-in** (`mock-middleware/`) answers without a model: a part is
   suggested when its label appears, ignoring case, in a sentence of the
   proof; the quote is that sentence as written; the answer is the
   document's.
8. **Errors.** `400 invalid_input`: `answer` missing, `accepted` or
   unknown; `proof` empty or over the limit; `parts` empty, over 100, or an
   entry without `address` or `label`; two parts with one address.

When built, §10.12's suite gains: every returned quote is a substring of
the proof under item 4's rule; no suggestion names an address outside
`parts`; no suggestion carries a key but `address`, `answer` and `quote`;
the reply's `change` is `null`; an input with `answer: accepted` is `400`.
