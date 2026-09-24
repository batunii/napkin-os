# mock-llm

A stand-in for the Anthropic Messages API, answered by headless Claude Code.
The middleware calls "the model" through the official `anthropic` SDK exactly
as it will in production; pointing it here instead of at the real API is one
environment variable.

```sh
cd mock-llm
python3 -m mock_llm                     # or: uv run python -m mock_llm
# -> listening on http://127.0.0.1:8791

ANTHROPIC_BASE_URL=http://127.0.0.1:8791 ANTHROPIC_API_KEY=dummy  <run the middleware>
```

To use the real API: unset `ANTHROPIC_BASE_URL`, set a real `ANTHROPIC_API_KEY`.
Nothing else changes.

It is **transport only** (build-plan decision M1): no prompts, retrieval,
schemas or cost logic live here. Python 3.11+, stdlib only. Requires Claude
Code on `PATH` and logged in; the server refuses to start without the binary.

## What it does

`POST /v1/messages` takes the Messages request body verbatim and runs one
fresh `claude -p` subprocess per request (never `--resume`, no session
persistence) in an empty temporary directory:

```
claude -p --model <alias> --output-format json --tools "" --max-turns 4 \
  --no-session-persistence --setting-sources "" --strict-mcp-config \
  --disable-slash-commands --system-prompt-file /dev/fd/N \
  [--json-schema '<schema>'] [--effort <level>]
```

- The prompt goes on **stdin** and `system` through a **pipe fd**, so neither
  is visible in `ps` nor written to disk. `--system-prompt-file` replaces
  Claude Code's agentic system prompt; with no `system`, a one-line neutral one
  is used.
- One user message is passed verbatim. A multi-turn history is rendered as a
  `<user>…</user>` / `<assistant>…</assistant>` transcript, since the CLI takes
  one prompt.
- `ANTHROPIC_BASE_URL` is removed from the child's environment so the CLI never
  routes back into the mock.

`GET /health` reports the concurrency cap, in-flight count and peak.

### Model map

| API id | CLI alias |
|---|---|
| `claude-opus-5` | `opus` |
| `claude-sonnet-5` | `sonnet` |
| `claude-haiku-4-5` (and `claude-haiku-4-5-20251001`) | `haiku` |
| `claude-fable-5-1` | `fable` |

Any other id is `404 not_found_error`, never a default.

### CLI envelope -> Messages response

| Response field | From |
|---|---|
| `id` | fresh `msg_…` |
| `model` | the request's `model`, echoed |
| `content[0].text` | `json.dumps(envelope.structured_output)` when `output_config.format` is `json_schema`; otherwise `envelope.result` |
| `stop_reason` | always `end_turn` |
| `stop_sequence` | always `null` |
| `usage.input_tokens` | `usage.input_tokens + usage.cache_creation_input_tokens + usage.cache_read_input_tokens` from the envelope (every prompt token the CLI says the model read), or 0 |
| `usage.output_tokens` | `usage.output_tokens`, or 0 |
| `usage.cache_creation_input_tokens`, `usage.cache_read_input_tokens` | always 0: the mock does not cache and does not claim to |

Usage is never estimated. A plausible-looking number would be believed by
the cost accounting downstream; a zero is not.

A failing envelope (`is_error: true`) maps to: "Not logged in" / 401 ->
`401 authentication_error` carrying the CLI's own message; `api_error_status`
404/429/529 -> that status and type; anything else `500 api_error`. A JSON
schema request whose envelope has no `structured_output` is `500 api_error`.

### Request handling

| Input | Result |
|---|---|
| `system` as a string or text blocks | joined; `cache_control` dropped |
| `temperature`, `top_p`, `top_k`, `stop_sequences`, `metadata`, `thinking`, `service_tier` | accepted and ignored (the CLI has no flag for them) |
| `output_config.effort` | passed as `--effort` |
| `output_config.format` `{type: json_schema, schema}` | `--json-schema`; other format types 400 |
| `tools`, `tool_choice`, `mcp_servers`, `container`, `context_management`, `tool_use`/`tool_result` blocks | 400: `claude -p` cannot emulate tool use, and dropping it silently would pass in development and fail in production |
| `stream: true` | 400: no fake single-chunk SSE |
| image / document / other non-text blocks | 400 |
| last message from `assistant` (prefill) | 400 |
| missing `max_tokens`, empty `messages`, unknown top-level field | 400 |
| body not UTF-8 or not JSON | 400 |
| body over the cap | 413 `request_too_large` (discarded unread-into-memory, up to 256 MiB, then the connection closes) |
| subprocess over the timeout | 504 `api_error`; the process group is killed |
| no free slot within the queue timeout | 529 `overloaded_error` |

All errors are Anthropic-shaped: `{"type": "error", "error": {"type", "message"}}`.
Two identical requests may return different text; there is no response cache.

## Environment

| Var | Default | |
|---|---|---|
| `MOCK_LLM_HOST` | `127.0.0.1` | |
| `MOCK_LLM_PORT` | `8791` | |
| `MOCK_LLM_CONCURRENCY` | `4` | cap on simultaneous `claude` subprocesses |
| `MOCK_LLM_TIMEOUT` | `180` | seconds per subprocess -> 504 |
| `MOCK_LLM_QUEUE_TIMEOUT` | = timeout | seconds waiting for a slot -> 529 |
| `MOCK_LLM_MAX_BODY_BYTES` | `33554432` | the real API's 32 MB cap -> 413 |
| `MOCK_LLM_MAX_TURNS` | `4` | CLI turns (structured output takes 2) |
| `MOCK_LLM_CLAUDE_BIN` | `claude` | path to the binary (the offline tests point it at a fake) |
| `MOCK_LLM_DUMP_REQUEST_BODIES_TO` | unset | a directory. **Off by default.** Request bodies carry client-confidential material; only set this to debug a specific request, and delete the directory afterwards |

The server logs one metadata line per request (id, model, status, duration),
never prompt or reply text.

## Divergences from the real API

- **`stop_reason` is always `end_turn`.** The CLI does not report truncation,
  and `max_tokens` is not enforced. Cover a `max_tokens` branch with a unit
  test on a synthetic response.
- **No prompt caching.** `cache_*` usage is always 0; the cacheable-prefix
  design is unverifiable here. Check cache economics against the real API.
- **Token counts are the CLI's**, and include Claude Code's own framing (it
  still prepends a short agent preamble and environment context - cwd,
  platform, date, the logged-in account's email - to every call) and, for
  structured output, the CLI's internal tool definition. Not a cost number.
- **No tool use, no streaming, text blocks only.** 400s, by design.
  Structured output is emulated by the CLI's `--json-schema` validation, not
  the API's constrained decoding: verify it against the real API before a
  structured-output task is called done.
- **Multi-turn is a flattened transcript,** not real turns.
- **Latency.** Measured through the mock with a one-line prompt (2026-09-24):

  | model | text | json_schema |
  |---|---|---|
  | claude-haiku-4-5 | 2.3 s | 3.5 s |
  | claude-sonnet-5 | 2.7 s | 3.4 s |
  | claude-opus-5 | 3.4 s | 3.8 s |
  | claude-fable-5-1 | 4.6 s | 5.5 s |

  Real prompts take far longer (the plan budgets 30-120 s). Do not tune the
  middleware's timeouts to the mock. Note the SDK retries 5xx/529 twice by
  default, so a 504 can cost three timeouts.

## Tests

```sh
cd mock-llm

# Offline: translation + the server against a fake claude (concurrency cap
# with 20 simultaneous requests, 504, 401, 413, no body on disk, system never on argv).
python3 -m unittest tests.test_offline -v

# Shape contract against ANY base URL (makes real, tiny haiku calls):
python3 tests/contract_test.py --base-url http://127.0.0.1:8791
python3 tests/contract_test.py --base-url https://api.anthropic.com --api-key "$ANTHROPIC_API_KEY"

# The official SDK with only the env changed:
ANTHROPIC_BASE_URL=http://127.0.0.1:8791 ANTHROPIC_API_KEY=dummy \
  uv run --no-project --with anthropic python tests/sdk_test.py
```

`tools` and `stream` refusals run only against the mock (the real API accepts
them); `--real` skips them, and is implied for `api.anthropic.com`.

### Removability

The middleware must not know the mock exists. From the repo root:

```sh
grep -rniE 'mock|8788|8791|claude -p|localhost' server/    # must print nothing
sh mock-llm/tests/removability.sh                          # the same, exit 1 on a hit
```

`server/napkin/model.py` constructs `anthropic.Anthropic()` from
`ANTHROPIC_BASE_URL` / `ANTHROPIC_API_KEY` with no mock branch, flag or `if`.
Removing the mock is unsetting one variable; there is no code to delete.
