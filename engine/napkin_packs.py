#!/usr/bin/env python3
"""napkin-packs — one verb for knowledge-pack maintenance: `sync`.

Reconciles the filesystem (a pack IS a directory under the corpus root)
against the local index and, when RAG_STORE=qdrant, the remote store:

  * new / changed docs   -> chunk, embed (only the changed chunks), upsert
  * deactivated packs    -> `_`-prefixed dirname: points deleted, source kept
  * vanished packs       -> orphaned tags deleted from the store
  * everything else      -> untouched

Idempotent — run it after any corpus change, or always. The local index
(chunks.jsonl) doubles as the embedding cache: every row carries a content
hash, and unchanged hashes reuse their stored vector, so editing one document
re-embeds one document, not the corpus.

Also writes rag/packs.lock — the ONLY machine-readable pack list, generated
here and shipped where the corpus doesn't travel (the app). Never hand-edit.

Usage:
    napkin-packs sync [--dry-run]
    napkin-packs status
Env:
    BRIEF_CORPUS      corpus root (default: engine/reference/rag or ../reference/rag)
    RAG_INDEX         local index dir (default: engine/rag/index)
    RAG_STORE         local | qdrant
    NVIDIA_API_KEY    required for real embeddings (RAG_EMBED=offline for tests)
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parent
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(ENGINE / "rag"))

import packs as packs_mod  # noqa: E402
from packs import Pack, discover_packs, corpus_root, write_lock  # noqa: E402
import rag  # noqa: E402


def _index_dir() -> Path:
    return Path(os.environ.get("RAG_INDEX", ENGINE / "rag" / "index"))


def _content_hash(chunk: dict) -> str:
    return hashlib.sha256(rag.embed_text_of(chunk).encode()).hexdigest()[:16]


def _chunk_pack(pack: Pack) -> list[dict]:
    """Chunk every .md in the pack dir; stamp metadata.source = pack.tag so
    dropped-in plain markdown needs no frontmatter at all."""
    assert pack.path is not None
    files = sorted(glob.glob(str(pack.path / "**" / "*.md"), recursive=True))
    files = [f for f in files if "DROP-ZIPS-HERE" not in f]
    chunks: list[dict] = []
    for f in files:
        for c in rag.chunk_file(Path(f)):
            c["metadata"].setdefault("source", pack.tag)
            if c["metadata"]["source"] != pack.tag:
                # frontmatter disagrees with the pack dir -> the dir wins;
                # a pack must be coherent under its own tag.
                c["metadata"]["source"] = pack.tag
            c["hash"] = _content_hash(c)
            chunks.append(c)
    return chunks


def _load_cache(index_dir: Path) -> dict[tuple, dict]:
    """Existing rows keyed by (filename, chunk id) — deliberately NOT by tag,
    because the tag isn't part of what gets embedded: retagging a chunk must
    reuse its vector, not re-embed it. Rows written before hashes existed get
    one computed from their stored text."""
    cache: dict[tuple, dict] = {}
    path = index_dir / "chunks.jsonl"
    if not path.exists():
        return cache
    for row in rag.load_index(index_dir):
        row.setdefault("hash", _content_hash(row))
        cache[(row.get("source"), row.get("id"))] = row
    return cache


def sync(dry_run: bool = False) -> int:
    root = corpus_root()
    if root is None:
        sys.exit("sync needs the corpus on disk — set BRIEF_CORPUS or run from the corpus repo.")
    packs = discover_packs(root)
    if not packs:
        sys.exit(f"no pack directories under {root}")
    index_dir = _index_dir()
    cache = _load_cache(index_dir)
    use_qdrant = rag._store() == "qdrant"

    # --- plan the work -----------------------------------------------------
    desired: list[dict] = []
    to_embed: list[dict] = []      # content changed or brand new -> needs a vector
    retagged: list[dict] = []      # vector reused, but payload (tag) changed -> re-upsert
    per_pack: dict[str, int] = {}
    for pack in packs:
        chunks = _chunk_pack(pack)
        per_pack[pack.tag] = len(chunks)
        for c in chunks:
            prev = cache.get((c["source"], c["id"]))
            if prev is not None and prev.get("hash") == c["hash"] and "vector" in prev:
                c["vector"] = prev["vector"]
                if prev.get("metadata", {}).get("source") != pack.tag:
                    retagged.append(c)
            else:
                to_embed.append(c)
            desired.append(c)

    active_tags = {p.tag for p in packs}
    desired_keys = {(c["source"], c["id"]) for c in desired}
    stale_rows = [row for key, row in cache.items() if key not in desired_keys]
    orphan_tags = {row.get("metadata", {}).get("source") for row in stale_rows} - active_tags

    print(f"corpus: {root}")
    print(f"packs:  {', '.join(p.id + (f'->{p.tag}' if p.tag != p.id else '') for p in packs)}")
    print(f"chunks: {len(desired)} desired · {len(to_embed)} to embed "
          f"({len(desired) - len(to_embed)} cached) · {len(retagged)} retagged "
          f"· {len(stale_rows)} stale to drop"
          + (f" · orphan tags: {sorted(str(t) for t in orphan_tags)}" if orphan_tags else ""))
    if dry_run:
        print("dry-run: no changes made")
        return 0

    # --- embed only the diff -----------------------------------------------
    if to_embed:
        vecs, mode = rag.embed([rag.embed_text_of(c) for c in to_embed], "passage")
        for c, v in zip(to_embed, vecs):
            c["vector"] = rag._norm(v)
    else:
        mode = "cache"
    dim = len(desired[0]["vector"]) if desired else 0

    # embed-model guard: refuse to mix vector spaces silently
    manifest_path = index_dir / "manifest.json"
    if manifest_path.exists() and to_embed and mode != "cache":
        prev_mode = json.loads(manifest_path.read_text()).get("embed_mode")
        if prev_mode and prev_mode != mode:
            sys.exit(f"embed model changed ({prev_mode} -> {mode}); vectors are not "
                     f"comparable across models. Re-embed everything: delete {index_dir} "
                     "and re-run sync.")

    # --- write the local index (cache + source of truth for push) ----------
    index_dir.mkdir(parents=True, exist_ok=True)
    with open(index_dir / "chunks.jsonl", "w", encoding="utf-8") as fh:
        for c in desired:
            fh.write(json.dumps(c) + "\n")
    manifest_path.write_text(json.dumps({
        "files": len({(c['metadata']['source'], c['source']) for c in desired}),
        "chunks": len(desired), "embed_mode": mode if mode != "cache" else
        (json.loads(manifest_path.read_text()).get("embed_mode", "cache")
         if manifest_path.exists() else "cache"),
        "dim": dim}, indent=2))

    # --- reconcile the remote store ----------------------------------------
    if use_qdrant:
        import store_qdrant
        store_qdrant.ensure_collection(dim=dim)
        # new/changed content AND retagged payloads; point ids are deterministic
        # (filename+index+id), so a retagged chunk overwrites its old point.
        changed = to_embed + retagged
        if changed:
            n = store_qdrant.upsert(changed)
            print(f"qdrant: upserted {n} points ({len(to_embed)} re-embedded, "
                  f"{len(retagged)} retagged)")
        # vanished packs with a real tag: one server-side filtered delete each
        for tag in sorted(t for t in orphan_tags if isinstance(t, str) and t):
            n = store_qdrant.delete_by({"source": tag})
            print(f"qdrant: deleted {n} points for vanished pack '{tag}'")
        # everything else stale goes by point id — EXCLUDING ids that a desired
        # chunk just overwrote (retag reuses the same point id).
        desired_pids = {store_qdrant.point_id(c) for c in desired}
        stale_pids = [pid for r in stale_rows
                      if not (isinstance(r.get("metadata", {}).get("source"), str)
                              and r["metadata"]["source"] in orphan_tags)
                      for pid in [store_qdrant.point_id(r)]
                      if pid not in desired_pids]
        if stale_pids:
            store_qdrant.delete_ids(stale_pids)
            print(f"qdrant: deleted {len(stale_pids)} stale points")

    # --- the lockfile: what the runtime discovers when no corpus exists ----
    real_mode = mode if mode != "cache" else json.loads(
        manifest_path.read_text()).get("embed_mode", "cache")
    lock = write_lock(packs, embed_model=real_mode, dim=dim, counts=per_pack)
    print(f"packs.lock -> {lock}")
    embedded = len(to_embed)
    print(f"sync done: {embedded} embedded, {len(desired) - embedded} cached, "
          f"{len(stale_rows)} dropped")
    return 0


def status() -> int:
    packs = discover_packs()
    if not packs:
        print("no packs found (no corpus on disk and no packs.lock)")
        return 1
    use_qdrant = rag._store() == "qdrant"
    counts = {}
    if use_qdrant:
        import store_qdrant
        for p in packs:
            try:
                counts[p.tag] = store_qdrant.count_by({"source": p.tag})
            except Exception:
                counts[p.tag] = None
    else:
        # local store: count from the lock written by the last sync
        lp = packs_mod.lock_path()
        if lp.is_file():
            for e in json.loads(lp.read_text()).get("packs", []):
                counts[e["tag"]] = e.get("chunks")
    print(f"{'pack':22s} {'tag':12s} {'kind':10s} k  loops{'':22s} "
          + ("store-chunks" if use_qdrant else "lock-chunks"))
    for p in packs:
        loops = ",".join(p.loops) if p.loops else "(all)"
        n = counts.get(p.tag, p.chunks)
        print(f"{p.id:22s} {p.tag:12s} {p.kind:10s} {p.k}  {loops:26s} {n}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="napkin-packs",
                                 description="reconcile knowledge packs with the index")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("sync", help="reconcile filesystem -> index/store, write packs.lock")
    sp.add_argument("--dry-run", action="store_true")
    sub.add_parser("status", help="show packs and their live chunk counts")
    args = ap.parse_args(argv)
    if args.cmd == "sync":
        return sync(dry_run=args.dry_run)
    return status()


if __name__ == "__main__":
    raise SystemExit(main())
