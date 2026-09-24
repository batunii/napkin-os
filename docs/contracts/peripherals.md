# Contract 5 — the peripherals

Status: **draft**, for the owner to confirm the **Open** points (§9). Owner:
Shrey. Implements the owner's decisions of 2026-09-24 (below) and M5; amends
M5's wording and meets P2's atomicity rule a new way (§4.6).
Reads with: `docs/contracts/middleware-api.md` (Contract 1 — host ↔
middleware, `napkin.middleware/1`; §10 there is Brief Maker's half of this
document), `docs/contracts/campaign-clan.md` (Contract 3), `docs/contracts/os-layer.md`
(Contract 4). Machine-checkable shapes: `docs/contracts/peripherals/*.schema.json`.

The middleware (`server/`) talks to four peripherals. This document is the
only thing the middleware's port clients and the peripherals' implementations
depend on. Two builds start from it at once and do not talk to each other:

- **the mock backend** — ONE process that fakes every peripheral, answering
  the generative ones with Claude underneath (§5);
- **the real middleware** — `server/` extended with the ports below, and Brief
  Maker moved onto it (§6, `middleware-api.md` §10).

Where this document and the code disagree, the code is wrong. Where it
disagrees with `foundation-spec.clan`, the spec wins and this document is a
defect to report.

### The owner's decisions this implements (2026-09-24)

> "We need one actual middleware, and one mock backend. When we hit API
> endpoints of our mock endpoint, it uses Claude at the backend to generate
> the output. The backend mock agent just fakes the peripherals attached to it."

| Decision | Here |
|---|---|
| The model port speaks the Anthropic Messages API **or** an OpenAI-compatible `/v1/chat/completions` (self-hosted NVIDIA NIM in production), chosen by config. The mock answers both | §1 |
| Retrieval over the award corpora and playbooks is a **peripheral service** queried over HTTP | §3 |
| **One mock process** fakes every peripheral, one route family each; each swaps to the real service by config only | §5 |
| The knowledge layers are a **peripheral, mocked**: today's `Layers` protocol becomes an HTTP contract; the middleware keeps a client | §4 |
| The middleware keeps **all** the logic — rules, tiering, derived confidence, merge, contests, reasoning (R1), cite rules, the judge. Peripherals store, search, fetch and generate | every section |

---

## 0. Shape

```
 host ──napkin.middleware/1──▶ middleware (server/)            all logic lives here
                                 │ Capabilities (scope bound from auth, M3)
       ┌─────────────────┬───────┴─────────┬─────────────────────┐
       ▼                 ▼                 ▼                     ▼
   model port       research port     retrieval port         layers port
   generate         find web sources  search the packs       store facts, sources,
   (§1)             + verbatim quotes  verbatim passages     taxonomy, roster (§4)
                    (§2)              (§3)
       │                 │                 │                     │
 real: Anthropic API  a search service  retrieval service    layers service
       or NIM /v1       (TBD)           (Qdrant + NIM         (Postgres + RLS)
                                         embeddings)
 mock: ───────────────── ONE mock backend, :8797 (§5) ─────────────────────
       claude -p          claude -p         claude -p over      SQLite, no model
                          + WebSearch       local pack files
```

### 0.1 The swap guarantee

Each port has one base-URL setting (§7). Moving a port from the mock to the
real service is changing that value (and its credential) — nothing else. The
middleware contains no mock branch, flag, port number or `if`. The contract
suite (§8) names no implementation and runs **unchanged** against the mock
and against the real peripheral; if a real peripheral needs the suite edited
to pass, the peripheral or this contract is wrong, and the fix is made here
first. The removability check stays: `grep -rniE 'mock|8797|claude -p' server/napkin`
prints nothing.

### 0.2 Rules every port shares

- **JSON over HTTP**, UTF-8, `Content-Type: application/json`. A 200 body never
  carries an `error` key. Never a 200 with a fallback: a peripheral that could
  not do the work says so with a non-2xx.
- **Errors.** Research, retrieval and layers: `{"error": {"type", "message"}}`
  with the status and `type` listed per port. The model port uses its wire's
  own shape (§1.7). An error message never carries request content.
- **Auth.** `Authorization: Bearer <token>`, the port's service credential from
  middleware configuration (§7). The middleware holds it; handlers never see it
  (M3). A peripheral configured with a token refuses a missing or wrong one
  with `401 unauthenticated`. The model port uses its wire's own header.
- **Scope travels in headers, never in bodies.** On the scoped ports (layers,
  retrieval) the port client sets `X-Napkin-Org: org/<slug>` and
  `X-Napkin-Brand: brand/<slug>` from the scope `Capabilities` was bound to,
  which the middleware derived from auth. The peripheral **never computes,
  widens or defaults scope**: a scoped route without the header it needs is
  `400 missing_scope`; a body that names `scope`, `org`, `org_id`, `tenant`,
  `tenant_id` or `brand_scope` at its top level is `400 invalid_input` (the
  same rule M3 applies at the host boundary). Headers, not a body field,
  because a body field is what a caller copies from its own input; the header
  is set in one function in the port client and nowhere else.
- **Attribution.** Every call carries `X-Napkin-Handler: <name>@<major.minor>`
  and `X-Napkin-Job: <job id>` (or `-`) for the peripheral's log and meter. A
  peripheral may log them; it never branches on them.
- **Bodies are not logged.** A peripheral logs one metadata line per request
  (method, path, status, duration, attribution). Request bodies carry
  client-confidential material; none is written anywhere unless a
  debug variable says so (§5.6).
- **Timeouts.** The middleware's client timeout per port is configuration
  (§7). A peripheral that runs past its own deadline answers `504` in its
  error shape and stops the work.
- **Retries.** The middleware retries a read once on a connection error, 429
  (honouring `Retry-After`), 502, 503 or 504. It retries a write only with the
  same `Idempotency-Key` (§4.5). The model port's retry policy is §1.7.
- **Health.** Research, retrieval and layers answer `GET /healthz` →
  `200 {"ok": true, "service": "<name>", "api": "napkin.<port>/1", ...}`.
  The model port needs none (the real APIs have none); the mock's is §5.5.

---

## 1. Model port — `napkin.model/1`

The middleware's one door to a language model: **structured output on every
call** (W2-C3), validated against the declared schema before it reaches any
writer, retried once with the validation error fed back, then a loud failure.
There is no prose salvage. That rule (today's `server/napkin/model.py`) is
unchanged; what changes is that two wire shapes can carry it.

### 1.1 Selection

| Setting | Values | |
|---|---|---|
| `NAPKIN_MODEL_API` | `anthropic` (default) \| `openai` | which wire shape the port speaks |
| `NAPKIN_MODEL_BASE_URL` | URL | `anthropic`: the API root (the SDK appends `/v1/messages`); unset = the SDK's own resolution (`ANTHROPIC_BASE_URL`, else the real API). `openai`: the root that `/chat/completions` is appended to, **including** `/v1`; required |
| `NAPKIN_MODEL_API_KEY` | secret | `anthropic`: unset = the SDK's own resolution (`ANTHROPIC_API_KEY`, …). `openai`: required unless the endpoint is keyless |
| `NAPKIN_MODEL` | model id | sent verbatim as `model` |
| `NAPKIN_VISION_MODEL` | model id | used for image transcription (§1.6); unset = `NAPKIN_MODEL` |
| `NAPKIN_MODEL_EXTRA_BODY` | JSON object | `openai` only: merged into every request body (e.g. `{"chat_template_kwargs": {"enable_thinking": false}}` for a NIM reasoning model). Never keys the port itself sets |

One `ModelPort` per process, one wire. Which wire answered is recorded as
`trace.backend`'s model half; `trace.model` is `NAPKIN_MODEL` (or the vision
model for a transcription). No handler knows which wire is in use.

### 1.2 What the middleware sends, in both shapes

A call is `(purpose, system, payload, schema, max_tokens, effort?, images?)`:

- `purpose` — a slug `^[a-z][a-z0-9_]{0,63}$` naming the call (`extract`,
  `draft_insight`, `judge_smp`, `transcribe`). It is the OpenAI `json_schema.name`
  and appears in logs.
- `system` — one string.
- the user turn — `"Task: <purpose>\n\n<input>\n<JSON payload>\n</input>"`, one
  text part, preceded by image parts when `images` is given (§1.6).
- `schema` — JSON Schema. The wire gets `strip_unsupported(schema)` (drops
  `minLength maxLength minimum maximum minItems maxItems uniqueItems pattern
  format`, which the providers refuse or ignore); the **full** schema is
  enforced locally after the call. Every object in a middleware schema has
  `additionalProperties: false` and lists every property in `required` (the
  shape strict mode needs in both wires; optional values are expressed as a
  nullable type).
- The retry turn (on invalid output): the original user turn, the model's text
  as an `assistant` turn, then a `user` turn "That output does not validate
  against the schema: <problems>. Return the corrected JSON only." The last
  turn is always `user` — never an assistant prefill.

Never sent, in either wire: tools or functions, streaming, sampling parameters
(`temperature`, `top_p`, `top_k`: current Claude models refuse them), stop
sequences, `n > 1`, logprobs, thinking configuration. (Claude Opus 5 thinks
adaptively by default; depth is `effort`.)

### 1.3 Wire A — Anthropic Messages

`POST <base>/v1/messages`, headers `x-api-key` (or `Authorization: Bearer`),
`anthropic-version: 2023-06-01`, through the official `anthropic` SDK exactly
as today.

```json
{ "model": "claude-opus-5", "max_tokens": 16000,
  "system": "…",
  "messages": [{ "role": "user", "content": [
      { "type": "image", "source": { "type": "base64", "media_type": "image/png", "data": "<b64>" } },
      { "type": "text", "text": "Task: extract\n\n<input>…</input>" } ] }],
  "output_config": { "format": { "type": "json_schema", "schema": { } },
                     "effort": "high" } }
```

`content` is a plain string when there are no images. `effort` is sent only
when the call names one.

Read from the response: the first `content` block of `type: text` is the JSON;
`stop_reason`; `usage`.

| `stop_reason` | Port result |
|---|---|
| `end_turn` | parse and validate |
| `max_tokens` | `ModelError(truncated)` — no retry (a retry would be cut off again) |
| `refusal` | `ModelError(refusal)` — no retry; `stop_details.category` is logged, never shown |
| anything else | `ModelError(invalid_output)` |

### 1.4 Wire B — OpenAI-compatible chat completions (NIM)

`POST <base>/chat/completions`, header `Authorization: Bearer <key>`.

```json
{ "model": "nvidia/llama-3.3-nemotron-super-49b-v1", "max_tokens": 16000,
  "messages": [
    { "role": "system", "content": "…" },
    { "role": "user", "content": [
        { "type": "image_url", "image_url": { "url": "data:image/png;base64,<b64>" } },
        { "type": "text", "text": "Task: extract\n\n<input>…</input>" } ] } ],
  "response_format": { "type": "json_schema",
                       "json_schema": { "name": "extract", "schema": { }, "strict": true } } }
```

plus `NAPKIN_MODEL_EXTRA_BODY` merged in. `content` is a plain string when
there are no images. Exactly one `system` message, first (on NIM a second
system message displaces the first — engine note of 2026-06-10). `effort` has
no equivalent in this wire and is not sent.

Read from the response: `choices[0].message.content`, with a leading
`<think>…</think>` block removed (NIM reasoning models emit one) before
parsing; `choices[0].finish_reason`; `choices[0].message.refusal`; `usage`.

| Condition | Port result |
|---|---|
| `finish_reason: stop` and `refusal` null or absent | parse and validate |
| `finish_reason: length` | `ModelError(truncated)` |
| `finish_reason: content_filter`, or `message.refusal` non-empty | `ModelError(refusal)` |
| no `choices`, or `content` null | `ModelError(invalid_output)` |

A `400` whose message says the endpoint does not support `response_format`
`json_schema` is **not** retried without it: structured output is enforced or
the call fails (`ModelError(unsupported)`). The engine's "drop the flag and
retry" (`parse_brief.py:1150`) is exactly the prose path the no-signal test
showed returning prose five times out of five, and does not port.

### 1.5 Model ids

The middleware sends `NAPKIN_MODEL` verbatim and never maps ids. The mock maps
them to Claude Code aliases:

| Id the middleware may send | Mock runs `claude -p --model` | Wire |
|---|---|---|
| `claude-opus-5` | `opus` | both |
| `claude-sonnet-5` | `sonnet` | both |
| `claude-haiku-4-5` | `haiku` | both |
| `claude-fable-5-1` | `fable` | both |
| any id in `MOCK_MODEL_ALIASES` (JSON `{"<id>": "<alias>"}`) | that alias | both |

`MOCK_MODEL_ALIASES` defaults to the NIM ids the engine uses, so a middleware
configured for NIM runs against the mock unchanged:
`{"nvidia/llama-3.3-nemotron-super-49b-v1": "sonnet",
"nvidia/llama-3.1-nemotron-70b-instruct": "sonnet",
"nvidia/llama-3.1-nemotron-nano-vl-8b-v1": "haiku"}`. Any other id is a
`404` in the wire's shape (`not_found_error` / `model_not_found`), never a
default. The response's `model` echoes the requested id.

Which NIM model production runs is **Open** (§9, O1).

### 1.6 Images (attachments that are pictures)

The engine turns an image into faithful text with a vision model before
capture (`parse_brief._vision_transcribe`), so the no-loss ledger measures
the client's words and nothing else. The middleware does the same: **an image
is transcribed first, and the transcription becomes that material's text**.

- A `transcribe` call per image: `NAPKIN_VISION_MODEL`, the image part(s) plus
  the engine's transcription instruction (verbatim, reading order, a bracketed
  note for a meaningful non-text visual, never summarise), schema
  `{text: string, visuals: [string]}`.
- **Anthropic wire:** an `image` block, `source.type: base64`. **OpenAI wire:**
  an `image_url` part with a `data:<media_type>;base64,…` URL. Never a remote
  URL in either wire: the bytes are client-confidential and must not be
  fetched by a third party from a link.
- Accepted: `image/png`, `image/jpeg`, `image/gif`, `image/webp` (the set both
  wires take); at most 5 MB decoded per image, at most 20 images per call.
  Anything else is not sent; the attachment is recorded unread.
- Documents (PDF) are never sent as document blocks: the host extracts their
  text on upload. An image-only PDF (a scanned deck) arrives with no text and
  is recorded unread — rendering its pages is **Open** (§9, O6).
- The material is marked `transcribed: {model, backend}` (Brief Maker's
  `materials` entry, `middleware-api.md` §10.4), and a span quoted from it has
  certainty at most `medium` (§10.7 there): a transcription is the model's
  reading of the picture, not the client's typed words.

### 1.7 Errors, timeouts, usage

Both wires' errors map to one `ModelError(kind)`; handlers see only the kind.

| Kind | Anthropic wire | OpenAI wire | Retried by the port |
|---|---|---|---|
| `auth` | 401, 403 | 401, 403 | no |
| `not_found` | 404 | 404 | no |
| `invalid_request` | 400, 413 | 400, 413, 422 | no |
| `rate_limited` | 429 | 429 | once, after `Retry-After` (cap 30 s) |
| `overloaded` | 529, 503 | 503 | once |
| `server` | other 5xx | other 5xx | once |
| `timeout` | client timeout or 504 | client timeout or 504 | no (a 504 already cost a timeout) |
| `truncated`, `refusal`, `invalid_output`, `unsupported` | §1.3 | §1.4 | no |

Error bodies: Anthropic `{"type": "error", "error": {"type", "message"}}`;
OpenAI `{"error": {"message", "type", "param", "code"}}`. The mock produces
exactly these (§5.3).

- **Timeout** is `NAPKIN_MODEL_TIMEOUT` seconds per HTTP attempt. With one
  retry a call can cost two. The SDK's own retries are set to match (one).
- **Usage is never estimated.** Anthropic: `input_tokens +
  cache_creation_input_tokens + cache_read_input_tokens` as input,
  `output_tokens` as output. OpenAI: `prompt_tokens`, `completion_tokens`. A
  response without usage counts as zero and the port logs that it did —
  `trace.usage` is what was reported, and a plausible guess would be believed
  by cost accounting downstream.
- A model failure never becomes content. What a stage does with it — a gap, an
  omitted field, a failed stage — is the handler's rule (`middleware-api.md`
  §2 and §10).

### 1.8 Embeddings are not the model port's

Embeddings belong to **the retrieval service**, which calls its own embedder
(today NIM `nvidia/nv-embedqa-e5-v5`, 1024-d, `POST /v1/embeddings` with
`input_type: passage | query`). The middleware never embeds and never sees a
vector.

- The index and its embedder are one thing. `napkin_packs.py` refuses to mix
  vector spaces when the embed model changes; that guard can only live where
  the index lives. A middleware that embedded queries itself could send a
  1024-d query to a 512-d index — the silent wrong-dimension failure the
  engine README warns about.
- The middleware's logic needs passages, not similarity. Keeping vectors out
  keeps the port contract text-in, text-out, which is also what lets the mock
  answer it with no embeddings at all (§3.6).
- So `/v1/embeddings` is **not** part of this contract. The retrieval service's
  embedder is its own dependency; the mock serves no embeddings (Claude cannot
  produce honest vectors) and answers `/v1/embeddings` with `404`.

---

## 2. Research port — `napkin.research/1`

Web source discovery for the Research Tool. The shape is today's
`mock-research` shape, kept; this section tidies what was left implicit.

### 2.1 `POST <NAPKIN_RESEARCH_URL>/v1/research`

`NAPKIN_RESEARCH_URL` is the service root; a value ending in `/v1/research` is
accepted and trimmed (as `ResearchPort` does today).

```json
{ "query": "BMW electric and hybrid cars market in Ireland",
  "lens": "market_structure", "market": "IE",
  "entity": "brand/bmw", "category": "automotive.ev_charging", "max_sources": 6 }
```

| Field | Rule |
|---|---|
| `query` | required, 1–500 characters after whitespace is collapsed. A research **question**: brand, comparator and category names, the lens's question. Never material text — the middleware does not send the client's words to a web search |
| `lens` | required, one of the eight lenses (`campaign-clan.md` §7) |
| `market` | required, ISO 3166-1 alpha-2; uppercased by the service; `UK` is refused (the UK is `GB`) |
| `entity` | optional, `^(brand\|org\|category)/[a-z0-9][a-z0-9._-]*$` — steers the search only |
| `category` | optional, a leaf code `<vertical>.<leaf>` — steers the search only |
| `max_sources` | optional, 1–20; the service default is 8. **The middleware always sends it** (today 6), so no behaviour depends on a default |

Any other field is `400 invalid_input`. No scope headers: the query is a
public-web question and the service holds nothing tenant-owned (whether the
org is sent for metering only is **Open**, §9, O8).

### 2.2 Response

```json
{ "sources": [ { "id": "src_…", "url": "https://…", "publisher": "…", "title": "…",
                 "retrieved_at": "2026-09-24", "published_at": "2026-08-01",
                 "excerpts": [ { "quote": "…verbatim…" } ] } ],
  "trace": { "backend": "claude-code-websearch", "queries": ["…"], "model": "sonnet" } }
```

- `sources` — at most `max_sources`; one per URL (http or https, with a host,
  no fragment). `title` non-empty; `publisher` non-empty (the URL's host when
  the page names none).
- `excerpts` — 1–5 per source, each `quote` non-empty, at most 1,500
  characters, **copied from the page, never paraphrased**. A source with no
  excerpt is not returned. The service does not claim to have re-verified the
  quote; the middleware treats it as a claim about a URL and checks every
  figure it extracts against it (`rules/figures.py`).
- `id` — a handle **within this response**, stable per URL. The layers assign
  the source id that facts cite (§4.3); the middleware never stores this one.
- `retrieved_at` — `YYYY-MM-DD`, set by the service (never by a model), the day
  the page was read. A cached answer keeps its original date.
- `published_at` — `YYYY-MM-DD`, not in the future, or **absent** when
  unknown (`null` is accepted and means the same).
- `trace.backend` required; `trace.queries` the searches run (may be empty);
  `trace.model` optional.
- **`sources: []` is an honest 200**: nothing citable was found. It becomes a
  gap, not an error.
- **No facts, confidence or tiers**, ever. Tiering by domain policy and derived
  confidence are the middleware's (`rules/tiering.py`, `rules/confidence.py`).

### 2.3 Errors

| Status | `type` | When |
|---|---|---|
| 400 | `invalid_input` | any rule in §2.1; body not JSON |
| 401 | `unauthenticated` | a configured token missing or wrong |
| 404 / 405 | `not_found` / `method_not_allowed` | unknown path or method |
| 429 | `rate_limited` | with `Retry-After` |
| 502 | `upstream_failed` | the search backend failed or returned nothing parseable |
| 504 | `timeout` | the service's own deadline; the work is stopped |

A failure is never cached. `?fresh=1` (skip a cache) is a mock extension; a
real service may ignore it.

---

## 3. Retrieval port — `napkin.retrieval/1`

Search over the knowledge packs — the licensed craft corpora (Cannes, D&AD,
Effie, IPA), the planner playbooks and the briefing templates — returning
**verbatim passages** with where they came from. Today this is
`engine/rag/rag.py` `search()` / `retrieve.py` over Qdrant or a local index
with NIM embeddings; it becomes a service.

### 3.1 `GET /v1/packs` — what this caller may search

Scoped: `X-Napkin-Org` required (`X-Napkin-Brand` accepted and ignored — S6
partitions per agency, not per brand).

```json
{ "packs": [
    { "tag": "cannes", "id": "cannes", "kind": "case", "scope": "house",
      "licence": "licensed-internal", "k": 2,
      "loops": ["loop4_insight", "loop6_substantiation"],
      "passages": 499, "filterable": ["category", "award_tier", "year"],
      "version": "sha256:4f1c…" },
    { "tag": "playbook", "id": "playbooks", "kind": "playbook", "scope": "house",
      "licence": "licensed-internal", "k": 2, "loops": [], "passages": 1224,
      "filterable": ["category", "type"], "version": "sha256:…" } ],
  "embed_model": "nim:nvidia/nv-embedqa-e5-v5",
  "backend": "qdrant:napkin_rag_v2" }
```

- `tag` is what a request names and what passages carry (`metadata.source` in
  the engine index); `id` the pack directory. `kind`: `case | playbook |
  template | digest`. `loops` and `k` are the pack's own declaration
  (`pack.yaml` / `packs.lock`); the middleware uses them to choose which packs
  a stage asks, as `loops_3_7` does today. Empty `loops` = every loop.
- `scope`: `house` (shared by every agency) or `agency` (visible only to its
  org). `licence`: `open | licensed-internal | client-confidential`.
- `version` changes whenever the pack's content does (a hash over its files or
  chunks). `embed_model` is informational; `null` when the service does not
  embed (the mock).
- The list is exactly what the caller may search: house packs plus the
  packs of `X-Napkin-Org`. Another agency's pack is never listed.

### 3.2 `POST /v1/retrieve`

Scoped as §3.1.

```json
{ "query": "find the human insight and cultural tension for midweek drinkers cutting back",
  "k": 5, "packs": ["playbook", "cannes"], "where": { "award_tier": "gold" },
  "purpose": "loop4_insight" }
```

| Field | Rule |
|---|---|
| `query` | required, 1–2,000 characters |
| `k` | required, 1–20: the number of passages wanted, across all the named packs together |
| `packs` | optional, tags from §3.1; absent = every pack the caller may search. A tag not visible to this caller — unknown, deactivated, or **another agency's** — is `404 unknown_pack` (never 403: the answer does not say the pack exists) |
| `where` | optional, `{key: string \| number \| boolean}`, **exact** match on passage metadata, ANDed; every key must be in the `filterable` of every named pack, else `400 invalid_input`. (Exact, as the engine made it, so local and remote stores return the same set) |
| `purpose` | optional slug, for the trace and log only; it never changes the result |

Any other field is `400 invalid_input`.

```json
{ "passages": [
    { "id": "psg_3b9f0c2e7a41d5c8e210",
      "uri": "passage://cannes/sainsburys-christmas.md#the-insight@9c41e0aa12b3f4d5",
      "pack": "cannes", "scope": "house", "licence": "licensed-internal",
      "source": "sainsburys-christmas.md", "section": "The insight",
      "citation": "sainsburys-christmas.md › The insight",
      "text": "…verbatim…", "text_sha256": "9c41e0aa12b3f4d5…(64 hex)",
      "truncated": false, "rank": 1, "score": 0.83,
      "metadata": { "source": "cannes", "award_tier": "gold", "year": 2019 } } ],
  "trace": { "backend": "qdrant:napkin_rag_v2", "embed_model": "nim:nvidia/nv-embedqa-e5-v5",
             "packs": { "cannes": "sha256:4f1c…", "playbook": "sha256:…" } } }
```

- **`text` is verbatim** — a contiguous span of the named section of the named
  file in the pack, at most 4,000 characters (a longer span is cut at a word
  boundary and `truncated: true`). Never a summary, never generated.
- `source` is the file (path within the pack), `section` its H1/H2 heading as
  the engine chunks (`rag.split_h2`); `citation` is `"<source> › <section>"`,
  the form the engine and `pipeline.yaml` already use.
- `text_sha256` is the full hex SHA-256 of `text` (UTF-8). `id` is `psg_` +
  the first 20 hex of `sha256(pack + "\n" + source + "\n" + section + "\n" +
  text)`, and `uri` is `passage://<pack>/<source>#<section slug>@<first 16 hex
  of text_sha256>` (slug: lowercase, each run of characters outside `[a-z0-9]` → one `-`, leading and trailing `-` removed). Both are
  computable by anyone holding the passage, so the same passage has the same
  id in every call and every document, and the suite can check them.
- Order is best first (`rank` 1…n). `score` is the service's, comparable only
  within one response, and **never read as confidence**. Reranking (the
  engine's model-driven `_rerank_hits`) is the service's business, inside
  this ordering.
- Fewer than `k` passages is a valid answer; `passages: []` is an honest 200.
- `trace.packs` gives the `version` of every pack searched, so a decision
  citing a passage can say what the corpus was.

### 3.3 Per-agency scope (S6)

- House packs (every licensed craft pack today) are searchable by every agency.
- An agency pack is searchable only with `X-Napkin-Org` equal to its owner, and
  its passages carry `scope: "agency"`. No filter the caller sends can widen
  this: scope comes from the header, and the header from the middleware's auth.
- Every passage names its `pack` and `scope`, and the middleware records both
  in `trace.hits` (`scope: house | agency:<org>`), so a mistake has a readable
  blast radius (Contract 4 §9).
- A real service partitions by index or collection per agency (the S6
  decision: "where the filter is the only barrier a forgotten filter is a
  breach"). The contract only fixes what the caller can observe.

### 3.4 Retrieval never fills anything

The engine's one hard rule — Loop-1 capture is RAG-free, because the no-loss
ledger measures fidelity to the client's own words — is the middleware's rule
(`middleware-api.md` §10.3). The port makes it structural:

- It has three operations: list packs, retrieve passages, health. None takes a
  document, a schema, a field name or client material; the request fields are
  closed, so "fill this brief" cannot be expressed.
- Every response string a caller could put in a document is verbatim pack text.
  There is no generated output to leak into a field.
- In the middleware, the `extract` stage's capability object has **no
  retrieval attribute** — the capture code cannot call the port, rather than
  being trusted not to.

### 3.5 Errors

| Status | `type` | When |
|---|---|---|
| 400 | `invalid_input` | a rule in §3.2; body not JSON; scope named in the body |
| 400 | `missing_scope` | no `X-Napkin-Org` |
| 401 | `unauthenticated` | a configured token missing or wrong |
| 404 | `unknown_pack` | a named pack this caller may not search (§3.2) |
| 429 | `rate_limited` | with `Retry-After` |
| 502 | `upstream_failed` | the store or embedder failed |
| 504 | `timeout` | the service's own deadline |

A store that is unreachable is `502`, never `200 {passages: []}`: an empty
answer must mean "nothing matched", or the middleware would write "no
precedent" on a brief when the index was down.

### 3.6 What the mock does

No embeddings, no vector store. Claude picks and quotes passages from **the
pack files on disk**:

1. **Packs** are directories under `MOCK_PACKS_DIR` (default
   `engine/packs_dist`), discovered as `engine/packs.py` does (dirname = pack
   id; `pack.yaml` for `tag`, `kind`, `k`, `loops`; `_`-prefixed = off). If
   `BRIEF_CORPUS` names the full corpus, that is used instead.
   `MOCK_AGENCY_PACKS_DIR/<org slug>/<pack>/` adds agency packs owned by
   `org/<org slug>`, so the suite can test S6 on the mock.
2. **Sections** are every `.md` file split at H1/H2 exactly as
   `rag.split_h2` splits it (so `section` is a real heading), filtered by the
   request's packs and `where` (frontmatter metadata, exact match).
3. **Selection.** When the candidates fit `MOCK_RETRIEVAL_MAX_CHARS` (default
   150,000) they all go to Claude; otherwise a lexical prefilter (token
   overlap with the query) keeps the best that fit. One fresh `claude -p`
   (`MOCK_RETRIEVAL_MODEL`, default `sonnet`) with `--json-schema
   {picks: [{section: <index>, quote: <string>}]}` is asked for the `k` most
   useful, best first, each quote copied exactly from its section.
4. **Validation.** Each quote must be a contiguous substring of its section
   (after collapsing whitespace on both sides), else it is dropped; duplicate
   picks are dropped. `text` is the quote as it appears in the file. `id`,
   `uri`, `text_sha256` per §3.2; `score` = `1 - (rank-1)/k`.
5. **Cache** by sha256 of the normalised request, the scope header, the model,
   a prompt version and every searched pack's `version`. The cache holds the
   response only — pack text, which is not confidential — never the query,
   which is built from the client's brief.

Honest limits, which the mock's health and `trace.backend`
(`claude-code-packs`) state:

- **`engine/packs_dist` holds digests, not the corpus**: five `digest.md` files
  (briefing-template, cannes, dandad, ipa, playbooks), three sections each
  ("What great looks like", "Craft rules for a brief", "Traps") of paraphrased
  notes distilled offline. There is **no Effie pack** in `packs_dist` or in
  `packs.lock`. Against `packs_dist` the mock lists those five packs with
  `kind: digest` and returns real, verbatim quotes of the digests — real file
  and section refs, but pattern notes, not award cases. The case corpora
  (`engine/reference/rag/`) are gitignored and not on this machine; pointing
  `BRIEF_CORPUS` at them makes the mock serve real cases.
- Selection is a model's judgement, not similarity; two fresh runs can differ.
  Determinism comes from the cache.

---

## 4. Layers port — `napkin.layers/1`

The knowledge layers (D1): category facts shared by everyone, brand facts
under one agency and brand, sources, the category tree and the brand roster.
Today a Python `Layers` protocol with `LocalLayers` on SQLite
(`server/napkin/layers/`); in production Postgres with row-level security. It
becomes an HTTP service. The middleware keeps a client, `HttpLayers`, that
implements the **same `Layers` protocol** — no handler changes — and the mock
serves it from SQLite.

### 4.1 Who owns what

| Middleware (logic) | Layers service (storage) |
|---|---|
| Source tiering by domain policy (`rules/tiering.py`) — the tier arrives on `POST /sources` | Storing sources, one row per URI, ids |
| Fact extraction, quote and figure checks | Fact rows with typed value columns |
| **Derived confidence** from tier and corroboration (`rules/confidence.py`) — never stored in the layer | Versions, supersession and contested status **for one identity**, in the append transaction (§4.4) |
| **Merge** across runs by entity + key + market, contests in the document (`rules/merge.py`) | The link from a fact to every source that corroborates it |
| Deciding what to pin and when to reuse (`NAPKIN_REUSE_DAYS`) | Resolving a pin URI to its exact row |
| Reasoning (R1), cite rules, the judge | Storing the decision that justified a write, whole, with it |
| Deriving scope from auth (M3) | Enforcing scope: rows visible only under the headers' scope (RLS in Postgres) |
| Which roster keys mean what, when to write a roster row | The roster write as one transaction |

### 4.2 Scope

Every route needs `X-Napkin-Org`; brand-layer reads and writes also need
`X-Napkin-Brand` (§0.2). The service never computes scope:

- **Category layer** (`layer: category`) rows are shared: readable by every
  scope, written with `written_by_org` = the header, stored with no scope.
- **Brand layer** (`layer: brand`) rows are stored under `(org, brand)` from
  the headers and are visible only under exactly that pair. The same entity
  key under another brand, or another org, is another row.
- **Sources** are one row per URI. A source whose `licence` is
  `client-confidential` (a `human:<id>` confirmation, a client document) is
  visible only to the org that added it; `open` and `licensed-internal`
  sources are visible to all. (Today `LocalLayers.sources()` returns any id to
  any scope — a defect this fixes.)
- **Brands** (the name registry behind `find_brands` / `note_brand`) are per
  org.
- **Decisions** stored with writes are visible under the scope they were
  written in.

### 4.3 Routes — the protocol, method by method

Base `NAPKIN_LAYERS_URL`; every path below is under it. `{ref}` is an entity
ref with its slash kept (`/v1/layers/roster/brand/lunasa`).

| Protocol method | Route | Request | 200 response | Not found |
|---|---|---|---|---|
| `leaves()` | `GET /v1/layers/categories` | — | `{taxonomy_version, leaves: [{code, name, vertical, vertical_name, aliases[], regulated, provisional}]}` in taxonomy order | — |
| `vertical_of(leaf)` | `GET /v1/layers/categories/{leaf}/vertical` | — | `{code, name, aliases[], leaves: [codes]}` | `404 unknown_leaf` → client returns `None` |
| `find(text)` | `POST /v1/layers/categories/find` | `{text}` | `{leaves: [codes]}` (the matching rule of `LocalLayers.find`: leaf code, name or alias as a whole word first; else every leaf of a matching vertical) | — |
| `facts(layer, entity, key?, market?, key_prefix?)` | `GET /v1/layers/facts?layer=&entity=&key=&market=&key_prefix=` | — | `{facts: [row]}`: current rows (not superseded), `key` exact or `key_prefix` (the key itself or `<prefix>.…`), `market` = that market **or** market-independent; ordered by key, market, version | `{facts: []}` |
| `append(fact, decision)` | `POST /v1/layers/facts` + `Idempotency-Key` | `{fact, decision}` (§4.4) | `{fact: row, outcome: created \| corroborated \| superseded \| contested}` | — |
| `resolve(pin_uri)` | `GET /v1/layers/facts/by-uri?uri=fact://…` | — | `{fact: row}` — that exact version, whatever its status now | `404 unknown_fact` → `None` |
| `add_source(source)` | `POST /v1/layers/sources` + `Idempotency-Key` | `{source: {uri, tier, domain, licence, publisher?, title?, retrieved_at?, published_at?}}` | `{id, created}` — an existing URI returns its id, `created: false`, and changes nothing | — |
| `sources(ids)` | `GET /v1/layers/sources?ids=src_a,src_b` | — | `{sources: [record]}` — the ids visible to this org, in request order; invisible or unknown ids are omitted | — |
| `roster(brand_ref)` | `GET /v1/layers/roster/{ref}` | — | `{ref, name, categories: [≤2 codes], client_org, facts: [row]}` from the active `roster.*` facts | `404 unknown_brand` → `None` |
| `set_roster(...)` | `PUT /v1/layers/roster/{ref}` + `Idempotency-Key` | `{name, categories: [1–2 leaf codes], client_org?: "org/<slug>", sources: [ids], decision}` | `{facts: [row]}` | — |
| `find_brands(text)` | `POST /v1/layers/brands/find` | `{text}` | `{brands: [{ref, name}]}` (the matching rule of `LocalLayers.find_brands`) → client returns `[(ref, name)]` | — |
| `note_brand(ref, name)` | `PUT /v1/layers/brands/{ref}` | `{name}` | `{ref, name}` — upsert of the org's display name | — |
| (audit, tests) | `GET /v1/layers/decisions/{id}` | — | `{decision}` as stored | `404 unknown_decision` |

A **fact row** (`peripherals/layers.schema.json#/definitions/fact_row`) is
today's `LocalLayers._row` shape: `{id, layer, entity, key, market | null,
value, unit, as_of, retrieved_at, status: active | contested | superseded,
version, supersedes | null, licence, method | null, decision, origin,
sources: [ids], source_records: [{id, uri, publisher, title, tier, domain,
licence, retrieved_at, published_at, quote}]}`. `value` is a number, string
or boolean — never an object (typed columns, W1-C1).

`roster` keys are fixed by this contract: `roster.categories.primary`,
`roster.categories.secondary`, `roster.client_org` (brand layer, unit
`code`, licence `client-confidential`, method `report`), as `LocalLayers.set_roster`
writes them. The categories are leaf codes the tree knows.

### 4.4 Append — append-only, with supersession

`POST /v1/layers/facts`:

```json
{ "fact": { "layer": "category", "entity": "category/automotive.ev_charging",
            "key": "market_structure.bev_share", "market": "IE",
            "value": 0.2, "unit": "proportion", "as_of": "2026-06-30", "retrieved_at": "2026-09-24",
            "sources": ["src_4f2a"], "quotes": { "src_4f2a": "…verbatim…" },
            "licence": "open", "method": "report" },
  "decision": { "id": "d_01JB…", "kind": "pin", "handler": "research_lens@1.0",
                "action": "research_merge", "rationale": "…", "cites": ["src_4f2a"],
                "reasoning": { "decided": "…", "because": [ ], "…": "…" } } }
```

Validation (all `400 invalid_input` unless named): `layer` `brand|category`;
`entity` `^(brand|org|category)/[a-z0-9][a-z0-9._-]*$`; `key`
`^[a-z0-9_]+(\.[a-z0-9_]+)*$`; `market` ISO alpha-2 or absent; `value`
scalar; `unit`, `as_of`, `retrieved_at` (`YYYY-MM-DD`) required;
`licence` **required** — no default (today `LocalLayers` defaults a missing
licence to `open`, which silently declassifies, C2); every `sources` id
visible to this org; `quotes` keys among `sources`; `status`, when sent, only
`contested`. A category-layer fact on `category/<leaf>` whose leaf the tree
does not know is `400 unknown_leaf` (Contract 3 §2.3: "the category layer
rejects an unknown leaf"; today it does not). `decision.id` and
`decision.kind` required; the decision is stored **whole**, reasoning
included.

The rule, for identity `(layer, scope, entity, key, market)`, over its current
(not superseded) rows — exactly `LocalLayers.append`:

| The current rows hold | Outcome | Rows after |
|---|---|---|
| nothing | `created` | a new row, version 1 above the entity + key's highest (versions count per entity + key **across markets**, so a `fact://` URI, which carries no market, names one row), `active` — or `contested` when the request said so |
| a row with the same value (numbers compared as numbers, booleans only with booleans) | `corroborated` | that row, unchanged; the new sources linked to it with their quotes |
| an `active` row with another value, and the new `as_of` is **later** | `superseded` | a new row `active` with `supersedes` = the old; the old row `superseded`, `superseded_by` the new |
| an `active` row with another value, and the new `as_of` is not later — or the request said `contested` | `contested` | a new row `contested`; every current row of the identity `contested` |

Rows are never updated except `status` and `superseded_by` as the table says,
and source links added. Nothing is deleted. The response's `fact` is the row
now current for the identity that the write landed on (`corroborated`: the
existing row).

This rule is per identity and inside one transaction, so it is the layer's.
Whether it should move to the middleware — the owner's "merge stays in the
middleware" — is **Open** (§9, O3; recommendation: keep it here, specified
exactly as above, because it must be atomic with the write).

### 4.5 Versions, pins, idempotency

- **Pin URI**: `fact://<layer>/<entity path>/<key>@<version>`, the entity's
  type segment dropped when it equals the layer (Contract 3 §4):
  `fact://brand/bulmers/awareness.prompted@12`,
  `fact://category/drinks.cider/rhythm.peak_months@3`,
  `fact://category/brand/orchard-hill/launch.date@1`. The service returns it
  as `origin` on every row; the middleware still computes it with
  `origin_uri()` for rows it builds. A brand-layer URI resolves only under the
  scope that wrote it.
- A pin is frozen: `resolve` returns the version named even after it is
  superseded, with its current `status`, so the host's `/stale` can offer the
  newer one.
- **Idempotency.** Every `POST` and `PUT` that writes carries
  `Idempotency-Key: <≤128 chars>`. The port client derives it
  deterministically — `sha256(decision.id + "\n" + layer + entity + key +
  market + canonical value)` for an append, of the URI for a source, of the
  decision id + ref for a roster — so a retried job step sends the same key.
  Within 24 hours a repeat with the same key and body returns the first
  response with `Idempotent-Replay: true`; the same key with a different body
  is `409 idempotency_conflict`. A missing key on a write is `400
  invalid_input`. (Appends are also naturally repeat-safe — the same value
  corroborates — but a supersession is not, which is why the key is required.)

### 4.6 Atomicity — a fact and its decision commit together (P2)

P2 rejected a network boundary through "the one operation that must be
atomic: a fact and the decision justifying it commit together". The owner's
decision puts the layers behind HTTP; the contract keeps the atomicity by
making it **one request**:

- `POST /v1/layers/facts` carries the fact **and** its decision; the service
  commits both, the source links and any status flips in one transaction, or
  none of it (a failure leaves no decision row, no fact row, no link).
- `PUT /v1/layers/roster/{ref}` writes every roster fact, the decision, and the
  brand's name in one transaction. (Today `set_roster` calls `append` per key
  and `note_brand` separately — not atomic; the service fixes it.)
- `GET /v1/layers/decisions/{id}` lets the suite check that a refused write
  left no decision.

What P2 also protected is **not** restored by this, and was already not true:
the layer write and the document's own change (the pin in
`shared/facts.yaml`, the decision in the chain) are two commits in two
stores. The research pipeline writes the layer first and pins from the row
the layer returns (D1), so a host that later refuses the change leaves a
layer fact whose decision is not in any document's chain. This is today's
behaviour with `LocalLayers` and remains so; it is noted, not solved.

### 4.7 Errors

| Status | `type` | When |
|---|---|---|
| 400 | `invalid_input` | a validation rule; body not JSON; scope in the body; write without `Idempotency-Key` |
| 400 | `missing_scope` | `X-Napkin-Org` missing, or `X-Napkin-Brand` missing on a brand-layer or roster route |
| 400 | `unknown_leaf` | a write naming a category leaf the tree does not know |
| 401 | `unauthenticated` | a configured token missing or wrong |
| 404 | `unknown_fact` \| `unknown_brand` \| `unknown_leaf` \| `unknown_decision` | the lookups in §4.3 |
| 409 | `idempotency_conflict` | same key, different body |
| 503 | `unavailable` | the store is unreachable — never an empty 200 |
| 500 | `internal` | anything else |

### 4.8 What the mock does

No model. The mock's layers are today's `LocalLayers` moved behind these
routes: the same SQLite schema (plus a `idempotency(key, body_sha256,
response, created_at)` table and the whole decision JSON in `decisions`), the
same append rule, seeded on first start with the taxonomy (`TREE`, 18
verticals, 108 leaves plus the provisional ones) — the data file moves with
it, and the real service seeds from the same file. `MOCK_LAYERS_DB` sets the
file (default `<MOCK_DATA>/layers.sqlite`); deleting it resets the layers. It
applies the fixes this contract makes (licence required, unknown leaf refused,
confidential sources org-scoped, roster atomic, idempotency).

---

## 5. The mock backend

One process fakes every peripheral. It replaces `mock-llm/` and
`mock-research/`, which retire once their tests are ported into it.

### 5.1 Process

- `mock-backend/`, Python 3.11+, **standard library only**, one
  `ThreadingHTTPServer`. `python3 mock-backend/server.py`.
- Binds `MOCK_BACKEND_HOST` (default `127.0.0.1`) : `MOCK_BACKEND_PORT`
  (default **8797**). Ports in use elsewhere, never to be taken: 8080, 8090,
  8787 (engine agent server / mock-agent), 8788, 8790 (mock-middleware),
  8791, 8792, 8795 (the middleware), 8796.
- Needs `claude` on `PATH`, signed in; it refuses to start without the binary
  when any Claude-backed peripheral is enabled.
- `MOCK_FAKES` (default `model,research,retrieval,layers`) chooses the families
  served; a disabled family's routes answer `404`. This is what lets a real
  peripheral replace one family while the mock serves the rest — the
  middleware still changes only a URL.

### 5.2 Routes

| Family | Route | Answered by |
|---|---|---|
| model | `POST /v1/messages` | `claude -p`, Anthropic shape (§1.3) |
| model | `POST /v1/chat/completions` | `claude -p`, OpenAI shape (§1.4) |
| model | `GET /v1/models` | the id map; Anthropic list shape when the request has `anthropic-version`, OpenAI list shape otherwise |
| research | `POST /v1/research` | `claude -p` + WebSearch, WebFetch (§2; today's `mock-research` behaviour) |
| retrieval | `GET /v1/packs`, `POST /v1/retrieve` | pack files + `claude -p` (§3.6) |
| layers | `/v1/layers/…` | SQLite, no model (§4.8) |
| — | `GET /healthz` | §5.5 |
| — | `/v1/embeddings` and anything else | `404` |

### 5.3 Claude underneath

- **One fresh subprocess per call**, never `--resume`, in an empty temporary
  directory, `--no-session-persistence --setting-sources "" --strict-mcp-config
  --disable-slash-commands --output-format json`, `--json-schema <schema>` when
  the call has one, and `--tools ""` except research (`--tools WebSearch
  WebFetch --allowedTools WebSearch WebFetch --permission-mode dontAsk`).
- The prompt goes on **stdin**, the system prompt through a pipe fd
  (`--system-prompt-file /dev/fd/N`) — neither on argv, neither on disk.
  `ANTHROPIC_BASE_URL` and `OPENAI_BASE_URL` are removed from the child's
  environment so the CLI never calls back into the mock.
- **Model family translation** (both shapes): system → the system fd; one user
  turn verbatim; a multi-turn history (the port's retry turn) as a
  `<user>…</user>` / `<assistant>…</assistant>` transcript; structured output
  via `--json-schema`, the envelope's `structured_output` serialised as the
  reply text; usage from the CLI envelope, never estimated (as `mock-llm`
  does today); `stop_reason: end_turn` / `finish_reason: stop`. Images: sent to
  the CLI as image content blocks through `--input-format stream-json`. If the
  installed CLI cannot take them, the mock answers `400` "image input is not
  supported by this stand-in" in the wire's shape rather than dropping the
  image — verifying this is the mock builder's first task (§9, O6).
- **Accepted and ignored** (no CLI flag exists): Anthropic `temperature top_p
  top_k stop_sequences metadata thinking service_tier cache_control`; OpenAI
  `temperature top_p seed user chat_template_kwargs nvext`.
- **Refused with the real API's error shape**, because dropping them silently
  would pass in development and fail in production: `tools`, `tool_choice`,
  `functions`, tool-use/tool-result blocks, `mcp_servers`; `stream: true`;
  an assistant-last prefill; `document`, `audio` or other non-text, non-image
  content; OpenAI `n > 1`, `logprobs`, `response_format` types other than
  `json_schema`; unknown top-level fields (Anthropic shape). All `400`
  (`invalid_request_error`).
- **Concurrency**: one semaphore of `MOCK_CONCURRENCY` (default 4) Claude
  subprocesses across all families. No free slot within `MOCK_QUEUE_TIMEOUT`
  → model: `529 overloaded_error` (Anthropic) / `503` (OpenAI,
  `{"error": {"type": "server_error", "code": "overloaded"}}`); research and
  retrieval: `503 {"error": {"type": "overloaded"}}`.
- **Timeouts** per family (`MOCK_TIMEOUT_MODEL` 180 s, `MOCK_TIMEOUT_RESEARCH`
  420 s, `MOCK_TIMEOUT_RETRIEVAL` 180 s): the process group is killed and the
  answer is `504` in the family's shape. A CLI failure (non-zero exit,
  `is_error`, no structured output) is `502` (research, retrieval) or the
  mapped model error (`mock-llm`'s table: not logged in → 401, 404/429/529 →
  that, else `500 api_error`).
- `MOCK_MAX_BUDGET_USD` passes `--max-budget-usd` per call when set.

### 5.4 Cache and data

- `MOCK_DATA` (default: the session scratchpad `…/mock-backend`) holds
  `cache/`, `work/` and `layers.sqlite`.
- **Research and retrieval** responses are cached on disk by sha256 of the
  normalised request (plus model, prompt version, and for retrieval the scope
  header and pack versions). A hit returns the stored response unchanged.
  Failures are never cached. `?fresh=1` or `MOCK_NO_CACHE=1` skips reading it.
  What is on disk: research — its query parameters (public-web questions) and
  public excerpts; retrieval — pack passages only, never the query (§3.6).
- **Model calls are never cached**: two identical requests may differ, as the
  real API's do.

### 5.5 Health

`GET /healthz` →

```json
{ "ok": true, "service": "napkin-mock-backend",
  "fakes": {
    "model":     { "api": "napkin.model/1", "shapes": ["anthropic", "openai"],
                   "models": { "claude-opus-5": "opus", "…": "…" }, "images": true },
    "research":  { "api": "napkin.research/1", "backend": "claude-code-websearch", "model": "sonnet" },
    "retrieval": { "api": "napkin.retrieval/1", "backend": "claude-code-packs",
                   "packs_dir": "engine/packs_dist", "packs": ["cannes", "…"], "kinds": ["digest"] },
    "layers":    { "api": "napkin.layers/1", "backend": "sqlite", "taxonomy_version": "0.1" } },
  "concurrency": 4, "in_flight": 0, "peak": 3 }
```

A family not served is absent from `fakes`.

### 5.6 What it never does

- Writes a request body anywhere, unless `MOCK_DUMP_REQUEST_BODIES_TO` names a
  directory (off by default; bodies carry client-confidential material —
  delete the directory afterwards). It logs one metadata line per request.
- Carries logic that belongs to the middleware: no tiering, confidence, merge,
  reasoning, judging, quote checks beyond its own honesty checks (research
  URLs, retrieval quotes verbatim in the pack file).
- Pretends: no fake streaming, no fake tool use, no estimated usage, no
  default model, no empty 200 on a failure.

---

## 6. Brief Maker on the middleware

The binding text is `middleware-api.md` §10 (tasks `draft_brief` and
`regenerate_field`, handlers `draft_brief@1`, `regenerate_field@1`). This
section is how that flow uses the ports.

| Stage (`job.stage`) | Work | Ports | Never |
|---|---|---|---|
| `extract` | Transcribe images; Loop-1 no-loss capture (one structured call over the materials, verbatim client words, every quote checked verbatim); the working brief's captured fields; open questions; the BetterBriefs scorecard; the no-loss ledger | model | retrieval, research — the capability object has none |
| `draft` | One drafter per strategy field, **in parallel**: each builds its loop's query from the working brief, retrieves, drafts (a tournament for insight and SMP), and cites passages, capture items and pinned facts | retrieval, model; layers read-only (`resolve` a carried pin's currency) | writes to the layers |
| `judge` | **Serial.** The Judge (engine's `golden_critic`, ported) runs the auto checks in code, the model checks field by field and the cross-field coherence checks, never seeing any drafter's context; one revision per failed field, re-judged once; verdicts become decisions | model; retrieval (loop-7 decision rules only) | reading drafter prompts, candidates or grounds (UC-6) |

**How a passage is cited.** Contract 3's cite rule (§17) binds the report's
claims to pins and findings and does not reach a brief; Contract 1 §3 requires
every id a `because` point cites to **resolve in the document after the
change**. A passage lives in the retrieval service, which can change, so a
cite to it would dangle. So:

- a decision cites a passage by its id `psg_…` (§3.2);
- every passage any decision in the change cites is written into the
  document by the same change, under **`data.passages.<psg_id>`** — a map, so
  staged writes merge key-wise, merge policy `append` — holding `{uri, pack,
  scope, licence, source, section, citation, text, text_sha256,
  pack_version, retrieved_at}`;
- its address is `<doc-id>#passages[psg_…]`.

A data block rather than a new member (`shared/passages.yaml`): passages are
small, written only by the middleware, never edited, and a member needs
packaging and registration work that buys nothing yet. **Open** (§9, O5) —
recommendation: the data block now; a member if a brief routinely carries
more than ~50 passages. Licence travels with each passage: `licensed-internal`
text is redacted at the client boundary by `compose_export` like any other
licensed-internal content (C2), with a visible tombstone.

Facts a brief cites are **pins in the document** (`clan.facts`) — e.g. a brief
spun off from a Research Tool campaign carrying its pins (D6) — cited by
`f_…`; a drafter never reads the raw layer. How the host sends a carried
upstream's facts is **Open** (§9, O7).

---

## 7. Middleware configuration

Environment only (`server/napkin/config.py`); handlers never see any of it.

| Variable | Default | Mock value | Real value |
|---|---|---|---|
| `NAPKIN_MODEL_API` | `anthropic` | `anthropic` or `openai` | `anthropic` (Claude API) or `openai` (NIM) |
| `NAPKIN_MODEL_BASE_URL` | unset (SDK default) | `http://127.0.0.1:8797` (anthropic) · `http://127.0.0.1:8797/v1` (openai) | unset / `https://api.anthropic.com` · `https://<nim host>/v1` |
| `NAPKIN_MODEL_API_KEY` | unset (SDK resolution) | any non-empty, e.g. `dummy` | the API or NIM key |
| `NAPKIN_MODEL` | `claude-opus-5` | `claude-opus-5` (or a NIM id in `MOCK_MODEL_ALIASES`) | `claude-opus-5` · the NIM model (O1) |
| `NAPKIN_VISION_MODEL` | = `NAPKIN_MODEL` | same | `claude-opus-5` · `nvidia/llama-3.1-nemotron-nano-vl-8b-v1` |
| `NAPKIN_MODEL_EXTRA_BODY` | unset | unset | e.g. `{"chat_template_kwargs":{"enable_thinking":false}}` for a NIM reasoning model |
| `NAPKIN_MODEL_TIMEOUT` | `600` | `600` (never tune to the mock's latency) | `600` |
| `NAPKIN_MODEL_CONCURRENCY` | `6` | `4` (= `MOCK_CONCURRENCY`) | provider limit |
| `NAPKIN_RESEARCH_URL` | unset (research units fail as gaps) | `http://127.0.0.1:8797` | the search service |
| `NAPKIN_RESEARCH_TOKEN` | unset | unset | service token |
| `NAPKIN_RESEARCH_TIMEOUT` | `900` | `900` | `900` |
| `NAPKIN_RESEARCH_CONCURRENCY` | `4` | `4` | `4` |
| `NAPKIN_RETRIEVAL_URL` | unset (drafters get no passages: fields certainty `low`, attention set) | `http://127.0.0.1:8797` | the retrieval service |
| `NAPKIN_RETRIEVAL_TOKEN` | unset | unset | service token |
| `NAPKIN_RETRIEVAL_TIMEOUT` | `120` | `240` (Claude picks) | `30` |
| `NAPKIN_LAYERS_URL` | **required** | `http://127.0.0.1:8797` | the layers service |
| `NAPKIN_LAYERS_TOKEN` | unset | unset | service token |
| `NAPKIN_LAYERS_TIMEOUT` | `30` | `30` | `30` |
| `NAPKIN_REUSE_DAYS` | `30` | `30` | `30` |
| `NAPKIN_DEV_ORG` / `NAPKIN_DEV_BRAND` | `org/dev-agency` / `brand/dev-brand` | same | unused (scope from auth) |
| `NAPKIN_TOKEN` | unset | unset | the host's middleware secret |
| `NAPKIN_PIPELINES` | every `app/templates/*/app/pipeline.yaml` | same | same |
| `NAPKIN_HOST` / `NAPKIN_PORT` | `127.0.0.1` / `8795` | same | deployment |

Removed: `NAPKIN_LAYERS=local:…` (the middleware no longer opens a database).
`ANTHROPIC_BASE_URL` / `ANTHROPIC_API_KEY` still work through the SDK when the
`NAPKIN_MODEL_*` values are unset, so today's `mock-llm` wiring keeps working
until it retires.

Mock backend variables (all optional): `MOCK_BACKEND_HOST`, `MOCK_BACKEND_PORT`
(8797), `MOCK_FAKES`, `MOCK_CONCURRENCY` (4), `MOCK_QUEUE_TIMEOUT` (= the
family timeout), `MOCK_TIMEOUT_MODEL` (180), `MOCK_TIMEOUT_RESEARCH` (420),
`MOCK_TIMEOUT_RETRIEVAL` (180), `MOCK_MAX_BODY_BYTES` (33554432 → `413`),
`MOCK_MAX_TURNS` (4), `MOCK_CLAUDE_BIN` (`claude`), `MOCK_MODEL_ALIASES`,
`MOCK_RESEARCH_MODEL` (`sonnet`), `MOCK_RETRIEVAL_MODEL` (`sonnet`),
`MOCK_RETRIEVAL_MAX_CHARS` (150000), `MOCK_PACKS_DIR` (`engine/packs_dist`),
`BRIEF_CORPUS`, `MOCK_AGENCY_PACKS_DIR`, `MOCK_LAYERS_DB`, `MOCK_DATA`,
`MOCK_NO_CACHE`, `MOCK_MAX_BUDGET_USD`, `MOCK_TOKEN` (when set, every route
requires it), `MOCK_DUMP_REQUEST_BODIES_TO` (off).

---

## 8. Contract tests

One suite per port, in `mock-backend/contract/`, each `python3
<port>_contract.py --base-url <url> [--token <t>]`, standard library only.
Every suite names no implementation and runs unchanged against the mock and
the real peripheral. Checks that make live model or search calls are marked
**live** and can be skipped with `--no-live`; checks the real service cannot
be asked to fail are behind a flag, never edited out.

### 8.1 Model — `model_contract.py --api anthropic|openai --model <id>`

- Plain text call: 200, the wire's response shape, `usage` integers ≥ 0. **live**
- Structured call with a strict schema (nested object, enum, array, nullable):
  the text parses and validates. **live**
- The retry shape (user, assistant, user) is accepted. **live**
- Image transcription: a generated PNG reading `NAPKIN 42` comes back
  containing `42`. **live**
- Unknown model id: `404` in the wire's error shape.
- Malformed body (missing `max_tokens` / `messages`): `400` in the wire's shape.
- `--expect-refusals` (the mock; the real APIs accept these): `tools`,
  `stream: true`, a prefill, a `document` block are each `400` in the wire's
  shape.
- The response `model` echoes the request's.

### 8.2 Research — `research_contract.py` (today's `mock-research/contract_test.py`, moved)

- Health. Each §2.1 rule violated → `400 invalid_input`; unknown path `404`.
- One live query: every source has an http(s) URL, a title, a publisher, 1–5
  non-empty quotes ≤ 1,500 characters, `retrieved_at` a date ≤ today,
  `published_at` a date ≤ today or absent/null; URLs unique; `trace.backend`
  present. **live**
- `max_sources` caps the list. **live**
- `--cache-check` (optional for a real service): a repeat is identical.

### 8.3 Retrieval — `retrieval_contract.py --org <org> [--agency-pack <tag> --agency-owner <org> --other-org <org>]`

- `GET /v1/packs` shape; every pack has a `version`; tags unique.
- Missing `X-Napkin-Org` → `400 missing_scope`; `scope` in the body → `400`.
- Unknown field (`"fill": "insight"`, `"document": {}`) → `400 invalid_input`.
- `k` out of range, `where` on a key not `filterable` → `400`.
- A live retrieve: ≤ `k` passages; `rank` 1…n; `score` non-increasing;
  `citation == source + " › " + section`; `text_sha256` is the SHA-256 of
  `text`; `id` and `uri` recompute from the §3.2 formula; `pack` among the
  requested; `trace.packs` has a version per searched pack. **live**
- Determinism of identity: the same passage in two responses has the same `id`.
- S6: with the fixture flags, the agency pack is listed and searchable for its
  owner, and for `--other-org` it is absent from `/v1/packs` and `404
  unknown_pack` when named.

### 8.4 Layers — `layers_contract.py` (no live calls; runs in seconds)

- Categories: `leaves()` non-empty with the documented keys; `vertical_of` of a
  known leaf lists it; an unknown leaf `404`; `find("cider")` finds
  `alcohol.cider`.
- Scope isolation: a brand fact written under (A, X) is visible under (A, X),
  invisible under (A, Y) and (B, X); a category fact written under A is
  visible under B; a `client-confidential` source added by A is invisible to B.
- Missing headers → `400 missing_scope`; scope in the body → `400`.
- The four append outcomes of §4.4 in order on one identity (created,
  corroborated with the second source linked, superseded with a later `as_of`,
  contested with an earlier one — both rows then `contested`); versions count
  across markets; `origin` matches the URI grammar.
- `resolve` returns the exact version, including a superseded one with its
  status; an unknown URI `404`.
- Validation: missing `licence`, object `value`, bad entity or key, unknown
  leaf → `400`; and **atomicity** — after each refused append,
  `GET /v1/layers/decisions/<its id>` is `404`.
- Idempotency: a replay returns the first response with `Idempotent-Replay:
  true` and no new version; same key, different body `409`; a write with no key
  `400`.
- Roster: `PUT` then `GET` round-trips categories and client; unknown brand
  `404`; the roster's facts carry the decision.

### 8.5 The middleware's own suite

`mock-middleware/contract_test.py` (the host ↔ middleware suite) gains the
Brief Maker checks in `middleware-api.md` §10.12 and still passes unchanged
against both middlewares. The middleware's unit tests stop importing
`LocalLayerStore` (`server/tests/conftest.py`) and use `HttpLayers` over an
in-process transport to the mock's layers application, so the tests exercise
the same client production uses.

---

## 9. Open points

Each with a recommendation; none is guessed in the text above beyond the
recommendation.

| # | Point | Recommendation |
|---|---|---|
| O1 | Which NIM model production runs, and whether it honours `response_format: json_schema` with `strict` (the engine found some NIM models reject it with a 400) | Pick one that does; the model port refuses to fall back to unconstrained JSON (§1.4). Run §8.1 against the NIM endpoint before committing |
| O2 | One model for every purpose, or per-role models (a cheaper drafter, a stronger judge) | One (`NAPKIN_MODEL`) plus the vision model now; add `NAPKIN_MODEL_<ROLE>` only when measurement shows a gain |
| O3 | Whether the layers service keeps the per-identity supersede/contest rule (§4.4) or the middleware decides and the service only inserts | Keep it in the service: it must run in the append transaction, and moving it means a read-then-write race across HTTP. The middleware's merge (across runs) and contests (in the document) stay middleware-side, as the owner decided |
| O4 | Same as O3 for `set_roster`'s key mapping (`roster.categories.primary` …) | Keep the route: the three keys are fixed here; the middleware still decides *when* a roster row is written |
| O5 | Passages as a data block (`data.passages`) or a member (`shared/passages.yaml`) | Data block now (§6) |
| O6 | Images: whether `claude -p --input-format stream-json` takes image blocks (the mock's vision path), and whether image-only PDFs get their pages rendered for transcription (engine did, with PyMuPDF) | The mock builder verifies the CLI first; if it cannot, the mock refuses images honestly and §8.1's image check runs only against a real endpoint. Page rendering belongs to the host's extraction (it already extracts PDF text), not the middleware — a host task. *The host now sends a picture attachment's bytes as `image`; rendering an image-only PDF's pages is still a TODO in `ops/read.rs` `splice_images` (it needs a PDF rasteriser in the host)* |
| O7 | How carried upstream facts reach the middleware: `clan.facts` holds only the brief's own member; D6's carried upstream is not sent (the host's `clan_context_for_agent` has no `carried`) | Host sends `clan.carried: [{id, facts, findings}]`; the middleware cites carried pins by address `<upstream-doc-id>#facts[f_…]`. Until then a brief cites only pins in its own `clan.facts` |
| O8 | Whether the research port receives the org for metering | Not in v1 (public-web questions); add `X-Napkin-Org` as informational only if billing needs it |
| O9 | Where the Brief Maker rubric (the golden-brief field prompts, examples, limits, checks) lives — M2's principle says app-specific knowledge arrives as a declaration | The field map (app key ↔ loop ↔ packs kind) is declared in Brief Maker's `pipeline.yaml` (`middleware-api.md` §10.2); the rubric text is bundled with `draft_brief@1` and versioned with it, since a major pins behaviour. Move it into the template when a second brief app needs a different rubric |
| O10 | N4's parallel drafters vs the engine's sequential dependency (insight → SMP → RTBs → desired response); the golden schema even makes SMP and RTBs depend on each other | Parallel, per N4; the serial Judge's coherence checks (`smp_derivation`, `rtb_supports_smp`, `response_ladders`) and one serial revision own the dependency. Watch the engine's SMP-derives-from-insight check as the quality measure; fall back to two waves (insight first) only if it regresses |
| O11 | Verdict reason codes for the Judge's failures (`middleware-api.md` §10.8 maps rubric checks to the ten codes) | Use the mapping; it needs the creative director's redline with the codes themselves |
| O12 | The Brief Maker `capture` and `review` blocks (`middleware-api.md` §10.4) are additions to its schema | Accept: without `capture` the verbatim client words and the ledger have no home in the document, and today they are lost after the context panel |

## 10. What the current code contradicts

For the two builds to fix; none is fixed by this document.

1. **`server/` opens the layers in-process.** `app.py` calls
   `layers.open_store(settings.layers)` → `LocalLayerStore` (SQLite);
   `tests/conftest.py` imports `LocalLayerStore`. The owner's decision makes
   the layers a peripheral: `HttpLayers` replaces both, and
   `server/napkin/layers/local.py` + `taxonomy.py` move to the mock (the
   taxonomy data to a file both the mock and the real service seed from).
   M5's text ("LocalLayers behind the Layers protocol") needs amending to "the
   layers service behind `napkin.layers/1`".
2. **P2's reason** (no network boundary through the fact + decision commit) is
   met by the one-request append (§4.6), not by a shared transaction; the spec
   should record that the owner accepted this.
3. **The model port is Anthropic-only.** `app.py` constructs
   `anthropic.Anthropic()` unconditionally and `model.py` speaks one wire; no
   OpenAI-compatible path, no vision, no `NAPKIN_MODEL_*` settings.
4. **No retrieval port.** `config.py` has no retrieval URL; retrieval exists
   only inside `engine/` (`rag.py`, Qdrant, NIM embeddings), with **no scope
   filter** — the foundation spec's "immediate" item ("converge the brief
   generation path onto the scope-filtered retrieval entry point").
5. **`LocalLayers` defaults a missing licence to `open`** (`add_source`,
   `append`) — a silent declassification (C2); refuses no unknown leaf
   (Contract 3 §2.3); returns any source to any scope; writes the roster
   non-atomically.
6. **The host requires reasoning only on `campaign.*`, `selection.*`,
   `report`** (`napkin-host/src/ops/middleware.rs` `AGENT_FIELDS`) —
   Research-Tool knowledge in the OS layer, which Contract 4 §1 forbids, and a
   gap for Brief Maker: a `draft_brief` edit to `insight` would not be required
   to carry reasoning. The middleware sends it anyway (§10.7 there); the host
   rule should become "every middleware `edit` that writes a data path other
   than `intake.messages` or `materials`", or be declared per app.
   *Fixed (task/brief-host): declared per app — the `reasoning` block of
   `app/pipeline.yaml` over a floor of pin, contest, finding, verdict and
   every proposal (`middleware-api.md` §3).*
7. **The engine reads the model's self-reported confidence** (`fill_derivable_fields`,
   `confidence_floor: 0.6`; `golden_critic` low-confidence questions) — which
   Contract 3 §3 and R1 forbid. It does not port; certainty is derived
   (`middleware-api.md` §10.7).
8. **The engine drops structured output on a 400** (`parse_brief.py:1150`) and
   parses prose leniently (`_loads_lenient`); the model port does neither.
9. **Brief Maker's `pipeline.yaml`** declares `request_kind: agent`, tasks
   without handlers and `endpoint_default: http://localhost:8787`;
   `registry.TASKS` has no `draft_brief` / `regenerate_field`. Both change
   (§10.2 there).
10. **Brief Maker's view writes the agent's output itself** (`agentWrite` →
    `patchData` with `actor: analysis-model`) — Contract 1 §5: the template
    never writes middleware output; the host applies the `change`.
    *Fixed (task/brief-host): the view submits the tasks and polls; the host
    applies.*
11. **The host still defaults a task to `draft_brief`** (`prompt.rs:89`) — the
    M4 defect; it leaves with W2-C2 and must not be relied on.
    *Fixed (task/brief-host): `prompt.rs` and the `/agent-prompt` route are
    removed with the browser-side model call.*
12. **`engine/packs_dist` has digests only and no Effie pack**, and
    `rag/packs.lock` lists no Effie pack, though Effie is named as a corpus
    (§3.6).
13. **`mock-llm` refuses image blocks** (by design today); the engine's vision
    path needs them (§1.6, O6).
