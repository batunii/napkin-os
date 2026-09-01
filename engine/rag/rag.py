#!/usr/bin/env python3
"""
Napkin Briefing — Planner/Effectiveness RAG  (Loops 3–7 retrieval layer)
=======================================================================

The "green lane" of the architecture: build a knowledge base once, retrieve
from it at runtime. Kept SEPARATE from parse_brief.py on purpose — Loop 1
capture stays RAG-free. This module only ever serves Loops 3–7.

Ingestion matches the playbooks' own ingestion guide:
  * one chunk per H2 (`## `) section
  * ~10% word overlap between consecutive chunks
  * YAML frontmatter attached to every chunk as metadata (parsed generically —
    any keys, so it fits the real frontmatter without hard-coding fields)
  * `RETRIEVAL_QUERIES` (a section or frontmatter key) indexed as extra recall text

Embeddings: NVIDIA NIM (your Inception key) via the OpenAI-compatible
/embeddings endpoint. Falls back to a deterministic offline embedder when no key
is present, so the pipeline runs and can be tested anywhere.

Store: local JSON index now; swappable to Qdrant later (one adapter, see STORES).

Usage:
    python rag.py build  --corpus ../reference/rag  --index ./index
    python rag.py query  --index ./index  "challenger brand, low salience"  -k 5
    python rag.py query  --index ./index  "..."  --where type=positioning
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import math
import os
import re
import sys
import urllib.request
import urllib.error
from pathlib import Path

EMBED_MODEL = os.environ.get("RAG_EMBED_MODEL", "nvidia/nv-embedqa-e5-v5")  # 1024-d
EMBED_BASE = os.environ.get("RAG_EMBED_BASE", "https://integrate.api.nvidia.com/v1")
OFFLINE_DIM = 512
BATCH = 32

# ---------------------------------------------------------------------------
# Frontmatter + markdown chunking  (matches 00-rag-ingestion-guide.md)
# ---------------------------------------------------------------------------

def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split leading --- ... --- block. Uses PyYAML if available, else a small
    generic parser (key: value, key: [a, b]). Returns (meta, body)."""
    text = text.lstrip("﻿")                      # strip UTF-8 BOM if present
    # tolerate leading blank lines/whitespace before the opening --- (real corpus
    # playbooks start with a leading \n before the frontmatter fence)
    m = re.match(r"^\s*---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.DOTALL)
    if not m:
        return {}, text
    raw, body = m.group(1), m.group(2)
    try:
        import yaml
        meta = yaml.safe_load(raw) or {}
        if isinstance(meta, dict):
            return meta, body
    except Exception:
        pass
    meta: dict = {}
    for line in raw.splitlines():
        if ":" not in line or line.strip().startswith("#"):
            continue
        k, v = line.split(":", 1)
        k, v = k.strip(), v.strip()
        if v.startswith("[") and v.endswith("]"):
            meta[k] = [x.strip().strip("'\"") for x in v[1:-1].split(",") if x.strip()]
        else:
            meta[k] = v.strip("'\"")
    return meta, body


def split_h2(body: str) -> list[tuple[str, str]]:
    """Return [(heading, section_text), ...], splitting at section headers.

    The real corpus is inconsistent: some playbooks mark their 9 sections with
    `## ` (H2), others with `# ` (H1). Within a file the level is consistent, so
    we split at H1 *or* H2 and leave H3+ (### RETRIEVAL_QUERIES, sub-blueprints)
    inside the section, matching the ingestion guide's intent (~9 chunks/file)."""
    sections, heading, buf = [], "(intro)", []
    for line in body.splitlines():
        if re.match(r"^#{1,2}\s+(?!#)", line):       # H1 or H2, not H3+
            if buf:
                sections.append((heading, "\n".join(buf).strip()))
            heading = line.lstrip("# ").strip()
            buf = []
        else:
            buf.append(line)
    if buf:
        sections.append((heading, "\n".join(buf).strip()))
    return [(h, t) for h, t in sections if t]


def _overlap_prefix(prev_text: str, pct: float = 0.10) -> str:
    words = prev_text.split()
    n = max(0, int(len(words) * pct))
    return " ".join(words[-n:]) if n else ""


def chunk_file(path: Path) -> list[dict]:
    meta, body = parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
    # pull RETRIEVAL_QUERIES (frontmatter key or a body section) for extra recall
    retrieval_q = meta.get("RETRIEVAL_QUERIES") or meta.get("retrieval_queries") or ""
    sections = split_h2(body)
    chunks, prev = [], ""
    for i, (heading, text) in enumerate(sections):
        if re.search(r"retrieval[_ ]queries", heading, re.I):
            retrieval_q = (retrieval_q + "\n" + text) if retrieval_q else text
            continue
        overlap = _overlap_prefix(prev) if i else ""
        body_text = (overlap + "\n" + text).strip() if overlap else text
        cid = hashlib.sha1(f"{path.name}:{i}:{heading}".encode()).hexdigest()[:12]
        chunks.append({
            "id": cid,
            "source": path.name,
            "section": heading,
            "chunk_index": i,
            "metadata": meta,                       # generic — whatever frontmatter has
            "text": body_text,
            "retrieval_queries": retrieval_q if isinstance(retrieval_q, str)
                                 else " ".join(retrieval_q or []),
        })
        prev = text
    return chunks


def embed_text_of(chunk: dict) -> str:
    """What we actually embed: heading + body + any retrieval queries."""
    parts = [chunk["section"], chunk["text"]]
    if chunk.get("retrieval_queries"):
        parts.append(chunk["retrieval_queries"])
    return "\n".join(p for p in parts if p)[:8000]


# ---------------------------------------------------------------------------
# Embeddings: NIM (Inception key) with deterministic offline fallback
# ---------------------------------------------------------------------------

def _offline_embed(texts: list[str], dim: int = OFFLINE_DIM) -> list[list[float]]:
    """Deterministic hashing bag-of-words embedder. No network, for dev/tests."""
    out = []
    for t in texts:
        v = [0.0] * dim
        for tok in re.findall(r"[a-z0-9]+", t.lower()):
            h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
            v[h % dim] += 1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        out.append([x / norm for x in v])
    return out


def _nim_embed(texts: list[str], input_type: str, key: str) -> list[list[float]]:
    body = json.dumps({"model": EMBED_MODEL, "input": texts,
                       "input_type": input_type, "encoding_format": "float",
                       "truncate": "END"}).encode()   # nv-embedqa caps input at 512 tok
    req = urllib.request.Request(
        EMBED_BASE.rstrip("/") + "/embeddings", data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    # Retry transient failures (502/503/504, timeouts) with backoff — a single blip
    # shouldn't abort a large multi-batch build.
    import time as _t
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.loads(r.read())
            return [d["embedding"] for d in data["data"]]
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 4:
                _t.sleep(2 * (attempt + 1)); continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < 4:
                _t.sleep(2 * (attempt + 1)); continue
            raise


def embed(texts: list[str], input_type: str = "passage") -> tuple[list[list[float]], str]:
    """Returns (vectors, mode). input_type: 'passage' for docs, 'query' for queries."""
    key = os.environ.get("NVIDIA_API_KEY")
    if not key or os.environ.get("RAG_EMBED") == "offline":
        return _offline_embed(texts), "offline"
    vecs: list[list[float]] = []
    for i in range(0, len(texts), BATCH):
        vecs.extend(_nim_embed(texts[i:i + BATCH], input_type, key))
    return vecs, f"nim:{EMBED_MODEL}"


# ---------------------------------------------------------------------------
# Local store (swappable). Qdrant adapter is a thin drop-in later.
# ---------------------------------------------------------------------------

def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))   # vectors are L2-normalised at store time


def _norm(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def build(corpus: Path, index_dir: Path):
    files = sorted(glob.glob(str(corpus / "**" / "*.md"), recursive=True))
    files = [f for f in files if "DROP-ZIPS-HERE" not in f]
    if not files:
        sys.exit(f"No .md files under {corpus}. Drop the playbooks in and re-run.")
    chunks: list[dict] = []
    for f in files:
        chunks.extend(chunk_file(Path(f)))
    print(f"  {len(files)} files → {len(chunks)} chunks")
    vecs, mode = embed([embed_text_of(c) for c in chunks], "passage")
    vecs = [_norm(v) for v in vecs]
    index_dir.mkdir(parents=True, exist_ok=True)
    with open(index_dir / "chunks.jsonl", "w", encoding="utf-8") as fh:
        for c, v in zip(chunks, vecs):
            fh.write(json.dumps({**c, "vector": v}) + "\n")
    (index_dir / "manifest.json").write_text(json.dumps({
        "files": len(files), "chunks": len(chunks), "embed_mode": mode,
        "dim": len(vecs[0]) if vecs else 0}, indent=2))
    print(f"  embed mode: {mode}  ·  dim {len(vecs[0]) if vecs else 0}")
    print(f"  index → {index_dir}")


def load_index(index_dir: Path) -> list[dict]:
    rows = []
    with open(index_dir / "chunks.jsonl", encoding="utf-8") as fh:
        for line in fh:
            rows.append(json.loads(line))
    return rows


def _store() -> str:
    return os.environ.get("RAG_STORE", "local").lower().strip()


def store_available(index_dir: Path | None = None) -> bool:
    """Is the index queryable? Local: chunks.jsonl exists. Qdrant: collection has points."""
    if _store() == "qdrant":
        import store_qdrant
        return store_qdrant.available()
    return index_dir is not None and (Path(index_dir) / "chunks.jsonl").exists()


def push(index_dir: Path) -> int:
    """Upsert an EXISTING local index into the remote store (no re-embedding).
    One-time migration: build locally once, then `push` to Qdrant."""
    if _store() != "qdrant":
        sys.exit("push requires RAG_STORE=qdrant (set QDRANT_URL/API_KEY/COLLECTION).")
    import store_qdrant
    rows = load_index(index_dir)
    if not rows:
        sys.exit(f"No local index at {index_dir} to push. Build it first.")
    store_qdrant.ensure_collection(dim=len(rows[0]["vector"]))
    n = store_qdrant.upsert(rows)
    print(f"  pushed {n} points → Qdrant collection '{store_qdrant.collection_name()}'")
    return n


def search(index_dir: Path, q: str, k: int = 5, where: dict | None = None
           ) -> list[tuple[float, dict]]:
    """Embed the query and return the top-k (score, chunk) rows. The single search
    code path — both the `query` CLI and retrieve.py (Loops 3–7) call this.
    Routes to the remote store when RAG_STORE=qdrant."""
    if _store() == "qdrant":
        import store_qdrant
        qv, _ = embed([q], "query")
        return store_qdrant.search(_norm(qv[0]), k=k, where=where)
    rows = load_index(index_dir)
    if where:
        # EXACT match, mirroring Qdrant's keyword filter — a pack must behave
        # identically whichever store it lives in (substring matching here once
        # made local and remote return different sets for the same filter).
        rows = [r for r in rows if all(
            str(r["metadata"].get(where_k, "")).lower() == str(where_v).lower()
            for where_k, where_v in where.items())]
        if not rows:
            return []
    qv, _ = embed([q], "query")
    qv = _norm(qv[0])
    scored = sorted(((_cosine(qv, r["vector"]), r) for r in rows),
                    key=lambda x: x[0], reverse=True)
    return scored[:k]


def query(index_dir: Path, q: str, k: int = 5, where: dict | None = None):
    scored = search(index_dir, q, k=k, where=where)
    if not scored:
        print("No chunks match the metadata filter."); return []
    offline = not os.environ.get("NVIDIA_API_KEY") or os.environ.get("RAG_EMBED") == "offline"
    mode = "offline" if offline else f"nim:{EMBED_MODEL}"
    print(f"\nQuery: {q!r}   [embed: {mode}]\n")
    for rank, (score, r) in enumerate(scored, 1):
        snippet = re.sub(r"\s+", " ", r["text"])[:160]
        print(f"{rank}. [{score:.3f}] {r['source']} › {r['section']}")
        print(f"     {snippet}…\n")
    return scored


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Napkin planner/effectiveness RAG")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build"); b.add_argument("--corpus", default="../reference/rag")
    b.add_argument("--index", default="./index")
    qp = sub.add_parser("query"); qp.add_argument("query")
    qp.add_argument("--index", default="./index"); qp.add_argument("-k", type=int, default=5)
    qp.add_argument("--where", help="metadata filter key=value")
    pp = sub.add_parser("push", help="upsert an existing local index into the remote store (RAG_STORE=qdrant)")
    pp.add_argument("--index", default="./index")
    a = ap.parse_args()
    here = Path(__file__).resolve().parent
    idx = Path(a.index) if Path(a.index).is_absolute() else here / a.index
    if a.cmd == "build":
        corp = Path(a.corpus) if Path(a.corpus).is_absolute() else here / a.corpus
        build(corp.resolve(), idx)
    elif a.cmd == "push":
        push(idx)
    else:
        where = None
        if a.where and "=" in a.where:
            kk, vv = a.where.split("=", 1); where = {kk: vv}
        query(idx, a.query, a.k, where)


if __name__ == "__main__":
    main()
