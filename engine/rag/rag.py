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

def _load_dotenv() -> None:
    """Load briefing/.env (next to this rag/ dir) into os.environ without overriding
    variables already set. No dependency; so `python3 rag.py …` works without sourcing."""
    env = Path(__file__).resolve().parent.parent / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:]
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


_load_dotenv()

EMBED_MODEL = os.environ.get("RAG_EMBED_MODEL", "nvidia/nemotron-3-embed-1b")  # 2048-d; nv-embedqa-e5-v5 retired (410) Sep 2026
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
# Store layer — pluggable. rag.py never touches a backend directly; it asks
# store_base.get_store() for whatever RAG_STORE names (local | qdrant | ...).
# ---------------------------------------------------------------------------

from store_base import VectorStore, get_store, list_stores, store_name, StoreConfigError  # noqa: E402
from store_local import LocalStore  # noqa: E402


def _norm(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _store() -> str:                       # kept for older callers
    return store_name()


def open_store(index_dir: Path | None = None, name: str | None = None) -> VectorStore:
    """The store the pipeline should read from. `index_dir` only matters for local."""
    return get_store(name, index_dir=index_dir)


def build(corpus: Path, index_dir: Path):
    """Chunk + embed the corpus. Always writes the LOCAL index (the canonical,
    re-pushable artefact); if RAG_STORE names a remote store, mirrors into it too."""
    files = sorted(glob.glob(str(corpus / "**" / "*.md"), recursive=True))
    files = [f for f in files if "DROP-ZIPS-HERE" not in f]
    if not files:
        sys.exit(f"No .md files under {corpus}. Drop the playbooks in and re-run.")
    chunks: list[dict] = []
    for f in files:
        chunks.extend(chunk_file(Path(f)))
    print(f"  {len(files)} files → {len(chunks)} chunks")
    vecs, mode = embed([embed_text_of(c) for c in chunks], "passage")
    rows = [{**c, "vector": _norm(v)} for c, v in zip(chunks, vecs)]
    dim = len(rows[0]["vector"]) if rows else 0

    local = LocalStore(index_dir)
    local.ensure(dim)
    local.replace_all(rows)
    local.write_manifest({"files": len(files), "embed_mode": mode, "embed_model": EMBED_MODEL,
                          "corpus": str(corpus), "built_at": _now()})
    print(f"  embed mode: {mode}  ·  dim {dim}")
    print(f"  index → {index_dir}")

    if store_name() != "local":
        remote = open_store()
        remote.ensure(dim)
        n = remote.upsert(rows)
        print(f"  mirrored {n} rows → {remote.describe()['label']}")


def _now() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_index(index_dir: Path) -> list[dict]:
    return list(LocalStore(index_dir).scroll())


def store_available(index_dir: Path | None = None) -> bool:
    """Is the configured store queryable (reachable and non-empty)?"""
    try:
        return open_store(index_dir).available()
    except Exception:                       # misconfigured or unreachable → not available
        return False


def migrate(src: str, dst: str, src_index: Path | None = None, dst_index: Path | None = None,
            replace: bool = False, batch: int = 256, force: bool = False) -> int:
    """Copy every row (with vectors) from one store to another. No re-embedding.
    This is how you switch backends: build once, migrate anywhere."""
    if src == dst and (src != "local" or src_index == dst_index):
        sys.exit("migrate: source and destination are the same store")
    try:
        s = get_store(src, index_dir=src_index)
        d = get_store(dst, index_dir=dst_index)
    except StoreConfigError as e:
        sys.exit(f"migrate: {e}\n  (rag.py reads briefing/.env automatically — check the variable is set there)")
    if not s.available():
        sys.exit(f"migrate: source {s.describe()['label']} is empty or unreachable")
    # Guard: vectors must come from the model queries will use, or retrieval is silently wrong.
    if isinstance(s, LocalStore):
        built_with = s.manifest().get("embed_model") or (s.manifest().get("embed_mode", "").split(":", 1)[-1] or None)
        if built_with and built_with != EMBED_MODEL and not force:
            sys.exit(f"migrate: source index was embedded with {built_with!r} but the current embed model is "
                     f"{EMBED_MODEL!r}. Queries would not match these vectors.\n"
                     f"  Rebuild first:  python3 rag.py build --index {s.index_dir}\n"
                     f"  or pass --force to push anyway.")
    if replace:
        d.delete_all()
    n, buf, dim = 0, [], None
    for row in s.scroll():
        if dim is None:
            dim = len(row["vector"]); d.ensure(dim)
        buf.append(row)
        if len(buf) >= batch:
            n += d.upsert(buf); buf = []
            print(f"  {n} rows → {d.describe()['label']}", end="\r", flush=True)
    if buf:
        n += d.upsert(buf)
    if isinstance(d, LocalStore):
        src_man = s.manifest() if isinstance(s, LocalStore) else {}
        d.write_manifest({k: src_man.get(k) for k in ("files", "embed_mode", "embed_model", "corpus") if src_man.get(k)}
                         | {"migrated_from": s.describe()["label"], "built_at": _now()})
    print(f"  migrated {n} rows  {s.describe()['label']}  →  {d.describe()['label']}")
    return n


def push(index_dir: Path) -> int:
    """Back-compat: local index → the store named by RAG_STORE."""
    if store_name() == "local":
        sys.exit("push requires RAG_STORE to name a remote store (e.g. qdrant).")
    return migrate("local", store_name(), src_index=index_dir)


def search(index_dir: Path, q: str, k: int = 5, where: dict | None = None
           ) -> list[tuple[float, dict]]:
    """Embed the query and return the top-k (score, chunk) rows. The single search
    code path — both the `query` CLI and retrieve.py (Loops 3–7) call this."""
    store = open_store(index_dir)
    qv, _ = embed([q], "query")
    return store.search(_norm(qv[0]), k=k, where=where)


def query(index_dir: Path, q: str, k: int = 5, where: dict | None = None):
    scored = search(index_dir, q, k=k, where=where)
    if not scored:
        print("No chunks match the metadata filter."); return []
    offline = not os.environ.get("NVIDIA_API_KEY") or os.environ.get("RAG_EMBED") == "offline"
    mode = "offline" if offline else f"nim:{EMBED_MODEL}"
    print(f"\nQuery: {q!r}   [embed: {mode} · store: {open_store(index_dir).describe()['label']}]\n")
    for rank, (score, r) in enumerate(scored, 1):
        snippet = re.sub(r"\s+", " ", r["text"])[:160]
        print(f"{rank}. [{score:.3f}] {r['source']} › {r['section']}")
        print(f"     {snippet}…\n")
    return scored


def stores_status(index_dir: Path):
    """Print every registered backend and whether it is configured / populated."""
    active = store_name()
    for n in list_stores():
        try:
            st = get_store(n, index_dir=index_dir)
            ok = st.available()
            cnt = st.count() if ok else 0
            print(f"  {'*' if n == active else ' '} {n:10} {st.describe()['label']:50} "
                  f"{'ready' if ok else 'empty'}  rows={cnt}")
        except StoreConfigError as e:
            print(f"  {'*' if n == active else ' '} {n:10} not configured — {e}")
        except Exception as e:                       # network etc.
            print(f"  {'*' if n == active else ' '} {n:10} error — {type(e).__name__}: {e}")


def store_check(name: str, index_dir: Path) -> bool:
    """Contract test: ensure → upsert 3 offline rows → search → scroll → count.
    Uses ids prefixed `__check__` and removes nothing else."""
    st = get_store(name, index_dir=index_dir)
    dim = OFFLINE_DIM
    vecs = _offline_embed(["alpha brand launch", "beta turnaround case", "gamma proposition"], dim)
    rows = [{"id": f"__check__{i}", "source": "__check__.md", "section": f"s{i}", "chunk_index": i,
             "metadata": {"source": "__check__", "year": "2000"}, "text": t, "retrieval_queries": "",
             "vector": _norm(v)} for i, (t, v) in enumerate(zip(["alpha", "beta", "gamma"], vecs))]
    st.ensure(dim)
    assert st.upsert(rows) == 3, "upsert count"
    hits = st.search(rows[1]["vector"], k=1, where={"source": "__check__"})
    assert hits and hits[0][1]["id"] == "__check__1", f"search returned {hits[:1]}"
    seen = {r["id"] for r in st.scroll() if str(r.get("id", "")).startswith("__check__")}
    assert seen == {r["id"] for r in rows}, f"scroll missing rows: {seen}"
    assert st.count() >= 3, "count"
    print(f"  {name}: contract OK ({st.describe()['label']})")
    return True


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
    pp = sub.add_parser("push", help="local index → store named by RAG_STORE (alias of migrate)")
    pp.add_argument("--index", default="./index")
    mg = sub.add_parser("migrate", help="copy rows+vectors between any two stores (no re-embed)")
    mg.add_argument("--from", dest="src", required=True, help="local | qdrant | ...")
    mg.add_argument("--to", dest="dst", required=True)
    mg.add_argument("--index", default="./index", help="local source index dir")
    mg.add_argument("--to-index", default=None, help="local destination index dir")
    mg.add_argument("--replace", action="store_true", help="wipe destination first")
    mg.add_argument("--force", action="store_true", help="push even if the source embed model differs from the current one")
    ss = sub.add_parser("stores", help="list backends and their status"); ss.add_argument("--index", default="./index")
    sc = sub.add_parser("store-check", help="run the VectorStore contract against a backend")
    sc.add_argument("name", nargs="?", default=None); sc.add_argument("--index", default="./_index_check")
    a = ap.parse_args()
    here = Path(__file__).resolve().parent
    def _abs(x): return Path(x) if Path(x).is_absolute() else here / x
    idx = _abs(a.index)
    if a.cmd == "build":
        build(_abs(a.corpus).resolve(), idx)
    elif a.cmd == "push":
        push(idx)
    elif a.cmd == "migrate":
        migrate(a.src.lower(), a.dst.lower(), src_index=idx,
                dst_index=_abs(a.to_index) if a.to_index else None, replace=a.replace, force=a.force)
    elif a.cmd == "stores":
        stores_status(idx)
    elif a.cmd == "store-check":
        store_check(store_name(a.name), idx)
    else:
        where = None
        if a.where and "=" in a.where:
            kk, vv = a.where.split("=", 1); where = {kk: vv}
        query(idx, a.query, a.k, where)


if __name__ == "__main__":
    main()
