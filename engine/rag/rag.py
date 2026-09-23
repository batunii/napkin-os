#!/usr/bin/env python3
"""
Napkin Briefing — Planner/Effectiveness RAG  (Loops 3–7 retrieval layer)
=======================================================================

The "green lane" of the architecture: build a knowledge base once, retrieve
from it at runtime. Kept SEPARATE from parse_brief.py on purpose — Loop 1
capture stays RAG-free. This module only ever serves Loops 3–7.

Ingestion matches the playbooks' own ingestion guide:
  * per-source chunking (chunking.py): whole-case parents + section children for
    IPA/Effie, whole case for Cannes, sections for playbooks, windows for templates
  * every chunk embedded with a context header (title · year · tier · sector …)
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

# Chunking lives in chunking.py (per-source strategies, context headers, parent/child).
from chunking import (parse_frontmatter, split_sections as split_h2, chunk_file, chunk_corpus,  # noqa: E402,F401
                      embed_text_of, STRATEGY_BY_SOURCE)


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


def _nim_embed(texts: list[str], input_type: str, key: str, attempts: int = 5) -> list[list[float]]:
    """Embed one batch through NVIDIA NIM's OpenAI-compatible /embeddings endpoint.

    `input_type` is 'passage' or 'query'; the model embeds the two differently. Over-long
    input is truncated at the end rather than rejected. HTTP 429/5xx, connection errors
    and timeouts are retried, five attempts in all, waiting 2, 4, 6 then 8 seconds; any
    other HTTP error, or the fifth failure, raises."""
    body = json.dumps({"model": EMBED_MODEL, "input": texts,
                       "input_type": input_type, "encoding_format": "float",
                       "truncate": "END"}).encode()   # nv-embedqa caps input at 512 tok
    req = urllib.request.Request(
        EMBED_BASE.rstrip("/") + "/embeddings", data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    # Retry transient failures (502/503/504, timeouts) with backoff — a single blip
    # shouldn't abort a large multi-batch build.
    import time as _t
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.loads(r.read())
            return [d["embedding"] for d in data["data"]]
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < attempts - 1:
                _t.sleep(2 * (attempt + 1)); continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < attempts - 1:
                _t.sleep(2 * (attempt + 1)); continue
            raise


class EmbedUnavailable(RuntimeError):
    """No embedding endpoint could answer: hosted failed and no fallback succeeded."""


# Query-time fallbacks, in order, after the hosted endpoint (RAG_EMBED_FALLBACK):
#   local    the SAME model on this machine (embed_local.py; weights fetched once)
#   keyword  no vector at all: search falls back to BM25 (see embed_query / search_vec)
# Queries only: an index build never falls back, so a vector-parity miss cannot leak
# into stored vectors. Measured need: every brief embeds its queries through one hosted
# trial endpoint, a single point of failure.
EMBED_FALLBACK = [x.strip() for x in os.environ.get("RAG_EMBED_FALLBACK", "local,keyword").split(",") if x.strip()]


def embed(texts: list[str], input_type: str = "passage") -> tuple[list[list[float]], str]:
    """Returns (vectors, mode). input_type: 'passage' for docs, 'query' for queries.

    For queries, a hosted failure falls back to the local copy of the same model when
    RAG_EMBED_FALLBACK includes `local` (fewer hosted retries first, so failover is quick);
    if that also fails, EmbedUnavailable. Passages never fall back."""
    key = os.environ.get("NVIDIA_API_KEY")
    if not key or os.environ.get("RAG_EMBED") == "offline":
        return _offline_embed(texts), "offline"
    use_local = input_type == "query" and "local" in EMBED_FALLBACK
    try:
        vecs: list[list[float]] = []
        for i in range(0, len(texts), BATCH):
            vecs.extend(_nim_embed(texts[i:i + BATCH], input_type, key, attempts=2 if use_local else 5))
        return vecs, f"nim:{EMBED_MODEL}"
    except Exception as e:
        if not use_local:
            if input_type == "query":
                raise EmbedUnavailable(f"hosted embedding failed: {type(e).__name__}: {e}") from e
            raise
        print(f"[!] hosted embedding failed ({type(e).__name__}); trying the local model", file=sys.stderr)
        try:
            import embed_local
            return embed_local.embed(texts, input_type), f"local:{embed_local.MODEL}"
        except Exception as e2:
            raise EmbedUnavailable(f"hosted and local embedding both failed: {e}; {e2}") from e2


def embed_query(text: str) -> list[float] | None:
    """One normalised query vector, or None when no endpoint answered and
    RAG_EMBED_FALLBACK allows `keyword` — the caller then searches lexically. Retrieval
    degrades, it does not fail."""
    global LAST_QUERY_EMBED
    try:
        vecs, LAST_QUERY_EMBED = embed([text], "query")
        return _norm(vecs[0])
    except EmbedUnavailable as e:
        if "keyword" not in EMBED_FALLBACK:
            raise
        print(f"[!] {e} — keyword-only search for this query", file=sys.stderr)
        LAST_QUERY_EMBED = "keyword-only"
        return None


# Which tier embedded the most recent query (nim:… | local:… | keyword-only | offline),
# so a caller can record it in its trace. Process-global: read it right after the call.
LAST_QUERY_EMBED = ""


# ---------------------------------------------------------------------------
# Store layer — pluggable. rag.py never touches a backend directly; it asks
# store_base.get_store() for whatever RAG_STORE names (local | qdrant | ...).
# ---------------------------------------------------------------------------

from store_base import VectorStore, get_store, list_stores, store_name, StoreConfigError  # noqa: E402
from store_local import LocalStore  # noqa: E402


def _norm(v: list[float]) -> list[float]:
    """L2-normalise a vector so a dot product equals cosine similarity, which every store
    assumes. An all-zero vector comes back unchanged rather than dividing by zero."""
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _store() -> str:                       # kept for older callers
    """The active store name (RAG_STORE, default 'local'). An alias of
    store_base.store_name() kept for older callers such as napkin_packs.py."""
    return store_name()


def open_store(index_dir: Path | None = None, name: str | None = None) -> VectorStore:
    """The store the pipeline should read from. `index_dir` only matters for local."""
    return get_store(name, index_dir=index_dir)


def build(corpus: Path, index_dir: Path, holdout: Path | None = None):
    """Chunk + embed the corpus. Always writes the LOCAL index (the canonical,
    re-pushable artefact); if RAG_STORE names a remote store, mirrors into it too.
    `holdout` = golden/holdout.json: those docs are embedded WITHOUT their retrieval
    queries so the golden eval measures retrieval, not memorised question text."""
    import chunking as _chunking
    _chunking.RQ_HOLDOUT = set(json.loads(Path(holdout).read_text())) if holdout else set()
    files = sorted(glob.glob(str(corpus / "**" / "*.md"), recursive=True))
    files = [f for f in files if "DROP-ZIPS-HERE" not in f]
    if not files:
        sys.exit(f"No .md files under {corpus}. Drop the playbooks in and re-run.")
    chunks = chunk_corpus([Path(f) for f in files])
    print(f"  {len(files)} files → {len(chunks)} chunks")
    vecs, mode = embed([embed_text_of(c) for c in chunks], "passage")
    rows = [{**c, "vector": _norm(v)} for c, v in zip(chunks, vecs)]
    dim = len(rows[0]["vector"]) if rows else 0

    local = LocalStore(index_dir)
    local.ensure(dim)
    local.replace_all(rows)
    by_src: dict[str, int] = {}
    for c in chunks:
        k = f"{c['metadata'].get('source')}/{c['metadata'].get('level')}"
        by_src[k] = by_src.get(k, 0) + 1
    local.write_manifest({"files": len(files), "embed_mode": mode, "embed_model": EMBED_MODEL,
                          "corpus": str(corpus), "built_at": _now(),
                          "rq_holdout_docs": len(_chunking.RQ_HOLDOUT), "holdout_file": str(holdout) if holdout else None,
                          "chunking": {"version": 3, "strategies": STRATEGY_BY_SOURCE, "by_source_level": by_src}})
    print(f"  embed mode: {mode}  ·  dim {dim}")
    print(f"  index → {index_dir}")

    if store_name() != "local":
        remote = open_store()
        remote.ensure(dim)
        n = remote.upsert(rows)
        print(f"  mirrored {n} rows → {remote.describe()['label']}")


def retag(corpus: Path, index_dir: Path, apply: bool = False) -> dict:
    """Refresh metadata on an existing index WITHOUT re-embedding.

    Metadata (category, bucket, section_role, ...) is payload: it is filtered on, never
    embedded. So a change to the contract or to normalise.py only needs the payload
    rewritten, which takes seconds, where a rebuild costs a full embedding pass.

    Safety: a row is only updated when its chunk id AND its embedded text are both
    unchanged. If the text moved, the vector is stale and the honest answer is a rebuild,
    so those rows are reported and left alone."""
    import chunking as _chunking
    files = sorted(glob.glob(str(corpus / "**" / "*.md"), recursive=True))
    files = [f for f in files if "DROP-ZIPS-HERE" not in f]
    fresh = {c["id"]: c for c in _chunking.chunk_corpus([Path(f) for f in files])}
    store = LocalStore(index_dir)
    rows = list(store.scroll())
    changed = stale = missing = 0
    for r in rows:
        f = fresh.get(r["id"])
        if f is None:
            missing += 1; continue
        if _chunking.embed_text_of(f) != _chunking.embed_text_of(r):
            stale += 1; continue                      # vector no longer matches the text
        if f["metadata"] != r["metadata"]:
            changed += 1
            if apply:
                r["metadata"] = f["metadata"]
    report = {"rows": len(rows), "metadata_changed": changed, "text_changed_needs_rebuild": stale,
              "not_in_corpus": missing, "new_chunks": len(set(fresh) - {r["id"] for r in rows}), "applied": apply}
    if apply and changed:
        store.replace_all(rows)
        man = store.manifest(); man["retagged_at"] = _now(); store.write_manifest(
            {k: v for k, v in man.items() if k not in ("chunks", "dim")})
    return report


def _now() -> str:
    """Current UTC time as an ISO-8601 string with a Z suffix, for manifests."""
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_index(index_dir: Path) -> list[dict]:
    """Every row, with vectors, in the local index at `index_dir`. Always reads the local
    files whatever RAG_STORE says. Used by napkin_packs.py."""
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


SEARCH_MODE = os.environ.get("RAG_SEARCH", "hybrid")      # hybrid | dense


def search(index_dir: Path, q: str, k: int = 5, where: dict | None = None, mode: str | None = None
           ) -> list[tuple[float, dict]]:
    """Embed the query and return the top-k (score, chunk) rows. The single search
    code path — both the `query` CLI and retrieve.py (Loops 3–7) call this.
    mode: 'hybrid' (dense + BM25 fused by RRF; default) or 'dense'. Stores without a
    search_hybrid() (Qdrant today) fall back to dense."""
    store = open_store(index_dir)
    return search_vec(store, embed_query(q), q, k=k, where=where, mode=mode)


def search_vec(store, qvec: list[float], q: str, k: int = 5, where: dict | None = None, mode: str | None = None
               ) -> list[tuple[float, dict]]:
    """search() with a pre-computed query vector (the golden eval embeds in batches).
    `qvec` None means no embedding was available: keyword-only search (BM25), or [] if
    the store cannot search lexically."""
    if qvec is None:
        return store.search_lexical(q, k=k, where=where) if hasattr(store, "search_lexical") else []
    mode = (mode or SEARCH_MODE).lower()
    if mode == "hybrid" and hasattr(store, "search_hybrid"):
        return store.search_hybrid(qvec, q, k=k, where=where)
    return store.search(qvec, k=k, where=where)


def query(index_dir: Path, q: str, k: int = 5, where: dict | None = None):
    """The `query` CLI: search, then print each hit's rank, score, citation and a
    160-character snippet. Returns the scored rows, or [] after printing a message when
    nothing matches the filter. The embed label it prints is worked out from the
    environment, not reported by the search."""
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
    """CLI entry point: build | query | push | migrate | retag | stores | store-check.
    Relative paths resolve against the rag/ directory, not the working directory.
    `query --where` takes a single key=value equality filter."""
    ap = argparse.ArgumentParser(description="Napkin planner/effectiveness RAG")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build"); b.add_argument("--corpus", default="../reference/rag")
    b.add_argument("--index", default="./index")
    b.add_argument("--holdout", default=None, help="golden/holdout.json: embed these docs without their retrieval queries")
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
    rt = sub.add_parser("retag", help="refresh metadata on an existing index without re-embedding")
    rt.add_argument("--corpus", default="../reference/rag"); rt.add_argument("--index", default="./index")
    rt.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    ss = sub.add_parser("stores", help="list backends and their status"); ss.add_argument("--index", default="./index")
    sc = sub.add_parser("store-check", help="run the VectorStore contract against a backend")
    sc.add_argument("name", nargs="?", default=None); sc.add_argument("--index", default="./_index_check")
    a = ap.parse_args()
    here = Path(__file__).resolve().parent
    def _abs(x):
        """Resolve a relative CLI path against the rag/ directory."""
        return Path(x) if Path(x).is_absolute() else here / x
    idx = _abs(a.index)
    if a.cmd == "build":
        build(_abs(a.corpus).resolve(), idx, holdout=_abs(a.holdout) if a.holdout else None)
    elif a.cmd == "push":
        push(idx)
    elif a.cmd == "migrate":
        migrate(a.src.lower(), a.dst.lower(), src_index=idx,
                dst_index=_abs(a.to_index) if a.to_index else None, replace=a.replace, force=a.force)
    elif a.cmd == "retag":
        print(json.dumps(retag(_abs(a.corpus).resolve(), idx, apply=a.apply), indent=1))
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
