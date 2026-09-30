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

- Prompts, retrieval, model calls, gate logic. Those are middleware (M1).
  (`prompt.rs`, which knew what a creative brief is and defaulted the task to
  `draft_brief`, is gone, with the browser-side model call it served.)
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
asset refs · upstream` where `upstream` is the frozen data of every document
upstream, at `data.upstream.<document_id>`, and `manifest.lineage.carried`
records the hop (§5). The server stores parts — rows in Postgres, blobs by
hash in S3. The desktop and wasm stores pack and unpack at the edge.

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
| `kind` | `edit` · `contest` · `resolve` · `verdict` · `classify` · `pin` · `finding` · `verify` · `approve` · `lease` · `backref` (§7) · `client_review` · `unlock` (§7.5) |
| `targets[]` | Addresses this decision is about |
| `cites[]` | Fact pins, sources, findings and decisions it rests on |
| `superseded_by` | The decision that replaced this one |
| `actor`, `handler`, `backend`, `scope` | Copied from `Ctx` — never from the body |
| `polarity`, `reason_code`, `taxonomy_version`, `reviewer_role` | Verdicts |
| `licence` | Classify: `{model, export, corpus}` per C2 |
| `rationale` | Required for `resolve`, `verdict` with polarity bad, `lease` force-take, and `approve`. The plain one-line summary older readers show: with `reasoning`, `decided` + its first `because` point, unless the writer supplied one |
| `reasoning` | Why, in the shape of the foundation spec's decisions (below). Optional in the format — a person's edit has none. Required of the middleware on every `pin`, `contest`, `finding` and `verdict` and every proposal, and on what the document's app declares in the `reasoning` block of its `app/pipeline.yaml` (`kinds`, and `edits`: the data paths an edit must say why it wrote) — the layer applies the declaration and knows no app's fields (`napkin.middleware/1` §3). Never rewritten by compression |
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
- The document may use a proposed finding, visibly labelled. It stays
  **"derived by the agent"** in every document that carries it, until a
  person verifies it — in that document, or upstream before the hop. In a brief it is **cited only**: it
  may ground a drafted field (insight, proposition, reasons to believe,
  desired response), with certainty at most medium, and never fills a field
  captured from the client's material (`napkin.middleware/1` §10.13; owner
  default, 2026-09-29, may be reversed).
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

*Changed 2026-09-29.* The table below was the rule; the SDK did not follow it.
`spinoff()` copied the target app's entries and the source's assets only, so
the facts, findings and sources members, the agent branches and every open
item's evidence were lost, and with no `map` the source's data was folded in
at the root, where research `materials` collided with the brief's and
`projection` came along. This section now says exactly where each part lands
and what the hop records, so the SDK, the host and the views build to one
shape. An app that does not opt in (below) spins off as before.

| Travels | Does not travel |
|---|---|
| All data, frozen, under the source's doc id — without its `projection` | The source app's view (`human/index.html`) |
| Facts member (pins), findings member and sources member, merged | The source app's task text (`agent/context.md`) |
| The entire chain: verdicts, classify marks, approvals, open contests | `human/patches.yaml` and `shared/edits.yaml` |
| Unmerged agent branches and the merge report, as collisions | View state, the lease, secrets |
| Assets (by hash on the server) | |
| Lineage, plus the carried hashes and last carried decision id | |

### 5.1 Opting in: `app.spinoff.upstream`

The target app declares the hop in its manifest, as it declares `map` and
`lift` today:

```yaml
app:
  spinoff:
    accepts: [ie.napkin.campaign-research]
    upstream: true        # carry the source whole, under data.upstream (this section)
    lift: {}              # optional seeds of the target's own fields (§5.4)
```

1. `upstream` defaults to `false`. With `false` nothing in this section
   applies and the hop is the older graft — `map`, or the root fold — so
   Advertising Studio's `map: brief` is unchanged.
2. With `true`, `map` must be absent (declared or passed as the `/spinoff`
   override); a spin-off with both is refused (`422`): the source's data has
   one place, `upstream.<id>`.
3. A spin-off from an unlocked document is allowed. What is open travels open
   (§5.5).
4. Source and target must be the same tenant; a cross-tenant hop is refused
   (`403`, §8).

### 5.2 Where each part lands

1. **Data.** The source's `shared/data.yaml`, less its `projection` and its
   `upstream`, is written at `data.upstream.<source document_id>` in the new
   document, value for value. It is host-written and read-only: no data
   patch, `/patch-data`, `/edit` or middleware `data_patch` may write
   anything under `upstream` (refused `400`, like `projection`).
2. **Upstream of upstream is hoisted.** Each key of the source's own
   `data.upstream` becomes a sibling key of the new `data.upstream`, value
   unchanged. Campaign → brief → deck: the deck holds
   `upstream.<campaign>` and `upstream.<brief>`, and the brief's copy holds no
   `upstream` of its own. A key the new document already holds is kept (it
   cannot happen in one hop).
3. **Members are merged, not frozen.** The entries registered with role
   `pinned-facts` (`shared/facts.yaml`), `findings` (`shared/findings.yaml`)
   and `sources` (`shared/sources.yaml`) are merged into the new document's
   member at the same path, by `id`: the target's own entries first, then the
   source's in the source's order, skipping an id the target already holds.
   When the target holds no entry of that member, the source's bytes are
   written unchanged. A member the target does not register is registered with
   the source's `files[]` role. The child verifies or rejects **its copies** of
   the findings and corrects its copies of the pins; the parent's members
   never change (§7, §8.1).
4. **The chain is carried whole.** Every source decision, field for field,
   below the spin-off marker and above the target template's own; `pinned`
   is set on each when `pin_source_decisions` holds (the default). Nothing is
   dropped, reordered or re-addressed — open contests, bad verdicts, classify
   marks and the source's `approve` included.
5. **Agent branches** (`agents/**`) keep their paths. **The merge report** is
   carried at `upstream/<source document_id>/merge-report.yaml` (role
   `upstream-merge-report`), not at the root: its keys name the source's data,
   which is no longer at the root, and the new document may make its own.
6. **Assets** (`human/assets/**`, the `.extracted/` sidecars included) are
   copied; the target's own asset wins a path collision.
7. **Not carried:** `human/index.html`, `agent/context.md`, the source's
   `app/**` and `agent/output-schema.json` (the target's own are the new
   document's), `human/patches.yaml`, and `shared/edits.yaml` — a person's
   wording for the *source's* view, meaningless in another app's.
8. **The projection is the new document's own.** The source's `projection`
   is not carried (item 1). The SDK writes none at the root; the host builds
   it from the merged members when it writes the spin-off (`/spinoff`), as it
   does on any member write (Contract 3 §5). A spin-off made by the SDK alone
   (`clan spinoff`) has no root `projection` until the host next writes its
   members.

### 5.3 What the hop records: `lineage.carried`

```yaml
lineage:
  parent_id: <source manifest id>          # as today
  parent_uri: clan-store:<DocId>           # as today: how the host finds the parent
  parent_sha256: sha256:…                  # as today: the source archive
  delta: spun off from "…" into Brief Maker v0.6.0
  parents: [ {source}, {template} ]        # as today
  carried:
    document_id: 3f2a…                     # the source's document_id: its key in data.upstream
    data_sha256: sha256:…                  # of the source's shared/data.yaml bytes, as read
    facts_sha256: sha256:…                 # of each member's bytes, as read; absent without it
    findings_sha256: sha256:…
    sources_sha256: sha256:…
    last_decision: d_01JA…                 # the newest source decision with an id; absent if none
```

1. Every hash is `clan_sdk::hash::sha256_prefixed` of the entry's bytes, the
   form `projection.built_from` already uses, so a host can compare the
   parent's current entries with no parsing.
2. `carried` names the **direct** parent only. A hoisted upstream's record is
   the parent's business: the brief was spun off from that version of the
   campaign, and the deck from this version of the brief.
3. Absent on a hop with `upstream: false`, and on every file written before
   it; every existing file still validates.

These make the freeze checkable before sealing exists: the server holds them
on append-only rows, and `GET /upstream` (§8) compares them with the parent.

### 5.4 Seeding the target's own fields

1. The graft (`app.spinoff.lift`) only seeds the target's own fields. With
   `upstream: true` a lift **copies** — the frozen copy stays whole — and
   each seeded field gets one decision:

   ```yaml
   - id: d_…                     # a fresh decision id
     kind: edit
     action: seed
     agent: napkin-spinoff
     actor: process:spinoff
     targets: ["<new doc id>#<to>"]
     cites: ["<source doc id>#<from>"]
     fields_changed: [<to's top-level key>]
     pinned: true
     rationale: Seeded <to> from <from> in "<source title>".
     reasoning:
       decided: Seeded <to> from <from> in "<source title>".
       because: [{ point: <target app> seeds <to> from the upstream <from>, cites: ["<source doc id>#<from>"] }]
       rejected: []
       only_option: The target app declares this lift; the value is copied unchanged.
       certainty: { level: high, why: the value is the upstream value, copied }
       would_change_if: a person edits <to>, or the document takes newer upstream data
   ```

2. Newest first, the chain reads: the seeds, the spin-off marker, the
   source's chain, the template's chain.
3. A lift whose `from` holds nothing seeds nothing and records nothing.

### 5.5 Open items, and when the parent changes

- It accumulates: campaign → brief carries campaign; brief → deck carries both.
- Open items travel open. They show at the field if the target app shows that
  address, otherwise in the decision tree under **open items carried from
  upstream**, and they are on the target's lock list (§7).
- **When the parent changes, the carried copy stays frozen.** The child shows
  "The research has changed since this brief started" (each app in its own
  nouns) and lists what changed — pins, findings, contests, and findings
  rejected upstream — from `GET /upstream`. Taking the newer upstream is an
  explicit, later action; it is not built. (Owner default, 2026-09-29; may be
  reversed.)
- No address is ever remapped: a carried decision on
  `<campaign>#selection.contested[ct_…]` means the same contest in the brief
  and in the deck.

### 5.6 The SDK surface

```rust
// manifest.rs
pub struct SpinoffSpec {
    pub accepts: Vec<String>,
    pub map: Option<String>,
    pub lift: BTreeMap<String, String>,
    pub pin_source_decisions: bool,          // default true
    /// Carry the source whole under `data.upstream.<id>` (Contract 4 §5).
    #[serde(default, skip_serializing_if = "is_false")]
    pub upstream: bool,                      // default false
}

pub struct Lineage {
    /* parent_id, parent_uri, parent_sha256, delta, parents, merge — unchanged */
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub carried: Option<Carried>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Carried {
    pub document_id: String,
    pub data_sha256: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub facts_sha256: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub findings_sha256: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sources_sha256: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub last_decision: Option<String>,
}

// instantiate.rs — unchanged signature; SpinoffOptions gains nothing.
pub fn spinoff(template: &ClanFile, source: &ClanFile, opts: SpinoffOptions) -> Result<Vec<u8>>;
```

`clan_sdk` re-exports `Carried` beside `Lineage`. Adding the two fields breaks
the struct literals that name every field. `SpinoffSpec` in `library.rs`,
`tests/routes.rs`, `examples/make_advertising_studio.rs` and `clan-cli` gains
`upstream: false` and nothing else, and the hand-written
`impl Default for SpinoffSpec` gains `upstream: false`. `Lineage` in
`instantiate.rs` gains `carried: None` for `instantiate()` and the filled
record for an upstream `spinoff()`. `Lineage` in `pack.rs`, `render.rs` and
`merge.rs` (fork and merge) copies `carried` from the revision it rewrites:
a document keeps the record of its hop on every later write, or the first
edit would erase it and `GET /upstream` and backrefs (§7.4) would have
nothing to compare or address.

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
| `unlock` (reopening a part, §7.5.6) | `client_review` records, confirmations and dismissals |

This narrows the spec's open question: the facts member needs no append
mechanism while only the lease holder or a merge writes it.

---

## 7. Lock = accept = seal (D7)

Locking is file-wide.

*Changed 2026-09-29.* Three things did not hold once a document carried its
upstream. A child of a locked parent was born locked, because the lock was
"any `approve` in the chain" and the chain carries the parent's. A child could
not settle what it carried: targets on another document were refused and
`/resolve` looked at the root contests only. And no `backref` was ever
written. This section scopes the lock to the document, says how each carried
item is settled in the child, and fixes the backref's shape and moment.

### 7.1 What the lock is

1. A document is locked when its chain holds an `approve` decision, not
   superseded, whose `targets` include **this document's id** — the bare
   `document_id`, the form `/approve` writes. When more than one does (a
   document locked again after a part was reopened, §7.5.6), the newest is
   the lock.
2. A carried `approve` (it targets the parent's id) records that the parent
   was accepted. It does not lock the child and settles nothing on the child's
   list.
3. An app's own flag (Brief Maker's older `data.locked: true`) is not the lock.
   The middleware honours either (`napkin.middleware/1` §10.5); a view locks
   through `/approve` only.

### 7.2 The lock list

A document **cannot lock** while anything is unresolved, **carried items
included — every one, whether or not the document cites or shows it** (owner
default, 2026-09-29; may be reversed):

1. **An open contest.**
   - in `data.selection.contested`, status `open`;
   - in any `data.upstream.<id>.selection.contested`, status `open`, unless a
     `resolve` decision in this chain, not superseded, targets
     `<id>#selection.contested[<ct id>]` — the frozen copy never changes, so
     the chain is where a carried contest is settled;
   - a `contest` decision in the chain, own or carried, with no later
     `resolve` on its target;
   - a conflict in `merge-report.yaml`, or in a carried
     `upstream/<id>/merge-report.yaml`.
2. **An unmerged agent branch** under `agents/`, carried ones included.
3. **A finding not yet verified or rejected** — any `proposed` entry in this
   document's findings member, carried copies included.
4. **A field citing a rejected finding** — its envelope's `finding_ids`, or
   its **current writing decision**: the newest decision in this chain that
   wrote the field (`napkin.middleware/1` §10.5's rule), not superseded and not
   a `propose`, citing the `fi_` id.
5. **A bad verdict not yet answered**, own or carried — the field revised
   since, or the verdict overridden since by a good verdict with a written
   reason (`/verdict good`) on the same address. A carried
   bad verdict on an upstream address can only be overridden: the frozen field
   is read-only.
6. **A client's rejection not yet answered** — a rejected part not edited
   since, or a rejected document whose parts are not known yet (§7.5.4). It
   can only arise on a locked document, so it bears on locking again.

A carried branch or carried merge-report conflict is settled only upstream:
merged in the parent, then taken with the newer upstream (§5.5, not built).
Until then it blocks. **Still open:** whether a child may dismiss one with a
written reason.

Item 4 applies to this document's own fields only. A field in a frozen copy
that cites a rejected finding (its `finding_ids`) is not on the child's list:
the frozen copy is read-only, so nothing in the child could revise it and it
would block the lock for good. The parent's own list still holds it;
nothing in the child names it yet (`GET /upstream`'s `cited_by` skips the
frozen copies too). **Still open:** whether the owner's "every carried
item" default should count it, which needs a way to settle it in the child
first (as `/verdict good` settles a carried bad verdict).

### 7.3 At lock

- The reviewer is shown the findings list. Confirming verifies them (`verify`
  per finding, each written to the layer). Rejecting a finding marks it
  rejected and flags every field that cites it — a new unresolved item, so the
  lock waits until those fields are revised.
- One `approve` decision records the exact version and content hash. The seal
  (W5-Z1) is taken over that.
- Carried upstream content inside the file is accepted with it — it is the
  frozen copy that was reviewed.
- **Parents are not marked accepted.** Each gets a `backref` (§7.4).
- A contest resolved in a child cannot flow back into the child's frozen copy
  or into the parent. The child records the resolution (§8, `/resolve`); the
  parent gets a `backref` "resolved in <doc>" and must still resolve it itself
  before it can lock.

### 7.4 Back-references

A parent learns what its children did with what they carried, and nothing
else: a `backref` is the only thing a child ever writes to a parent.

1. **When.** In the same request as the child's decision, one `backref` to
   each parent the store holds (found by `lineage.parent_uri` for the direct
   parent, by `document_id` for a hoisted one; one the store does not hold, or
   holds for another tenant, is skipped):

   | The child's decision | Goes to | `action` | `targets` |
   |---|---|---|---|
   | `/approve` | every document in `data.upstream` | `used` | `["<parent id>"]` |
   | `/resolve` of a carried contest | the document the contest is on | `resolved` | `["<parent id>#selection.contested[<ct>]"]` |
   | `/verify` of a finding the direct parent holds | the direct parent | `verified` | `["<parent id>#findings[<fi>]"]` |

2. **Shape.**

   ```yaml
   - id: d_…
     kind: backref
     action: used | resolved | verified
     agent: napkin-host
     actor: human:<id>             # the person in the child's Ctx
     targets: [ … as above … ]
     rationale: Used in locked "<child title>" at sha256:….   # or: Resolved in "<child title>": <chosen>. / Verified in "<child title>".
     from:                          # flatten tail
       document_id: <child document_id>
       decision: d_…                # the child's approve, resolve or verify
       version: sha256:…            # the child's version that decision names
   ```

3. **Allowed on a locked parent.** A backref changes nothing but the parent's
   chain: not its data, members, lock list or lock. It needs no lease (§6).
   The seal (W5-Z1) is over the approved version; a later backref append is
   outside it.
4. A backref settles nothing in the parent. "Resolved in <doc>" is a pointer
   for the parent's reviewer, not a resolution.

Still open: what unsealing means (W5-Z1).

### 7.5 Client review

*Added 2026-09-30.* The owner's design (prototype, version 4; the research
behind it is `docs/research-tool-experiment/client-approval-research.md`,
which this section overrides where they differ). A client's answer to a
locked document, recorded by the agency person it reached — pasted from an
email, attached as a file, or noted from a call.

1. **An OS feature, not an app's.** Client review is a global mode of the
   shell, like edit mode. Every app gets it the same way; an app only declares
   its parts (item 3).
2. **Only on a locked document** (§7.1) with no part reopened (§7.5.6).
   `POST /client-review` is refused `409` otherwise. It does not unlock the
   document: it appends to the chain and changes nothing else. It needs no
   lease (§6), as a backref does not (§7.4).
3. **Parts are the app's.** A brief's fields, a report's sections, later a
   deck's slides: the app hands the shell a list of `{address, label}` and the
   shell sends it with each review (§8.2). How the view hands the list over is
   the bridge's (the UI, built elsewhere). The host checks every address and
   computes each part's value hash from the document; it never takes a hash
   from the body.
4. **Its own kind, `client_review`.** Not `approve`: that is the agency
   accepting its own work, and it is the lock (§7.1). Not `verdict`: verdicts
   feed the quality dossier, and a reasoned good verdict answers a bad one
   (§7.2, item 5), so a client's "accepted" would silently clear the Judge's
   objection. A `client_review` never carries `polarity` or `reason_code` —
   `Decision::is_verdict` counts any decision with a `polarity` as a verdict.
5. **Recorded by staff, about a client.** The recorder is the person in `Ctx`
   (`403` for a process), as for every review route. There is no client
   actor: `Actor::parse` gains nothing, and a body naming `actor` or
   `recorded_by` is refused `400`. The client is data — a name and an email
   the recorder typed — and nothing checks that the client is that person;
   the record states its evidence (§7.5.2), never more.
6. **Confidential is not a filter here** (owner, 2026-09-29: confidential
   means only "kept out of RAG and the corpus"). Clients see everything, so
   nothing in client review — the records, `/decisions`, the extract's
   rendering (§7.5.8) — filters by a `classify` mark. One rule that is not
   about clients still holds: a part whose value is marked `model: false` is
   sent to Ellis without its value (`napkin.middleware/1` §11), as every
   model call withholds it (`napkin.middleware/1` §10.13, item 6).

#### 7.5.1 Parts and hashes

- **`address`** is a data path on this document in dotted keys — the paths
  `/edit` can write, since "Make this change" answers a request with an
  `/edit` (§7.5.6). An entity-keyed segment (`sections[s_2]`) is refused
  until `/edit` takes one; a part the recorder could reopen but not edit would
  block locking again for good. Bare, or `<this document id>#<path>`; the host
  records the full form. Refused `400`: another document's id (a carried part is the parent's work, and its
  frozen copy is read-only), `upstream`, `projection`, a member
  (`facts[…]`, `findings[…]`, `sources[…]`), `text[<key>]` (a view's wording
  is keyed by layout and is not stable), two parts with one address, and a
  part inside another (`audience` and `audience.commercial`), which would make
  "that part changed" ambiguous. An address the data does not hold yet is
  allowed: the client can reject a field left empty.
- **`label`**: non-empty, at most 80 characters — what the chip and the
  extract call the part. At most 100 parts per review.
- **Part hash**: `clan_sdk::hash::sha256_prefixed` of the canonical JSON of
  the value at the address — object keys sorted by code point, no
  insignificant whitespace, strings escaped as `serde_json` writes them. A
  field envelope (an object with `value` and `origin`, Contract 3 §2) hashes
  its `value` only: its provenance is not what the client read. A value the
  data does not hold hashes as `null`.
- **`doc_hash`**: `sha256_prefixed` of the `shared/data.yaml` bytes as read,
  the §5.3 form.

A part answer is **stale** when its part's hash now differs from the hash it
recorded. Another part changing does not stale it.

#### 7.5.2 The records

One `POST /client-review` writes, in one change: one **document answer**,
one **part answer** per part the recorder marked, and — when parts were not
marked and there is proof text — one **suggestion** per part Ellis found
(§8.2). Confirming a suggestion writes a part answer; dismissing it writes a
dismissal. "Make this change" writes an `unlock` (§7.5.6).

```yaml
# The document answer — a person's
- id: d_01K…A
  kind: client_review
  action: client_answer
  agent: human:aoife
  actor: human:aoife                   # the recorder, from Ctx
  targets: ["3f2a…"]                   # the bare document id, the form /approve writes
  cites: [d_01K…LOCK]                  # the approve it answers
  covers: document
  answer: rejected
  reasons: [off_brief, tone]
  said: "Honestly this isn't the brief we talked about. The summer line doesn't feel like us."
  client: { name: Jane Murphy, email: jane@acme.ie }
  channel: file
  evidence: { asset: human/assets/re-brief.eml, sha256: "sha256:…", strength: strong }
  seen:
    version: "sha256:…"                # the version the lock's approve names
    doc_hash: "sha256:…"
    parts:                             # every part the app declared, as the client saw it
    - { address: "3f2a…#single_minded_proposition", label: Single-minded proposition, part_hash: "sha256:…" }
    - { address: "3f2a…#audience", label: Audience, part_hash: "sha256:…" }
  rationale: Jane Murphy rejected the document (off brief, tone), from an attached file; recorded by aoife.

# A suggestion — the agent's, counts for nothing until a person confirms it
- id: d_01K…S
  kind: client_review
  action: suggest_part
  agent: find_client_parts
  actor: process:middleware
  handler: find_client_parts@1.0
  backend: …
  targets: ["3f2a…#single_minded_proposition"]
  cites: [d_01K…A]
  covers: part
  review: d_01K…A
  label: Single-minded proposition
  answer: rejected
  quote: "The summer line doesn't feel like us."
  found_by: agent
  seen: { version: "sha256:…", doc_hash: "sha256:…", part_hash: "sha256:…" }
  rationale: "Derived by the agent: Jane Murphy may mean Single-minded proposition (rejected): “The summer line doesn't feel like us.”"
  reasoning:
    decided: Suggested that Jane Murphy's answer is about Single-minded proposition.
    because: [{ point: "The client's words name it: “The summer line doesn't feel like us.”", cites: [d_01K…A] }]
    rejected: []
    only_option: the words are the client's and the part list is the app's; a person confirms or dismisses it
    certainty: { level: medium, why: found by the agent in the client's words; not yet confirmed }
    would_change_if: a person dismisses it
    attention: Derived by the agent. Confirm or dismiss it; only a confirmed part counts.

# A part answer — a person's: marked in the review, marked later, or a confirmed suggestion
- id: d_01K…P
  kind: client_review
  action: client_answer_part
  agent: human:aoife
  actor: human:aoife
  targets: ["3f2a…#single_minded_proposition"]
  cites: [d_01K…A, d_01K…S]            # the document answer, and the suggestion it confirms
  covers: part
  review: d_01K…A
  label: Single-minded proposition
  answer: rejected
  client: { name: Jane Murphy, email: jane@acme.ie }   # copied from the document answer
  found_by: agent                      # person, when the recorder marked it
  suggestion: d_01K…S                  # found_by agent only: this decision is the confirmation
  quote: "The summer line doesn't feel like us."        # found_by agent only
  seen: { version: "sha256:…", doc_hash: "sha256:…", part_hash: "sha256:…" }
  rationale: aoife confirmed that Jane Murphy rejected Single-minded proposition.

# A dismissal — a person's
- { id: d_01K…X, kind: client_review, action: dismiss_part, actor: human:aoife,
    targets: ["3f2a…#audience"], cites: [d_01K…S2], covers: part, review: d_01K…A,
    suggestion: d_01K…S2, rationale: "Dismissed: she means the proposition, not the audience." }
```

| Field | Type | Rule |
|---|---|---|
| `covers` | `document` \| `part` | The owner's `scope`, spelled `covers`: `scope` on a decision is the org and brand from `Ctx` (§3, `DecisionScope`), and a string there would not parse — the whole chain would fail to read |
| `answer` | `accepted` \| `accepted_with_changes` \| `rejected` | Required on a document answer, a suggestion and a part answer. A part's answer may differ from the document's ("love the proposition, the audience is wrong") |
| `reasons` | list of `off_brief` · `wrong_audience` · `tone` · `facts_wrong` · `budget` · `other` | Document answer only, `rejected` only (`400` otherwise). Optional; no repeats, the recorder's order. Absent when empty |
| `said` | string | The client's words, **verbatim**: stored exactly as sent, never trimmed inside, summarised, rewritten or compressed (`compress_chain` rewrites `rationale` only; `decision.rs`, the `rationale` field). All-whitespace is absent. At most 20,000 characters. With `call` it is the recorder's note of what the client said, and every reader says so |
| `client` | `{name, email?}` | Required on a document answer, copied onto each part answer. `name` non-empty, at most 120 characters. `email`, when given, lower-cased, one `@` with something either side. Data, not identity |
| `channel` | `pasted_email` \| `file` \| `call` \| `none` | `pasted_email` and `call` need `said`; `file` needs an asset; `none` takes neither. An asset means `file` |
| `evidence` | `{asset?, sha256?, strength}` | Host-written; the body sends `asset` only. `asset` is `human/assets/<name>` in this document, a `.eml` or `.pdf`, stored first with `/upload-asset` (which a lock does not refuse); `sha256` is of its bytes. `strength` is `strong` with an asset, `weaker` without — pasted text, a call note, or nothing |
| `seen` | `{version, doc_hash, parts[]}` on the document answer; `{version, doc_hash, part_hash}` on a part record | `version` is the one the lock's `approve` names: every answer is tied to the locked version. `parts[]` is `{address, label, part_hash}` for every part the app declared |
| `review` | decision id | Part records: the document answer they belong to |
| `label` | string | Part records: the part's label as the app declared it |
| `found_by` | `person` \| `agent` | Part answers and suggestions. `agent` on a part answer means a person confirmed a suggestion: the part answer is itself the confirming decision, and `suggestion` names what it confirmed |
| `suggestion` | decision id | A part answer with `found_by: agent`, and a dismissal |
| `quote` | string | Suggestions, and part answers confirming one: the sentence Ellis found, a verbatim substring of the proof (`napkin.middleware/1` §11), at most 500 characters. Never compressed |
| `recorded_by` | — | Not stored. It is the decision's `actor`, from `Ctx`, like every decision's; reads show it as `recorded_by` (§8.2). Never from the body |

What counts: a document answer, and part answers. A suggestion or a
dismissal is never an answer — "derived by the agent" until a person
confirms it, and a confirmed one is a part answer like any other. The
document answer and the client's words always count, whatever happens to the
suggestions.

#### 7.5.3 The state of each part

For a part address `A` on this document, the **current answer** is the
newest of: a part answer targeting `A`, and a document answer `accepted`
whose `seen.parts` holds `A` ("accepted needs nothing else and marks every
part accepted on that version"). A newer document answer that is not
`accepted` and names no part leaves `A` at its last answer.

| State | When |
|---|---|
| `accepted`, `accepted_with_changes`, `rejected` | The current answer's `answer` |
| `stale` (flag) | `A`'s hash now differs from the one the answer recorded (`seen.part_hash`, or the entry in `seen.parts`) |
| `answered` (flag) | `accepted_with_changes` or `rejected`, and a person's `edit` touching `A` (it, or a path inside it) came after the answer. Every `/edit` says why — it is refused without a rationale — so any such edit is "edited with a reason" |
| `reopened` (flag) | An `unlock` targets `A` after the newest lock (§7.5.6) |

The document's own state is its newest document answer: `current` when its
`seen.version` is the lock's version now, and `parts_known` when a part answer
names it as its `review`.

A carried `client_review` (it targets a parent's address) is the parent's
record: it shows in the history and counts in none of this — no part state,
no lock item, no reopen.

A suggestion still open when the document is locked again is closed by that
lock: `/approve` writes, after its `approve`, one `dismiss_part` per open
suggestion, by the person locking, with `closed_by: <the approve>` and a
rationale saying the new lock closed it, so it leaves `client.suggestions` and
asks for nothing (owner rule, 2026-09-30).

#### 7.5.4 Lock rules — §7.2 item 6

A client's rejection not yet answered. Only locking **again** can meet it (a
client review exists only on a locked document), but `/decisions` lists it
from the moment it is recorded, so the view shows what the next lock needs.

- **a.** A part whose current answer is `rejected` and not `answered`:
  "Jane Murphy rejected Single-minded proposition. Edit it, saying why, before
  locking again." (`client_rejected`, blocks)
- **b.** The newest document answer is `rejected` and no part answer names it:
  "Jane Murphy rejected the document and which parts is not known yet. Mark or
  confirm them, then edit them, before locking again."
  (`client_rejected_parts_unknown`, blocks)
- `accepted_with_changes` **never blocks.** Until answered it is attention:
  "Jane Murphy asked for a change to Audience." (`client_change_asked`)
- An open suggestion is attention, not a blocker: "Ellis thinks Jane Murphy's
  answer is about Audience. Confirm or dismiss it." (`client_part_suggested`)
- A client's `accepted` is never required to lock.

#### 7.5.5 What the edit must be

"Make this change" answers the request with an ordinary `/edit` on the part,
reason pre-filled "<client> asked: …", which carries `answers: <part answer
id>` (§8.2). The link is for the reader; the rule in §7.5.3 does not need it.

#### 7.5.6 Reopening one part: `unlock`

The lock is file-wide (§7), and an edit needs an unlocked document. "Make this
change" therefore reopens **one part** with a decision of a new kind, and
nothing else:

```yaml
- id: d_01K…U
  kind: unlock
  action: reopen_part
  agent: human:aoife
  actor: human:aoife
  targets: ["3f2a…#single_minded_proposition"]
  cites: [d_01K…P]
  answers: d_01K…P                     # the part answer it reopens for
  lock: d_01K…LOCK                     # the approve it reopens under
  rationale: "Jane Murphy asked: The summer line doesn't feel like us."
```

1. **Only for an open client request.** `POST /client-review/reopen {answer}`:
   a person, holding the lease (§6), on a locked document; `answer` is a part
   answer that is its part's current answer, `accepted_with_changes` or
   `rejected`, not `answered`, on a part not already reopened. Anything else
   is `409`. A document answer with parts unknown reopens nothing: mark or
   confirm a part first.
2. **While a part is reopened** — an `unlock` on it newer than the newest
   `approve` — `/edit` of that address or a path inside it is allowed despite
   the lock. It is `not_locked`'s one exception. Every other write stays
   refused: `/edit` elsewhere, `/edit-text`, the members, jobs, the review
   routes, and `/client-review` itself (a client answers a locked version, and
   with a part reopened the document is no longer the version the lock
   accepted).
3. **Locking again.** `/approve` is allowed on a locked document that has a
   reopened part (still `409` on one that has none). It runs the whole §7.2
   list, item 6 included, writes a new `approve` naming the new version, and
   sends backrefs as any `/approve` does (§7.4). The newest unsuperseded
   `approve` is the lock; the older one is not rewritten. The new `approve`
   closes every reopened part, edited or not.
4. **Nothing else changes.** §7.1 is unchanged; there is no whole-document
   unlock (what unsealing means stays W5-Z1's).

Why this, and not an `/edit` permitted on a locked document for a part with
an open request:

- **Honest.** An edit on a locked document leaves the lock claiming it accepted
  content it never saw. With `unlock`, the chain reads, in order: the lock, the
  client's answer, the reopen and why, the edit, the new lock. The seal
  (W5-Z1) is over the approved version, and is simply out of date until the
  next `approve`.
- **Smallest.** One kind, one exception in `not_locked`, one relaxation in
  `/approve`. The lock predicate does not change.
- **Scoped to what the client asked about.** The other parts stay exactly as
  the client saw them, so their answers do not go stale.
- **A cancelled edit is harmless.** A reopened `rejected` part still blocks
  the next lock (item 6a) until it is edited; an `accepted_with_changes` part
  does not, and the next lock closes it.

Rejected: unlocking the whole document (superseding the `approve`). It reopens
every part, including those the client accepted, for a request about one.

#### 7.5.7 Spin-off

Client reviews travel with the chain (§5.2, item 4) and keep their addresses.
In the child they are the parent's (§7.5.3, last paragraph).

#### 7.5.8 In the extract

The extract (`docs/contracts/clan-extract.md`, in design in another branch;
not in this one) renders client reviews. The rules it must keep:

- **One block per document answer, scoped to that client.** Two clients'
  answers are two blocks; they are never merged or counted together.
- The block says: the overall answer and the version it answers; the client's
  words as a quote, **verbatim** (never paraphrased; the recorder's note of a
  call is introduced as such); the reasons; the evidence strength in words; who
  recorded it and when.
- **Part lines only for part answers** — marked by a person, or a suggestion a
  person confirmed. Never an unconfirmed suggestion, a dismissal, or the parts
  an `accepted` document answer implies (the overall line says "accepted").
  Each part line says whether it is stale and whether it was answered.
- Evidence strength in words: `strong` — "from an attached file (<name>)";
  `weaker` — "from pasted email text, not verified", "on a call, as noted by
  <recorder>", or "no evidence attached; recorded by <recorder>".
- No filter by confidentiality (item 6 above).

> **Client review** of revision `sha256:9c1e…`: **rejected** (off brief,
> tone). Jane Murphy (jane@acme.ie), from an attached file (re-brief.eml),
> recorded by aoife on 30 Sept: "Honestly this isn't the brief we talked
> about. The summer line doesn't feel like us."
> - Single-minded proposition: rejected — found by the agent, confirmed by
>   aoife: "The summer line doesn't feel like us." Edited since.

Still open: recording an answer against an earlier lock than the newest one;
when the client said it (`said_at`), as against when it was recorded; DKIM
checking of an attached `.eml`, which would make "strong" mean more than
"attached"; and text extraction of `.eml` attachments — the host extracts
PDF and plain text only today (`ops/mod.rs`, `extract_text`), so an `.eml`'s
words reach Ellis only when the recorder pastes them as `said`.

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
  `/verify`, `/approve`, `/history`, `/timeline`, `/open`, `/stale`,
  `/upstream` (§8.1), and `/client-review`, `/client-review/confirm` and
  `/client-review/reopen` (§8.2).
- Landed (2026-09-28), each a person's decision — a process is refused `403` —
  recording one decision as the person in `Ctx` with what it changes, in one
  generation; refused `409` once the document is locked (a reopened part's
  `/edit` and locking again excepted, §8.2):
  - `/verdict {target, polarity: good|bad, rationale, reason_code?}` — on a
    finding, `bad` rejects it (`status: rejected`, `rejection`), `good` is
    refused: a finding is verified. A bad verdict needs its rationale.
  - `/classify {target, model, export, corpus, rationale}` — the mark is
    recorded; taking it to the fact's licence in the layer (§4) is not built.
  - `/resolve {contest, chosen, rationale}` — the contest resolved; a chosen
    value not yet pinned is pinned from the copy the research froze beside it,
    and a pin of the same identity it replaces is kept, marked `replaced_by`.
  - `/verify {finding, rationale?}` — the host asks the middleware's
    `verify_finding` to write the finding to the layer (a `human:<id>` source,
    a synthesis fact under the decision id the host chose), then pins it and
    records the verification. No middleware, no verification.
  - `/edit {path, value, gate?, rationale?}` — a person's edit (the shell's
    Edit mode). One pinned `edit` decision; a campaign field's envelope
    becomes `origin: stated`, `by` the person. Members and the projection are
    not edited here.
  - `/edit-text {key, html}` — a person's wording for a piece of a view's
    text, kept in `shared/edits.yaml` (role `edits`) and handed to the view as
    `window.__CLAN__.edits`. One pinned `edit` decision on `text[<key>]`.
  - `/correct {fact, value, source_uri?, rationale}` — a person corrects a
    fact. The middleware's `correct_fact` writes the value to the layer with
    the person (or their link) as source; the host pins it, keeps the old pin
    marked `replaced_by`, and records a pinned `edit`. Like `/verify` and
    `verify_finding`, it resolves for any document, whatever pipeline it was
    made with.
  - `/acknowledge {decision, rationale?}` — "Looks right": a good verdict
    that cites the decision, which clears what its agent flagged or was unsure
    of (§4) without touching what it wrote. Refused on a finding (verify it).
  - `/approve {rationale?}` — refused while anything on the §7 list is open;
    otherwise one `approve` decision naming the exact version accepted. What
    locking does to other writes (`/patch-data`, a job) is W5-Z1's.
- `/decisions` (landed): every decision, newest first, with who made it, its
  targets as labels, its cites resolved, and what needs a person — derived by
  the host from the chain, the members and the data: `reasoning.attention`,
  low certainty, and the §7 lock list. The shell renders it as the OS layer's
  decision blocks. `/open` above names the lock list alone; it is not built,
  and the path is taken today by the launcher's open-a-document route.
- On the server, `/api-proxy` becomes a dispatch into the handler registry. The
  desktop keeps the transport; it does not assemble prompts.

### 8.1 Upstream-aware routes

*Changed 2026-09-29.* The review routes refused any target on another
document, so a child could not settle what it carried (§7), and nothing told a
child that its parent had moved on (§5.5). What follows is added; no existing
body or reply changes meaning.

1. **Ancestor addresses.** `/verdict`, `/classify`, `/resolve` and `/verify`
   accept a target `<id>#<path>` when `<id>` is this document's id or a key of
   `data.upstream`; any other document id is `400`, as before. The rule: **a
   decision targets what it changes; where it changes nothing — the frozen
   copy — it targets the upstream address.**
   - `facts[…]`, `findings[…]` and `sources[…]` are this document's members,
     whatever the prefix: the entry is looked up in the child's merged member
     (§5.2), what changes is the child's copy, and the decision targets
     `<this id>#…`.
   - Any other path under an ancestor is looked up in
     `data.upstream.<id>`; the decision targets `<id>#<path>` and the frozen
     copy is not written. `/verdict good` with a rationale on such a target is
     how a carried bad verdict is overridden (§7.2, item 5).
   - `/acknowledge {decision}` takes a decision id, own or carried, as today.
2. **`/resolve {contest, chosen, rationale}`.** `contest` is a `ct_` id or an
   address. It is looked up in `data.selection.contested`, then in each
   `data.upstream.<id>.selection.contested`. For a carried contest:
   - refused `409` when the frozen entry is not `open`, or a `resolve` in this
     chain, not superseded, already targets it;
   - the chosen value's pin comes from the frozen `values[].pin` and is
     written to **this document's** facts member, with the `replaced_by` rule
     as today;
   - the decision targets `<id>#selection.contested[<ct>]`, plus the
     `<this id>#facts[…]` it pinned or replaced; the frozen entry is left as
     it was;
   - the parent gets a `backref` (§7.4).
3. **`/verify {finding}`** of a finding the direct parent also holds: as
   today, plus a `backref` to the parent. **`/approve`**: the §7.2 list,
   carried items included; then a `backref` to each document in
   `data.upstream` the store holds. The reply of each of the three gains
   `backrefs: [{document_id, decision}]`, one per backref written.
4. **`upstream` is read-only.** `/patch-data`, `/patch`, `/edit` and a
   middleware `data_patch` that name `upstream` or anything under it are
   refused `400` ("upstream is the frozen copy of <id>; it is read-only"),
   as `projection` is.
5. **`/spinoff {app_id, title?, map?}`** — body and reply unchanged. The
   target app's `app.spinoff.upstream` decides the carry (§5.1). Refused
   `422` when `upstream` is declared and a `map` is given; refused `403` when
   the source document and `Ctx` are different tenants (the host passes `Ctx`
   into the spin-off; a library read never crosses tenants).
6. **`GET /upstream`** — what changed upstream since this document was spun
   off. A read: any actor, locked or not. `200` on every open document; one
   with no `lineage.carried` answers `{document_id, carried: null, upstream: []}`.

   ```json
   {
     "document_id": "9c1e…",
     "carried": { "document_id": "3f2a…", "data_sha256": "sha256:…",
                  "facts_sha256": "sha256:…", "findings_sha256": "sha256:…",
                  "sources_sha256": "sha256:…", "last_decision": "d_01JA…" },
     "upstream": [
       { "document_id": "3f2a…", "direct": true, "in_store": true,
         "title": "Lúnasa 0.0 launch", "app_id": "ie.napkin.campaign-research",
         "version": "sha256:…", "locked": false,
         "status": "changed",
         "changed": { "data": true, "facts": true, "findings": true, "sources": false },
         "decisions_since": 4,
         "pins": [ { "id": "f_…", "change": "added | replaced | changed", "label": "…", "replaced_by": "f_…" } ],
         "findings": [ { "id": "fi_…", "change": "added | verified | rejected", "statement": "…",
                         "reason": "…", "cited_by": ["9c1e…#insight"] } ],
         "contests": [ { "id": "ct_…", "change": "opened | resolved", "key": "…",
                         "chosen": "f_…", "resolved_here": false } ] },
       { "document_id": "77ab…", "direct": false, "in_store": true,
         "title": "…", "app_id": "…", "status": "not_compared" }
     ]
   }
   ```

   - `upstream[]` has one entry per key of `data.upstream`, the direct parent
     (`carried.document_id`) first. Only the direct parent is compared; a
     hoisted one is `not_compared` (§5.3, item 2). A parent the store does not
     hold for this tenant is `{document_id, direct, in_store: false, status:
     "unknown"}`.
   - `status` is `current` when each `carried` hash equals the
     `sha256_prefixed` of the parent's entry now (absent on both sides is
     equal), else `changed`. `changed` names which hashes differ. When
     `current`, `pins`, `findings` and `contests` are `[]`.
   - `decisions_since`: the parent's decisions newer than
     `carried.last_decision`; `null` when that id is absent or no longer in
     the parent's chain.
   - `pins`, by id, the parent's facts member now against this document's:
     `added` (only the parent holds it), `replaced` (the parent's copy has
     `replaced_by`, this one's has not; `replaced_by` its value), `changed`
     (`value`, `unit`, `as_of` or `status` differ). A pin only this document
     holds is its own and is not listed.
   - `findings`, likewise: `added`; `verified` (the parent's is `verified`,
     this one's `proposed`); `rejected` (the parent's is `rejected`, this
     one's is not; `reason` the parent's `rejection.reason`). `cited_by`, on
     every entry, lists this document's fields that cite the finding, by the
     §7.2 item 4 rule. A view lists `rejected` entries first.
   - `contests`, the parent's `selection.contested` now against the frozen
     `data.upstream.<id>.selection.contested`: `opened` (open in the parent,
     absent from the frozen copy), `resolved` (open when frozen, resolved in
     the parent; `chosen` the parent's pick; `resolved_here` whether this
     document has resolved it too).
   - Lists are sorted by `id`. `label` is the pin's readable name, as
     `/decisions` labels a target.

### 8.2 Client review routes

*Added 2026-09-30.* The routes behind §7.5. Each is a person's (`403` for a
process), records as the person in `Ctx`, and writes one change.

1. **`POST /client-review`** — the document-level answer, plus any parts the
   recorder marked.

   ```json
   {
     "answer": "accepted | accepted_with_changes | rejected",
     "client": { "name": "Jane Murphy", "email": "jane@acme.ie" },
     "reasons": ["off_brief", "tone"],
     "channel": "pasted_email | file | call | none",
     "said": "…",
     "asset": "human/assets/re-brief.eml",
     "parts": [ { "address": "single_minded_proposition", "label": "Single-minded proposition" },
                { "address": "audience", "label": "Audience" } ],
     "marked": [ { "address": "audience", "answer": "rejected" } ]
   }
   ```

   - Refused `409` unless the document is locked with no part reopened
     (§7.5, item 2). Refused `400` for a field that breaks §7.5.2's table, a
     part that breaks §7.5.1, a `marked` address not in `parts`, a part marked
     twice, any `marked` with `accepted` (it marks every part already), and a
     body naming `actor` or `recorded_by`. `404` when `asset` is not in the
     document.
   - `parts` is the app's whole list, every time: it is what `seen.parts`
     records. It may be empty — an app that declares no parts gets the
     document answer alone.
   - **Asking Ellis.** When `answer` is not `accepted`, `marked` is empty,
     `parts` is not, and there is proof text, the host asks the middleware's
     `find_client_parts` before it writes (`napkin.middleware/1` §11), as
     `/verify` asks `verify_finding`: `client_review_request` builds the ask,
     the shell sends it, and `client_review` writes the answer with what came
     back. The proof text is `said` when there is one, else the asset's
     extracted text (`human/assets/.extracted/<name>.txt`). The host checks
     every suggestion again — its address is one of `parts`, its answer one of
     the three, its quote a verbatim substring of that proof text by the §11
     rule, one per address — and drops any that fails. No middleware, a
     middleware error, or a call over its bound: the answer is recorded without
     suggestions, and the reply says so. The recorder can still mark parts by
     hand (item 2).
   - Writes the document answer, a part answer per `marked` entry
     (`found_by: person`), and a suggestion per surviving suggestion, in that
     order, in one change.

   ```json
   { "ok": true, "decision": "d_…", "parts": ["d_…"],
     "suggestions": { "status": "none | found | unavailable", "decisions": ["d_…"], "dropped": 0,
                      "reason": "why unavailable, when it is" } }
   ```

   `none`: nothing was asked (accepted, parts marked, no parts, no proof).
2. **`POST /client-review/confirm`** — confirm or dismiss a suggestion, or
   mark a part by hand after the fact.

   ```json
   { "suggestion": "d_…", "confirm": true, "answer": "rejected", "rationale": "…" }
   { "review": "d_…", "address": "audience", "answer": "accepted_with_changes" }
   ```

   - With `suggestion`: it must be a `suggest_part` in this chain that no part
     answer or dismissal names yet (`409` otherwise). `confirm: true` writes a
     part answer, `found_by: agent`, with the suggestion's `review`, `label`,
     `quote` and `seen`; `answer` may correct the suggestion's, and defaults
     to it. `confirm: false` writes a dismissal; `rationale` is optional.
   - With `review` and `address`: `review` must be a document answer that is
     not `accepted`, and `address` one of its `seen.parts`, which give the
     label and hash. Writes a part answer, `found_by: person`.
   - Either way, `409` when a part answer for that `review` and address
     exists already: a later answer from the client is a new
     `/client-review`. Allowed while the document is locked, a part reopened
     or not. No lease.
3. **`POST /client-review/reopen {answer}`** — "Make this change" (§7.5.6).
   Writes the `unlock`. The reply gives the shell what edit mode opens with:

   ```json
   { "ok": true, "decision": "d_…", "address": "3f2a…#single_minded_proposition",
     "label": "Single-minded proposition", "answers": "d_…",
     "reason": "Jane Murphy asked: The summer line doesn't feel like us." }
   ```

   `reason` is "<client name> asked: " and then the part answer's `quote`,
   else the document answer's `said`, else its reasons in words, else "a
   change", clipped to 300 characters. It is the `unlock`'s rationale too.
4. **`/edit {path, value, gate?, rationale, answers?}`.** On a locked
   document, allowed only for a path at or inside a reopened part (§7.5.6,
   item 2). `answers`, when given, must be the part answer that part was
   reopened for (`400` otherwise); the `edit` decision records it as
   `answers` and cites it. Unchanged otherwise.
5. **`/approve`** on a locked document with a reopened part locks again
   (§7.5.6, item 3). Body and reply unchanged.
6. **`GET /decisions`** gains `client`, and `lock` gains two fields. A read:
   any actor, locked or not.

   ```json
   "lock": { "can_lock": false, "blockers": 1, "locked": true,
             "reopened": [ { "address": "3f2a…#single_minded_proposition", "label": "Single-minded proposition",
                             "decision": "d_…U", "answers": "d_…P" } ] },
   "client": {
     "available": false,
     "answer": { "decision": "d_…A", "answer": "rejected", "reasons": ["off_brief", "tone"],
                 "said": "…", "client": { "name": "Jane Murphy", "email": "jane@acme.ie" },
                 "channel": "file", "evidence": { "asset": "human/assets/re-brief.eml", "sha256": "sha256:…", "strength": "strong" },
                 "recorded_by": { "id": "aoife", "name": "aoife" }, "at": "2026-09-30T10:20:03Z",
                 "seen": { "version": "sha256:…", "doc_hash": "sha256:…", "parts": [ … ] },
                 "current": true, "parts_known": true },
     "answers": [ "… every document answer, newest first, in the shape of `answer`" ],
     "parts": [ { "address": "3f2a…#single_minded_proposition", "label": "Single-minded proposition",
                  "state": "rejected", "decision": "d_…P", "review": "d_…A", "found_by": "agent",
                  "quote": "The summer line doesn't feel like us.",
                  "client": { "name": "Jane Murphy" }, "at": "2026-09-30T10:21:40Z",
                  "stale": false, "answered": false, "reopened": true } ],
     "suggestions": [ { "decision": "d_…S2", "review": "d_…A", "address": "3f2a…#audience", "label": "Audience",
                        "answer": "rejected", "quote": "…" } ]
   }
   ```

   - `lock.locked`: §7.1 holds. `lock.reopened`: the parts §7.5.6 item 2
     reopens. `can_lock` and `blockers` count §7.5.4 too.
   - `client.available`: `POST /client-review` would be accepted now (locked,
     nothing reopened).
   - `client.answer`: the newest document answer, `null` when there is none.
     `recorded_by` is the actor, labelled as `who` labels a person.
   - `client.parts`: one entry per address that has a current answer
     (§7.5.3), sorted by address. `found_by: document` (a read value; no record carries it)
     marks a part whose current answer is an `accepted` document answer; it
     has no `quote`.
   - `client.suggestions`: suggestions nobody has confirmed or dismissed yet,
     oldest first.
   - The §7.5.4 items are in `attention` with their codes, `address` the
     part's, `decision` the answer's; an open suggestion's item names the
     suggestion.

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
| 2 | What travels at a hop? | Everything but the source app's view, task text, legacy patches, text edits and view state (§5.2) |
| 3 | Filter or partition? | Partition per agency; per-hit scope still recorded (§9) |
| 4 | Can a handler name its own scope? | No. Scope, actor and branch namespace come from `Ctx` (§3, §6) |
| 5 | Approval vocabulary vs rejection? | `approve` is file-wide, bound to a hash; `verdict` is field-scoped with polarity (§4, §7) |
| 6 | What raises confidence; is a human one? | Tier and corroboration only; a human verification is a source (§4) |
| 7 | What is frozen at a spin-off, and checkable how? | The whole upstream, at `data.upstream.<id>`; `lineage.carried` holds its hashes and last decision id, and `GET /upstream` compares them (§5.3, §8.1) |
| 8 | What runs under someone else's lease? | See the lease table (§6) |
| 9 | What is parallel; does its target append? | Research and per-field drafters on branches; the chain appends by row (§6) |
| — | How does a client's answer reach the document? | A `client_review` on the locked version, recorded by staff with its evidence strength; per-part state by value hash; a rejection blocks locking again; "Make this change" reopens one part (§7.5, §8.2) |

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
