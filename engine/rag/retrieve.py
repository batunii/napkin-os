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
import re
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
    """True if the configured store (RAG_STORE: local | qdrant | ...) is reachable and non-empty."""
    d = Path(index_dir) if index_dir else DEFAULT_INDEX
    return rag.store_available(d)


def index_label(index_dir: Path | str | None = None) -> str:
    """Human label of the active store for run metadata, e.g. 'qdrant:napkin_rag' or a path."""
    d = Path(index_dir) if index_dir else DEFAULT_INDEX
    try:
        return rag.open_store(d).describe()["label"]
    except Exception as e:                        # misconfigured store — still label it
        return f"{rag.store_name()}:unavailable ({type(e).__name__})"


# Material no brief-side caller should ever be served, whatever it asks for:
#   stage=production   D&AD design-craft entries, indexed for the production clan. They
#                      have no client and no strategy, and they only entered the index in
#                      chunker v2 — before that they were skipped, so nothing downstream
#                      was ever written expecting to filter them out.
#   status=superseded  a human overruled it.
# `ne` matches chunks where the field is absent, so this narrows nothing that existed
# before these fields did. Pass brief_safe=False to search the index as it really is
# (the golden eval and any production-clan caller).
BRIEF_SAFE = {"stage": {"ne": "production"}, "status": {"ne": "superseded"}}


def retrieve(query: str, k: int = 5, where: dict | None = None,
             index_dir: Path | str | None = None, level: str | None = None,
             brief_safe: bool = True) -> list[dict]:
    """Top-k chunks for a query, each carrying a `source › section` citation.
    `level` narrows to 'parent' (whole cases — what Loops 4/6 want as precedents),
    'child' (case sections) or 'chunk' (playbooks/templates). Returns [] if the
    index is absent or nothing matches the metadata filter."""
    d = Path(index_dir) if index_dir else DEFAULT_INDEX
    if brief_safe:
        where = {**BRIEF_SAFE, **(where or {})}       # an explicit filter still wins
    if level:
        where = {**(where or {}), "level": level}
    if not rag.store_available(d):
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
            "category": md.get("doc_kind") or md.get("category"),   # doc_kind = the corpus's old `category`
            "text": r["text"],
            "header": r.get("header") or "",
            "doc_id": md.get("doc_id"),
            "level": md.get("level"),
            "metadata": md,
        })
    return out


# ---- the brief's retrieval entry point -------------------------------------------
def brief_context(pairs: dict, index_dir: Path | str | None = None, **kw):
    """Campaign-clan pairs -> four budgeted, citable blocks. See brief_context.py.
    Re-exported here so the pipeline has ONE import surface for retrieval."""
    import brief_context as _bc
    return _bc.build(pairs, index_dir=Path(index_dir) if index_dir else DEFAULT_INDEX, **kw)


_CITE = re.compile(r"\[([A-Za-z0-9][A-Za-z0-9_:.\-]*(?:#[a-z_]+)?)\]")


def check_grounding(text: str, allowed: dict | set) -> dict:
    """Verify that every citation in generated text points at something actually in the
    retrieved context.

    This is the check that makes grounding mechanical rather than a matter of reading.
    A model that invents `[ipa_0999]` to support a claim is making the exact mistake a
    reviewer would otherwise have to catch by hand, and an unsupported claim of precedent
    is worse than no claim at all. Returns the cited ids, the invented ones, and whether
    anything was cited — a confident paragraph with no citation is its own smell.

    Deliberately NOT a model call: it is string matching against a known set, so it costs
    nothing and cannot itself hallucinate."""
    cited = list(dict.fromkeys(_CITE.findall(text or "")))
    ok = set(allowed)
    invented = [c for c in cited if c not in ok]
    return {"cited": cited, "invented": invented, "grounded": bool(cited) and not invented,
            "uncited": not cited}


if __name__ == "__main__":                       # tiny manual check: retrieve.py "query" [k]
    q = sys.argv[1] if len(sys.argv) > 1 else "challenger brand vs entrenched leader"
    kk = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    if not index_available():
        sys.exit(f"No index at {DEFAULT_INDEX}. Build it: cd rag && ./build_rag.sh")
    for h in retrieve(q, k=kk):
        print(f"[{h['score']:.3f}] {h['citation']}")
