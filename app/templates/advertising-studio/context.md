# Advertising Studio — Film and Advertising Production

We are producing a **short advertising film** and its delivery versions. The
journey is brief → script → storyboard → blocking → picture → sound → edit →
formats → export, and the whole point is that it stays *connected*: an approved
script line must be traceable through the shots and audio that express it, all
the way to the delivered file.

A production is a **set of CLAN containers**, tiered by how often each is
rewritten — not split into "the file" and "the media".

- **`project.clan`** (this one) — hot, rewritten on every edit. The ledger plus
  everything a human edits: storyboard frames, floor plans, start/end frames,
  thumbnail strips, scratch audio, the brand pack.
- **`<shot>.takes.clan`** — one vault per shot, appended when a take lands.
  Holds take media *stored uncompressed*, so it is byte-addressable and streams
  in place. Each vault is a valid `.clan` in its own right.
- **`campaign.clan`** — written once per release. The approved masters,
  captions, timeline manifest, quality report and approval history. The file
  you hand a client, which plays on its own.

Vaults are referenced from `manifest.external[]` by id and `sha256`, and point
back through `lineage.parent_id`. Restoring a project is resolving those refs by
hash — nothing is relinked by filename.

The presentation (`human/index.html`) renders from `shared/data.yaml`; do not
generate or edit HTML. Write structured fields via `patch-data` matching
`agent/output-schema.json`. Every write is attributed and appended to the
decision chain.

## Rules that are not negotiable

- **A change creates a revision; it never overwrites an approved one.** Script,
  blocking, take, timeline and format revisions are append-only. Old approvals
  stay as history.
- **Approvals bind an exact revision and content hash.** An automated review
  advises; it cannot grant human approval, and it cannot silently rewrite brand
  or character references.
- **Retaking one shot must preserve every unrelated take and approval.** Mark
  what a change invalidates in `stale[]` rather than deleting it.
- **Never turn a provider failure into a success.** A `mock` or `scratch` asset
  is labelled as such and blocks final-ready status; it may appear in a clearly
  marked review cut.
- **A file that decodes has passed a technical check, not a creative one.**
  `takes[].checks` separates the two.
- Cost has three separate fields — `estimate`, `reserved`, `confirmed` — and an
  unknown actual cost stays `unknown: true`. Do not invent one.

## Where the stages live

`brief` and `concepts` (step 1) · `script.revisions[].beats[]` with stable beat
IDs (step 2) · `shots[]` (step 3) · `blocking[]` floor plan and recipe (step 4)
· `takes[]` (step 5) · `audio.tracks[].cues[]` (step 6) ·
`timeline.revisions[]` (step 7) · `formats[]` (step 8) · `exports[]` (step 9).

`stage.current` and `stage.next_action` drive the project desk. Keep
`next_action` a single plain-language sentence naming one thing to do.
