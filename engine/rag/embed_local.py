#!/usr/bin/env python3
"""
embed_local.py — the SAME embedding model as the index, run on this machine.

    python3 embed_local.py --download        # fetch nvidia/Nemotron-3-Embed-1B-BF16 (~2.2 GB) once
    python3 embed_local.py --parity          # compare with the hosted endpoint on golden queries

Why: every brief embeds its queries at run time, and today only the hosted NVIDIA trial
endpoint can do that — a single point of failure (and trial-only by its terms). The
index's 7,392 stored vectors were made by nvidia/nemotron-3-embed-1b, so a fallback is
only valid if it is the same model producing the same vectors. This module runs the open
weights (OpenMDW-1.1) with the model card's recipe: `query: ` / `passage: ` prefixes via
sentence-transformers' encode_query / encode_document, mean pooling, L2-normalised,
2048-d. rag.embed() uses it when RAG_EMBED_FALLBACK includes `local`.

Rules
  * Never downloads at run time (local_files_only): a brief must not stall on 2.2 GB.
    Missing weights -> LocalEmbedUnavailable, and rag.embed() moves on to keyword-only.
  * Loaded once per process (thread-safe), on mps if available, else cpu
    (RAG_EMBED_LOCAL_DEVICE overrides).
  * Do not rely on it until `--parity` passes: cosine >= PARITY_MIN_COSINE per query and
    identical golden top-1 on the sample. BF16 here vs whatever the hosted build runs can
    drift slightly; the gate decides whether that drift matters.
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

MODEL = os.environ.get("RAG_EMBED_LOCAL_MODEL", "nvidia/Nemotron-3-Embed-1B-BF16")
PARITY_MIN_COSINE = 0.999
_LOCK = threading.Lock()
_MODEL = None


class LocalEmbedUnavailable(RuntimeError):
    """The local embedder cannot run here: no weights, no library, or a load failure."""


def _device() -> str:
    """mps when available, else cpu; RAG_EMBED_LOCAL_DEVICE overrides."""
    d = os.environ.get("RAG_EMBED_LOCAL_DEVICE")
    if d:
        return d
    try:
        import torch
        return "mps" if torch.backends.mps.is_available() else "cpu"
    except Exception:
        return "cpu"


def model():
    """The loaded SentenceTransformer, built once per process from the local HF cache."""
    global _MODEL
    with _LOCK:
        if _MODEL is None:
            try:
                from sentence_transformers import SentenceTransformer
                _MODEL = SentenceTransformer(MODEL, device=_device(), local_files_only=True,
                                             trust_remote_code=True)
            except Exception as e:
                raise LocalEmbedUnavailable(
                    f"local embedder {MODEL} not loadable ({type(e).__name__}: {e}); "
                    f"fetch it once with: python3 embed_local.py --download") from e
        return _MODEL


def embed(texts: list[str], input_type: str = "passage") -> list[list[float]]:
    """L2-normalised 2048-d vectors, the model card's query / passage recipe."""
    m = model()
    fn = m.encode_query if input_type == "query" else m.encode_document
    vecs = fn(texts, normalize_embeddings=True, convert_to_numpy=True)
    return [v.tolist() for v in vecs]


def _download() -> None:
    """Fetch the weights into the HF cache (explicit, never at brief time)."""
    from huggingface_hub import snapshot_download
    t = time.time()
    path = snapshot_download(MODEL, allow_patterns=["*.json", "*.safetensors", "*.py", "*.txt",
                                                    "tokenizer*", "*.model", "modules.json", "*/*.json"])
    print(f"{MODEL} -> {path} ({time.time() - t:.0f}s)")


def _parity(n: int) -> None:
    """Hosted vs local on n held-out golden queries: per-query cosine, and whether each
    query's top-1 hit in the current index is the same with either vector."""
    import golden
    import rag
    os.environ.setdefault("RAG_STORE", "local")
    cases, _ = golden.load_golden(HERE / "golden")
    qs = list(dict.fromkeys(c["query"] for c in cases if c["holdout"]))[:n]
    t = time.time(); hosted = [rag._norm(v) for v in rag._nim_embed(qs, "query", os.environ["NVIDIA_API_KEY"])]
    th = time.time() - t
    t = time.time(); local = embed(qs, "query"); tl = time.time() - t
    cos = [sum(a * b for a, b in zip(h, l)) for h, l in zip(hosted, local)]
    store = rag.open_store(Path(os.environ.get("RAG_INDEX") or HERE / "_index_v4"))
    same = sum(1 for q, h, l in zip(qs, hosted, local)
               if [r["id"] for _, r in store.search(h, k=1)] == [r["id"] for _, r in store.search(l, k=1)])
    ok = min(cos) >= PARITY_MIN_COSINE and same == len(qs)
    print(f"parity on {len(qs)} held-out queries: cosine min {min(cos):.5f} mean {sum(cos)/len(cos):.5f}; "
          f"dense top-1 identical {same}/{len(qs)}; hosted {th:.1f}s, local {tl:.1f}s "
          f"({tl / len(qs) * 1000:.0f} ms/query) -> {'PASS' if ok else 'FAIL'}")


def main() -> None:
    """CLI: --download the weights, or --parity against the hosted endpoint."""
    ap = argparse.ArgumentParser(description="Local copy of the index's embedding model")
    ap.add_argument("--download", action="store_true")
    ap.add_argument("--parity", action="store_true")
    ap.add_argument("-n", type=int, default=200)
    a = ap.parse_args()
    if a.download:
        _download()
    if a.parity:
        import rag  # noqa: F401  (loads engine/.env for the hosted key)
        _parity(a.n)


if __name__ == "__main__":
    main()
