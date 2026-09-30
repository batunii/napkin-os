# Brief Maker agent server

The real backend behind the Brief Maker app's **Generate** flow — replaces
`mock-agent/` (kept for one release behind `serve.py --mock-agent`; `serve.py` with no
flag starts this server, on the Claude Code login when there is no API key). Implements the contract
declared in the template's `app/pipeline.yaml`.

## Run

```bash
cd engine
pip install -e ".[yaml,dotenv]"        # once, any venv/conda env (py ≥3.11)
cp .env.example .env                   # add keys (see table)
python3 agent-server/server.py         # listens on :8787
```

Point the app at it via `NAPKIN_AGENT_URL` or `workspace.yaml`
(see `docs/workspace.example.yaml`). **Port 8787 is the same slot mock-agent
uses — run one of them at a time.**

## Env

| var | effect |
|---|---|
| `NAPKIN_AGENT_PORT` | listen port (default 8787) |
| `BRIEF_LOOPS37=1` | RAG-grounded Loops 3–7 (insight/SMP/RTBs); forces golden fill on |
| `BRIEF_GOLDEN=1` | golden-brief fill without loops37 |
| `BRIEF_RESEARCH=0` | disable the research dossier (default on when keys exist; it runs only with Loops 3–7 off or `RESEARCH_WEB` on) |
| `RESEARCH_WEB=claude` | live web track via headless `claude` CLI (dev-mode; default `off`) |
| `RAG_STORE=qdrant` + `QDRANT_*` | remote corpus store. `NVIDIA_API_KEY` is **required** for query embedding in this mode — without it queries silently embed at the wrong dimension and Qdrant errors |

Zero keys → heuristic capture only: strategy boxes stay empty and the patch
rationale says so. That's honest, not broken.

## Contract (what the host sends / expects)

- `POST /` `{request_kind, payload, clan}`; reply is 2xx **bare JSON of brief
  fields** (+ `rationale`, `context` meta keys the app strips).
- `draft_brief` may carry `payload.upstream`, the research steps' output:
  `{brand, category, competitors, facts, decisions}`, every key optional.
  `facts` are verified fact rows (ADR 0014), `decisions` what people decided on
  the research (ADR 0015); both reach the strategy writers only, never the
  Loop-1 capture. It is passed to `parse_brief.run(upstream=...)`; a key of the
  wrong type is dropped and logged, and with no `upstream` the draft runs
  exactly as before.
- Empty fields are **omitted**, never `""` — a second Generate must not blank
  planner-edited boxes.
- `regenerate_field` replies use the **literal dotted key**
  (`objectives.commercial`) with string-or-array type per `mapping.FIELD_TYPES`.
- Errors are non-2xx; the host strips error bodies, so diagnostics only exist
  in this server's log. A 200 body never contains an `error` key.
- `GET /stats` — session totals (drafts, regens, tokens, wall time).

Research never enters Loop-1 capture (the engine's one hard rule — the no-loss
ledger measures fidelity to the client's own brief). The dossier surfaces in
the app's context panel; strategy grounding comes from Loops 3–7's own
retrieval over the same corpora. With Loops 3–7 on and the web track off (the
app default) the dossier is skipped: the panel already lists the precedent the
writers read, and the dossier's second retrieval plus Opus gist call (~20 s,
~$0.03 a draft) only added a line of citation ids.

## Tests

```bash
python3 -m unittest test_mapping -v
```

Fixtures are synthetic-derived only — never add real client-run outputs.
