#!/usr/bin/env python3
"""
Loops 3–7 retrieval hook
========================

A thin runtime adapter over rag.py. The briefing tool (parse_brief.py) imports
ONLY this for its Loops 3–7 stage; it reuses rag.py's embed + search rather than
duplicating any logic. Never imported by the Loop-1 capture path.

    from rag.retrieve import retrieve, index_available
    hits = retrieve("challenger brand, nervous CMO", k=5, where={"category": "Comms Planning"})
    # -> [{score, source, section, citation, framework, category, text, metadata}, ...]

Degrades gracefully: if the index hasn't been built, retrieve() returns [] and
index_available() returns False, so the caller can skip the stage cleanly.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:                    # let `import rag` resolve next to us
    sys.path.insert(0, str(HERE))

import rag                                        # noqa: E402  (after sys.path tweak)

# Default index is rag/index; override with RAG_INDEX (absolute or relative-to-rag/)
# so the pipeline can be pointed at a test corpus without touching production.
_ENV_INDEX = os.environ.get("RAG_INDEX")
DEFAULT_INDEX = (Path(_ENV_INDEX) if os.path.isabs(_ENV_INDEX or "")
                 else HERE / _ENV_INDEX) if _ENV_INDEX else HERE / "index"


def index_available(index_dir: Path | str | None = None) -> bool:
    # Qdrant mode: availability = the remote collection has points (no local file).
    if rag._store() == "qdrant":
        return rag.store_available()
    d = Path(index_dir) if index_dir else DEFAULT_INDEX
    return (d / "chunks.jsonl").exists()


def retrieve(query: str, k: int = 5, where: dict | None = None,
             index_dir: Path | str | None = None) -> list[dict]:
    """Top-k chunks for a query, each carrying a `source › section` citation.
    Returns [] if the index is absent or nothing matches the metadata filter."""
    d = Path(index_dir) if index_dir else DEFAULT_INDEX
    if rag._store() != "qdrant" and not (d / "chunks.jsonl").exists():
        return []
    out: list[dict] = []
    for score, r in rag.search(d, query, k=k, where=where):
        md = r.get("metadata", {}) or {}
        out.append({
            "score": round(float(score), 4),
            "source": r["source"],
            "section": r["section"],
            "citation": f"{r['source']} › {r['section']}",
            "framework": md.get("framework_name"),
            "category": md.get("category"),
            "text": r["text"],
            "metadata": md,
        })
    return out


if __name__ == "__main__":                       # tiny manual check: retrieve.py "query" [k]
    q = sys.argv[1] if len(sys.argv) > 1 else "challenger brand vs entrenched leader"
    kk = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    if not index_available():
        sys.exit(f"No index at {DEFAULT_INDEX}. Build it: cd rag && ./build_rag.sh")
    for h in retrieve(q, k=kk):
        print(f"[{h['score']:.3f}] {h['citation']}")
