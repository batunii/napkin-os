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

## Addendum 2026-09-28 — a broken library is an error

A failure test of `handle()` (project_plan.clan `rag_io_failure_test_2026_09_28`) found that
a missing or empty passage library returned `ok` with 0 hits in every field, no note and no
error, in 0.5 s: the middleware could not tell it from a brief with nothing relevant.
`handle()` now calls `require_store()` before any search: the store must open and its own
`available()` must say it holds passages, else `StoreUnavailable` names the library and
says nothing was searched. An unconfigured backend (`StoreConfigError`) is reported the same
way, and an unreachable Qdrant now fails in ~3 s with this error instead of a raw
`RuntimeError` after ~17 s of retries. Retrieval is unchanged when the library is fine
(same evidence, same order). Tests: `test_rag_io.py` (missing / empty on both paths,
unconfigured, injected retrieval skips the check).

The brief tool (`parse_brief.loops_3_7`) does not go through `handle()` and keeps its own
behaviour: `index_available()` is checked, and a missing library falls back to the pack
digests loudly, with the reason in the result (`fallback`). Stopping the brief instead is a
separate decision; Sai kept the fallback (2026-09-28).

Suggested handling for the middleware (Sai approved 2026-09-28; project_plan.clan
`store_unavailable_plan_2026_09_28`): retry once after ~30 s; if it still fails, write the
brief from the pack digests, clearly marked as written without the library, as the brief
tool does; one alert per outage; later, a synced backup library with the AWS move. Never
treat the error as an empty result or retry in a tight loop.

## Addendum 2026-09-28 (2) — `degraded`: what did not work, at the top of the answer

The same failure test found that a checker that timed out on every field, or a search that
fell back to keywords, returned `notes: []`: the only record was deep in `trace`, which the
middleware has no reason to read. Contract 1.4.0 adds `degraded`, a list of
`{kind, fields, why}` built from what the trace already records, with one plain line per
entry in `notes`. Kinds: `checker_skipped`, `keyword_only`, `empty_field`, `generic_query`.
Additive and optional, so no existing reader changes; the middleware owner (Shrey) is told
in the handover. Evidence is unchanged; there are no extra calls.

Found while testing it, not fixed here: on the mix path with the validator on, `validation`
carries the per-field record (`per_field`, `calls`), which `$defs/validation` does not admit,
so a live mix response does not validate against its own contract. The offline tests run with
the validator off and never saw it.

