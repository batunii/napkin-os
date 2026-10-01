# mock-backend

Napkin's one mock backend: a single process that fakes every peripheral the
middleware talks to, answering the generative ones with headless Claude Code.
The spec is `docs/contracts/peripherals.md` (contract 5) and
`docs/contracts/peripherals/*.schema.json`; §5 is this process.

```sh
python3 mock-backend/server.py          # http://127.0.0.1:8797
```

The middleware points each port at it by URL and at the real service the same
way. Nothing else changes (§0.1):

```sh
NAPKIN_MODEL_API=anthropic NAPKIN_MODEL_BASE_URL=http://127.0.0.1:8797    NAPKIN_MODEL_API_KEY=dummy
NAPKIN_MODEL_API=openai    NAPKIN_MODEL_BASE_URL=http://127.0.0.1:8797/v1 NAPKIN_MODEL_API_KEY=dummy
NAPKIN_RESEARCH_URL=http://127.0.0.1:8797
NAPKIN_RETRIEVAL_URL=http://127.0.0.1:8797
NAPKIN_LAYERS_URL=http://127.0.0.1:8797
```

Which drafter writes a brief is one setting. With `NAPKIN_BRIEF_ENGINE_URL` unset (the
default), `draft_brief` runs the middleware's own crew: Ellis captures, Sam fills what the
client left open from the research (or Dara proposes it as an assumption), Dara drafts and
Jude judges. Set it to the engine's agent server (`python3 serve.py --claude-code` listens on
:8787) and the engine writes the brief instead, falling back to the middleware's crew when it
cannot; the capture decision's attention note says which ran and why:

```
NAPKIN_BRIEF_ENGINE_URL=http://127.0.0.1:8787   # Sai's engine; unset = the middleware's crew
```

Python 3.11+, **standard library only**: no dependency to justify. The
Claude-backed families need `claude` on `PATH`, signed in. The process refuses
to start without it unless `MOCK_FAKES=layers`.

It holds **no middleware logic**: no tiering, confidence, merge, reasoning or
judging. Peripherals store, search, fetch and generate. The only checks it
makes are its own honesty checks: research URLs and quote shapes, and
retrieval quotes verbatim in the pack file.

It replaces `mock-llm/` (:8791) and `mock-research/` (:8792). Both were
retired once their tests were ported here.

## Routes

| Family | Route | Answered by |
|---|---|---|
| model | `POST /v1/messages` | one `claude -p`, Anthropic Messages shape (§1.3) |
| model | `POST /v1/chat/completions` | one `claude -p`, OpenAI-compatible shape with `response_format` `json_schema` (the NIM wire, §1.4) |
| model | `GET /v1/models` | the id map: Anthropic list shape when the request has `anthropic-version`, OpenAI list shape otherwise |
| research | `POST /v1/research` | one `claude -p` with WebSearch + WebFetch (§2) |
| retrieval | `GET /v1/packs`, `POST /v1/retrieve` | the pack files + one `claude -p` that picks and quotes (§3.6) |
| layers | `/v1/layers/…` (every route in §4.3) | SQLite, no model (§4.8) |
| — | `GET /healthz` | what it fakes (§5.5) |
| — | `/v1/embeddings`, anything else | `404` |

`MOCK_FAKES` (default `model,research,retrieval,layers`) chooses which
families are served. A family that is switched off answers `404`, and is
absent from `/healthz`. This lets a real peripheral replace one family while
the mock serves the rest.

## Claude underneath

Every call is **one fresh subprocess**, never `--resume`. It runs in an empty
temporary directory under `<MOCK_DATA>/work` with these flags:
`--no-session-persistence --setting-sources "" --strict-mcp-config
--disable-slash-commands`. The prompt goes on stdin. A system prompt (model
and retrieval calls) goes through a pipe fd (`--system-prompt-file
/dev/fd/N`), so it is never on argv and never on disk. Research sends no
system prompt: it keeps Claude Code's own, which drives its WebSearch and
WebFetch tool use, as `mock-research` did. `ANTHROPIC_BASE_URL` and `OPENAI_BASE_URL` are removed from
the child's environment. One semaphore of `MOCK_CONCURRENCY` subprocesses is
shared by all families.

### Model

- **Ids** (§1.5):

  | Model id | Alias |
  |---|---|
  | `claude-opus-5` | `opus` |
  | `claude-sonnet-5` | `sonnet` |
  | `claude-haiku-4-5` | `haiku` |
  | `claude-fable-5-1` | `fable` |

  `MOCK_MODEL_ALIASES` (a JSON object `{"<id>": "<alias>"}`) adds more ids. It
  replaces the default list, which maps these NIM ids:

  | NIM id | Alias |
  |---|---|
  | `nvidia/llama-3.3-nemotron-super-49b-v1` | `sonnet` |
  | `nvidia/llama-3.1-nemotron-70b-instruct` | `sonnet` |
  | `nvidia/llama-3.1-nemotron-nano-vl-8b-v1` | `haiku` |

  Any other id is a `404` in the wire's shape: `not_found_error` for
  Anthropic, `model_not_found` for OpenAI. It never falls back to a default.
  The response echoes the requested id.
- **Translation.** One user turn is passed through verbatim. A multi-turn
  history (the port's retry turn) is flattened into a `<user>…</user>` /
  `<assistant>…</assistant>` transcript. The call's `schema` goes to
  `--json-schema`. The envelope's `structured_output`, serialised, becomes the
  reply text. `effort` becomes `--effort` (Anthropic wire only).
  `stop_reason` is always `end_turn` and `finish_reason` always `stop`.
- **Images: supported.** Anthropic `image` blocks (`source.type: base64`) and
  OpenAI `image_url` parts (`data:<type>;base64,…`) are accepted. The bytes go
  to the CLI as image content blocks through `--input-format stream-json
  --output-format stream-json --verbose`. We verified that Claude Code 2.1.281
  reads them: the `NAPKIN 42` PNG in `contract/png_text.py` came back as
  `NAPKIN 42` on both wires (contract open point O6: the mock's half is
  answered yes).

  Accepted: `image/png`, `image/jpeg`, `image/gif` and `image/webp`, up to
  5 MB decoded per image and 20 images per call, in user turns only.

  Refused: a remote image URL. The bytes are client-confidential and must not
  be fetched from a link.
- **Usage** comes from the CLI envelope and is never estimated. Anthropic
  `input_tokens` is fresh + cache-creation + cache-read tokens as the CLI
  reports them. The `cache_*` fields are always 0. OpenAI `prompt_tokens` and
  `completion_tokens` use the same numbers.
- **Accepted and ignored** (the CLI has no flag for them):

  | Wire | Fields |
  |---|---|
  | Anthropic | `temperature top_p top_k stop_sequences metadata thinking service_tier cache_control` |
  | OpenAI | `temperature top_p seed user chat_template_kwargs nvext frequency_penalty presence_penalty stop max_completion_tokens stream_options parallel_tool_calls service_tier metadata store reasoning_effort` |

  Other unknown OpenAI fields are ignored too, because the contract refuses
  unknown fields only on the Anthropic wire.
- **Refused with `400 invalid_request_error`**, because dropping them silently
  would pass in development and fail in production:
  - on both wires:
    - `tools`, `tool_choice`, `functions`, `function_call` and `mcp_servers`;
    - tool blocks and tool messages;
    - `stream: true`;
    - an assistant-last prefill;
    - document, audio or file content;
  - on the OpenAI wire:
    - `n > 1`;
    - `logprobs`;
    - a `response_format` other than `json_schema`;
    - a system message after the first user message;
  - on the Anthropic wire, an unknown top-level field.
- **CLI failures:**

  | CLI result | Anthropic wire | OpenAI wire |
  |---|---|---|
  | not logged in | `401` | `401` |
  | `api_error_status` 404, 429 or 529 | that status | that status; 529 becomes `503 overloaded` |
  | anything else, or no JSON | `500 api_error` | `500 server_error` |
  | a run past the timeout | `504` | `504` |
  | no free slot within the queue timeout | `529 overloaded_error` | `503 {type: server_error, code: overloaded}` |

- **Never cached.** Two identical requests may differ, as they do on the real
  API.

### Research

`mock-research`'s behaviour, with contract 5's tidy-ups:

- **Request checks:** `query` is 1–500 characters. `market` is uppercased, and
  `UK` is refused (the UK is `GB`). `entity` and `category` must match their
  patterns. `max_sources` is 1–20 (default 8). Any other field is `400
  invalid_input`.
- **What comes back:** sources and verbatim excerpts only. URLs are
  normalised and deduped. There are 1–5 quotes per source, each at most 1,500
  characters. `retrieved_at` is set by the service. `published_at` is absent
  when unknown or in the future. `sources: []` is an honest 200.
- **Failures:** a CLI failure is `502 upstream_failed`, and a run past the
  timeout is `504 timeout`. Neither is cached.
- **Cache:** `<MOCK_DATA>/cache/research/<sha256>.json`, keyed by the
  normalised request, the model and a prompt version. It holds the query
  parameters (a public-web question, never material text) and public
  excerpts. `?fresh=1` or `MOCK_NO_CACHE=1` skips reading it.

### Retrieval

- **Packs** are directories under `BRIEF_CORPUS` when that is set, else under
  `MOCK_PACKS_DIR` (default `engine/packs_dist`). They are discovered as
  `engine/packs.py` does:
  - the directory name is the pack id;
  - a `_`-prefixed directory is switched off;
  - `pack.yaml` sets the tag, `kind`, `k`, `loops` and `licence`.

  A pack without `pack.yaml` takes its tag, `k` and `loops` from
  `engine/rag/packs.lock`, so the digests answer to the tags the real index
  uses (`template`, `cannes`, `dandad`, `ipa`, `playbook`).

  A pack with a `.digest_state.json` is `kind: digest`. A house pack is
  `licensed-internal` unless `pack.yaml` says otherwise.

  `MOCK_AGENCY_PACKS_DIR/<org slug>/<pack>/` adds agency packs, owned by
  `org/<org slug>` (S6). They are listed and searchable only under that
  `X-Napkin-Org`. For any other org they are `404 unknown_pack`, never 403.
- **Sections** come from every `.md` file, split at H1/H2 exactly as
  `rag.split_h2` splits it (a test compares the two). They are filtered by
  `where`, an exact match on frontmatter metadata. `filterable` is the set of
  frontmatter keys the pack's files carry, and the digests carry none. With
  no `packs` named, `where` must be filterable in every visible pack.
- **Selection.** When the candidates fit `MOCK_RETRIEVAL_MAX_CHARS` they all
  go to Claude. Otherwise a lexical prefilter (token overlap with the query)
  keeps the best that fit. One `claude -p` (`MOCK_RETRIEVAL_MODEL`) returns
  `{picks: [{section, quote}]}`.
- **Verification.** A quote must be a contiguous span of its section after
  whitespace is collapsed on both sides. The span is returned exactly as it
  stands in the file, markdown included. A quote that is not in the file is
  dropped, and so is a bad index, a second pick from the same section, and
  anything past `k`.

  `id`, `uri` and `text_sha256` follow the §3.2 formula. `score` is
  `1 - (rank-1)/k`. A span over 4,000 characters is cut at a word and marked
  `truncated`.
- **Honest limits.** `engine/packs_dist` holds five digests (three pattern-note
  sections each), not the award corpora, and there is no Effie pack.
  Selection is a model's judgement, not similarity. Determinism comes from
  the cache.
- **Cache:** `<MOCK_DATA>/cache/retrieval/`, keyed by the normalised request,
  the org, the model, a prompt version and every searched pack's version. It
  holds the response only (pack passages). It never holds the query.

### Layers

`server/napkin/layers/local.py` (LocalLayers) moved behind the §4 routes on
SQLite, with no model. The append rule is exactly today's:

| Outcome | When |
|---|---|
| `created` | nothing current for the identity |
| `corroborated` | same value; the new sources are linked |
| `superseded` | another value with a later `as_of` |
| `contested` | another value with an earlier or equal `as_of`, or the request said `contested` |

Versions count per entity + key across markets.

The fixes the contract makes:

- `licence` is **required** on facts and sources, with no default.
- A category-layer fact on an unknown `category/<leaf>` is `400 unknown_leaf`,
  and so is a roster category.
- A `client-confidential` source is visible only to its org. That covers
  `sources(ids)`, a fact row's source links, and the ids an append may cite.
- The append (fact + decision + links + status flips) is one transaction, and
  so is the roster write (every roster fact + the decision + the brand name).
  This settles O3, O4 and P2/M5 as the owner decided.
- `Idempotency-Key` is required on `POST /facts`, `POST /sources` and
  `PUT /roster/{ref}`. A replay within 24 h returns the first response with
  `Idempotent-Replay: true`. The same key with another body is `409`. Keys
  are per org.
- Decisions are stored whole, reasoning included. `GET
  /v1/layers/decisions/{id}` returns one, visible only in the scope it was
  written in.

The taxonomy seed is `docs/contracts/peripherals/taxonomy.json` (18 verticals,
108 leaves plus one provisional leaf). Both the mock and a real layers service
seed from it. `MOCK_LAYERS_DB` sets the file (default
`<MOCK_DATA>/layers.sqlite`); delete it to reset the layers.

**In-process** (for the middleware's unit tests, §8.5):

```python
sys.path.insert(0, "mock-backend")
from layers_port import LayersApp
from common import Request
app = LayersApp(":memory:")          # or a file path
resp = app.handle(Request.build("GET", "/v1/layers/categories", {"X-Napkin-Org": "org/a"}))
resp.status, resp.body, resp.headers
```

This answers exactly as the HTTP routes do, so an `HttpLayers` client can run
over a transport that calls `app.handle` instead of a socket.

## Shared rules

- **Scope** comes only from `X-Napkin-Org: org/<slug>` and `X-Napkin-Brand:
  brand/<slug>`. A scoped route without the header it needs is `400
  missing_scope`. A body naming `scope`, `org`, `org_id`, `tenant`,
  `tenant_id` or `brand_scope` at its top level is `400 invalid_input`.
- **Auth:** with `MOCK_TOKEN` set, every route requires it. Research,
  retrieval, layers and `/healthz` take `Authorization: Bearer`. The model
  routes take their wire's own header (`x-api-key` or Bearer for Anthropic,
  Bearer for OpenAI) and answer `401` in the wire's shape.
- **Errors:** research, retrieval and layers answer `{"error": {"type",
  "message"}}`. The model routes use the wire's own shape. No message
  carries request content. A 200 never carries `error`.
- **Bodies are never written or logged.** The log is one metadata line per
  request: id, method, path, status, duration, `X-Napkin-Handler` and
  `X-Napkin-Job`. Set `MOCK_DUMP_REQUEST_BODIES_TO=<dir>` only to debug one
  request, then delete the directory.
- **Size:** a body over `MOCK_MAX_BODY_BYTES` is discarded unread and answered
  `413`.

## Environment

| Var | Default | |
|---|---|---|
| `MOCK_BACKEND_HOST` / `MOCK_BACKEND_PORT` | `127.0.0.1` / `8797` | refuses 8080, 8090, 8787, 8788, 8790, 8791, 8792, 8795, 8796 |
| `MOCK_FAKES` | `model,research,retrieval,layers` | families served |
| `MOCK_CONCURRENCY` | `4` | Claude subprocesses at once, all families |
| `MOCK_QUEUE_TIMEOUT` | the family's timeout | wait for a slot -> 529/503 |
| `MOCK_TIMEOUT_MODEL` / `_RESEARCH` / `_RETRIEVAL` | `180` / `420` / `180` | seconds -> 504, process group killed |
| `MOCK_MAX_BODY_BYTES` | `33554432` | -> 413 |
| `MOCK_MAX_TURNS` | `4` | CLI turns for model and retrieval calls (structured output takes 2) |
| `MOCK_CLAUDE_BIN` | `claude` | the tests point it at `tests/fake_claude.py` |
| `MOCK_MODEL_ALIASES` | the three NIM ids above | JSON `{"<id>": "<alias>"}` |
| `MOCK_RESEARCH_MODEL` / `MOCK_RETRIEVAL_MODEL` | `sonnet` / `sonnet` | CLI aliases |
| `MOCK_RETRIEVAL_MAX_CHARS` | `150000` | above this, the lexical prefilter |
| `MOCK_PACKS_DIR` | `engine/packs_dist` | house packs |
| `BRIEF_CORPUS` | unset | the full corpus; replaces `MOCK_PACKS_DIR` |
| `MOCK_AGENCY_PACKS_DIR` | unset | `<org slug>/<pack>/` agency packs |
| `MOCK_DATA` | the session scratchpad `…/mock-backend` if it exists, else `$TMPDIR/napkin-mock-backend` | `cache/`, `work/`, `layers.sqlite` |
| `MOCK_LAYERS_DB` | `<MOCK_DATA>/layers.sqlite` | |
| `MOCK_NO_CACHE` | unset | `1` = never read the research/retrieval cache |
| `MOCK_MAX_BUDGET_USD` | unset | `--max-budget-usd` per call |
| `MOCK_TOKEN` | unset | when set, every route requires it |
| `MOCK_DUMP_REQUEST_BODIES_TO` | unset | **off**; bodies are client-confidential |

## Tests

```sh
# Unit and server tests against a fake claude: no network, no Claude spend (~15 s).
python3 -m unittest discover -s mock-backend/tests -v

# The contract suites (§8), stdlib only, run unchanged against the mock or a real service.
python3 mock-backend/contract/layers_contract.py    --base-url http://127.0.0.1:8797
python3 mock-backend/contract/retrieval_contract.py --base-url http://127.0.0.1:8797 --org org/dev-agency
python3 mock-backend/contract/research_contract.py  --base-url http://127.0.0.1:8797 [--no-live]
python3 mock-backend/contract/model_contract.py     --base-url http://127.0.0.1:8797    --api anthropic --expect-refusals
python3 mock-backend/contract/model_contract.py     --base-url http://127.0.0.1:8797/v1 --api openai    --expect-refusals

# All of them, a URL per port (each defaults to --base-url):
python3 mock-backend/contract/run_all.py --base-url http://127.0.0.1:8797 --no-live --expect-refusals
python3 mock-backend/contract/run_all.py --layers-url https://layers.internal --layers-token T --only layers

# The official SDKs with only the environment changed (live, tiny):
ANTHROPIC_BASE_URL=http://127.0.0.1:8797 ANTHROPIC_API_KEY=dummy \
  uv run --no-project --with anthropic python mock-backend/tests/sdk_test.py anthropic
OPENAI_BASE_URL=http://127.0.0.1:8797/v1 OPENAI_API_KEY=dummy \
  uv run --no-project --with openai python mock-backend/tests/sdk_test.py openai

# The middleware must not know the mock exists (§0.1):
sh mock-backend/tests/removability.sh
```

`--no-live` skips every check that makes a model or search call. The layers
suite never makes one.

### Live sample (2026-09-24, Claude Code 2.1.281)

| Suite | Result | Time | Model |
|---|---|---|---|
| model, anthropic wire | 14/14 | text 2.8 s, strict schema 4.8 s, retry turn 7.8 s, image 4.2 s | `claude-haiku-4-5` → haiku |
| model, openai wire | 14/14 | text 2.8 s, strict schema 5.1 s, retry turn 7.7 s, image 3.9 s | `nvidia/llama-3.1-nemotron-nano-vl-8b-v1` → haiku |
| retrieval | 34/34 | two retrieves over the five digests (15 sections), 17.3 s and 8.2 s, $0.048 and $0.031 | sonnet |
| layers | 77/77 | under a second | none |
| research | 32/32 | BMW EV market in IE, `max_sources` 3: 97.7 s and $0.42 for three sources (BMW Group Ireland, SIMI, CSO) with 3–4 quotes each; the repeat was a cache hit in 0.00 s | sonnet + WebSearch/WebFetch |

The two SDK checks passed as well: anthropic 1.4.0 and openai 3.19.2.

## Divergences from the real services

- **Model:**
  - `stop_reason` is always `end_turn`, and `max_tokens` is not enforced.
  - There is no prompt caching.
  - Token counts include Claude Code's own framing and structured-output
    tool, so they are not a cost number.
  - Structured output is the CLI's `--json-schema` validation, not
    constrained decoding.
  - Multi-turn history is a flattened transcript.
  - Latency is not production latency. Never tune `NAPKIN_MODEL_TIMEOUT` to
    the mock.
- **Research:**
  - Quotes are not re-verified against the page (the middleware checks
    figures).
  - Results are not deterministic across cache misses.
  - The cache never expires.
- **Retrieval:**
  - There are no embeddings; `embed_model` is `null`.
  - Selection is a model's judgement.
  - Against `packs_dist` the passages are digest notes, not award cases.
- **Layers:** SQLite with one connection and one lock, not Postgres with RLS.
  The observable behaviour is the contract's.
