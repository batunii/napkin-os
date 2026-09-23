# RAG metadata contract

`rag_metadata.v1.json` is the single source of truth for what every chunk in the
vector store carries under `metadata`. Three tracks read it:

| Track | Reads it for |
| --- | --- |
| A — CLAN OS layer (Rust) | the closed enums a reviewer can pick: `verdict`, `reason_code`, `reviewer_role`, `status` |
| B — dossier export (Rust) | the exact keys and values a dossier chunk must be emitted with |
| C — RAG (Python) | which fields get a payload index, defaults, validation, chunker stamping |

It is JSON, not Python or Rust, because two languages have to agree on the same
bytes. Neither side copies a value out of it into code.

## Rules

- **Enums are closed.** A value not in the list is a validation error, never a new
  filter value. All names and values are lower snake_case, enforced by test.
- **Versioning is semver.** Adding an optional field = minor bump + backfill of old
  chunks (a filter on a missing field silently drops the point). Renaming or removing
  a field = major bump and a new file, `rag_metadata.v2.json`. Every chunk carries the
  `schema_version` it was written under.
- **`locked: false`** until Phase 0 signs off `category` and `reason_code` with a
  creative director. Do not ingest dossiers before it flips to `true`.
- **`excluded`** lists fields that must never appear. Firmographics are out of v1 on
  purpose: bucketed revenue, headcount and audience age re-identify a client and are
  usually the confidential information under the NDA.

## Field origins

- `system` — written by the pipeline (`schema_version`).
- `review` — comes from a human verdict via Tracks A/B. Corpus material gets the
  defaults: `status=active`, `verdict=none`, `scope=global`.
- `corpus` — already carried by the awards corpus (`source`, `level`, `parent_id`…).
  Kept so the existing index stays valid under the contract.

Note `category` vs `doc_kind`: the awards corpus's frontmatter key `category`
(`ipa_effectiveness_case`, `effie_cautionary`) is renamed to `doc_kind` at chunk time.
The contract's `category` is the closed client-category list only.

## v1.1.0 additions (2026-09-16)

`bucket` (exemplars | craft | rules | instructions — which prompt slot a chunk fills),
`section_role` (what kind of section, from the heading), `effectiveness_type` and
`strategic_territory` (IPA), `discipline` (playbooks), `lions_category` (Cannes), and
`award_tier` is now a closed enum with the body's own spelling kept in `award_tier_raw`.
All are derived from existing frontmatter by `engine/rag/normalise.py` — the only place
the spelling tables live. `category` for corpus chunks comes from the IPA sector via
`normalise.SECTOR_TO_CATEGORY`; Cannes ("general") stays unknown until the enrichment pass.

## Python side

```python
from contract import SCHEMA, indexed_fields, apply_defaults, validate, require_valid
```

`engine/rag/contract.py` loads the JSON once and exposes it. `validate()` returns a
list of problems rather than raising, so the backfill audit can count failures.
`require_valid()` raises, for write paths that must refuse bad data.

```
python3 engine/rag/contract.py          # print the contract as a table
python3 -m pytest engine/rag/test_contract.py -q
```

## `rag_io.v1.json` — the RAG module's call contract (2026-09-23)

A second contract, separate from the chunk metadata above: what the middleware sends the
RAG module and what it gets back. JSON Schema (draft 2020-12), so any language validates
the same bytes. v1.2.0, `locked: false` until the middleware owner signs off.

- `authority` (tenant, authorised brand, reference brands with roles) is the only input
  that can reach the confidentiality boundary, and it rejects unknown keys.
- Enum values are referenced from this directory's metadata contract with `x-enum-from`,
  never copied.
- Every field is `x-status: live` (acted on) or `planned` (validated, not yet used); a
  test fails if a field marked live does not reach retrieval.

Reference: `engine/rag/README.md` → *RAG I/O contract*. Reasoning:
`engine/rag/docs/adr/0001-rag-io-contract.md`. Python side: `engine/rag/rag_io.py`.
