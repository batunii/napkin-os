# Brief maker: reaching a senior agency's brief

Measured 2026-10-02 on the Samaritans tender, against a senior agency's reference brief for the same
job (kept outside git). The reference has 27 sections, and the engine's brief covers 10 of them. The
insight and proposition are already comparable to the reference's. Everything around them is missing:
the reframe, barrier, objection, persona, channel roles, safety and legal lines, measurement against
baselines, the evidence appendix and the requirement trace. The drafter covers 3.5 of 27.

## Root causes, ranked

1. **The schema is too narrow.** It has 11 fields, and its `modules` are never read. 16 of the 27
   sections have no slot (EC-046).
2. **Captured content is lost.** The Loop-1 capture reads metrics, scoring and constraints, but the
   client brief renders only the golden extraction (EC-043).
3. **Image-only PDF pages are never read**, so old figures replace current ones (EC-042).
4. **No strategy stage.** Nothing writes the reframe, barrier, objection, persona, or the insights
   considered and not chosen.
5. **No evidence ledger or fact-check step.** Unverified research is stated as fact.
6. **Research reaches only the hero writers.** Safety and legal facts have nowhere to go, and research
   decisions were not passed.
7. **Gates empty fields instead of repairing them** (EC-011, EC-044; REQ-01).
8. **No requirement extraction or trace.**

## Target pipeline

- **S0. Read everything** (code + Sonnet vision, parallel per page). Transcribe image-only pages and
  embedded images, and keep a read ledger.
- **S1. Requirement extraction and trace** (Sonnet, low effort, parallel chunks over the whole input).
  Rows carry a verbatim quote and a locator. The supersession rule is Q&A > TRD > RFT. This replaces
  the Opus Loop-1 capture (EC-045) and feeds the golden extraction.
- **S2. Evidence ledger and fact-check** (code, then Sonnet with medium effort):
  - client figures, research facts and decisions in one place;
  - a rejected finding blocks its facts, and a proposed one marks them unverified;
  - a claim fact-check (supported / unverified / unfalsifiable);
  - an optional web gap-filler that reuses the research tool's search port.
- **S3. Strategy chain** (Opus, high effort, serial): situation → reframe → barrier → job → feel →
  think-about. Then the existing insight/SMP writers and judge, whose losing candidates become
  "considered and not chosen". Then the objection.
- **S4. Section writers** (Sonnet, medium effort, parallel): persona and audience reach; objectives and
  measurement from the baselines; channel roles; safety and legal from category and market packs;
  language and adaptation; competitors; precedent; practicalities and scoring.
- **S5. Assembly** (code). A section schema of about 20; REQ-01 labels; the trace check (every
  requirement maps to a section).
- **S6. Critic and one repair pass** per failing section, then the best draft is flagged.

## Knowledge to add (house packs, versioned, with sources)

- **Category safety packs:** suicide and self-harm (Samaritans media guidelines for Ireland), then
  gambling, alcohol, HFSS food and financial promotion.
- **Market legal packs:** for IE, the ASAI Code, Coimisiún na Meán codes and Charities Regulator
  statements; then RO.
- **Measurement and channel-role packs.**

## Phases (each tested on the Samaritans tender and one short brief, with cost and time)

Section counts are against the Samaritans reference.

| Phase | Change | Expected | Tender / short, per brief (estimate) |
|---|---|---|---|
| P0 | Vision for image-only pages; render captured metrics and criteria; exempt identifiers from the citation gate; pass research decisions; REQ-01 labels in code | ~13/27 | $3.5, 6 min / $0.4, 1.7 min |
| P1 | S1 replaces the Opus capture; requirement trace | same sections, faster | $2.0, 2.5 min / $0.4, 1.5 min |
| P2 | S2 ledger and fact-check; evidence appendix; "what we will not claim" | ~16/27 | $2.4, 3.2 min / $0.6, 1.8 min |
| P3 | Section schema, S4 writers, IE and suicide packs | ~22/27 | $3.1, 4 min / $1.0, 2.2 min |
| P4 | S3 strategy chain | ~25/27; job check ≥10/14 | $3.9, 5 min / $1.3, 2.5 min |
| P5 | Reference-style critic and repair; more references | stable across references | $4.3, 5.5 min / $1.5, 2.7 min |

## Evaluation

- Section coverage against a section contract.
- Content agreement where a reference exists.
- Citation accuracy in code.
- Trace completeness.
- The 14-test job check and the critic's health.
- Repeats read against the noise line.

To avoid fitting one reference, build 2-3 more references for test-five briefs (an engine run, then a
long agentic run, then a planner's merge) and keep one back as a test set.

## Reading big inputs (research 2026-10-02)

How others do it:

- **claude.ai Projects** load files whole until near the limit, then switch to retrieval and see only
  passages.
- **Claude's PDF path** sends each page as text plus a page image (about 2.3k tokens a page), so charts
  and scans are read. Anthropic's own guidance is to put inputs under about 200k tokens whole in the
  prompt with caching, and above that to use contextual retrieval (chunk context + embeddings + BM25 +
  rerank).
- **ChatGPT file search, Perplexity and Copilot** are retrieval-first and read only top passages.
- **Gemini and NotebookLM** lean on long context.

No product guarantees "nothing unread" through retrieval alone. Only a chunk ledger with full coverage
does.

Design for the engine (S0/S1 above):

1. **Parse** to text with locators (current readers; Docling/Marker are optional local parsers for
   tables). Pages that are image-only, low-text or charts are rendered and transcribed by Claude vision
   (Haiku for plain OCR, Sonnet for charts and tables). Cache by file hash.
2. **Chunk** on document and section boundaries at about 30-40k chars. Never split a table. Per lot for
   multi-lot tenders, with a shared preamble cached for 1 h.
3. **Map:** Sonnet per chunk, in parallel, into a requirement and evidence ledger with locators, and a
   mark (used / irrelevant / item id) on every paragraph. Unmarked paragraphs are re-queued. Under 100%
   coverage fails loudly. Check `stop_reason` on every call (no silent truncation, cf. EC-040).
4. **Reduce:** Opus merges, dedupes and flags conflicts, keeping locators. It sees raw text only for a
   conflict.
5. **Later steps** get ledger items plus passages retrieved from a per-brief BM25 + embedding index
   (engine/rag code), not the whole input. Summaries never replace the ledger.

Estimates, to be checked against `llm_stats`:

| Input | Cost | Time |
|---|---|---|
| 4k chars | about $0.10 | under 1 min |
| 60k chars | about $0.35 | 1-2 min |
| 228k chars | about $0.90 | 2-3 min (today: $3.4, 6 min) |
| 1M chars | about $3 | 4-6 min |
| 5M chars | about $13 | 15-25 min, two-level reduce |

Batch API at -50% for the map step when time allows. Prices are per million tokens:

| Model | Input | Output | Cache read |
|---|---|---|---|
| Opus 5.5 | $4 | $20 | $0.20 |
| Sonnet 5.5 | $2 | $10 | $0.20 |
| Haiku 4.5 | $1 | $5 | $0.10 |

There is no long-context surcharge on the 4.6+ models, and the new tokenizer produces about 30% more
tokens.

## Gap-filler (Sai, 2026-10-02)

When a field is empty or thin after drafting, a gap-filler looks for the material in two tiers:
1. **Stored facts first.** That means the research CLAN's facts, findings and human decisions
   (rejected findings blocked), plus facts cached from earlier research runs in the knowledge-layers
   store. jev picks the relevant ones ("does FACT answer what this field needs?", yes/no with p).
   There are none stored yet; they will be cached as research runs.
2. **The web only when nothing is stored:** one targeted search per empty field through the research
   tool's search port. What it finds is cached for the next brief.

The field is then written as a labelled proposal: "Proposed (not given by the client): ... based on
[F:id] ... To confirm: ...". It is never presented as the client's words.

## P1 step A result (2026-10-03): read whole while it fits

Measured on the 228k-character tender, with the same code twice:

| | One read | Six chunks |
|---|---|---|
| Time | 169 s | 185 s |
| Cost | $2.25 | $2.82 |
| Sentences accounted for | 100% | 99.1% |
| Capture items | 71 | 262, many repeats |
| Blind head-to-head (both orders) | won | lost: "duplicated contract noise and dozens of false 'which holds?' conflicts" |

- **The long-input problem at this size was already solved by today's reader fixes.** The Word reader
  no longer repeats merged cells (54k fewer characters), and extraction runs on Opus 5.5. The old
  334 s and 20-34% coverage (EC-045) came from the clipped, duplicated input and the fallback model.
- **Chunking stays as a safety valve above 600k characters** (`BRIEF_CHUNK_ABOVE`). Its merge asks
  "which holds?" only for short factual values that differ, at most 5 times.
- **Step B is parked** until an input needs it: the other steps reading the capture instead of the
  whole input.
- **The same run scored 13.5 of 27 reference sections**, up from 8-10 that morning.

## P3 result (2026-10-06, ADR 0022)

Seven section writers (measurement, the person, competitors and alternatives, channel roles, safety and legal
with rule packs, language and adaptation, practicalities) in two waves, with code checks on sources, figures,
contact details and legal lines. On six briefs: won 6 of 6 blind against the same brief without them; the
Samaritans brief covers 16 of the reference's 27 sections (12 before); job check 5.0 to 6.2; at most $0.45
and 50 s a brief. Eight of the nine criteria set beforehand pass; unsupported claims are 5.1 a brief against
a target of 2 or fewer, highest where the research is single-source and unverified. The eight rule packs
(IE, GB, RO; alcohol, gambling, food high in fat, salt or sugar, charity fundraising; suicide and self-harm)
are off until Sai reviews them.
