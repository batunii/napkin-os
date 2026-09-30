# DGX Spark — execution plan

- **Status:** planned, 2026-09-23. The device arrives the week of 2026-09-28; nothing has run on it yet.
- **Owner:** Sai. Status and reasoning are tracked in `project_plan.clan` (`dgx_spark_plan`); this file is the runbook.
- **Scope:** what we use the Spark for, in what order, with a measurable gate before each thing is adopted.

---

## 1. Goal and what "done" means

Get both retrieval legs — **embeddings** and **reranking** — running on hardware we control, with the
same models the index was built with, measured parity with the hosted endpoints, and a licensing
position that allows production use. Then use the box for the offline work the hosted setup
cannot do cheaply: full-scale evaluation, calibration labelling, enrichment, and the storage decision.

Done when:

1. Embeddings and reranking each run on the Spark with parity and latency gates passed (§6, W1–W2).
2. The validation chain uses the Spark as its first non-jev backend, with hosted and local fallbacks.
3. A production licensing path is chosen and documented (§8).
4. Full-scale golden evaluation runs on the Spark and reproduces the Mac numbers.

**Not in scope:** brief generation (draft / judge / revise stays on Claude per `model_strategy`), and
serving fee-paying agencies from the Spark before §8 is settled and the stress test passes.

---

## 2. Facts this plan rests on

Verified 2026-09-23 against primary sources unless marked otherwise.

| Fact | Source |
|---|---|
| GB10: 20-core Arm (10 X925 + 10 A725), Blackwell GPU (sm_121), 128 GB unified LPDDR5x at **273 GB/s**, 10 GbE + ConnectX-7, 240 W, DGX OS 7 (Ubuntu 24.04) | [hardware](https://docs.nvidia.com/dgx/dgx-spark/hardware.html), [release notes](https://docs.nvidia.com/dgx/dgx-spark/release-notes.html) |
| **The embedder we use has a GB10 NIM**: `nvcr.io/nim/nvidia/nemotron-3-embed-1b:2.3`, multi-arch, "DGX Spark support starts with version 2.3", 2048-d only, `POST /v1/embeddings` with `input_type: query\|passage` — the same field as the hosted API | [support matrix](https://docs.nvidia.com/nim/nemo-retriever/text-embedding/latest/support-matrix.html), [getting started](https://docs.nvidia.com/nim/nemo-retriever/embedding/latest/getting-started.html) |
| **The reranker we use has a GB10 NIM**: `llama-nemotron-rerank-vl-1b-v2`, NIM 2.3, arm64. Self-hosted path is `POST /v1/ranking` (hosted is `.../reranking`), same `{model, query:{text}, passages:[{text}], truncate}` body, up to 256 passages per request. FP8 silently falls back to FP16 on GB10 | [support matrix](https://docs.nvidia.com/nim/nemo-retriever/reranking/2.3/support-matrix.html), [getting started](https://docs.nvidia.com/nim/nemo-retriever/reranking/2.3/getting-started.html) |
| Embedder weights are open: `nvidia/Nemotron-3-Embed-1B-BF16`, OpenMDW-1.1, "ready for commercial use". Recipe to reproduce vectors outside NIM: `query: ` / `passage: ` prefixes, mean pooling over the attention mask, L2 normalise, 2048-d. OpenMDW-1.1 is not yet OSI-certified | [model card](https://huggingface.co/nvidia/Nemotron-3-Embed-1B-BF16) |
| Reranker weights: NVIDIA Open Model License plus the Llama 3.2 Community License ("Built with Llama") | [model card](https://huggingface.co/nvidia/llama-nemotron-rerank-vl-1b-v2) |
| **The hosted endpoints we use today are trial-only by contract**: the NVIDIA API Trial Terms of Service give access "for limited trial purposes only and without use of the API Service or Generated Content in production" | [API Trial ToS](https://assets.ngc.nvidia.com/products/api-catalog/legal/NVIDIA%20API%20Trial%20Terms%20of%20Service.pdf) |
| **Self-hosted NIM under the free Developer Program is also dev/test only** ("prototyping, research, development and testing purposes only", up to 16 GPUs). Production use of NIM "requires an NVIDIA AI Enterprise license" | [NIM product docs](https://docs.api.nvidia.com/nim/docs/product) |
| "NVIDIA AI Enterprise — DGX Spark" is a separate entitlement, not bundled with the hardware; a free 90-day licence exists with community support only; no published price. Inception's page lists credits and hardware pricing, not an NVAIE discount | [DGX Spark support](https://docs.nvidia.com/dgx/dgx-spark/support.html), [Inception](https://www.nvidia.com/en-us/startups/) |
| sm_121 is young: stock PyPI PyTorch wheels target up to sm_120 (binary-compatible for most work); `nvcr.io/nvidia/pytorch:25.12-py3` is the known-good container; vLLM has an open sm_121/aarch64 issue; HF text-embeddings-inference ships an experimental GB10 image `ghcr.io/huggingface/text-embeddings-inference:121-1.9` | [PyTorch forum](https://discuss.pytorch.org/t/dgx-spark-gb10-cuda-13-0-python-3-12-sm-121/223744), [vLLM #36821](https://github.com/vllm-project/vllm/issues/36821), [TEI README](https://github.com/huggingface/text-embeddings-inference/blob/main/README.md) |
| Local LLMs, measured on Spark: gpt-oss-20b (Apache-2.0) ~3,670 prefill / ~83 decode tok/s (NVIDIA; community llama.cpp ~2,000 / ~61); gpt-oss-120b ~1,725 / ~55; dense Llama 3.1 70B only 2.7 tok/s decode. MoE with few active parameters is what this bandwidth suits | [NVIDIA blog](https://developer.nvidia.com/blog/how-nvidia-dgx-sparks-performance-enables-intensive-ai-tasks), [LMSYS review](https://www.lmsys.org/blog/2025-10-13-nvidia-dgx-spark/) |
| pgvector: HNSW indexes cap at 2,000 dims for `vector` but **4,000 for `halfvec`**, so our 2048-d vectors need `halfvec`. Build with `make OPTFLAGS=""` if the image must run on other machines | [pgvector README](https://github.com/pgvector/pgvector) |
| Not verified: any published reranker or embedder latency on GB10; that the 2.3 embed NIM actually works on a real Spark (the one field report found is a *different, older* embed NIM failing with `cudaErrorSymbolNotFound`); forum reports of thermal throttling under sustained load | treat as unknown until measured |

**The fact that decides what the Spark is for:** 273 GB/s of memory bandwidth, not 128 GB of capacity.
Small encoders (our embedder and reranker, ~1B parameters) are compute-bound and should be fast.
Large dense models are slow single-stream. MoE models with few active parameters are fine for batch.

---

## 3. Owners

| Who | Owns |
|---|---|
| Sai | Licensing and NVIDIA contact, legal read, network placement, go/no-go at each gate |
| Claude (in session) | Prep scripts, measurements, integration code, docs |
| Shrey | Middleware reachability to the Spark, auth in front of it |
| Whoever runs IT | Physical placement, power, network access, updates policy |

---

## 4. Phase 0 — before it arrives (this week)

| # | Task | Owner | Done when |
|---|---|---|---|
| P0.1 | Request the **NVAIE — DGX Spark 90-day trial**. Forum reports show certificates arriving "on hold", so start now | Sai | trial certificate in hand, or a date for it |
| P0.2 | **Legal read** of OpenMDW-1.1 (embedder), NVIDIA Open Model License + Llama 3.2 Community License (reranker) for commercial self-hosting | Sai | yes / no / conditions, written down |
| P0.3 | Decide where the Spark sits on the network and who administers it | Sai + IT | hostname, IP, SSH access list, update policy |
| P0.4 | Raise the **API Trial ToS** finding with NVIDIA alongside N1: production traffic runs on endpoints whose terms exclude production | Sai | answer recorded against N1 |
| P0.5 | Write the prep scripts (§10) so day 0 is measurement, not setup | Claude | scripts committed with tests; each runs on the Mac against hosted endpoints first |
| P0.6 | Make the reranker backend's endpoint configurable: base URL and path (hosted `.../reranking` vs self-hosted `/v1/ranking`), no key required for a LAN endpoint | Claude | backend tests cover both; hosted behaviour unchanged |
| P0.7 | Make `rag.embed()` work against a self-hosted endpoint: today it requires `NVIDIA_API_KEY` even when `RAG_EMBED_BASE` points at a keyless NIM, and it has no fallback endpoint | Claude | keyless base URL works; ordered fallback list (Spark, then hosted) with the fallback recorded |
| P0.8 | Freeze the comparison fixtures: the 159 reranker pools and the Mac latency baseline, the golden held-out sample, 200 chunks + 200 queries for embedding parity | Claude | fixtures tracked under `engine/rag/golden/spark/`, hashes recorded |

---

## 5. Phase 1 — day 0 bring-up (half a day)

Run in order; stop and record at the first failure.

1. First boot, DGX OS updates, create user accounts, enable SSH (or NVIDIA Sync for the desktop link).
2. `docker run --rm --gpus all nvcr.io/nvidia/pytorch:25.12-py3 nvidia-smi` — confirms the driver, container toolkit and GPU.
3. NGC login (`docker login nvcr.io`, NGC API key).
4. Clone the repo; copy `engine/rag/_index_v3` (336 MB) across. **Never** point bulk work at the shared Qdrant.
5. Inside the PyTorch container: `cd engine/rag && RAG_STORE=local RAG_INDEX=./_index_v3 python3 -m pytest -q`. Same count as the Mac.
6. Pull and start both NIMs (2.3), wait for `GET /v1/health/ready`, send one request each.
7. Record versions: DGX OS, driver, CUDA, container tags, NIM tags.

**Gate:** tests pass and both NIMs answer. If a NIM fails the way the older embed NIM did
(`cudaErrorSymbolNotFound`), fall back to the open-weights path for that model (§6, W3) and report it to NVIDIA.

---

## 6. Phase 2 — week 1 measurements

Each item has a gate. Nothing is adopted until its gate passes.

| # | Measure | Gate |
|---|---|---|
| W1 | **Reranker NIM** (same model as hosted) on the 159 frozen pools: latency at pools 10 / 20 / 40, and ranking agreement with the hosted endpoint | p90 < 1s at pool 40; top-1 agreement ≥ 99% and recall@1/5 within 0.005 of the hosted run (FP16 on GB10 vs whatever hosted uses, so logits will not be bit-identical) |
| W2 | **Embedding NIM** parity: embed the 200 frozen chunks and 200 queries on both | cosine ≥ 0.999 per vector **and** golden top-1 identical on the held-out sample. If it fails: try the open-weights recipe (sentence-transformers `encode_query` / `encode_document`, BF16). If that fails too, plan a full re-embed on the Spark and a migrate — destructive to the shared collection, so ask first |
| W3 | **Open-weights fallbacks**: bge-reranker-v2-m3 and the embedder via TEI `121-1.9` | latency recorded; decides whether the vendor-free fallback runs on the Spark or stays on the Mac |
| W4 | **Full-scale golden eval**: reproduce the Mac numbers on the same sample first, then all 11,651 cases, tuning sweeps on all held-out cases | reproduction within 0.002; the +2.0-point tuning gain re-measured at full n |
| W5 | **Stress test**: 2 hours of sustained reranker + embedder load at production shape, logging temperature, clocks, errors | no throttling below the W1 latency gate, no crashes. Forum reports of thermal shutdowns make this a hard gate before any always-on use |

---

## 7. Phase 3 — week 2: integration

| # | Task | Done when |
|---|---|---|
| I1 | Add the Spark to the validation chain as its own backend (the reranker backend pointed at the Spark URL): `RAG_VALIDATOR=jev,spark,nemotron,local`. The Spark goes **ahead of** hosted, which moves production traffic off the trial endpoint | chain tests; `judge.py check` shows all four; trace records which backend answered |
| I2 | Embeddings via the Spark with the hosted endpoint as fallback (P0.7) | parity gate held; the trace records which endpoint embedded each query |
| I3 | Front the Spark with a reverse proxy, token auth and TLS. NVIDIA Sync, SSH tunnels and Tailscale are developer conveniences, not production ingress | middleware reaches it through the proxy only; unauthenticated requests refused |
| I4 | Monitoring: health checks on both NIMs, temperature and uptime logging, alert when a backend falls through | a fall-through is visible without reading logs |
| I5 | Docs: README component sections, ADR for the Spark backend and the endpoint fallback | written and linked |

---

## 8. Phase 4 — production licensing decision

Three paths; choose after P0.1, P0.2 and W1–W5.

| Path | What it is | Needs | Trade-off |
|---|---|---|---|
| **A. NVAIE — DGX Spark** | Run the NIM containers in production | purchased entitlement after the 90-day trial | supported, same containers we measured; ongoing cost, price not published |
| **B. Open weights, our own serving** | Same models via TEI / sentence-transformers, no NIM | P0.2 legal yes; W3 latency acceptable | no NIM licence; we own the serving stack on a young platform |
| **C. Hosted, paid** | Keep the hosted endpoints on a production subscription | commercial agreement with NVIDIA | no hardware dependency; keeps the vendor in the path |

Whatever is chosen, **one Spark is a single point of failure**. Production keeps a second path
(hosted paid, or a cloud GPU on AWS / Nebius) as the fallback in the chain. The Spark is an on-prem
node plus the dev and eval box, not the only production host.

---

## 9. Phase 5 — offline batch work (weeks 2–3)

| # | Job | Model / stack | Estimate | Gate |
|---|---|---|---|---|
| B1 | Pre-label the ~300 brief-shaped calibration pairs (plan step 6) | gpt-oss-20b via llama.cpp or Ollama | minutes | agreement with Sai's labels reported; the model's label is never ground truth |
| B2 | Revisit playbook context notes: 100-section sample first | gpt-oss-20b | full 1,200 notes ~19–28 min sequential (from the measured prefill/decode rates, ~800-token prompts, ~60-token outputs) | golden playbook slice improves on the sample, else it stays declined |
| B3 | Qdrant vs Postgres: Postgres + pgvector with `halfvec(2048)` + HNSW, a `store_pgvector.py` from `store_template.py`, the same golden eval | — | a day | recall within 0.005 of local / Qdrant; latency per brief measured |
| B4 | Confidential dossier inference | — | — | only after per-agency stores (foundation-spec S8) exist |

Stack choice for LLM batch jobs: **llama.cpp / Ollama first** — measured on Spark and not affected by
vLLM's open sm_121 issue. vLLM only through NVIDIA's Spark-validated container.

---

## 10. Prep scripts (P0.5)

All live under `engine/rag/spark/`, run on the Mac against hosted endpoints first so the Spark run
is a comparison, not a first attempt.

| Script | Does |
|---|---|
| `bench_rerank.py` | Latency p50 / p90 / max at pools 10 / 20 / 40 for any backend in `judge.py`, on the frozen pools; recall@k and top-1 agreement against a reference run |
| `embed_parity.py` | Embeds the frozen 200 + 200 via two endpoints; per-vector cosine, and golden top-1 agreement using each endpoint's query vectors against the existing index |
| `eval_full.sh` | Golden eval and both tuning rounds with the holdout-safe builder, writes JSON under `golden/spark/` |
| `stress.sh` | Sustained mixed load for N minutes; logs `nvidia-smi` temperature and clocks, latency and errors per minute |

---

## 11. Risks

| Risk | Mitigation |
|---|---|
| Production traffic today is outside the hosted endpoints' trial terms | P0.4 now; I1 moves traffic to the Spark; §8 settles it properly |
| The 2.3 NIMs fail on real hardware | open-weights path (W3); report to NVIDIA |
| Embedding parity fails, so the index would be invalid | open-weights recipe, then a planned re-embed and migrate, never a silent switch |
| Thermal throttling or shutdowns under sustained load | W5 stress gate; monitoring (I4); fallbacks stay in the chain |
| sm_121 software gaps | NVIDIA containers first; pin versions recorded on day 0 |
| Single box | never the only production path (§8) |
| OpenMDW-1.1 not OSI-certified | P0.2 legal read before path B |

---

## 12. Timeline

| When | What |
|---|---|
| Week of 2026-09-23 | Phase 0 |
| Arrival day | Phase 1 |
| Arrival week | Phase 2 (W1–W5) |
| Following week | Phase 3 and the start of Phase 5 |
| After the gates, P0.1 and P0.2 | Phase 4 decision |
