# The CLAN extract: research and verdict

Date: 2026-09-30. Scope: whether turning a `.clan` document into its essence
as Markdown — what it holds, which agent made what, and the reasons people
gave in their own words — is a good way to feed the agency's knowledge base
(RAG) and the writer of a brief; what it gains over the alternatives; the
risks and how the grammar handles them; what to build first; and what it
costs. This is research only. The contract that came out of it is
`docs/contracts/clan-extract.md`. Code was read in the `clan-fields` worktree
(`/home/batunii/Documents/Code/napkin-os-wt/clan-fields`, branch
`Research-tool-experiment`, HEAD `fe7fb03`), with Contract 4 §7.5 as it stands
in the working tree, and the file:line references below point there. The
RAG work on `origin/ragAdded` (`6137720`) was read with `git show`, and so,
after the owner's answers of 2026-09-30, was Sai's RAG module and brief engine
on `origin/jev-hardening` (`7009001`), which the contract now fits as they are.
The test tenant's documents were read locally, for their structure only. No
document content went to any outside service, and web searches were generic.

---

## 0. The short version

- **Verdict: yes, with conditions.** A deterministic extract is the right way
  to feed both the knowledge base and the brief's writer. It is the only form
  that carries *why* next to *what*: who made each value, which person accepted,
  rejected or changed it, and the reason they gave, word for word, beside what
  the value is now and whether it is still current. The raw members hold the
  values without the history, the decision chain holds the history without the
  state, and neither is in a shape a retriever can use.
- **The five conditions**, each a rule of the grammar:
  1. what is current is kept apart from what happened, and anything that
     stopped being current says so in words and in its status;
  2. one record is one chunk, and only grammar-written text is embedded — a
     context line, a short heading and the statement; dates, statuses and
     most ids are fields for filters;
  3. a value marked confidential is never written into the corpus version,
     nor any exact occurrence of it, so it is never embedded;
  4. only locked revisions are ingested;
  5. a brief reads its own research from its own bytes; RAG is for what the
     agency learned in other documents.
- **We adapt to Sai's module and engine as they are** (owner, 2026-09-30).
  The corpus version is written one file per record in a form the RAG
  module's existing `sections` strategy ingests unchanged, retrieval is
  `rag_io` 1.4.0 as it is, and the brief is written by Sai's engine, which
  takes the research as verified fact rows through its existing `upstream`
  input and writes the `[F:<id> v<version>]` lines itself.
- **What reaches a brief today is narrower than the draft planned.** The
  engine takes fact rows (pins and verified findings) and three facets
  (brand, category, competitors). People's decisions and reasons, client
  answers, open contests, gaps and the research's own fields have no engine
  input; and the engine's brief path retrieves with no tenant or brand, so
  the agency's knowledge base reaches a `rag_io` caller but no brief writer.
  Both are open questions for the owner.
- **The main risks are leaks and stale answers**, and both are handled by
  rule. On confidentiality the owner chose exactness: "We block the exact
  figures — prose, marked confidential, that's it. Nothing else." A marked
  value and its exact occurrences are replaced; a restatement in other words
  is not caught, and the contract says so.
- **Nothing is ingestible today.** No document in the test tenant has a
  client scope and most have no lock. Names now come from the account (a
  dummy account until accounts exist), and a hosted embedder may embed client
  material; the NVIDIA endpoints in use are on trial terms, which is a
  licensing matter for whoever signs them.
- **Build order**: the grammar and both agent printers; the engine fed with
  fact rows; the corpus version; ingest through the module's own build; then
  measure with a golden question set before tuning anything.
- **Cost**: the extract calls no model. The engine's writers read at most 52
  fact rows as lines; embedding is one short record per chunk.

---

## 1. What the owner asked for, and where it lands

| The owner's idea | In the contract |
|---|---|
| "turns a .clan document into its essence as Markdown, RAG-ingestible and usable" | One Markdown file per step of the document's making; one record per chunk; three parts per record (§1, §2) |
| "Agent X made <thing>" | Every agent record leads with the agent's name: `Max, the market structure agent, researched market structure in XA on 2026-09-20.` (§3.4) |
| "<human> chose this because: <reason given by the human>" | Every reason is a verbatim quote under a label that says whose words it holds: `Alex Doe chose this because (their words, as written):` (§3.3, R1) |
| "These were the changes in the brief that <human name> made, and the reasons with it" | The title of each person's review file, with each change's before and after and who wrote the before (§2.9, R3) |
| "Each step can have its own file" | A folder per step, `20-intake` to `26-report`, a review folder per person, a client folder, with one file per record, as the RAG module needs (§1.4, §1.5, §1.6) |
| "all the files go to RAG, except confidential fields" | The corpus version replaces what is marked confidential, wherever that exact value appears, and everything else is ingested once the document is locked (§5) |
| "a deterministic process driven by good grammar rules" | Byte-identical output; no clock, network or model; versioned constants; golden tests (§6) |
| "extract on the research + the brief prompt + any attached document → retrieve from RAG → the model makes the brief with correct citations" | Sai's engine writes the brief from the prompt and the attached files; the research reaches it as verified fact rows, which it cites as `[F:<id> v<version>]` and checks in code, and its `fact_refs` map back to the `.clan` (§11.1–§11.4). Retrieval from the agency's knowledge base is `rag_io`'s, and does not reach the engine's writers today (§11.5, §11.7) |

The owner's decisions of 2026-09-29/30 are the contract's §0.2 (OD1–OD11):
confidential means only "kept out of the shared memory", one function with a
`redact_confidential` switch, `[Marked confidential]` in the value slot only,
the agent version never stored, sections for a model call, and the
three-part record (OD1–OD6); then, on 2026-09-30, a hosted embedder allowed
(OD7), exact values only (OD8), names from the account (OD9), Sai's RAG
module used as it is (OD10), and Sai's engine as the brief generator, used as
it is (OD11). The server's drafters are no longer the generator the extract
feeds.

---

## 2. Is this a good idea for RAG?

### 2.1 What the evidence says

| Finding | Numbers | What the grammar does |
|---|---|---|
| The format barely matters | 9,649 runs, 11 models, YAML, Markdown, JSON and TOON: no significant accuracy difference (p = 0.484). In a table test Markdown key-value lines scored best (60.7%), CSV worst (44.3%) | Chooses Markdown for determinism and readability, with `key: value` metadata lines |
| Chunks that follow structure beat fixed-size chunks | Chroma: about 88–90% token recall for recursive splitting at 400 tokens, 91–92% for semantic chunkers; D-RAC R@6 0.798 against 0.717 for fixed-size | One record is one chunk; a record is never cut across another |
| A short context in front of each chunk helps a lot | Top-20 retrieval failure 5.7% → 3.7% with contextual embeddings, → 2.9% adding contextual BM25, → 1.9% adding a reranker; heading-path prefixes raised MRR@5 by 23.8% | A context line written by the grammar — client · doc type · market · category · lens — at no cost and byte-stable |
| Dense embeddings handle ids and numbers poorly | Hybrid BM25 with RRF is the usual fix | Ids and statuses are metadata, for the RAG module's filters; the statement keeps the display value and the exact one, so its keyword search finds either |
| Similarity cannot tell a contradiction from a duplicate | AUROC 0.59; a deterministic supersession ledger reached 0.95–1.00 accuracy against 0.20–0.47 for RAG | Supersession comes from the chain — edits, corrections, resolved contests — and is never inferred at retrieval |
| Stale passages poison answers | Outdated retrieval flipped 30–37% of correct answers, 66–75% when the model was told to follow the documents; stating when old evidence stopped applying fixed nearly all of it | Every record that stopped being current says when and why, in words, and carries `status: superseded` |
| Version-aware structure wins on version questions | 90% against 58% for naive RAG and 64% for GraphRAG | Current state and history in different records; the revision and the lock in metadata |
| Embeddings can be turned back into text | Vec2Text recovered up to 92% of 32-token inputs exactly | A marked value, and every exact occurrence of it, is never written into the corpus version: no filter at query time is trusted with it |
| Retrieved text can carry instructions | PoisonedRAG about 90% attack success with 5 texts; spotlighting held static attacks near 1%, adaptive ones passed 95% | People's, clients' and agents' words only in labelled quote blocks; no outside string inside a grammar sentence; the system prompt treats quotes as data — layered, never one defence |
| Models cite after the fact | Up to 57% of RAG citations post-rationalised | The engine's writers cite `[F:<id> v<version>]` and the engine checks each cite in code (an id not given, a figure not in the cited fact, a research figure not cited); a quote given with any other short id must appear verbatim in the cited line |
| Related but wrong text hurts | One study: accuracy 56.4% → 37.8% with 18 distractors | Weak events fold into the record they concern; agents' working history stays out of the agent version |
| Small corpora do not need RAG | Anthropic: under about 200k tokens, put the knowledge in the prompt | A brief reads its own research from its own bytes, as its agent version; RAG serves other documents |
| History helps "why" questions | Evidence is thin and mostly from software ADR studies; several sources are 2026 preprints | A golden question set is built before anything is tuned (O5) |

### 2.2 The verdict

**Yes, with the five conditions of §0.** The design follows what the
evidence supports and nothing the evidence warns against: records that stand
alone, a context the grammar writes, explicit validity, hybrid retrieval,
marked values never embedded, and citations checked in code. It does so
within Sai's module and engine as they are, which costs two things the draft
had planned: the engine reads only fact rows, and its brief path does not
retrieve the agency's own material (§4, §7). The one claim the evidence supports only weakly — that people's reasons
make "why" answers better — is cheap to test: the golden set measures
retrieval with and without the history records before they go into default
retrieval.

---

## 3. What it gains over the alternatives

| Alternative | What it gets right | Why the extract is better here |
|---|---|---|
| Ingest `shared/data.yaml` and the members as they are | Exact data | The RAG module reads only Markdown split at headings. YAML objects chunk badly and say nothing about who or why. Classify marks are not applied. Raw person ids and content hashes would be embedded |
| Ingest the rendered report, or the HTML export | Reads well | Values without history and without people's reasons; free agent HTML as embedded text; `compose_export` applies no classify mark (`read.rs:196-230`) |
| Ingest the decision chain as YAML or TOON | The full history | No current state; older rationales compressed to 280 characters; texts the host and the apps write read as a person's words; raw ids; nothing says what stopped being current |
| Model-written summaries or context headers | Strong retrieval gains | Not byte-stable, so every re-ingest re-embeds and hashing cannot deduplicate; a paraphrase of a person's reason breaks "written as it is"; a cost per document; one more place for injected text |
| GraphRAG over the documents | Multi-hop questions | The decision graph already exists in the chain — targets, cites, lineage — and code walks it exactly and for free; studies find no general win for GraphRAG |
| Tool use or search inside the document | The model picks what it needs | Rejected by the owner for now (OD4): prompts that differ on every call, no prefix caching, more calls, less control over what a drafter sees |
| No RAG; whole documents in the prompt | Simple and faithful | Right for a brief's own research, which is what the agent version does; wrong across hundreds of documents |

The extract also fixes things the platform gets wrong today, which any of the
alternatives would inherit: the view's default texts shown as people's words,
fields whose current setter is a verdict, contested facts that vanish from the
state, and dates merged into one `as_of`.

---

## 4. The risks, and what the grammar does about each

| Risk | What could go wrong | The rule | What remains |
|---|---|---|---|
| A stale value served as current | A corrected figure, a stale pin or a contest's loser comes back as fact | Status per record, filtered by default; validity sentences in words; old values only inside the edit that replaced them (§8) | A hit that escapes a filter still says it is out of date |
| Half a contest | Retrieval returns one value of a disputed figure | A contest is always one record with every side; an open contest's facts get no records of their own (§4.3) | — |
| A confidential value embedded | The knowledge base holds, and can leak, a figure a person marked | Never written into the corpus version: the value slot reads `[Marked confidential]` (§5) | — |
| A confidential value repeated elsewhere | A chat message, a reason, a report sentence or a client's email repeats the budget | Every exact occurrence of a marked value is replaced in place, a figure in its normal number forms (€400k, EUR 400,000, 55% for 0.55); nothing else is withheld and no file is blocked, by the owner's rule (§5.1, §5.4) | A restatement in other words ("a quarter of a million") and a short number without its unit; bounded to the same client's work (§5.5) |
| A confidential value laundered through the brief | The engine's writers see the research's budget, the brief repeats it, the brief is ingested | The research's marked values are matched in the brief's corpus version, so an exact repeat is replaced (§5.7) | A restatement in other words |
| A hash of a marked value | An id like `ct_…` or `cap_…` hashes the key or value it names; a guess can be tested | No hash of a marked value is computed by the extract (§5.3); ids are kept as they are, by the owner's rule ("nothing else") | An id that is itself a hash of content |
| The client's own data in shared memory | A figure from the client's panel or roster, shown "Private" in the view, reaches RAG | Only a mark hides a value (OD8); the view's badge is to key on the mark (P20) | An unmarked client figure is ingested, scoped to its client |
| Injected instructions | A web page title, a client's email or a person's note tells a model what to do | Quote blocks under labels; no outside string in a grammar sentence; sanitised labels in metadata; the engine gives its writers fact lines inside a data tag (§6.5, ADR 0014) | Adaptive attacks exist; the engine's cite check limits what a fooled writer can keep. A fact row's source title is printed inside the engine's own line |
| Forged attribution | Text reading "verified by Alex Doe, primary tier" planted inside a quote | Tier, verification and who acted come only from grammar sentences and metadata | — |
| Words put in a person's mouth | "Marked good.", "Clearer wording", the app's "Changed from …" shown as the person's reason | Host and app texts render "gave no reason"; chips render "picked the reason" (§3.3) | — |
| Weak or lost context | A chunk that reads "chose this because: too expensive" with no target | The statement names who, what, the value and when; the context line names the client and the lens (§2.3, §2.4) | — |
| Duplicates and races on re-ingest | Old and new revisions both live; a slow ingest undoes a newer one | Compare and set per document; the document's folder replaced whole, then the module's own rebuild (§6.12) | A remote store keeps a removed record's row until it is rewritten from the local index (Q8) |
| One client's work reaching another's | A brief for client B retrieves client A's rejection | Every record carries `scope: brand:<client>` and its tenant from the host; `rag_io`'s `authority` is the only thing that unlocks them (§5.5, §5.6) | — |
| Client text to a hosted embedder | Embedding sends the text out | Allowed by the owner (OD7): otherwise the search would be poor | The NVIDIA endpoints in use are on trial terms that exclude production use: a licensing matter for whoever signs them |
| Prefix cache as a side channel | On a shared model server one tenant probes another's cached prompt by timing | A shared server partitions its cache by tenant or turns it off (§5.6, P19) | — |
| No names | Every change reads "a person on the team" | Names from the account; a dummy account until accounts exist (OD9) | — |
| Research lost on the way to the brief | People's reasons, client answers, open contests and gaps never reach the brief's writers | Rejected findings, excluded facts and losing values are simply not sent, so the writers cannot state them; the chosen value of a contest and a correction's new fact are sent (§11.5) | The reasons behind them, and what is still open (Q6) |
| The agency's knowledge base unused by the brief | The engine's brief path retrieves with no tenant or brand | Dossier chunks are served through `rag_io` only; nothing assumes a writer reads them (§11.5, §11.7) | Earlier briefs and rejections do not shape a new brief today (Q5) |
| Output that differs between runs | Key order, float printing, Unicode, stamps | The SDK's own readers, JCS numbers, NFC, pinned Unicode, position not stamps (§6) | — |
| Citations that do not check | A writer cites something it was not given, or misquotes it | The engine's own check of its `[F:…]` cites; its `fact_refs` map back to `.clan` anchors (§11.4) | A writer who rounds an exact value fails the engine's figure check, and the field stays open (§11.8, L17) |

---

## 5. What to build first

1. **Four small platform changes**, none of them in Sai's module or engine:
   the client in the host context (P15), Brief Maker locking through
   `/approve` and writes honouring the lock (P12), pinning every person's
   decision at write (P1), and names from the account, a dummy account for
   now (P3).
2. **The grammar core, in `clan-sdk`** (`extract.rs`, `clan extract`): parsing,
   the cast, the frames, the records, and the `upstream` and `sections`
   printers. Golden fixtures: the example `.clan`, the shapes of the test
   research documents `139f43e9` and `b4b39e85`, of the briefs `13be1177`,
   `2c526738` and `ecbb9f1c` (the last two hold a lock, a client's answer, an
   `unlock` and a second lock), and a real spun-off brief made in the stack.
   A test runs `research_facts.current` and `line` from `origin/jev-hardening`
   on the rows. Nothing here needs RAG.
3. **The engine is given the rows** (P22): the platform calls
   `parse_brief.run(upstream=…)` and `mapping.map_brief` in the engine's
   process, since its HTTP server does not forward `upstream`, and records
   each field's `fact_refs` as the draft's cites. This is the first gain a
   person sees: briefs grounded in the research's verified facts and
   findings, with cites the engine checks and the `.clan` resolves.
   *Superseded 2026-09-30:* the draft's step 3, the server's drafters reading
   a research block (P7), is withdrawn (OD11).
4. **The corpus version**: exact replacement of marked values (§5), and the
   leak tests. Bundles on disk for locked documents, read by a person before
   anything is ingested.
5. **Ingest** (P21): the platform's step checks a bundle, places its
   `corpus/` folder in the module's corpus directory, and runs the module's
   own `rag.py build`; a test runs the module's chunker, unchanged, on every
   fixture. Then a `rag_io` caller for the knowledge base, once the owner says
   who reads it (Q5).
6. **Measure** with the golden question set (O5) — "why was this audience
   chosen", "what did the client reject", "which figure won the contest" —
   with and without the history records, before any cap, bucket or `k` is
   tuned.

Later: marks on a locked document (P13), compression recording what it
rewrote (P14), marks made upstream after a spin-off (P16), and the ingester's
reverse index for renames and erasure (P17).

---

## 6. Cost notes

- **The extract** is pure Rust with no model and no network. It runs in
  milliseconds per document and can run on every lock and every brief job.
- **Embedding** takes each record's context line, heading and statement. A
  research document holds on the order of 50–150 records (the example: up to
  19 fields, 18 facts, 5 findings, 23 decisions, a report), most under 150
  words: roughly 5,000–20,000 tokens per document, on the module's embedder.
- **Storage** with the module's 2048-dimension dense vectors is about 8 KB per
  row before the payload: around 1–2 MB per research document.
- **Re-ingest** is the module's own rebuild of its local index. A record whose
  file, heading and embedded text are unchanged keeps its row id, and a change
  to metadata alone can take the module's `retag`, which does not embed
  again.
- **Brief writing** costs what the engine costs. The extract adds at most 52
  fact rows, which every hero writer reads as one line each, a few thousand
  tokens.
- **Retrieval** through `rag_io` is the module's, one request per brief.

---

## 7. Open questions for the owner

*Answered 2026-09-30:* a hosted embedder may embed client material (OD7);
only exact marked values are replaced, and no file is blocked (OD8); names
come from the account, a dummy account until accounts exist (OD9); the
retrieval path is Sai's RAG module, used as it is (OD10); the brief generator
is Sai's engine, used as it is (OD11). Open:

1. **The agency's knowledge base reaches no brief writer.** The engine's
   brief path retrieves with no tenant and no brand, so it serves house and
   global material only; earlier briefs, rejections and verified findings of
   the same client are ingested and reach a `rag_io` caller, but no writer.
   Is that acceptable until the engine changes on its own schedule, and which
   `rag_io` caller reads them meanwhile — a panel beside the brief, for the
   planner?
2. **The research's decisions do not reach the engine.** People's reasons,
   client answers, open contests, gaps and the research's own fields have no
   engine input; the engine gets verified facts only. Is the loss accepted, or
   should they reach the planner some other way, such as that panel?
3. **A mark on a whole material hides only its name.** Its text is never
   rendered, and a quote taken from it is not the marked value. Is that what
   "the exact figures" means for a marked material, or should the person mark
   the quoted figure itself?
4. **Who may rewrite the remote store?** A removed record keeps its row in a
   remote store until the store is rewritten from the local index with
   `migrate --replace`, which the module's README asks to be agreed first.
   Who agrees it, and how often is it run?

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
campaign-clan.md, clan-fields.md}`; on `origin/ragAdded`,
`engine/rag/{chunking.py, normalise.py, brief_context.py, store_qdrant.py,
rag.py}` and `engine/schema/rag_metadata.v1.json`; and on
`origin/jev-hardening` at `7009001`, `engine/rag/{README.md, chunking.py,
normalise.py, contract.py, brief_context.py, rag_io.py, rag.py,
docs/adr/0014-research-facts.md, docs/vector-store-migration.md,
docs/dgx-spark-plan.md}`, `engine/schema/{rag_io.v1.json,
rag_metadata.v1.json}`, `engine/{parse_brief.py, research_facts.py,
brief_ingest.py}` and `engine/agent-server/{server.py, mapping.py}`.
