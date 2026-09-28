# Contract 5 — the OS's fields

The components every Napkin app shows recorded data with, and the vocabulary an
agent lays a report out in. One snippet, `app/templates/shared/clan-fields.html`,
spliced into a template at `<!-- @napkin:clan-fields -->` by the packer, after
the agent figures. Owner direction, 2026-09-28: the agent composes freely; the
OS keeps every figure traceable and reviewable.

## 1. The rule

**What a field is, and what it asks of a person, is the record's — never the
tag's.** An agent chooses how a thing looks (`as="big"`, where it sits, what it
is next to). Whether it needs a person's check, shows two disagreeing sources,
is struck through or is confidential comes from the document: the projection
(`projection.pins`, `.findings`, `.sources`), `selection.contested` and
`selection.gaps`, and the host's decision view (`clan://decisions`). A plain
`<clan-field>` around an unverified finding still asks for a check.

What needs a person is the host's list (`/decisions`, `attention` with
`blocks_lock`), shown by `<clan-tray>` whatever the layout shows. An item the
layout leaves out is listed as such and opens in the drawer.

## 2. Elements

| Element | Attributes | Shows |
|---|---|---|
| `<clan-field ref>` | `as`: `inline` (default) · `big` · `stat` · `cell` · `claim`; `caption` | A pin (`f_…`): its value, formatted from its unit, and its source. A finding (`fi_…`): its statement. A contest (`ct_…`): both values while open, the pick once resolved. |
| `<clan-cite refs>` | | A sentence's evidence when it states no figure of its own: who says so ("Retail Pulse +1"); opens the list of what it rests on. |
| `<clan-chart refs kind>` | `kind`: `bar` · `line` · `stack`; `title`, `labels` (comma list), `rest` (stack: the unmeasured remainder, drawn without a number), `compact` | Only pins with a number. A ref that is not one is named in the footer, never drawn. |
| `<clan-quote ref>` | `source` (a `src_` id; default the pin's first) | The pin's verbatim quote from that source. |
| `<clan-gap ref>` | | A `selection.gaps` entry: what was looked for and where. |
| `<clan-sources>` | `title` | Every source the document carries, with how many facts each backs. |
| `<clan-tray>` | | What needs a person, and the lock. |

Every element opens the evidence drawer: the value, the verbatim quote with the
figure marked, the source's record and link, the decisions recorded on it, and
the element the agent wrote. A pin whose source record is not in the document
(pinned before sources travelled) says so.

## 3. Decisions

Each is one host route (Contract 4 §8); the reply carries the document as it
now stands, which replaces `window.__CLAN__.data` and fires `clan:dataupdated`.

| In the drawer | Route |
|---|---|
| Looks right / It's wrong… (a pin) | `/verdict` good / bad (bad needs a reason) |
| Verify (a finding) | `/verify` — refused, with the host's reason, when the layer cannot be written |
| Reject (a finding) | `/verdict` bad on `findings[id]` (reason required) |
| Use this value (a contest) | `/resolve` (reason required) |
| Confidential… | `/classify` — model, export, corpus, and why |
| Lock report (the tray) | `/approve` — disabled while the tray lists anything |

A locked document offers no decisions.

## 4. What an agent may write

Text, the elements above, and: `section div header footer article aside h1 h2
h3 p span strong em b i small br hr ol ul li table thead tbody tr td th figure
figcaption blockquote`. Attributes: `class` (only `cl-*` and `compact`), the
element attributes above, `colspan`, `rowspan`, `data-tone`. Everything else —
script, style, links, images, event handlers, `style=` — is dropped whole, at
render (`ClanFields.layout`), whatever the writer checked before.

The layout vocabulary: `cl-report` (the page), `cl-head` (`compact`),
`cl-eyebrow`, `cl-title`, `cl-dek`, `cl-nums`, `cl-cols`, `cl-split`,
`cl-grid`, `cl-prose`, `cl-figure`, `cl-cap`, `cl-label`, `cl-band`
(`data-tone="soft"`), `cl-block`, `cl-callout`, `cl-row`, `cl-table`,
`cl-list`, `cl-foot`.

## 5. API

`ClanFields.mount(el, html)` sanitises and mounts a layout; `.refresh()`
re-renders every element from the record; `.reload()` re-reads the decision
view first; `.open(ref)` opens the drawer; `.format(pin)`, `.label(pin)`.

## 6. Edit mode

The shell's **Edit** button (every authored app) turns edit mode on; the edit
bridge announces it in the frame as `clan:editmode`. Then:

- A `<clan-field ref="campaign.problem">` — any data path — is a value a
  person owns. It shows its value and where it came from ("from your
  material", "proposed", "you said"); in edit mode a click edits it in place
  (text, a comma-separated list, a named thing), saved through `/edit`.
  Values with more structure keep the app's own rendering.
- A pin is **corrected**, never overwritten: "Correct it…" takes the right
  value, where it comes from and why, through `/correct`. The field then
  shows the new value, marked updated; the old one stays on record.
- Findings are verified or rejected; contests are resolved. Neither is edited.

Every edit is the person's pinned decision: a job proposes around it and
never writes over it.

## 7. The shell and the app

The OS's decision panel (`app/src/shell/decisions/`) talks to the app frame
with three messages, all `postMessage` between the shell and its own frame:

- `clan:open {ref, path}` — show one thing. A pin, finding, contest, gap or
  source opens in the evidence drawer; anything else is dispatched to the app
  as `clan:focus {path}`, for it to scroll to.
- `clan:can-run {tasks}` (app → shell, also on `clan:can-run?`) — the tasks
  the app runs when asked. The panel offers "Research again", "Research a
  skipped lens", "Redo the audience" or "Write the report again" only for
  these, so no button does nothing.
- `clan:run {task, input}` — run one, as the app's own action would.

Judging a value is the drawer's; who did what and why is the panel's. The
panel's "Needs you" only points at where to settle an item.

## 8. Not yet

Campaign fields (`campaign.*` envelopes citing material spans) as refs;
re-reading a pin's source on request; images from the document's assets;
taking a classify mark to the fact's licence in the layer (Contract 4 §4).
