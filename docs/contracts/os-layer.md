# Contract 4 — the OS layer

Status: **draft**, pending the W1-C4 contract gate. Owner: Shrey.
Implements foundation decisions P1b (amended), N3, N4, D5, D6, D7, C4, L4.
Task: `W1-C5` in `napkin-build-plan.clan`.

The OS layer is `crates/napkin-host`. It is everything Napkin OS does with a
CLAN document that is not drawing pixels and not thinking: opening it, the
`clan://` contract an app calls, typed decisions, spin-off, locking, and the
app library. Three shells run it: `server/` (FastAPI, via PyO3), the Tauri
desktop shell, and `napkin-wasm`.

This document is what every other track reads. Where it disagrees with the
code, the code is wrong. Where it disagrees with `foundation-spec.clan`, the
spec wins and this document is a defect to report.

---

## 1. Where it sits

```
 App — the installed version (same major) from the app library, sandboxed iframe
      │  clan:// contract, versioned
 ─────┼───────────────────────────────────────────────────────────────
 Shells   server/ FastAPI (authoritative) │ Tauri │ napkin-wasm
      │     └ middleware: auth → Ctx, lease, handlers, ports, gates
 ─────┼───────────────────────────────────────────────────────────────
 OS layer = napkin-host — a library, stateless, domain-free
   routes    clan:// dispatch, trust gate, capabilities
   ops       (Ctx, Document@version, input) → Change
   ports     PartStore · Library · FactResolver · Config
 ─────┼───────────────────────────────────────────────────────────────
 clan-sdk — the format: pack, unpack, verify, fork, merge, spinoff
```

The layer is a library, not a network tier. Python calls it inside its own
transaction (P2). The desktop and wasm shells call it directly.

### What never enters the OS layer

- Prompts, retrieval, model calls, gate logic. Those are middleware (M1). Today
  `prompt.rs` knows what a creative brief is and defaults the task to
  `draft_brief` — both leave with W2-C2.
- Tenancy and identity decisions. The layer receives a resolved `Ctx`; it never
  derives scope from a request body (M3).
- Database handles or credentials. It reaches storage only through its ports.
- Anything app-specific. Apps declare; the middleware runs logic; the layer
  provides generic primitives.

---

## 2. Sync model (N3)

The server holds every document. The viewer never downloads a working copy.

1. **Edits go up as operations.** A write carries the fields that changed and
   the version it was based on: `patch-data {patch, base_version}`.
2. **The server applies, records, versions.** One transaction: the change, the
   decision behind it, version N → N+1. A stale `base_version` is a distinct
   conflict error — never retried, merged or overwritten.
3. **Changes fan out.** Every open view of the document receives
   `{doc, version, change}` over SSE and re-renders from `window.__CLAN__.data`.
   The editing client updates optimistically and reconciles on the ack.
4. **One editor at a time.** A lease with heartbeat, presence and a visible
   handoff (N1). A force-take is a recorded decision.
5. **A `.clan` file is packed on demand** for export, client handoff and the
   offline presenter. A packed file is a snapshot, never a working copy.

The offline presenter is **read-only**. Offline editing, if it is ever needed,
becomes a branch that merges back through §6 — no redesign.

Rejected: downloading files and syncing them. Every write re-deflates the whole
ZIP, so byte diffs are the whole file; a held copy written back is the observed
viewer clobber; and two merged offline copies make the chain ambiguous.

---

## 3. Core types

### Ctx — who is asking, resolved before the layer sees the request

```
Ctx {
  actor:     "human:<user-id>" | "process:<job-id>" | "<producer>/<version>"
  org, brand: resolved from the authenticated session, never the body
  handler:   Option<"name@major.minor">   // set when a handler is acting
  backend:   Option<String>               // set when a judgement port answered
  lease:     Option<LeaseId>
}
```

### Address — a stable pointer to a field

`<doc-id>#<entity-keyed path>`, e.g. `3f2a…#competitors[tesco].share`.

- The document id is part of the address, so an address stays valid inside any
  document that carries the origin (§5). No remapping, ever.
- Paths are entity-keyed. A positional index (`shots.0`) is invalid, because a
  re-sort silently re-points it and the app bridge turns arrays into maps.

### Document — the unpacked parts

`manifest · data · facts (pins) · findings · chain · context · view ref ·
asset refs · carried[]` where `carried[]` is the frozen upstream documents
(§5). The server stores parts — rows in Postgres, blobs by hash in S3. The
desktop and wasm stores pack and unpack at the edge.

Until the loop has closed once, SDK operations still run over `ClanFile`; the
layer packs a transient archive to call them (Tier 0 rule). Moving the SDK
operations onto the part model is a later task.

### Change — what an operation returns instead of writing

```
Change {
  doc, expected_version,
  parts: { data?, facts?, findings?, context?, view?, assets? },
  decisions: [Decision],        // appended, never rewritten
  events: [HostEvent],          // fanned out after commit
}
```

The caller applies it: `FsStore` as a version-checked file write, the server as
one Postgres transaction. This is the single write funnel — the version check
(W2-A4) and the seal check (W5-Z1) live here and nowhere else.

### Decision — one primitive for every mark (D5)

| Field | Meaning |
|---|---|
| `id` | Stable, generated at creation, never content-derived |
| `kind` | `edit` · `contest` · `resolve` · `verdict` · `classify` · `pin` · `finding` · `verify` · `approve` · `lease` · `backref` |
| `targets[]` | Addresses this decision is about |
| `cites[]` | Fact pins, sources, findings and decisions it rests on |
| `superseded_by` | The decision that replaced this one |
| `actor`, `handler`, `backend`, `scope` | Copied from `Ctx` — never from the body |
| `polarity`, `reason_code`, `taxonomy_version`, `reviewer_role` | Verdicts |
| `licence` | Classify: `{model, export, corpus}` per C2 |
| `rationale` | Required for `resolve`, `verdict` with polarity bad, `lease` force-take, and `approve`. The plain one-line summary older readers show: with `reasoning`, `decided` + its first `because` point, unless the writer supplied one |
| `reasoning` | Why, in the shape of the foundation spec's decisions (below). Optional in the format — a person's edit has none — and required of the middleware on the kinds `napkin.middleware/1` §3 names. Never rewritten by compression |
| `#[serde(flatten)] rest` | Unknown fields survive a read-modify-write |

Existing fields (`agent`, `action`, `timestamp`, `fields_changed`, `pinned`,
`trace_ref`) stay. Every new field is optional, so every existing file still
validates. Struct work is W1P-I5.

#### `reasoning` — the spec's decision discipline, on every agent decision

```yaml
reasoning:
  decided: Opened a contest on the flagship year; nothing is picked.   # one sentence
  because:                                   # ≥ 1 point
  - point: The market_structure/IE run says 2019
    cites: [f_01JB…IE, src_01JB…]            # facts, findings, sources, materials, decisions, addresses
  - point: The market_structure/GB run says 2020
    cites: [f_01JB…GB]
  rejected:                                  # the alternatives and why each lost
  - { option: pick either value silently, why: nothing in the evidence says which run is right }
  only_option: …                             # only when `rejected` is empty: why there was one option
  certainty: { level: high, why: the values differ as their sources state them }
  would_change_if: a primary source settles the value, or a person resolves the contest
  attention: The runs disagree on the flagship year; a person should resolve it.   # optional
```

| Field | Rule |
|---|---|
| `decided` | Non-empty: what was decided, one sentence |
| `because[]` | At least one point; every cite a non-empty id; a point that states a figure (holds a digit) cites where the figure is |
| `rejected[]` | Each `{option, why}`, both non-empty. Empty only with a non-empty `only_option` saying why there was one option |
| `certainty` | `level` one of `high` · `medium` · `low`, and a non-empty `why`. For anything resting on facts it is the **derived** confidence (source tier + corroboration, Contract 3 §6.1) and its basis — never self-reported |
| `would_change_if` | Non-empty: what new evidence would reverse it |
| `attention` | Optional, non-empty when present: why a person should look — an uncertain call, thin evidence, a contest, a skipped lens that might matter |

It maps onto the foundation spec's decisions one to one: `decision` →
`decided`, `because` → `because`, `rejected` → `rejected`, `reverses_if` →
`would_change_if`; `certainty` and `attention` are what a reviewer needs that
a design record does not. `validate` checks the shape wherever a decision
carries one and requires it nowhere (the format cannot tell who decided);
unknown fields inside it survive like the decision's own. The viewer renders
it as the decision's readable block; `rationale` stays for every reader that
predates it.

---

## 4. The features, as decision kinds

### Collisions (app-specific policy, generic mechanism)

- A **contest** is opened when two writers produce different values for one
  address: two research lenses, a job against a human edit (N2), or a branch
  merge. Fact collisions live in the knowledge layer as contested rows; field
  collisions live in the document as `contest` decisions.
- Each app declares a merge policy per field (`ask`, `append`, `max`, `min`,
  `agent-priority`, `last-write`). **`ask` is the default. An undeclared field
  is `ask`; an unknown policy name is a hard error** (M4). Today
  `clan-sdk/src/merge.rs:264` and `:363` fall through to `last-write` —
  fixed by W1P-I7.
- Under `ask`, the old value stays and nothing is picked. The lease holder
  resolves with a rationale; the losing value is superseded, never deleted.
- Only the contested field waits. The rest of the document keeps syncing.

### Confidential (C4)

A `classify` decision setting the three C2 destinations: may it reach the
model, may it appear in an export, may it enter the corpus.

- On a field backed by a pinned fact, the mark supersedes the **fact's
  licence in the layer**, with the decision behind it. A document-only mark
  would still leak through the layer into the next campaign.
- On authored text, it is a licence on the field in the document.
- Enforced at gate G4 (prompt selection), `compose_export` (this layer), and
  the dossier promotion check (W5-Z2).

### Good and bad (L4)

A `verdict` decision with `polarity`, a `reason_code` and a rationale
(required when bad, and for `other`). Field-scoped.

- **Quality verdicts** go to verdict rows (W4-V4) and the dossier: good feeds
  exemplars, bad feeds CONSTRAINTS.
- **Truth verdicts** ("this figure is wrong") reach the fact. A human
  verification is a **source**: uri `human:<id>`, tier `reviewer-verified`,
  linked to the fact. Confidence stays derived from tier and corroboration.
- The vocabulary gains positive codes beside the ten failure codes. Both need
  the creative director's redline.

### Findings (D1 amended)

- Agent synthesis is a `finding`, stored in `shared/findings.yaml`, status
  `proposed`, labelled **"derived by the agent"**, with `cites[]` to the facts
  it rests on.
- The document may use a proposed finding, visibly labelled.
- A finding reaches the knowledge layer only when a human verifies it
  (`verify`, which creates the `human:<id>` source). Then it is a fact with
  `method: synthesis`, pinned like any other, and the next campaign inherits it.
- Nothing unverified ever enters the layers.

### The decision history, made useful

With `id`, `targets`, `cites` and `superseded_by`, the chain is a graph. The
layer serves generic views over it:

- `/history?address=` — why is this value here: every decision on the field,
  the facts it cites, each fact's source and tier.
- `/timeline` — filterable by actor and kind.
- `/open` — every unresolved item (§7), including carried ones.
- Handler version and backend on every entry explain why two documents from
  the same app differ.

`compress_chain` must stop splitting on full stops before rationales that cite
paths can be shown (W2-A2).

---

## 5. Spin-off carries everything (D6)

A hop carries the source whole. The next document holds it as a frozen,
read-only upstream under the source's document id, so every address,
decision and mark from upstream still resolves.

| Travels | Does not travel |
|---|---|
| All data, frozen, under the source's doc id | The source app's view (`human/index.html`) |
| Facts member (pins) and findings member | The source app's task text (`agent/context.md`) |
| The entire chain: verdicts, classify marks, approvals, open contests | `human/patches.yaml` |
| Unmerged agent branches, as collisions | View state, the lease, secrets |
| Assets (by hash on the server) | |
| Lineage, plus the carried facts hash and last carried decision id | |

- The target app's graft (`app.spinoff` map/lift) only seeds the target's own
  fields. Each seeded field gets a decision citing the upstream address.
- Source and target must be the same tenant; a cross-tenant hop is refused.
- It accumulates: campaign → brief carries campaign; brief → deck carries both.
- Open items travel open. They show at the field if the target app shows that
  address, otherwise in the decision tree under **open items carried from
  upstream**.
- Spin-off from an unlocked document is allowed.

The carried hash and last decision id make the freeze checkable before sealing
exists: the server holds them on append-only rows.

---

## 6. Parallel work (N4)

- **Parallel:** research agents, and **one drafting agent per field**.
- **Serial:** the judge, the revision, and everything after it. The judge never
  sees the drafting context (UC-6), and it checks coherence across the whole
  brief — per-field drafting's risk is `not_single_minded`.
- Every parallel agent writes to its own branch, `agents/<user>.<agent>.<task>/`
  (spec §24). The user and agent parts come from `Ctx`, never from the request.
- A branch merges under the lease, through the §4 merge policies.

### What needs the lease

| Needs the lease | Does not |
|---|---|
| Edits to data, facts and findings | Gate verdicts, reviewer verdicts |
| `resolve`, `verify` | `classify` marks |
| Merging a branch | Agent work on its own branch |
| A job, only at commit (N2) | Research lenses writing layer rows |
| `approve` (lock) | Decision appends in general — append-only rows cannot clobber |

This narrows the spec's open question: the facts member needs no append
mechanism while only the lease holder or a merge writes it.

---

## 7. Lock = accept = seal (D7)

Locking is file-wide. A document **cannot lock** while anything is unresolved:

- an open contest, including carried ones;
- an unmerged agent branch;
- a finding not yet verified or rejected;
- a bad verdict not yet answered — the field revised, or the verdict overridden
  with a written reason.

At lock:

- The reviewer is shown the findings list. Confirming verifies them (`verify`
  per finding, each written to the layer). Rejecting a finding marks it
  rejected and flags every field that cites it — a new unresolved item, so the
  lock waits until those fields are revised.
- One `approve` decision records the exact version and content hash. The seal
  (W5-Z1) is taken over that.
- Carried upstream content inside the file is accepted with it — it is the
  frozen copy that was reviewed.
- **Parents are not marked accepted.** Each gets a `backref` decision: "used in
  locked <doc> at <hash>".
- A contest resolved in a child cannot flow back into the child's frozen copy.
  The child records the resolution; the parent gets a `backref` "resolved in
  <doc>". The parent must still resolve it itself before it can lock.

Still open: what unsealing means (W5-Z1).

---

## 8. Routes — contract v1

`/capabilities` reports `contract: 1`. A document is shown with the installed
version of its app when that version has the same major and is not older than
the one the document was made with; otherwise with the copy it carries (owner
decision, 2026-09-24 — M2's `name@major` rule applied to views). The copy in
the `.clan` is what export, handoff and the offline presenter use, and old
majors still run, so the route surface is a contract apps depend on: add
freely, never redefine.

- Existing routes stay as they are: `/patch`, `/patch-data`, `/fork`,
  `/upload-asset`, `/assets/*`, `/chain`, `/api-proxy`, `/spinoff`,
  `/spinoff-targets`, `/set-title`, `/set-context`, `/export`, `/notify`,
  `/set-theme`, and the library routes.
- A body `agent` field on `/patch-data` is kept as `claimed_agent` on the
  decision. The actor always comes from `Ctx`.
- New in v1: `/verdict`, `/classify`, `/contest`, `/resolve`, `/finding`,
  `/verify`, `/approve`, `/history`, `/timeline`, `/open`, `/stale`.
- `/decisions` (landed): every decision, newest first, with who made it, its
  targets as labels, its cites resolved, and what needs a person — derived by
  the host from the chain, the members and the data: `reasoning.attention`,
  low certainty, and the §7 lock list. The shell renders it as the OS layer's
  decision blocks. `/open` above names the lock list alone; it is not built,
  and the path is taken today by the launcher's open-a-document route.
- On the server, `/api-proxy` becomes a dispatch into the handler registry. The
  desktop keeps the transport; it does not assemble prompts.

---

## 9. Retrieval scope

Each agency has its own RAG index (S6, reversed 2026-09-23). House material
stays shared. Every retrieval still records per-hit scope — which index — in
the trace, so a mistake has a readable blast radius. The layer's part is storing
`trace_ref` on the decision; today `TraceRef` is never constructed with a value.

---

## 10. Conformance

Checked against the nine questions in `campaign-flow-usecases.clan`.

| # | Question | Answer here |
|---|---|---|
| 1 | Where does a synthesis live? | `shared/findings.yaml` until verified; then a layer fact, `method: synthesis` (§4) |
| 2 | What travels at a hop? | Everything but the source app's view, task text, legacy patches and view state (§5) |
| 3 | Filter or partition? | Partition per agency; per-hit scope still recorded (§9) |
| 4 | Can a handler name its own scope? | No. Scope, actor and branch namespace come from `Ctx` (§3, §6) |
| 5 | Approval vocabulary vs rejection? | `approve` is file-wide, bound to a hash; `verdict` is field-scoped with polarity (§4, §7) |
| 6 | What raises confidence; is a human one? | Tier and corroboration only; a human verification is a source (§4) |
| 7 | What is frozen at a spin-off, and checkable how? | The whole upstream; carried hash plus last decision id in lineage (§5) |
| 8 | What runs under someone else's lease? | See the lease table (§6) |
| 9 | What is parallel; does its target append? | Research and per-field drafters on branches; the chain appends by row (§6) |

---

## 11. Build order

| Task | What |
|---|---|
| W1-C5 | This contract |
| W1P-I5 | Decision struct: the §3 fields |
| W1P-I7 | Merge policies: `ask` by default, unknown is an error |
| W2-O1 | Stateless OS layer: Document, Change, Ctx, PartStore/Library split, ViewState out of Session |
| W2-O2 | Typed decision operations and routes (§4, §7, §8) |
| W2-O3 | Bind the OS layer to Python |
| W2-O4 | The server part store and change fan-out |
| W3-O5 | Route parity, then retire `napkin-web` |

Amended by this contract: W1-C1, W1-C3, W1-C4, W1P-I5, W2-A3, W2-A4, W2-A5,
W2-C4, W2-C6, W2-D1, W3-J3, W4-V1, W4-V2, W5-Z1.
