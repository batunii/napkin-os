# ADR 0005 — Claude transport: API key or Claude Code login, switchable per run

Status: accepted (Sai, 2026-09-24) · Code: `engine/parse_brief.py` (`_claude_transport`,
`_chat_claude_cli`, `_call_link`, `transport_used`), `serve.py`, `engine/rag/e2e_eval.py` ·
Tests: `engine/rag/test_claude_cli_transport.py`

## Context

On 2026-09-24 the Anthropic API key ran out of credit in the middle of a comparison run:
one brief failed halfway, its health checker judged 0 of 14 checks, and every blind-judge
call failed. The pipeline could not be tested at all until the key was topped up, although
the machine has a logged-in Claude Code account. `serve.py` already used that account for
the lightweight mock agent (`mock-agent/server.py`, one `claude -p` call per request), but
the real engine pipeline could only reach Claude through the API key.

Testing needs both: API runs for production-shaped cost and latency, and runs on the Claude
Code login when the key is unavailable or when a test should not spend API credit.

## Decision

One setting, `BRIEF_CLAUDE_TRANSPORT`, decides how every `anthropic:` link in the model chain
is sent. Chains, model pins (`model=`), labels (`anthropic:<model>`) and pricing are the same
on every transport, so runs stay comparable; only the transport changes.

| Value | Behaviour |
|---|---|
| `api` (default) | The API key, exactly as before. A typo also reads as `api`, so nothing moves by accident. |
| `cli` | `claude -p` on the logged-in Claude Code account. |
| `auto` | The API; the process switches to the CLI on a credit or auth failure ("credit balance is too low", `AuthenticationError`, `PermissionDeniedError`), or at once when no key is set but `claude` is installed. Announced once on stderr. Transient errors (429, 5xx) do **not** switch: the chain handles them as before. |

How to choose it:

```
python3 serve.py --claude-code | --api | --auto | --mock-agent      # the app backend
python3 engine/rag/e2e_eval.py --trace <stem> --transport cli       # measurement runs
BRIEF_CLAUDE_TRANSPORT=cli python3 engine/parse_brief.py <brief>     # anything else
```

**Parity with the API path.** The CLI is called with no tools, one turn, no session file,
no user or project settings (`--setting-sources ""`), no MCP servers
(`--strict-mcp-config`) and our own system prompt (`--system-prompt`), so the model sees
only what the API call would send. The prompt goes on stdin. Models that do not think by
default on the API (Opus 4.6) run with `MAX_THINKING_TOKENS=0`; measured: without it the CLI
spent 59 thinking tokens on a one-line puzzle the API answers without thinking. Models that
do (Opus 5.5, Sonnet 5) get the API's default effort (`medium` for Opus 5.5, `high`
otherwise; `BRIEF_CLI_EFFORT` overrides). The output cap is the caller's `max_tokens` plus the
same thinking headroom as the API path (`CLAUDE_CODE_MAX_OUTPUT_TOKENS`). A `schema` call
passes `--json-schema` and returns the CLI's validated `structured_output`.

**The key is removed from the CLI's environment.** With `ANTHROPIC_API_KEY` set, the CLI
bills that key, which is exactly the key this transport exists to avoid.

**Failures map onto the chain.** A non-zero exit, an `is_error` envelope or a timeout
(`BRIEF_CLI_TIMEOUT`, default 240 s) raises, so the chain fails over; the account's usage
limit raises `_RateLimited`, so the link cools down.

**Every run records its transport**: `brief.meta.claude_transport`, and `claude_transport`
in each `e2e_eval` trace and summary row (`api`, `cli`, or `api→cli` after an `auto` switch).

## Measured (2026-09-24)

| | Opus 4.6 | Opus 5.5 | Sonnet 5 |
|---|---|---|---|
| One small JSON call over the CLI, wall time | 3.9 s | 4.6 s | 3.0 s |
| Same, with `--json-schema` | 5.7 s | 4.7 s | 3.4 s |
| CLI overhead per call (process start, envelope) | ~1.5 s | | |
| Input tokens for a 1-line prompt (CLI system-prompt overhead) | 346 | | |

## Consequences

- The pipeline can be tested end to end with no API credit. Cost figures from a CLI run are
  computed from the recorded tokens at API list prices (as for an API run); they describe
  what the run would cost on the API, not what the Claude Code account was charged.
- A CLI run is slower per call (~1.5 s process start), so wall-clock figures from CLI runs
  are not production latency. Compare latency only between runs on the same transport.
- The default stays `api`: a production server never silently moves traffic onto a
  personal login. `auto` is for local testing.
- The mock agent is unchanged and still reachable with `serve.py --mock-agent`.
