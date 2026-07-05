# Handoff: AI Brief Builder

## Overview
An app where AI agents draft a complete creative brief from source material (a client brief PDF, notes, an email thread, a transcript), and a human then edits, regenerates, locks, or augments each section. The brief can be presented in three escalating template modes — **Traditional** (document-style), **Creative** (adds moodboards/territory), and **Full Creative** (campaign theme colors, gradient/image backgrounds, and generated film/radio/moodboard media). Light and dark themes throughout.

The sample content is a real Volkswagen Commercial Vehicles (VWCV) brief, used only as the worked example — the product itself is brief-agnostic.

## About the Design Files
The file in this bundle (`Brief Builder.dc.html`) is a **design reference created in HTML** — a working prototype showing the intended look, flow, and interactions. It is **not production code to ship directly**. The task is to **recreate this design in your target codebase** using its established framework, component library, and patterns (React, Vue, Svelte, etc.). If no front-end environment exists yet, choose the most appropriate framework and implement there.

The prototype is authored as a "Design Component" — a single HTML file whose markup is driven by a small `Component` logic class. Treat that class as a behavior spec, not as code to copy.

## Fidelity
**High-fidelity.** Final colors, typography, spacing, interactions, streaming animations, and both light/dark palettes are all specified below and should be recreated faithfully. Recreate the UI pixel-accurately using your codebase's existing libraries; only substitute primitives (button, toggle, textarea) where your design system already has equivalents.

---

## App Flow (3 views, one screen at a time)

The app is a single-page experience with three sequential views controlled by one `view` state: `'start' → 'generating' → 'editor'`. A top progress bar (2px, animated accent gradient) is fixed to the top whenever any field is mid-generation.

### View 1 — Start / New Brief
- **Purpose**: The front door. User brings in source material, names the brief, picks a template format, and triggers generation.
- **Layout**: Sticky 60px top bar (logo + breadcrumb left, dark-mode toggle right). Centered content column, `max-width: 760px`, `padding: 56px 30px 100px`. Fade-up entrance.
- **Components**:
  - **Eyebrow**: "New brief" — Geist Mono 12px, uppercase, letter-spacing .18em, color `--faint`.
  - **Title**: "Start a brief" — Newsreader serif 44px/1.05, weight 400, color `--ink`.
  - **Lede**: 17px/1.6 Geist weight 300, color `--text-3`, `max-width: 54ch`.
  - **Source material box**: rounded 14px, `1.5px solid --border-2`, bg `--surface`. Contains a borderless `<textarea>` (min-height 148px, 15px/1.6, color `--text-input`) with placeholder "Paste a client brief, meeting notes, an email thread or a call transcript — or attach a file below." Footer row (bg `--surface-2`, top border `--border-soft`): "＋ Attach file" button, an attached-file chip (`client_brief.pdf` with a red `#D24B3E` dot), spacer, and an accent-colored note "✦ 12 sections will be drafted".
  - **Brief name input**: single-line, rounded 11px, `1.5px solid --border-2`, bg `--surface`, 15px Geist, default value "Volkswagen Commercial Vehicles".
  - **Format picker**: 3 equal-width selectable cards (flex, gap 14px). Each card: padding 16px, rounded 13px, `1.5px` border, a mini visual preview (42px tall) + title (14px Geist 500) + one-line description. Selected card: border = accent, bg = accent-soft.
    - *Traditional* — 3 stacked gray lines preview. "Clean, document-style brief."
    - *Creative* — 3 striped bars preview. "Adds moodboards & territory."
    - *Full Creative* — 3 colored dots + a play triangle preview. "Theme, film, audio & mood."
  - **Generate button**: filled accent, white text, rounded 11px, padding 13px 24px, "✦ Generate brief", shadow `0 2px 8px <accent>55`. Followed by helper text "Agents draft all sections in a few seconds." (color `--faint`).

### View 2 — Generating / Drafting
- **Purpose**: Shows agents drafting each section, one per field, as a live checklist.
- **Layout**: Same 60px top bar. Centered column `max-width: 620px`. Fade-up entrance.
- **Components**:
  - **Eyebrow**: "Drafting" in accent color.
  - **Title**: "Agents are writing your brief" — Newsreader 34px.
  - **Sub**: "One agent per section, working from `client_brief.pdf`."
  - **Checklist card**: rounded 14px, `1px solid --border`, bg `--surface`. One row per field (12 rows), each with a status indicator + field label + status word:
    - *queued*: hollow 18px circle (`1.5px solid --border-2`), label color `--faint`, word "queued".
    - *working* (thinking or streaming): 18px spinner (`2px` ring, accent top-color, `spin .7s linear infinite`), label color `--text`, word "writing…".
    - *done*: 18px filled accent circle with white "✓", word "done".

### View 3 — Editor
- **Purpose**: The core workspace. Every brief section is editable; per-field AI controls; template-mode switching; (Full Creative) background + media controls.
- **Layout**: Sticky 60px top bar with blur (`backdrop-filter: blur(12px)`, bg `--chrome`). Left: logo + "＋ New brief" button. Center: pill segmented control (Traditional / Creative / Full Creative) on a `--track` background. Right: dark-mode toggle + "✦ Regenerate all" (filled accent). Below (Full Creative only): a secondary toolbar. Then the brief canvas, centered, `max-width: 880px` (Traditional) or `1080px` (Creative/Full), `padding: 0 30px 120px`.
- **Brief header**:
  - Traditional/Creative: provenance chip ("✦ Generated from client_brief.pdf", accent-soft pill, only if `showProvenance`), "Creative Brief" eyebrow, an editable title `<input>` styled as Newsreader 50px, and a subtitle "Radio · OOH · Digital — Irish market — peak sales period".
  - Full Creative: a 46×3px accent rule, the brief name as accent eyebrow, and the **proposition (SMP) rendered as a large italic Newsreader 56px pull-quote** in quotes — it reflects the live value of the `smp` field.
- **Creative bands** (Creative + Full only):
  - "Creative territory": 4-up grid of 4:5 placeholder tiles (diagonal-stripe fill, caption bottom-left): "van hero — product", "on-site / trade", "craft detail", "irish landscape".
- **Media band** (Full only): 2-up grid — a 16:9 "hero film — 30s" tile with a play triangle, and a 16:9 "radio cutdown — :30" tile with an audio-waveform of accent bars.
- **Field stack**: each of the 12 fields is a 2-column grid row (`212px | 1fr`, gap 44px, `padding 30px 0`, top border `--border`):
  - **Left column** (controls): field label (Geist Mono 11px uppercase, `--text-2`); a "NEEDS INPUT" amber pill on the open-questions field; provenance status ("● AI generated" in accent, or "● Edited by you" in `--muted` once the user types); then **Regenerate** button (accent-soft), a **Lock from AI** toggle switch, and a **+ Add detail** text link.
  - **Right column** (content): an auto-growing borderless `<textarea>` (16.5px/1.62 Geist, color `--text-input`) holding the field value, with a subtle hover background `--hover`. While generating: a 3-line shimmer skeleton. While streaming: a pulsing "generating…" caption. When "+ Add detail" is open: an inline accent-bordered prompt row (✦ icon + text input + "Add" button).

---

## The 12 Brief Fields (sections + agents + sample content)
Each field has an `id`, a display `label`, an assigned `agent` name, and 2–3 `variants` (regenerate cycles through them). Default value is empty until generated.

1. **background** — *Context agent*
2. **objectives** — *Objectives agent*
3. **audience** — *Audience agent*
4. **competitor** (Competitor context) — *Market agent*
5. **insight** (The insight) — *Insight agent*
6. **smp** (Single-minded proposition) — *Proposition agent* — 3 variants; drives the Full Creative pull-quote
7. **rtb** (Reasons to believe) — *Evidence agent*
8. **response** (Desired response) — *Response agent*
9. **tone** (Tone & world) — *Tone agent*
10. **budget** (Budget & scope) — *Scope agent*
11. **mandatories** — *Compliance agent*
12. **questions** (Open questions) — *Diligence agent* — `kind: 'open'`, shows the "NEEDS INPUT" pill

The exact variant strings and the "+ Add detail" suggestion lines are in the prototype's `state.fields` array and `_detailLine()` method — copy them verbatim if you want the same sample data.

---

## Interactions & Behavior

### Generation (whole brief)
- `generate()`: switches to the `generating` view, marks all fields `queued`, then staggers each field's start by **420ms × index**. Each field goes `queued → thinking (300ms) → streaming → idle`. Streaming reveals text by slicing it in `length/40` chunks every **16ms**. When all 12 are done, wait **750ms**, then switch to the `editor` view.

### Per-field regenerate
- `regen(id)`: `thinking` for **720ms**, then advances to the next variant (`vIdx` cycles), clears the "edited" flag, and streams the new text (`length/55` chunks every **18ms**). Streaming is purely visual — set the field readOnly while busy.

### Regenerate all
- `regenAll()`: regenerates every **unlocked** field, staggered by **150ms × index**. Locked fields are skipped.

### Lock
- `toggleLock(id)`: flips a per-field `locked` boolean. A locked field is excluded from "Regenerate all". The toggle switch turns accent-colored when on, knob slides 12px.

### Edit
- Typing in a field's textarea sets its value and flips `edited: true`, which swaps the "AI generated" badge for "Edited by you". The textarea auto-grows to fit content (set height to scrollHeight on input/mount).

### Add detail
- "+ Add detail" opens an inline prompt under that field. Submitting appends a new line to the field value via a brief `thinking` (620ms) then a stream of just the appended text (so existing text stays put and the addition types in). In the prototype the appended line is canned per-field; in production this is where you'd call the model with the user's instruction + current value.

### Template mode switch
- The pill control sets `mode` to `traditional | creative | full`. This changes content `max-width` (880 → 1080), reveals/hides the creative + media bands, swaps the brief header treatment, and (Full only) reveals the background/media toolbar.

### Dark mode
- Sun/moon toggle in every header flips a `dark` boolean → sets `data-theme="dark"` on the root, which swaps the entire CSS-variable palette. Instant, no reload.

### Animations / easing
- Entrances: `fadeUp .4s ease` (opacity + 8px translateY).
- Skeleton: `shimmer 1.15s linear infinite` (200% background sweep).
- Spinner: `spin .7s linear infinite`.
- Streaming caption dot: `pulse 1s ease infinite`.
- Top progress bar: `slide 1.05s linear infinite`.
- Color/background transitions: `.35s ease` on theme change; `.15s–.18s` on controls.

---

## State Management
Single component state:
- `view`: `'start' | 'generating' | 'editor'`
- `mode`: `'traditional' | 'creative' | 'full'`
- `dark`: boolean
- `bgId`: selected Full-Creative background preset id
- `title`: brief name (string)
- `source`: source-material text (string)
- `detailFor`: id of the field whose "Add detail" prompt is open, or null
- `detailText`: the in-progress add-detail input
- `fields[]`: each `{ id, label, agent, kind?, vIdx, locked, status, edited, value, variants[] }` where `status ∈ 'queued' | 'thinking' | 'streaming' | 'idle'`

Data fetching: in production, `generate`, `regen`, and `submitDetail` each map to a model call (per-section agent). The prototype fakes these with timers and canned variants. Stream tokens into the field value as they arrive; keep the same status machine.

---

## ⭐ Tweak / Configuration Options (IMPORTANT)
The prototype exposes four top-level configuration props. **Recreate these as component props / settings** in your implementation — they are first-class knobs, not hard-coded values:

| Prop | Type | Default | Effect |
|---|---|---|---|
| `startMode` | `'traditional' \| 'creative' \| 'full'` | `'traditional'` | Which template mode the editor opens in. |
| `dark` | `boolean` | `false` | Initial theme. Also runtime-toggleable via the header sun/moon button. |
| `aiAccent` | color (hex) | `#5D5FEF` | The single accent color for all AI affordances — generate/regenerate buttons, provenance badges, spinners, streaming caption, progress bar, add-detail prompt, selected format card, and the proposition rule in Full Creative. Everything AI-related derives from this one value (with derived `+14`/`+33` alpha tints for soft fills and borders in light mode, `+26`/`+4D` in dark). Expose it as a theming prop. |
| `showProvenance` | `boolean` | `true` | Toggles all AI-provenance UI: the "Generated from…" chip and the per-field "AI generated / Edited by you" status labels. Turn off for a clean client-facing view. |

Note the `aiAccent` is **distinct** from the Full-Creative campaign background presets (below) — `aiAccent` themes the *tool's* AI chrome; campaign presets theme the *brief's* background.

### Full-Creative background presets (`bgId`)
Selectable swatches in the Full Creative toolbar, grouped Solid / Gradient / Image, each carrying its own `accent`:
- **Solid**: blue `#2C4BD4`, green `#1F7A5A`, rust `#B0541E`, violet `#6741D9`, ink `#2A2824` (page = a 5.5% tint of the accent).
- **Gradient**: sunrise (`linear-gradient(135deg,#FDEFE3,#F8E7EF,#E9ECFC)`, accent `#B0541E`), dawn (`#EAF2FF→#F1ECFF→#FDEFF4`, accent `#6741D9`), meadow (`#ECF6EA→#F5F3E0→#E5F1F3`, accent `#1F7A5A`).
- **Image** (placeholders, replace with real photography): coast/`#2C4BD4`, dusk/`#B0541E`, studio/`#3A3833` — each a soft radial/linear mood gradient. Plus a dashed "＋ upload" affordance for user-supplied background images.
- In **dark mode**, each preset's page becomes an accent-tinted dark gradient (`radial-gradient(...,<accent>3D 0%, <accent>14 42%, transparent), #141318`) and the accent is lightened ~42% toward white for legibility.

---

## Design Tokens

### Typography
- **Display / serif**: Newsreader (Google Fonts) — weights 300/400/500, supports italic; used for the big title (44–56px) and Full-Creative pull-quote.
- **UI / sans**: Geist (Google Fonts) — weights 300/400/500/600; body, labels, buttons.
- **Mono**: Geist Mono — weights 400/500; eyebrows, field labels, status words, provenance, caps labels (uppercase + letter-spacing .1–.18em).

### Color — Light (`:root`)
```
--page:#F6F5F1  --surface:#ffffff  --surface-2:#FBFAF7  --hover:#FCFBF7
--bar:rgba(255,255,255,.6)  --chrome:rgba(246,245,241,.82)
--border:#E7E4DC  --border-2:#E0DCD2  --border-soft:#F0EEE8  --track:#EBE9E2
--ink:#16150F  --text:#1A1916  --text-input:#26241E  --text-2:#56524A
--text-3:#7C786E  --muted:#8A867C  --faint:#A29E94  --slash:#C9C5BB
--tab-bg:#1A1916  --tab-fg:#ffffff  --logo:#1A1916
--line-mute:#DAD6CC  --stripe-a:#E4E0D6  --stripe-b:#EFEDE7
--tile-a:#EEECE6  --tile-b:#F6F5F1  --skel-a:#EDECFB  --skel-b:#DAD9F8
--needs-bg:#FBF1DD  --needs-fg:#9A6B12
--swatch-ring:#E0DCD2  --swatch-edge:#ffffff
```

### Color — Dark (`[data-theme="dark"]`)
```
--page:#141318  --surface:#1F1D24  --surface-2:#26242D  --hover:#272530
--bar:rgba(31,29,36,.55)  --chrome:rgba(20,19,24,.82)
--border:#302E38  --border-2:#3A3743  --border-soft:#2A2832  --track:#2A2832
--ink:#F4F2EC  --text:#ECE9E2  --text-input:#E4E0D8  --text-2:#B9B4A9
--text-3:#948F85  --muted:#8B867C  --faint:#6E6961  --slash:#494650
--tab-bg:#F0EEE8  --tab-fg:#1A1916  --logo:#F0EEE8
--line-mute:#3A3743  --stripe-a:#2C2A33  --stripe-b:#242229
--tile-a:#242229  --tile-b:#1B1A20  --skel-a:#2A2838  --skel-b:#3B3653
--needs-bg:#3A2E18  --needs-fg:#E0A94C
--swatch-ring:#3A3743  --swatch-edge:#1F1D24
```
- **Accent (AI)**: `--aiAccent` default `#5D5FEF`. Soft fill = accent + `14`(light)/`26`(dark) alpha; soft border = accent + `33`/`4D`.
- **Misc fixed**: attached-file dot `#D24B3E`.

### Radii & spacing
- Radii: pills 999px; cards/boxes 11–14px; buttons 7–9px; tiles 10px; small chips 5–8px.
- Section row padding 30px vertical; content side padding 30px; field grid columns `212px | 1fr`, gap 44px.
- Top bars 60px tall. Knob/track toggle: 30×18px track, 14px knob, 12px travel.

### Shadows
- Generate button: `0 2px 8px <accent>55`. Regenerate-all: `0 1px 4px <accent>55`. Active tab: `0 1px 3px rgba(0,0,0,.2)`. Toggle knob: `0 1px 2px rgba(0,0,0,.25)`.

---

## Assets
- **Fonts**: Newsreader, Geist, Geist Mono (Google Fonts) — load via `<link>` or your font pipeline.
- **Icons**: all glyph/CSS-drawn — "✦" (sparkle, AI), "✓", "＋", "☾/☀" (theme toggle), play triangle (CSS borders), audio waveform (CSS bars). No icon library required; swap for your icon set if preferred.
- **Imagery**: none real — the creative/media/background tiles are CSS placeholders. Wire to real uploads/generation in production.

## Files
- `Brief Builder.dc.html` — the full high-fidelity prototype (markup + `Component` logic class with all sample content, variants, timings, and the token system). This is the single source of truth for look and behavior.
