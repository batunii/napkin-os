# Campaign Research template

The Research Tool app: the campaign document of Contract 3
(`docs/contracts/campaign-clan.md`). Build-plan task W1-C3, split across three
agents. Each file has one owner; change someone else's file by asking them.

| File | Owner |
|---|---|
| `schema.json` (becomes `agent/output-schema.json` when packaged) | schema/contract (W1-C3, part 1) |
| `facts.schema.json`, `findings.schema.json` | schema/contract |
| `context.md` (becomes `agent/context.md`) | schema/contract |
| `app/pipeline.yaml` | schema/contract |
| `example/` — the filled EXAMPLE document (fabricated, for testing) | schema/contract |
| `docs/contracts/campaign-clan.md`, `tools/check_example.py` | schema/contract |
| `index.html` — the human view | view agent |
| `crates/clan-sdk/examples/make_campaign_research.rs`, `agent/requirements.yaml`, the packaged example `.clan`, registering `shared/facts.yaml` and `shared/findings.yaml` as members, research-agent wiring | packaging agent |

## The example

`example/` is laid out as the members of a document, so packaging can copy it
in place:

```
example/shared/data.yaml            campaign.*, selection.*, materials, projection
example/shared/facts.yaml           18 pins, brand + category layers, 2 stale
example/shared/findings.yaml        5 findings: 1 verified, 3 proposed, 1 rejected
example/agent/decision-chain.yaml   17 decisions, newest first
example/assets/client-email.txt     the ask source (mat_email01), hashed in data.yaml
```

The example's document id is `7c1e9a42-5b3d-4f8e-9a6c-2d1f0e8b4a17`; every
address in the chain uses it. Package it with that manifest id, or rewrite the
addresses. `projection.built_from` holds the sha256 of `facts.yaml` and
`findings.yaml` byte for byte, so those two members must be packed verbatim —
if they are edited, regenerate the projection.

Everything in `example/` is **EXAMPLE — fabricated for testing**.

Check it (schemas, cross-member references, no defaults, cold start):

```
uv run --with jsonschema --with pyyaml python app/templates/campaign-research/tools/check_example.py
```

Add `--write-projection` after editing `facts.yaml` or `findings.yaml`.
