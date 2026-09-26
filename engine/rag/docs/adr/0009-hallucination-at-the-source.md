# ADR 0009 — Hallucination at the source: stop invention entering the brief

Status: accepted (batch 2 of the 2026-09-24 audit fix plan; Sai's "go on" 2026-09-26; the
visible-marks half was decided in ADR 0008) · Findings: H1, JL-11, H7, F9, H10, H3, H8,
F13, critic-G1, H5, F3, JL-9, F11, J13 · Code: `engine/parse_brief.py` (`docx_text`,
`_brief_block`, `_clip_report`, `_gen_field_system`, `_allowed_facts`, `_numbers_not_in`,
`_strip_unsupplied_names`, `_check_synthesis`, `_retrieval_fields_from_golden`,
`fill_derivable_fields`, `run`), `engine/rag/labelset.py` · Tests:
`engine/rag/test_hallucination_at_the_source.py`.

## Context

The audit measured where invented material enters a brief. Opus 4.6 invented 19 of 25
reasons-to-believe items and every one of the 11 invented figures in writer fields (RTB and
desired response); the RTB writer never saw the brief's proof points, was told "do not
extract it, derive it", and copied the shape of a style example that was itself an invented
statistic. Writers were told to cite award precedents that were never sent, so Opus 4.6
named campaigns from memory in 24 of 24 RTB and desired-response rationales. Loop
syntheses named retailers, places and schemes in neither the brief nor the evidence, and
2.8% of their citations matched nothing. The extractor turned a stated requirement into a
proof point, and relabelled its own inferred audience persona as a fact for retrieval, so
"Ana, 32, Bucharest" reached the queries, all five syntheses and the insight. The docx
reader put every table at the end of the text, moving the employer brief's budget sentence
past the 6,500-character judge clip; 28% of that brief was never read by the golden
extraction, scorecard or territory call.

## Decisions

**Documents are read in order and in full.** `docx_text` walks the body in document order
(paragraphs and tables interleaved), shared with the label tool. The golden extraction,
scorecard and territory calls read the same 12,000-character window as the capture. A
brief longer than that gets `meta.clipped` and a high open question naming where the
unread part starts.

**The brief is delimited as data.** Every prompt that embeds the brief uses `_brief_block`:
`<client_brief>` tags (a brief containing a triple quote broke the old delimiter), one line
saying it is data, and that attachment text is supporting material. The judge's context
and candidates are tagged the same way.

**The RTB writer selects; it does not derive.** It receives ALLOWED FACTS: the capture's
proof points with their verbatim quotes (when the capture has landed within 45 s; the fill
starts before it, ADR 0006) plus every brief sentence carrying a figure. Its prompt says
select and phrase, never add a figure, test, ingredient, award, scheme, date or history,
and write "TO CONFIRM: …" when the proof is absent. A code rule fails any RTB or
desired-response item carrying a figure the brief does not contain, and the code-rule
repair rewrites it once.

**Precedents are claimed only when sent.** `_gen_field_system(has_precedents=…)`: the
precedent sentences and the rationale's demand to name one appear only for a hero field
whose retrieval returned IPA cases; RTB, desired response, the refine pass and an empty
retrieval get "no precedents are supplied: name none". IPA cases reach the writer as body
text, not the title-and-award header. A proper name in a rationale that is not in the
material sent is replaced with "[name not in the material supplied]" and recorded on the
entry.

**Extraction rules.** A requirement the work must meet is not a proof point (capture and
golden prompts). Attachment text is inferred at most, never client-stated.

**Inferred values travel as assumptions.** Retrieval from the golden extraction carries
status `assumption` for inferred values and strips a "Name, 32, City," persona prefix from
an inferred audience; writers see inferred dependencies as "audience (assumption): …".

**Syntheses are checked after the call.** `_check_synthesis` removes proper names absent
from the gist and that loop's evidence and turns a "(doc › section)" whose doc is not in
the evidence into "(uncited)"; `loops3_7.uncited` and `names_removed` carry the totals.
The prompt also says to name no competitor, retailer, place, scheme or campaign that is
not in the material.

## Consequences

- The invented-figure class of RTB error is caught by code; invented *claims* without a
  figure still depend on the judge and, later, the grounding gate (parked with Shrey).
- Some RTB items become "TO CONFIRM: …" where the brief gives no proof; that is the
  intended honest outcome and shows up in `rtb_supports_smp`.
- Name stripping is a heuristic (runs of two or more capitalised words absent from the
  material); a legitimate two-word name the brief never mentions is removed too, and the
  entry says so.
- The wider window costs about 650 input tokens on each of three calls for a long brief.
- Not done here: jev checks on RTB provenance (H10's Choice call, parked with Shrey's jev
  scope), app regeneration grounding (H14/F10), docx attachments in the app (critic-G2),
  scanned-image transcription (critic-G8).
