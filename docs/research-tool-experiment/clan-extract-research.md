# The CLAN extract: research and verdict

Date: 2026-09-30. Scope: whether turning a `.clan` document into its essence
as Markdown — what it holds, which agent made what, and the reasons people
gave in their own words — is a good way to feed the agency's knowledge base
(RAG) and the drafters of a brief; what it gains over the alternatives; the
risks and how the grammar handles them; what to build first; and what it
costs. This is research only. The contract that came out of it is
`docs/contracts/clan-extract.md`. Code was read in the `clan-fields` worktree
(`/home/batunii/Documents/Code/napkin-os-wt/clan-fields`, branch
`Research-tool-experiment`, HEAD `fe7fb03`), with Contract 4 §7.5 as it stands
in the working tree, and the file:line references below point there. The
RAG work on `origin/ragAdded` (`6137720`) was read with `git show`. The test
tenant's documents were read locally, for their structure only. No document
content went to any outside service, and web searches were generic.

---

## 0. The short version

- **Verdict: yes, with conditions.** A deterministic extract is the right way
  to feed both the knowledge base and the drafters. It is the only form that
  carries *why* next to *what*: who made each value, which person accepted,
  rejected or changed it, and the reason they gave, word for word, beside what
  the value is now and whether it is still current. The raw members hold the
  values without the history, the decision chain holds the history without the
  state, and neither is in a shape a retriever can use.
- **The five conditions**, each a rule of the grammar:
  1. what is current is kept apart from what happened, and anything that
     stopped being current says so in words and in its status;
  2. one record is one chunk, and only its grammar-written sentence is
     embedded — ids, dates and statuses are fields for filters and keyword
     search;
  3. a confidential value is never written into the corpus version, so it is
     never embedded — two nets and an output scan make sure of it;
  4. only locked revisions are ingested, and only into an embedder and a
     store inside the agency's boundary;
  5. a brief reads its own research whole, from its own bytes; RAG is for what
     the agency learned in other documents.
- **It pays back before any RAG exists.** The agent version gives a
  research-fed brief's drafters the research's fields, the people's decisions
  with their reasons, and the evidence for each drafter's lenses, in fixed
  sections that a prefix cache serves. That works the day it ships.
- **The main risks are leaks and stale answers**, and both are handled by
  rule. What remains is paraphrase of a confidential value, which can reach
  only the same client's later work, and a short number written without its
  unit.
- **Nothing is ingestible today.** No document in the test tenant has a
  client scope, most have no lock, no name exists for any person, and the
  hosted embedder and store are outside the boundary. Those come first.
- **Build order**: the grammar and the agent version; the drafters use it;
  the corpus version with its nets and scan; ingest; then measure with a
  golden question set before tuning anything.
- **Cost**: the extract calls no model. A drafter's research block is at most
  about 15,000 tokens, served from a prefix cache after the first call;
  embedding is one short statement per record.

---

## 1. What the owner asked for, and where it lands

| The owner's idea | In the contract |
|---|---|
| "turns a .clan document into its essence as Markdown, RAG-ingestible and usable" | One Markdown file per step of the document's making; one record per chunk; three parts per record (§1, §2) |
| "Agent X made <thing>" | Every agent record leads with the agent's name: `Max, the market structure agent, researched market structure in XA on 2026-09-20.` (§3.4) |
| "<human> chose this because: <reason given by the human>" | Every reason is a verbatim quote under a label that says whose words it holds: `Alex Doe chose this because (their words, as written):` (§3.3, R1) |
| "These were the changes in the brief that <human name> made, and the reasons with it" | The title of each person's review file, with each change's before and after and who wrote the before (§2.9, R3) |
| "Each step can have its own file" | `20-intake` to `26-report`, a review file per person, a client file (§1.5, §1.6) |
| "all the files go to RAG, except confidential fields" | The corpus version hides what is marked confidential or private by default, and everything else is ingested once the document is locked (§5) |
| "a deterministic process driven by good grammar rules" | Byte-identical output; no clock, network or model; versioned constants; golden tests (§6) |
| "extract on the research + the brief prompt + any attached document → retrieve from RAG → the model makes the brief with correct citations" | The research as sections for each drafter, the agency's memory by bucket, structured drafting with cited short ids, citations checked in code (§10, §11) |

The owner's decisions of 2026-09-29/30 are the contract's §0.2 (OD1–OD6):
confidential means only "kept out of the shared memory", one function with a
`redact_confidential` switch, `[Marked confidential]` in the value slot only,
the agent version never stored, sections for the drafters, and the three-part
record.

---

## 2. Is this a good idea for RAG?

### 2.1 What the evidence says

| Finding | Numbers | What the grammar does |
|---|---|---|
| The format barely matters | 9,649 runs, 11 models, YAML, Markdown, JSON and TOON: no significant accuracy difference (p = 0.484). In a table test Markdown key-value lines scored best (60.7%), CSV worst (44.3%) | Chooses Markdown for determinism and readability, with `key: value` metadata lines |
| Chunks that follow structure beat fixed-size chunks | Chroma: about 88–90% token recall for recursive splitting at 400 tokens, 91–92% for semantic chunkers; D-RAC R@6 0.798 against 0.717 for fixed-size | One record is one chunk; a record is never cut across another |
| A short context in front of each chunk helps a lot | Top-20 retrieval failure 5.7% → 3.7% with contextual embeddings, → 2.9% adding contextual BM25, → 1.9% adding a reranker; heading-path prefixes raised MRR@5 by 23.8% | A context line written by the grammar — client · doc type · market · category · lens — at no cost and byte-stable |
| Dense embeddings handle ids and numbers poorly | Hybrid BM25 with RRF is the usual fix | Ids and numbers stay in the record's metadata for keyword search; the statement keeps the display value and the exact one |
| Similarity cannot tell a contradiction from a duplicate | AUROC 0.59; a deterministic supersession ledger reached 0.95–1.00 accuracy against 0.20–0.47 for RAG | Supersession comes from the chain — edits, corrections, resolved contests — and is never inferred at retrieval |
| Stale passages poison answers | Outdated retrieval flipped 30–37% of correct answers, 66–75% when the model was told to follow the documents; stating when old evidence stopped applying fixed nearly all of it | Every record that stopped being current says when and why, in words, and carries `status: superseded` |
| Version-aware structure wins on version questions | 90% against 58% for naive RAG and 64% for GraphRAG | Current state and history in different records; the revision and the lock in metadata |
| Embeddings can be turned back into text | Vec2Text recovered up to 92% of 32-token inputs exactly | A confidential value is never written into the corpus version: no filter at query time is trusted with it |
| Retrieved text can carry instructions | PoisonedRAG about 90% attack success with 5 texts; spotlighting held static attacks near 1%, adaptive ones passed 95% | People's, clients' and agents' words only in labelled quote blocks; no outside string inside a grammar sentence; the system prompt treats quotes as data — layered, never one defence |
| Models cite after the fact | Up to 57% of RAG citations post-rationalised | Every cite is a short id checked in code, with a quote that must appear verbatim in the cited line |
| Related but wrong text hurts | One study: accuracy 56.4% → 37.8% with 18 distractors | Weak events fold into the record they concern; agents' working history stays out of the drafters' sections |
| Small corpora do not need RAG | Anthropic: under about 200k tokens, put the knowledge in the prompt | A brief reads its own research whole, as its agent version; RAG serves other documents |
| History helps "why" questions | Evidence is thin and mostly from software ADR studies; several sources are 2026 preprints | A golden question set is built before anything is tuned (O5) |

### 2.2 The verdict

**Yes, with the five conditions of §0.** The design follows what the
evidence supports and nothing the evidence warns against: records that stand
alone, a context the grammar writes, explicit validity, hybrid retrieval on
ids and numbers, confidential content never embedded, and citations checked in
code. The one claim the evidence supports only weakly — that people's reasons
make "why" answers better — is cheap to test: the golden set measures
retrieval with and without the history records before they go into default
retrieval.

---

## 3. What it gains over the alternatives

| Alternative | What it gets right | Why the extract is better here |
|---|---|---|
| Ingest `shared/data.yaml` and the members as they are | Exact data | ragAdded reads only Markdown split at headings. YAML objects chunk badly and say nothing about who or why. Classify marks are not applied. Raw person ids and content hashes would be embedded |
| Ingest the rendered report, or the HTML export | Reads well | Values without history and without people's reasons; free agent HTML as embedded text; `compose_export` applies no classify mark (`read.rs:196-230`) |
| Ingest the decision chain as YAML or TOON | The full history | No current state; older rationales compressed to 280 characters; texts the host and the apps write read as a person's words; raw ids; nothing says what stopped being current |
| Model-written summaries or context headers | Strong retrieval gains | Not byte-stable, so every re-ingest re-embeds and hashing cannot deduplicate; a paraphrase of a person's reason breaks "written as it is"; a cost per document; one more place for injected text |
| GraphRAG over the documents | Multi-hop questions | The decision graph already exists in the chain — targets, cites, lineage — and code walks it exactly and for free; studies find no general win for GraphRAG |
| Tool use or search inside the document | The model picks what it needs | Rejected by the owner for now (OD4): prompts that differ on every call, no prefix caching, more calls, less control over what a drafter sees |
| No RAG; whole documents in the prompt | Simple and faithful | Right for a brief's own research, which is what the agent version does; wrong across hundreds of documents |

The extract also fixes things the platform gets wrong today, which any of the
alternatives would inherit: the view's default texts shown as people's words,
fields whose current setter is a verdict, contested facts that vanish from the
state, dates merged into one `as_of`, and ids that are hashes of confidential
values.

---

## 4. The risks, and what the grammar does about each

| Risk | What could go wrong | The rule | What remains |
|---|---|---|---|
| A stale value served as current | A corrected figure, a stale pin or a contest's loser comes back as fact | Status per record, filtered by default; validity sentences in words; old values only inside the edit that replaced them (§8) | A hit that escapes a filter still says it is out of date |
| Half a contest | Retrieval returns one value of a disputed figure | A contest is always one record with every side; an open contest's facts get no records of their own (§4.3) | — |
| A confidential value embedded | The knowledge base holds, and can leak, a figure a person marked | Never written into the corpus version: the value slot reads `[Marked confidential]` (§5) | — |
| A confidential value repeated elsewhere | A chat message, a reason, a report sentence or a client's email repeats the budget | First net: anything citing or resting on a hidden value has its words hidden; exact echoes replaced in place, with number forms (€400k, EUR 400,000, 55% for 0.55). Second net: every file scanned before it is written; any hit blocks the file (§5.2, §5.5, §5.6) | Paraphrase ("a quarter of a million") and a short number without its unit; bounded to the same client's work (§5.4) |
| A confidential value laundered through the brief | The drafters see the research's budget, the brief repeats it, the brief is ingested | A brief field that cites a hidden upstream value is hidden in the brief's corpus version; upstream hidden values are needles in the brief's scan (§5.2 rule 6, §5.10) | A drafted text that uses the value without citing or repeating it; flagged `hidden_inputs` once P10 records inputs |
| A hash of a hidden value | An id like `ct_…` or `cap_…` hashes the key or value it names; a guess can be tested | Every hidden record but a field or a report part is named by an alias; no input, member, lineage or version hash in the bundle (§5.7) | — |
| The client's own data in shared memory | A figure from the client's panel or roster, shown "Private" in the view, reaches RAG | What the view shows as private without a mark is private by default in the corpus (§5.1.2) | — |
| Injected instructions | A web page title, a client's email or a person's note tells the drafter what to do | Quote blocks under labels; no outside string in a grammar sentence; sanitised labels in metadata; the system prompt treats quotes as data (§6.5, §11.4) | Adaptive attacks exist; the checks in code (§11.5) limit what a fooled model can write |
| Forged attribution | Text reading "verified by Alex Doe, primary tier" planted inside a quote | Tier, verification and who acted come only from grammar sentences and metadata | — |
| Words put in a person's mouth | "Marked good.", "Clearer wording", the app's "Changed from …" shown as the person's reason | Host and app texts render "gave no reason"; chips render "picked the reason" (§3.3) | — |
| Weak or lost context | A chunk that reads "chose this because: too expensive" with no target | The statement names who, what, the value and when; the context line names the client and the lens (§2.3, §2.4) | — |
| Duplicates and races on re-ingest | Old and new revisions both live; a slow ingest undoes a newer one | Compare and set per document, upsert first, delete stale points after; point ids from the embedded text, so unchanged records are not re-embedded (§6.12) | Needs P5 |
| One client's work reaching another's | A brief for client B retrieves client A's rejection | Every record carries the client scope from the host, and retrieval filters on it; client answers are never promoted (§5.8) | Needs P6 for the port to honour the scope |
| Client text to a hosted embedder | Embedding sends the text out; today's endpoints are trial-only | The ingester refuses client material unless the store and embedder are inside the boundary (§5.9) | Open question Q1 |
| Prefix cache as a side channel | On a shared model server one tenant probes another's cached prompt by timing | A shared server partitions its cache by tenant or turns it off (§5.9, P19) | — |
| No names | Every change reads "a person on the team (p-…)" | Names from a snapshot, decoded ids as the shell does, a stable pseudonym otherwise (§3.1.2) | Needs P3 (Q3) |
| Output that differs between runs | Key order, float printing, Unicode, stamps | The SDK's own readers, JCS numbers, NFC, pinned Unicode, position not stamps (§6) | — |
| Citations that do not check | The model cites something it was not given, or misquotes it | Short ids only, quotes verbatim, no cite of a hidden, superseded or don't-use line (§11.5) | — |

---

## 5. What to build first

1. **The owner's four answers** (§7) and four small platform changes: the
   client in the host context (P15), Brief Maker locking through `/approve`
   and writes honouring the lock (P12), pinning every person's decision at
   write (P1), and names (P3).
2. **The grammar core, in `clan-sdk`** (`extract.rs`, `clan extract`): parsing,
   the cast, the frames, the records, and the agent printer. Golden fixtures:
   the example `.clan`, the shapes of the test research documents `139f43e9`
   and `b4b39e85`, of the briefs `13be1177`, `2c526738` and `ecbb9f1c` (the
   last two now hold a lock, a client's answer, an `unlock` and a second
   lock), and a real spun-off brief made in the stack. Nothing here needs
   RAG.
3. **The drafters read it** (P7): the research block first in the prompt,
   short ids among the allowed cites, the checks of §11.5. This is the first
   gain a person sees: briefs grounded in the research's decisions and
   people's reasons, with citations that check.
4. **The corpus version**: the hiding rules, the needles and echoes, the
   output scan, the aliases, and the leak tests. Bundles on disk for locked
   documents, read by a person before anything is ingested.
5. **Ingest** (P5): the dossier strategy that embeds only the statement,
   per-document ingest, the gate, the metadata contract at 1.4.0 — into a
   store and embedder inside the boundary. Then dossier packs on the retrieval
   port (P6) and agency memory in the drafters.
6. **Measure** with the golden question set (O5) — "why was this audience
   chosen", "what did the client reject", "which figure won the contest" —
   with and without the history records, before any cap, bucket or `k` is
   tuned.

Later: decisions recording their inputs (P10), marks on a locked document
(P13), compression recording what it rewrote (P14), marks made upstream after
a spin-off (P16), the ingester's reverse index for renames and erasure (P17),
and the training filter (P18).

---

## 6. Cost notes

- **The extract** is pure Rust with no model and no network. It runs in
  milliseconds per document and can run on every lock and every drafting job.
- **Embedding** takes only each record's context line and statement. A
  research document holds on the order of 50–150 records (the example: up to
  19 fields, 18 facts, 5 findings, 23 decisions, a report), most under 150 words:
  roughly 5,000–20,000 tokens per document. With the planned self-hosted
  embedder that is compute only.
- **Storage** with ragAdded's 2048-dimension dense vectors is about 8 KB per
  point before the sparse vector and the payload: around 1–2 MB per research
  document.
- **Re-ingest** re-embeds only records whose embedded text changed: a point's
  id is built from that text, and a new revision id only updates the payload.
- **Drafting.** A research block is capped at 60,000 characters, about 15,000
  tokens; most will be smaller. A brief job makes four drafter calls
  (insight, proposition, desired response, reasons to believe) plus any
  regenerations, and the block's shared part is identical across all of them.
  - On the Anthropic wire, a cache write costs 1.25× the input price for five
    minutes (2× for an hour) and a read about 0.1× (0.05× on Claude Opus 5.5);
    the minimum cacheable prefix is 512–4,096 tokens by model, which the block
    exceeds. At an input price of $5 per million tokens, four uncached calls
    of 12,000 tokens cost 24 cents; cached, the shared part is paid in full
    once and at a tenth after.
  - On NIM, KV-cache reuse (`NIM_ENABLE_KV_CACHE_REUSE=1`) skips recomputing a
    repeated prefix; NVIDIA reports about a 2× faster time to first token when
    most of the prompt repeats. A NIM shared by more than one agency must
    partition that cache by tenant.
- **Retrieval** is four small asks per drafter (`k` from 2 to 4), negligible
  next to the model call.

---

## 7. Open questions for the owner

1. **May client material be embedded by a hosted embedder?** Nearly every
   corpus record is client material, and embedding — and every query — sends
   its text to the embedder. Recommended: no. Ingest only into a store and an
   embedder inside the agency's boundary (the planned Qdrant on EC2, and an
   embedding NIM on the DGX Spark or the agency's own cloud); the hosted trial
   endpoints serve test tenants only.
2. **An exact echo of a confidential value in text that does not cite it — the
   planner's prompt, a client's email — replaced in place, or blocking the
   whole file?** Your rule says a hit in the scan blocks the file. The
   contract replaces exact echoes first, marking the span `[Marked
   confidential]` and saying so in the label, and lets the scan block only
   what is left. Recommended: replace in place; a blocked intake or report file
   would take everything else in it out of the knowledge base too.
3. **Where do names come from until accounts exist?** Recommended: a display
   name the person types once, kept on the tenant or session and snapshotted
   with each extract. Without it every change reads "a person on the team
   (p-…)".
4. **Which retrieval path serves the drafters' agency memory?** Recommended:
   the server's retrieval port grown with dossier packs and implemented over
   ragAdded's store, so the server keeps one retrieval contract and the
   stand-in and the engine stay swappable by configuration. The alternative is
   the brief job calling ragAdded directly.

---

## 8. Sources

Several are 2026 preprints, not yet peer reviewed; their numbers are
indicative.

**Retrieval, chunking and format**

- Anthropic, *Contextual Retrieval*: https://www.anthropic.com/engineering/contextual-retrieval
- McMillan, format and accuracy across 9,649 runs (arXiv 2602.05447): https://arxiv.org/abs/2602.05447
- ImprovingAgents, input data formats for LLMs: https://www.improvingagents.com/blog/best-input-data-format-for-llms/
- TOON benchmarks: https://toonformat.dev/guide/benchmarks
- Chroma, evaluating chunking: https://www.trychroma.com/research/evaluating-chunking
- D-RAC (arXiv 2609.24220): https://arxiv.org/pdf/2609.24220
- HiChunk (arXiv 2509.11552): https://arxiv.org/abs/2509.11552
- Late chunking (arXiv 2409.04701): https://arxiv.org/pdf/2409.04701
- Title-chain prefixes (arXiv 2608.00824): https://arxiv.org/abs/2608.00824
- Qdrant, hybrid search: https://qdrant.tech/articles/hybrid-search/
- Qdrant, multitenancy: https://qdrant.tech/documentation/manage-data/multitenancy/
- RAG against GraphRAG, a systematic evaluation (arXiv 2502.11371): https://arxiv.org/abs/2502.11371

**Supersession, staleness and versions**

- MemStrata (arXiv 2606.26511): https://arxiv.org/abs/2606.26511
- Stale-Document Poisoning (arXiv 2609.31342): https://arxiv.org/abs/2609.31342
- VersionRAG (arXiv 2510.08109): https://arxiv.org/abs/2510.08109
- Time-organised knowledge (arXiv 2506.07270): https://arxiv.org/abs/2506.07270
- LongMemEval (arXiv 2410.10813): https://arxiv.org/abs/2410.10813
- LangChain indexing API: https://reference.langchain.com/python/langchain-core/indexing
- LangChain, syncing data sources to vector stores: https://www.langchain.com/blog/syncing-data-sources-to-vector-stores

**Citations, prompts and caching**

- Anthropic, citations: https://platform.claude.com/docs/en/build-with-claude/citations
- Anthropic, search results: https://platform.claude.com/docs/en/build-with-claude/search-results
- Anthropic, long-context prompting: https://docs.anthropic.com/en/docs/build-with-claude/prompt-engineering/long-context-tips
- Anthropic, prompt caching: https://platform.claude.com/docs/en/build-with-claude/prompt-caching
- Anthropic, pricing: https://platform.claude.com/docs/en/about-claude/pricing
- FACTUM, post-rationalised citations (arXiv 2601.05866): https://arxiv.org/pdf/2601.05866
- Distractors in RAG (arXiv 2401.14887): https://arxiv.org/html/2401.14887v3
- Random noise does not help, a non-replication (arXiv 2607.03615): https://arxiv.org/abs/2607.03615
- NVIDIA NIM for LLMs, KV-cache reuse: https://docs.nvidia.com/nim/large-language-models/latest/kv-cache-reuse.html

**Leakage and injection**

- Vec2Text, text embeddings reveal almost as much as text (arXiv 2310.06816): https://arxiv.org/pdf/2310.06816
- OWASP Top 10 for LLM applications (LLM08, vector and embedding weaknesses): https://genai.owasp.org/llm-top-10/
- PoisonedRAG explained: https://www.promptfoo.dev/blog/rag-poisoning/
- Microsoft, defending against indirect prompt injection: https://www.microsoft.com/en-us/msrc/blog/2025/07/how-microsoft-defends-against-indirect-prompt-injection-attacks
- NVIDIA, structuring applications to secure the KV cache: https://developer.nvidia.com/blog/structuring-applications-to-secure-the-kv-cache/
- Metadata filters in private RAG: https://vdf.ai/blog/metadata-filters-private-rag/
- Permissions and access control for production RAG: https://www.useparagon.com/learn/permissions-access-control-for-production-rag-apps/

**Provenance and decision records**

- W3C PROV primer: https://www.w3.org/TR/prov-primer/ (PROV-N: https://www.w3.org/TR/prov-n/, PROV-O: https://www.w3.org/TR/prov-o/)
- MADR: https://adr.github.io/madr/ and ADR templates: https://adr.github.io/adr-templates/
- Nygard, documenting architecture decisions: https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions
- Y-statements: https://medium.com/olzzio/y-statements-10eb07b5a177
- Keep a Changelog: https://keepachangelog.com/en/1.1.0/
- Attempto Controlled English: https://en.wikipedia.org/wiki/Attempto_Controlled_English
- Hallucination in natural language generation, a survey (arXiv 2202.03629): https://arxiv.org/pdf/2202.03629
- Context-aware ADR generation (arXiv 2604.03826): https://arxiv.org/abs/2604.03826
- GADR (arXiv 2608.17694): https://arxiv.org/abs/2608.17694
- Nanopublications (arXiv 1809.06532): https://arxiv.org/pdf/1809.06532

**Determinism**

- RFC 8785, JSON canonicalisation: https://www.rfc-editor.org/info/rfc8785/
- Unicode normalisation forms, UAX #15: https://unicode.org/reports/tr15/
- Reproducible builds: https://reproducible-builds.org/docs/definition/ and `SOURCE_DATE_EPOCH`: https://reproducible-builds.org/docs/source-date-epoch/

**In this repository** (at `fe7fb03`): `crates/clan-sdk/src/{decision.rs,
compress.rs, instantiate.rs, manifest.rs, validate.rs}`;
`crates/napkin-host/src/ops/{review.rs, client_review.rs, decisions.rs,
edit.rs}`; `crates/napkin-web/src/tenant.rs`;
`app/templates/shared/clan-fields.html`, `app/templates/brief-maker/index.html`,
`app/templates/campaign-research/index.html`; `app/src/studio/model.ts`,
`app/src/shell/decisions/words.ts`, `app/src/shell/docTitle.ts`;
`server/napkin/{model.py, retrieval.py, util.py, doc.py}`,
`server/napkin/brief/{drafters.py, capture.py, judge.py}`,
`server/napkin/pipeline/research.py`; `mock-backend/layers_port.py`;
`docs/contracts/{os-layer.md, middleware-api.md, peripherals.md,
campaign-clan.md, clan-fields.md}`; and on `origin/ragAdded`,
`engine/rag/{chunking.py, normalise.py, brief_context.py, store_qdrant.py,
rag.py}` and `engine/schema/rag_metadata.v1.json`.
