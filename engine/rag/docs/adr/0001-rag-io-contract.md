# 0001 — RAG I/O contract

- **Status:** accepted (contract `locked: false` pending middleware-owner sign-off)
- **Date:** 2026-09-23
- **Owner:** Sai
- **Files:** `engine/schema/rag_io.v1.json`, `engine/rag/rag_io.py`, `engine/rag/test_rag_io.py`

## Context

The RAG module is being rebuilt as a pipeline: input → extraction → validation → rerank
→ ordering → output. The middleware is its only caller. Until now there was no defined
interface: `brief_context.build()` took an untyped dict of pairs plus `brand` and
`tenant` keyword arguments, and the middleware (`agent-server/research.py`) called
`parse_brief` directly. Three constraints shaped the contract:

1. **The middleware may not be Python**, and the metadata contract already established
   that cross-language agreements live in JSON, not in code.
2. **Confidentiality is enforced at retrieval.** Session 3 found that attachment text
   could choose the brand scope because a model-extracted client name flowed into
   `scopes_for()`. Whatever the contract looks like, nothing in it except a
   middleware-injected field may reach the scope boundary.
3. **A contract nothing honours is worse than none.** `app/templates/brief-maker/app/pipeline.yaml`
   calls itself the pipeline contract and nothing reads it. People trust it anyway.

## Decision

1. **JSON Schema (draft 2020-12)** in `engine/schema/rag_io.v1.json`, next to
   `rag_metadata.v1.json`, with the same versioning rules: optional field = minor bump;
   rename, removal or tightening = major bump and a new file.
2. **`authority` is the only boundary input.** `tenant`, `brand` and `references` are
   injected by the middleware from the session and the campaign record. The object has
   `additionalProperties: false`, so the boundary cannot grow a key nobody reviewed.
   Everything else — brand record, campaign pairs, attachments — is at most search text.
3. **Every field carries `x-status: live | planned`.** Planned fields are validated but
   deliberately ignored by the adapter, not half-used. A test sets each live field to a
   sentinel and asserts `build()` receives something different; marking a field live
   without wiring it fails the suite. Verified by mutation: flipping `brand.parent` to
   live made the test fail.
4. **Enums are referenced, not copied.** `x-enum-from: category` resolves against
   `rag_metadata.v1.json` at check time.
5. **The response is structured evidence, not a summary** — blocks of citable hits plus
   the rendered `prompt_text`. Each hit carries a `weight` (`constraint` / `advice` /
   `evidence`) so a reviewer's rejection never looks like textbook advice, and a
   `relevance` slot that stays null until the validation stage exists.
6. **The campaign object accepts unknown keys** (they join the query text); `authority`
   and `brand` do not. Growth where it is harmless, closure where it is not.
7. **stdlib validator** for the subset of JSON Schema the contract uses, matching
   `contract.py`'s no-dependency rule. Any standard validator can check the same file on
   the middleware side; only the two `x-` keywords need handling there.

## Alternatives rejected

- **Python dataclasses / pydantic as the contract.** One language only, and the
  middleware owner would have to read Python to know the interface.
- **Extending `rag_metadata.v1.json`.** That file describes chunks at rest; this one
  describes a call. Different lifecycles, different owners of sign-off.
- **Letting `brand.name` imply authorisation when `authority.brand` is absent.**
  Convenient, and exactly the session-3 bug in a new place.
- **Joining aliases into the `brand` pair.** Tried first. `scopes_for()` reads the
  `brand` pair as the document's claim about who the client is, so "BMW, BMW i" produced
  a false "document claims client 'bmw_bmw_i'" warning on every aliased brand. Aliases
  now ride on `product`, which is also an exact keyword key. Regression test:
  `test_aliases_do_not_trigger_a_false_client_mismatch`.
- **Accepting a caller-supplied scope list.** Scope is derived from authority, never
  passed in (foundation-spec M3). `authority.scopes` is explicitly rejected.

## Consequences

- The middleware owner (Shrey) must sign off the request shape before `locked: true`.
- `brand.categories` accepts two values but only the first filters; the second is
  recorded in `notes`. Using it as a widening step is future work.
- `target`, `research`, `attachments`, `memory` and most of `limits` are accepted now so
  the middleware can start sending them; the validation stage is the first consumer.
- `loops_3_7` does not go through this contract yet. Until it does, the path that
  actually generates briefs bypasses it.
- Measured on the local index: a full request returns 13 hits / 8,909 tokens in ~6s, of
  which most is local index load (store instances are not cached — a known dev-only cost).
