# mock-research

A stand-in for the research / source-discovery service behind the Napkin
middleware's research port. Each request is answered by headless Claude Code
with web search and fetch, so the sources are real, fetched URLs and the
excerpts are quoted from them. A real search service replaces it by changing
one URL.

The endpoint, the request and the response are fixed by the components brief
(§3, research port). The lenses are the eight of the Planner Research
Taxonomy (`app/templates/campaign-research/app/pipeline.yaml`,
`docs/contracts/campaign-clan.md` §7).

## Run

```sh
python3 mock-research/server.py            # http://127.0.0.1:8792/v1/research
curl -s 127.0.0.1:8792/v1/research -H 'content-type: application/json' -d '{
  "query": "BMW electric and hybrid cars market in Ireland",
  "lens": "market_structure", "market": "IE",
  "entity": "brand/bmw", "category": "automotive.ev_hybrid"}'
```

Python 3.11+, standard library only. It needs `claude` on `PATH`, already
signed in. A first call takes a few minutes (search, then a fetch for every
source). A repeat comes from the cache and takes milliseconds.

## The endpoint

`POST /v1/research`

```json
{ "query": "…", "lens": "market_structure", "market": "IE",
  "entity": "brand/bmw", "category": "automotive.ev_hybrid", "max_sources": 8 }
```

`query`, `lens` and `market` are required. `market` is ISO 3166-1 alpha-2 and
is uppercased. `max_sources` is 1 to 20 (default 8). An unknown field is a
400. `?fresh=1` skips the cache for this request.

```json
{ "sources": [ { "id": "src_…", "url": "https://…", "publisher": "…", "title": "…",
                 "retrieved_at": "2026-09-24", "published_at": "2026-08-01",
                 "excerpts": [ { "quote": "…verbatim…" } ] } ],
  "trace": { "backend": "claude-code-websearch", "queries": ["…"], "model": "sonnet" } }
```

| Status | When |
|---|---|
| 200 | Research ran. `sources: []` is an honest "nothing citable found". |
| 400 | Invalid input. |
| 404 / 405 | Unknown path or method. |
| 502 | The CLI failed: a non-zero exit, `is_error`, no JSON, or no structured output. |
| 504 | The call ran past `MOCK_RESEARCH_TIMEOUT`. The process group is killed. |

Error bodies look like `{"error": {"type", "message"}}`. A 200 never carries
an `error` key.

`GET /healthz` returns the backend, the model, the concurrency and the timeout.

## How it works

1. The request is validated and normalised (whitespace collapsed, market
   uppercased).
2. The cache key is a sha256 over the normalised request plus the model and a
   prompt version. The query is lowercased for the key only. On a hit, the
   stored response comes back unchanged, including its original
   `retrieved_at`.
3. If there is no hit, the service waits for a semaphore slot and runs one
   fresh session in an empty working directory:
   `claude -p --output-format json --model <alias> --no-session-persistence
   --strict-mcp-config --permission-mode dontAsk --json-schema <sources schema>
   --tools WebSearch WebFetch --allowedTools WebSearch WebFetch`. The prompt
   goes in on stdin. It asks the model to:
   - search for this lens in this market, preferring primary sources
     (regulators, official statistics, company filings), then industry bodies
     and trade press;
   - fetch every page before citing it;
   - copy 1 to 3 passages from each page exactly as written;
   - list the queries it ran.

   `ANTHROPIC_BASE_URL` is removed from the child's environment, so the CLI
   never calls a Messages stand-in by mistake.
4. The CLI envelope's `structured_output` is then validated. The service keeps
   only:
   - http(s) URLs with a host (fragment removed, host lowercased, trailing
     slash stripped);
   - sources with a non-empty title. An empty publisher is replaced by the URL's
     host;
   - quotes that are non-empty after trimming, 1,500 characters or less, at most
     5 per source. A source with no quotes left is dropped;
   - one source per URL. Duplicates are merged and their quotes combined;
   - at most `max_sources` sources;
   - a `published_at` only if it is a real `YYYY-MM-DD` date and not in the
     future.

   `retrieved_at` is set by the service to the current UTC date, never by the
   model. `id` is `src_` plus a hash of the URL, so the same page always has
   the same id.
5. The response is written to the cache and returned. Failures (502/504) are
   never cached.

## Environment

| Var | Default | |
|---|---|---|
| `MOCK_RESEARCH_PORT` | `8792` | listen port |
| `MOCK_RESEARCH_HOST` | `127.0.0.1` | bind address |
| `MOCK_RESEARCH_MODEL` | `sonnet` | Claude Code model alias |
| `MOCK_RESEARCH_TIMEOUT` | `420` | seconds allowed for one research call |
| `MOCK_RESEARCH_CONCURRENCY` | `4` | research subprocesses running at once |
| `MOCK_RESEARCH_DATA` | the session scratchpad `…/scratchpad/mock-research` | holds `cache/` and `work/` |
| `MOCK_RESEARCH_NO_CACHE` | unset | `1` never reads the cache (results are still written) |
| `MOCK_RESEARCH_MAX_BUDGET_USD` | unset | per-call `--max-budget-usd` |
| `MOCK_RESEARCH_CLAUDE_BIN` | `claude` | the CLI to run (the tests point it at a fake) |

**What is on disk:** `cache/<sha256>.json` holds the normalised query
parameters (query, lens, market, entity, category, max_sources), the validated
response (public URLs, titles and excerpts from public pages) and drop counts.
Nothing else from a request is written. Query parameters are not
confidential: the middleware sends research questions, not client material.
Delete the directory to clear the cache.

## Swap by URL

The middleware reads `NAPKIN_RESEARCH_URL`. Today it points here
(`http://127.0.0.1:8792/v1/research`). To use a real search/discovery service,
point it at that service's endpoint. The service must speak the same
`POST /v1/research` contract, and nothing in the middleware changes. The swap
guarantee is the contract suite:

```sh
python3 mock-research/contract_test.py --base-url http://127.0.0.1:8792
python3 mock-research/contract_test.py --base-url https://research.example --no-live
```

The suite names no implementation. It checks:
- health;
- a 400 for each kind of invalid input and a 404 for an unknown path;
- one live query, which must satisfy the response shape;
- that repeating the query returns identical sources quickly (a cache hit);
- that `max_sources` caps the list.

Flags: `--save FILE` writes the live response to a file. `--request FILE`
sends a different live body. `--fresh`, `--no-cache-check`, `--no-cap-check`
and `--no-live` do what their names say.

The offline tests run the server against `tests/fake_claude.py`. They cover
validation, dedupe, the cap, normalisation of cache keys, `?fresh=1`, the
honest empty answer, 502 in each of its forms, and 504. They need no network:

```sh
python3 mock-research/tests/test_server.py
```

## What it deliberately doesn't do

- **No facts, confidence or tiers.** It returns sources and quotes only.
  Extracting facts, tiering sources by domain policy and deriving confidence
  from tier and corroboration are the middleware's job.
- **It does not re-verify quotes.** The model is told to copy text verbatim
  from pages it fetched, and the service checks the shape, not the substring.
  Pages rendered by JavaScript and PDFs make a naive re-fetch unreliable. The
  middleware treats a quote as a claim about a URL.
- **No paywall or login access.** No search index or ranking of its own. What
  it finds is whatever Claude Code's web search finds today.
- **Not deterministic across cache misses.** Two fresh runs of the same
  question can return different sources. Determinism comes from the cache.
- **No cache expiry.** A cached answer, including an honest empty one, stays
  until you delete it or pass `?fresh=1`.
- **No auth and no tenant scope.** It binds to localhost. Research questions
  are public-web queries, so scope is not its concern.
- **No streaming or job API.** One request holds its connection until it is
  answered or times out.
