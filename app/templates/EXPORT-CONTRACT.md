# Studio Export Contract

How a Napkin Studio OS template gets a shareable HTML/PDF export — and why most
templates need to do almost nothing.

## The split: template declares, OS composes

Export is an **OS-layer capability**, not a per-studio feature. The desktop
host and the `clan` CLI both call one function — `clan_sdk::export_html` — to
turn a `.clan` file into a self-contained document. The template's only job is
to declare *what the printed document should contain*; the platform owns
everything cross-cutting and identical across all 19 studios:

| Owned by the OS layer (`clan-sdk::export`)            | Owned by the template                     |
|-------------------------------------------------------|-------------------------------------------|
| `{{binding}}` resolution against `shared/data.yaml`   | Which fields appear, in what order        |
| Asset inlining (`assets/*` → `data:` URIs)            | Per-field presentation / print CSS        |
| Brand header + "powered by CLAN" footer               | Section grouping and headings             |
| Optional attributed **provenance appendix**           | —                                         |
| Standalone scaffold + `@page` print margins           | —                                         |
| `<script>` stripping (the export is static)           | —                                         |
| HTML → PDF conversion (headless browser)              | —                                         |

This is why export works **without a running app**: `clan export doc.clan
--format pdf` produces the same document as the viewer's ⬇ Export button, and
Export Studio can batch-export a whole project lineage headless.

## Path A — declarative (preferred for new studios)

Provide a **binding-based export view** and let the OS compose everything else.

1. Ship `human/export.html` in your template (falls back to `human/index.html`
   if absent). It is plain HTML with `{{dotted.path}}` tokens that resolve
   against `shared/data.yaml`:

   ```html
   <header>
     <div class="eyebrow">Creative brief</div>
     <h1>{{project_name}}</h1>
     <div class="client">{{client}}</div>
   </header>
   <section>
     <h2>Proposition</h2>
     <p class="spp">{{single_minded_proposition}}</p>
   </section>
   ```

   - **Scalars** (`string`, `number`, `bool`) resolve to escaped text. A missing
     path resolves to empty — safe to leave placeholders for unfilled fields.
   - **Objects and arrays** do not inline through a single `{{...}}`; render them
     with your own repeated markup (the data is in the file; author the loop in
     the view, or expose the values as scalars).
   - A `{{ ... }}` that isn't a valid dotted path is left verbatim, so prose and
     code samples are safe.

2. Reference images by their asset name; the OS inlines them as `data:` URIs:

   ```html
   <img src="assets/hero.png" />        <!-- or clan://localhost/assets/hero.png -->
   ```

3. Put your print styling in a `<style>` block in the export view. The OS adds
   only scoped `.napkin-export-*` / `.napkin-provenance` styles plus `@page`
   margins — it never overrides your CSS.

That's it. No JavaScript, no host bridge, no export code.

## Path B — imperative fallback (legacy)

Some views are genuinely *code*: the final DOM only exists after the app's JS
runs (e.g. `brief-maker` builds a report with hero paragraphs and a mood-board
grid). Those apps build their own standalone HTML and push it to the host:

```js
window.clan.exportDoc('pdf', filename, standaloneHtml)  // → clan://export
```

This path still works and is not deprecated, but it **only runs inside the
desktop viewer** — it can't be driven headless by the CLI or Export Studio, and
it re-implements asset inlining and chrome per template. Prefer Path A for new
studios; reach for Path B only when print layout can't be expressed as bindings.

## Invoking export

- **Viewer:** the toolbar ⬇ Export button → `export_current` Tauri command →
  `clan_sdk::export_html` → native save dialog.
- **CLI / headless / Export Studio:**
  ```bash
  clan export doc.clan                      # → doc.html, branded
  clan export doc.clan --format pdf         # → doc.pdf  (needs Chromium/Chrome)
  clan export doc.clan --provenance         # append the attributed decision log
  clan export doc.clan --no-brand           # drop the Napkin header/footer
  ```

## Notes & current limits

- Output is **deterministic** for a given file (nothing is stamped that isn't
  already in the manifest/decision chain).
- Asset inlining covers `src`/`href` attributes. CSS `url(...)` references
  (e.g. `background-image`) are **not** inlined yet — use `<img>` for anything
  that must travel in the file.
- PDF conversion requires a headless-capable Chrome/Chromium on `PATH`. Without
  one, export to HTML and print to PDF from a browser.
