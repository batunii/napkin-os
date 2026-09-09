#!/usr/bin/env python3
"""
chunking.py — per-source chunking strategies for the RAG brain.

One vector store, many document shapes. Each file is chunked by the strategy that
suits its shape, chosen from its frontmatter `source`/`category` (or inferred from
its directory when a file has no frontmatter). Every chunk carries:

    metadata.source      corpus (ipa | cannes | dandad | effie | playbook | template)
    metadata.doc_id      stable per-file id (framework_id or file stem)
    metadata.level       parent | child | chunk
    metadata.parent_id   the parent chunk id for children (else null)
    metadata.strategy    which strategy produced it

and is embedded as: CONTEXT HEADER + section name + body + retrieval queries.
The header ("Xero UK (2024) · IPA Bronze · Financial Services · Brand Building · Insight")
is what lets a 40-word Results paragraph be found by a query about B2B software.

Strategies
  case_parent_child  award cases with a fixed template (IPA, Effie): ONE whole-case
                     parent + one child per section. Loops 4/6 retrieve parents.
  case_whole         award cases with variable sections (Cannes): whole case only.
  skip               too thin to embed (D&AD title+overview) — kept out of vectors.
  sections           playbooks: one chunk per H1/H2 section, split at paragraphs
                     above SECTION_MAX_WORDS, no overlap, framework name prefixed.
  windows            templates/guides: heading split, then ~WINDOW_WORDS windows
                     with WINDOW_OVERLAP overlap (the only place overlap earns it).

Retrieval Queries are collected in a first pass and attached to EVERY chunk of the
file (the old single-pass chunker only attached them to chunks after the RQ section,
which was always last — so none got them).
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

# ---- knobs ---------------------------------------------------------------
SECTION_MAX_WORDS = 400          # playbooks: split sections above this at paragraphs
WINDOW_WORDS = 300               # templates: window size
WINDOW_OVERLAP = 0.15            # templates: fraction of previous window repeated
MIN_WORDS = 8                    # drop chunks thinner than this (headings-only etc.)

# source -> default strategy. `category` can override (see strategy_for).
STRATEGY_BY_SOURCE = {
    "ipa": "case_parent_child",
    "effie": "case_parent_child",
    "cannes": "case_whole",
    "dandad": "skip",
    "playbook": "sections",
    "template": "windows",
}
DEFAULT_STRATEGY = "windows"

# directory -> source, for files with no frontmatter (37 in the corpus today)
DIR_SOURCE = [("ipa", "ipa"), ("cannes", "cannes"), ("dandad", "dandad"), ("effie", "effie"),
              ("playbooks", "playbook"), ("briefing-template", "template")]


# ---- frontmatter (shared with rag.py) -----------------------------------
def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Split leading --- ... --- block. PyYAML if available, else a small generic
    parser (key: value, key: [a, b]). Returns (meta, body)."""
    text = text.lstrip("﻿")
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


def infer_source(path: Path, meta: dict) -> str | None:
    if meta.get("source"):
        return str(meta["source"]).lower()
    parts = {p.lower() for p in path.parts}
    for d, src in DIR_SOURCE:
        if d in parts:
            return src
    return "template" if path.suffix == ".md" else None


def strategy_for(source: str | None, meta: dict) -> str:
    cat = str(meta.get("category") or "").lower()
    if source in ("ipa", "effie") and not cat.endswith("_case"):
        return "windows"                    # IPA pattern-analysis docs, not cases
    return STRATEGY_BY_SOURCE.get(source or "", DEFAULT_STRATEGY)


# ---- structure -----------------------------------------------------------
_HEADING = re.compile(r"^#{1,2}\s+(?!#)")


def split_sections(body: str) -> list[tuple[str, str]]:
    """[(heading, text)] split at H1/H2 (H3+ stays inside). Same rule as before."""
    sections, heading, buf = [], "(intro)", []
    for line in body.splitlines():
        if _HEADING.match(line):
            if buf:
                sections.append((heading, "\n".join(buf).strip()))
            heading = line.lstrip("# ").strip()
            buf = []
        else:
            buf.append(line)
    if buf:
        sections.append((heading, "\n".join(buf).strip()))
    return [(h, t) for h, t in sections if t]


def _is_rq(heading: str) -> bool:
    return bool(re.search(r"retrieval[_ ]queries", heading, re.I))


def _title(meta: dict, sections: list[tuple[str, str]], path: Path) -> str:
    t = meta.get("framework_name") or meta.get("title") or meta.get("name")
    if not t:
        # a leading "# Title" becomes a section whose heading is the title with empty/short text
        for h, _ in sections:
            if h != "(intro)":
                t = h
                break
    return str(t or path.stem).strip().strip('"')


def context_header(source: str | None, meta: dict, title: str, strategy: str = "") -> str:
    """One line of identity that precedes every chunk's embedded text."""
    g = lambda k: (str(meta.get(k) or "").strip())          # noqa: E731
    yr = g("year")
    head = f"{title} ({yr})" if yr and yr not in title else title
    parts = [head]
    if strategy in ("windows", "sections") and source not in ("playbook",):
        parts += [g("category") or g("type") or "briefing reference"]
    elif source == "ipa":
        parts += [f"IPA {g('award_tier')}".strip(), g("sector"), g("effectiveness_type"),
                  g("strategic_territory"), f"client {g('client')}" if g("client") else ""]
    elif source == "effie":
        parts += [f"Effie {g('award_tier')}".strip(), g("sector"), g("category"), g("client")]
    elif source == "cannes":
        parts += [g("award_tier") or "Cannes Lions", g("lions_category"), g("subcategory"),
                  f"client {g('client')}" if g("client") else "", g("agency")]
    elif source == "dandad":
        parts += [f"D&AD {g('award_tier')}".strip(), g("client")]
    elif source == "playbook":
        parts += ["planning framework", g("category") or g("type"), g("stage")]
    else:
        parts += [g("category") or g("type") or "briefing reference"]
    return " · ".join(p for p in parts if p and p.lower() not in ("general", "not recorded", "none"))


def _split_paragraphs(text: str, max_words: int) -> list[str]:
    """Split a long section at paragraph boundaries into pieces <= max_words (best effort)."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    out, buf, n = [], [], 0
    for p in paras:
        w = len(p.split())
        if buf and n + w > max_words:
            out.append("\n\n".join(buf)); buf, n = [], 0
        buf.append(p); n += w
    if buf:
        out.append("\n\n".join(buf))
    return out or [text]


def _windows(text: str, size: int, overlap: float) -> list[str]:
    words = text.split()
    if len(words) <= size:
        return [text]
    step = max(1, int(size * (1 - overlap)))
    return [" ".join(words[i:i + size]) for i in range(0, len(words), step) if i < len(words)]


# ---- chunk builder -------------------------------------------------------
def _mk(path: Path, meta: dict, *, source: str | None, doc_id: str, strategy: str, level: str,
        header: str, heading: str, text: str, idx: int, parent_id: str | None = None,
        rq: str | None = "") -> dict:
    cid = hashlib.sha1(f"{path.name}:{strategy}:{level}:{idx}:{heading}".encode()).hexdigest()[:12]
    md = {**meta, "source": source, "doc_id": doc_id, "level": level,
          "parent_id": parent_id, "strategy": strategy}
    return {"id": cid, "source": path.name, "section": heading, "chunk_index": idx,
            "metadata": md, "text": text, "header": header, "retrieval_queries": rq or ""}


def chunk_file(path: Path) -> list[dict]:
    meta, body = parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
    source = infer_source(path, meta)
    strategy = strategy_for(source, meta)
    if strategy == "skip":
        return []
    sections = split_sections(body)
    title = _title(meta, sections, path)
    doc_id = str(meta.get("framework_id") or meta.get("id") or path.stem)
    header = context_header(source, meta, title, strategy)

    # pass 1: retrieval queries (frontmatter key and/or a section), body sections
    rq_parts = []
    fm_rq = meta.get("RETRIEVAL_QUERIES") or meta.get("retrieval_queries")
    if fm_rq:
        rq_parts.append(fm_rq if isinstance(fm_rq, str) else " ".join(map(str, fm_rq)))
    body_secs = []
    for h, t in sections:
        if _is_rq(h):
            rq_parts.append(t)
        elif h == title and len(t.split()) < MIN_WORDS:
            continue                                        # bare "# Title" line
        else:
            body_secs.append((h, t))
    rq = "\n".join(p.strip() for p in rq_parts if p and p.strip())
    if not body_secs:
        return []

    common = dict(source=source, doc_id=doc_id, strategy=strategy, header=header, rq=rq)
    chunks: list[dict] = []

    if strategy in ("case_parent_child", "case_whole"):
        whole = "\n\n".join(f"{h}: {t}" if h != "(intro)" else t for h, t in body_secs)
        parent = _mk(path, meta, level="parent", heading=title, text=whole, idx=0, **common)
        chunks.append(parent)
        if strategy == "case_parent_child":
            for i, (h, t) in enumerate(body_secs, 1):
                if len(t.split()) < MIN_WORDS or h == title or h == "(intro)":
                    continue                                # header already carries the identity line
                chunks.append(_mk(path, meta, level="child", heading=h, text=t, idx=i,
                                  parent_id=parent["id"], **common))
        return chunks

    if strategy == "sections":
        i = 0
        for h, t in body_secs:
            for piece in _split_paragraphs(t, SECTION_MAX_WORDS):
                if len(piece.split()) < MIN_WORDS:
                    continue
                chunks.append(_mk(path, meta, level="chunk", heading=h, text=piece, idx=i, **common)); i += 1
        return chunks

    # windows (templates, guides, anything unknown)
    i = 0
    for h, t in body_secs:
        for w in _windows(t, WINDOW_WORDS, WINDOW_OVERLAP):
            if len(w.split()) < MIN_WORDS:
                continue
            chunks.append(_mk(path, meta, level="chunk", heading=h, text=w, idx=i, **common)); i += 1
    return chunks


def embed_text_of(chunk: dict, cap: int = 16000) -> str:
    """What we embed: context header, section, body, retrieval queries."""
    parts = [chunk.get("header") or "", chunk["section"], chunk["text"]]
    if chunk.get("retrieval_queries"):
        parts.append(chunk["retrieval_queries"])
    return "\n".join(p for p in parts if p)[:cap]
