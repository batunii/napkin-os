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
example/shared/data.yaml            campaign.*, selection.*, materials, intake, report, projection
example/shared/facts.yaml           18 pins, brand + category layers, 2 stale
example/shared/findings.yaml        5 findings: 1 verified, 3 proposed, 1 rejected
example/agent/decision-chain.yaml   23 decisions, newest first
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

## The view (`index.html`)

One page, rendered from `window.__CLAN__.data` and the chain (`clan://chain`).
Top to bottom: the report, the request (how the campaign started), the gates, then the ask, facts,
findings, selection and history. The page carries no platform brand: the OS
shell's bar brands it; the page shows "Research Tool" as a small title.

**Starting a campaign (Contract 3 §16, middleware-api §8).** An empty document
opens on the intake, the way Brief Maker does: a prompt box, attach any number
of files (picker or drop), one primary action, **Start research**. On it the
view:

1. uploads each file (`clan.uploadAsset(name, bytes, 'human')`) and hashes it
   (`sha256:` of the bytes);
2. writes, as ONE human `patch-data`, the prompt as a `kind: prompt` material
   (sha256 of its UTF-8 text), one material per file, the user message
   `intake.messages.<msg_id>` (`by`, `at`, `material_id`, `attachments`), and
   `campaign.id` / `campaign.ask_source` (the first file) / `campaign.name`
   (when typed) as `stated`;
3. sends `start_campaign {prompt, attachments: [{material_id, name, sha256}]}`
   (`name` is the asset name, so the host splices in the extracted text), then
   polls `job_status` (free) until `done` or `failed`.

`by` is the host's actor, read from the chain: the upload is attributed to the
person, so with a file attached the id is known before the message is written.
The host does not otherwise say who the viewer is, so with no file and no
earlier human decision the id comes from a small disclosure under the form
("Your user id, if the host cannot tell who you are"), which opens by itself
when it is needed. When the host gains a "who am I" route, that field goes.

**The request, shown as a form, not a chat.** `intake.messages` is read, not
drawn as a thread. Once submitted, the start screen becomes three cards:

- *Your request* — the first user message as a filled form: the prompt, the
  attachments as chips, the working name, who submitted it and when; then
  one row per field the agent asked about, holding the latest answer
  ("Subject brand · Lúnasa · confirmed by you", or "stated by you"), and any
  words the person gave before the agent turned them into choices.
- *Progress* — the six stages from `job.stage` / `job.progress`, the current
  stage's agent figure (the shell's `AgentFigure`, from the shared snippet
  `app/templates/shared/agent-figures.html` that the packer inlines at the
  `<!-- @napkin:agent-figures -->` marker; regenerate it with `npm run
  figures`), drawn on the Plan ground while it works, and one
  status line updated in place ("Research is researching Ireland and Great
  Britain…"), under it the stage's latest narration. Every agent message is
  kept, as plain lines, in a collapsed *Activity* disclosure; the job's
  `result.messages` not written yet are marked *in flight*.
- *The question* — only while the job is `needs_input`.

On open, the view polls the newest job once, unless its report has landed.

**The agent asks.** `job.question` shows as a focused card attributed to the
stage's agent: its figure and name ("Identify asks"), the question as the
heading, the agent's message as context, and form controls — one button per
option with its origin ("You said this" with the quote, "We inferred this"
with the pins, "You typed this"), the escape, and a labelled text field when
`allow_text`. The card takes the focus once when it opens. A pick is the
person's write, before `answer_question` (§16.3): the answer message and the
field at `address`, `confirmed` from the option's origin (keeping `source` /
`fact_ids`) or `stated`. Free text writes only the message; the middleware
asks again with candidates. Once answered, the card goes and the answer is a
row in *Your request*.

**The report (§17)** is read-only: headline, summary, sections of `claim`
(cites as chips linking to the pin or finding), `pins`, `finding`, `gap` and
`contest` blocks, the confirm list (each item jumps to its field's Confirm,
or confirms in place), and `not_researched`. Every string is escaped. It is marked out of
date when `based_on`'s hashes differ from `projection.built_from`, or a
decision after the one that wrote the report, in the chain's order (never
by the clock), touches `campaign.*` / `selection.*`; **Refresh report**
sends `compose_report`. A locked report is the version agreed to: it is never
marked out of date, and Refresh report is not offered.

The document title (`clan.setTitle`) is the subject brand and its markets
("Lunasa · Ireland and Great Britain"), else the working name
(`campaign.name`), else the brand. It is set while the title is one nobody
chose (the app's default, "Untitled…", the working name, or one this rule
made); a title a person gave it is left alone. A brief spun off from the
research names the research by that title. The export
(`clan:export`, Path B) carries the report, the request with its answers and
the progress, and drops the start screen, the open question and every control.

