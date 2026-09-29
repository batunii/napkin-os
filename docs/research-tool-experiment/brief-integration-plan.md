# Brief Maker in the evidence system: the plan

Planned 2026-09-29 from a read of this branch at 954106e. It has two parts:

- **A.** Brief Maker's parts become clan fields with evidence.
- **B.** A brief can be started from a research document (spin-off).

Research feeds the brief through the spin-off. The writing agents then read the research's **extract** (docs/contracts/clan-extract.md, in design), not raw facts.

## Defaults taken (the owner may reverse them)

1. **Unchecked findings are cited only.** They appear in the drafted fields (insight, SMP, RTB, desired response), marked "derived by the agent", with certainty at most medium. They never fill a field captured from the client's material, and never enter capture.
2. **When the parent research changes, the carried copy stays frozen.** The brief shows "The research has changed since this brief started" and lists what changed, including findings rejected upstream. Taking the newer research is an explicit, later action.
3. **The lock needs every carried item settled**, including ones the brief does not cite.
4. **Cost.** Each writing step gets only the evidence it needs, chosen in code. Prompt caching waits for a measured run.

## Bugs and gaps found

1. **`spinoff()` drops the evidence members.** instantiate.rs:437-447 copies the template entries and the source's human/assets only. facts, findings and sources are lost, and so are `agents/**` branches. `lineage.carried` is missing.
2. **Research data is folded in at the root.** With map None the research data lands at the root of the brief: research `materials` collides with the brief schema, and `projection` comes along too. os-layer §5 wants the data frozen under the source's document id.
3. **A child of a locked parent is born locked.** `lock_of` (review.rs:195-200) and `lockDecision` (clan-fields.html:557) count a carried `approve`.
4. **A child cannot settle what it carried.** `target_path` refuses targets on another document (review.rs:231-238), and `/resolve` looks at the root selection.contested only. Backrefs are never written.
5. **A brief field citing a rejected finding is not flagged.** `flagged_field` only looks at envelope `finding_ids`.
6. **The decision panel's cite resolver does not know `cap_` / `psg_`.**
7. **clan-fields cannot yet serve brief fields.** It has no single-segment paths, no provenance taken from the writing decision, no proposals, and it reads contests only at the root.
8. **Brief Maker has no fields snippet, and it locks with `patch('locked', true)`**, which bypasses the lock list.
9. **Drafters don't see findings.** `fi_` is not in `known()`.
10. **model:false classify marks do not reach the drafters.** All pins are sent.
11. **The brief schema has no `projection`, `upstream` or member schemas.**

## Work packages

No two packages share a file. Package 0 owns every contract document.

**P0: contracts (docs only), first.**
- **os-layer.md §5.**
  - Frozen data lives at `upstream.<src document_id>`, written by the host and read-only. A multi-level upstream is hoisted as siblings.
  - Members are merged into the child's own, and the child verifies, rejects or resolves its copies.
  - `lineage.carried = {data_sha256, facts_sha256, findings_sha256, sources_sha256, last_decision}`.
  - A lift is an `edit` decision with action `seed`, agent `napkin-spinoff`, actor `process:spinoff`.
  - `shared/edits.yaml` and `human/patches.yaml` do not travel.
- **os-layer.md §7.**
  - A lock counts only an `approve` whose target is this document.
  - A backref is appended to the parent on the child's `/approve`, `/resolve` or `/verify` of a carried item, and is allowed even when the parent is locked.
- **os-layer.md §8.** The review routes accept ancestor addresses. Add `GET /upstream`.
- **middleware-api.md §10.**
  - `clan.findings` is read.
  - Drafters may cite `fi_`. Capture stays free of RAG and of research.
  - An `approve` in the chain counts as the lock.
  - An `fi_` cite resolves in findings. An unverified finding caps certainty at medium and adds attention "rests on a finding not yet verified". A figure point that cites a finding also cites that finding's pins.
  - New §10.13, research-fed briefs: which evidence each loop gets; model:false never goes into a payload.
- **clan-fields.md.**
  - Refs `psg_`, `cap_` and `mat_`, and single-segment data paths.
  - Provenance comes from the current writing decision.
  - "Use the proposal".
  - `<clan-tray noun>`.
- **peripherals/brief-maker.schema.json.** Add `projection` and `upstream`.

**P1: the SDK spin-off carries everything.**
- Files: crates/clan-sdk/src/instantiate.rs, manifest.rs, and a new tests/spinoff.rs.
- `SpinoffSpec.upstream: bool` defaults to false, so Advertising Studio's `map: brief` is unchanged. Add `Lineage.carried`.
- With `upstream` set:
  - data goes to `upstream.<src id>`, without the source's projection;
  - the source's own `upstream.*` is hoisted;
  - facts, findings and sources are merged by id;
  - `agents/**` and the merge report are copied;
  - each lift gets a seed decision;
  - `carried` is filled.
- Tests:
  - members, assets and the whole chain are carried byte-faithful;
  - no address is rewritten;
  - the root fold is unchanged when `upstream` is false;
  - research → brief → deck keeps both upstreams;
  - the spun-off brief validates.

**P2: host review, lock and cites for bare-value, upstream-aware documents.**
- Files: crates/napkin-host/src/ops/{review.rs, decisions.rs, decisions/tests.rs, edit.rs, middleware.rs}, tests/{review.rs, middleware.rs}.
- The lock is scoped to this document.
- `target_path` accepts `<ancestor>#path` when the ancestor is in `data.upstream`.
- `/resolve` on a carried contest pins the chosen value from the frozen values into the child's facts; its decision targets the upstream address. `/verdict` and `/acknowledge` on upstream targets work the same way.
- The lock list also scans `upstream.*.selection.contested`.
- `flagged_field` fires when a field's current writing decision (not superseded, not a propose) cites a rejected `fi_`.
- The cite resolver learns `cap_`, `psg_` and upstream addresses.
- `upstream` is refused in data_patch, /patch-data and /edit.

**P3: host spin-off plumbing, backrefs and upstream status.** After P1's API.
- Files: crates/napkin-host/src/{library.rs, routes.rs, session.rs, ops/read.rs}, crates/napkin-web/src/api.rs, crates/napkin-wasm/src/lib.rs, tests/{routes.rs, view_from_library.rs}.
- `Ctx` is passed into `spinoff_from`, and a spin-off across tenants is refused.
- A backref goes to each parent in the store on `/approve`, `/resolve` or `/verify` of carried items.
- `GET /upstream`: compares `lineage.carried` with the parent's current state and reports changed pins, findings and contests, and findings rejected there.
- In `clan_context_for_agent`, `data.upstream` is replaced with a small index.

**P4: middleware. Research evidence goes to the drafters only.** Held until the extract grammar is agreed: the drafters will read extract sections.
- Files: server/napkin/brief/{drafters.py, job.py, fields.py}, handlers/{draft_brief.py, regenerate_field.py}, server/tests/test_brief.py.

**P5: Brief Maker packaging and schema.** After P1.
- Files: app/templates/brief-maker/{schema.json, app/pipeline.yaml, context.md, facts.schema.json, findings.schema.json}, crates/clan-sdk/examples/make_brief_maker.rs.
- Add `projection` (as research has) and a read-only `upstream` block.
- Register the empty members and their schemas.
- `AppInfo.spinoff = {accepts: ["ie.napkin.campaign-research"], upstream: true, lift: {}}`, version 0.6.0.
- Self-check: spin off `campaign-research.example.clan`.

**P6: clan-fields snippet logic.** Waits for the restyle, which is editing clan-fields.html.

**P7: the Brief Maker view.** Waits for the restyle.
- clan-field parts and the lock through `/approve`.
- A "from research" state with "Draft from the research".
- The `/upstream` notice.

**P8 (optional):** the research view shows "Used in brief …" backrefs.
