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
from datetime import date
from pathlib import Path

import contract
import normalise

# ---- knobs ---------------------------------------------------------------
SECTION_MAX_WORDS = 400          # playbooks: split sections above this at paragraphs
WINDOW_WORDS = 300               # templates: window size
WINDOW_OVERLAP = 0.15            # templates: fraction of previous window repeated
MIN_WORDS = 8                    # drop chunks thinner than this (headings-only etc.)

# source -> default strategy. `category` can override (see strategy_for).
STRATEGY_BY_SOURCE = {
    "ipa": "case_parent_child",
    "effie": "case_parent_child",
    "cannes": "case_parent_child",   # v2: each entry-form answer is a self-contained child
    "dandad": "group",               # v2: too thin alone; grouped by discipline+year in chunk_corpus()
    "playbook": "sections",
    "template": "sections",          # v2: sections never split a table; windows did
}
DEFAULT_STRATEGY = "sections"
_CASE_SOURCES = {"ipa", "effie", "cannes", "dandad"}   # sources whose `year` is the award year

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
        return "sections"                   # IPA pattern-analysis docs, not cases
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


_ANY_HEADING = re.compile(r"^(#{1,6})\s*(.*?)\s*$")


def extract_rq(body: str) -> tuple[str, str]:
    """Pull every Retrieval Queries block out of the body at ANY heading level and return
    (rq_text, body_without_them). Cases mark them with `## Retrieval Queries` (H2) but
    playbooks use `### RETRIEVAL_QUERIES` / `##### RETRIEVAL QUERIES` — below the H1/H2
    split, so they used to hide inside the last section's body. A block runs until the
    next heading of the same or a higher level."""
    lines = body.splitlines()
    keep, rq, i = [], [], 0
    while i < len(lines):
        m = _ANY_HEADING.match(lines[i])
        if m and _is_rq(m.group(2).strip("*_ ")):
            depth = len(m.group(1)); i += 1
            while i < len(lines):
                m2 = _ANY_HEADING.match(lines[i])
                if m2 and len(m2.group(1)) <= depth:
                    break
                rq.append(lines[i]); i += 1
            continue
        keep.append(lines[i]); i += 1
    return "\n".join(rq).strip(), "\n".join(keep)


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
def _as_of(meta: dict, path: Path, source: str | None) -> str:
    """The contract's `as_of` (YYYY-MM-DD) for corpus material, which has no review date.

    Award cases: the award year, as `YYYY-01-01`. That is when the lesson was judged true.
    Everything else (playbooks, templates): the file's modification date. A playbook's
    `year` is when the FRAMEWORK was invented (FCB Grid: 1980, first-principles: 350 BC),
    which says nothing about how current the write-up is; the day the document was last
    authored does. An explicit `as_of` in frontmatter always wins."""
    explicit = str(meta.get("as_of") or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", explicit):
        return explicit
    if source in _CASE_SOURCES:
        y = str(meta.get("year") or "").strip()
        if re.fullmatch(r"\d{4}", y):
            return f"{y}-01-01"
    return date.fromtimestamp(path.stat().st_mtime).isoformat()


def _finalise_md(md: dict, *, source: str | None, heading: str, level: str, path: Path,
                 role: str | None = None) -> dict:
    """Everything that turns raw frontmatter into contract-valid metadata. One place, so
    every chunk (per-file or corpus-level) goes through the same door."""
    # Frontmatter `category` is the document KIND (ipa_effectiveness_case, effie_cautionary...).
    # The contract reserves `category` for the closed client-category list, so move it aside.
    if "category" in md:
        md["doc_kind"] = md.pop("category")
    if md.get("year") is not None:
        md["year"] = str(md["year"])            # PyYAML reads `year: 2024` as int; contract says string
    md.setdefault("as_of", _as_of(md, path, source))
    # Free-text labels -> contract enums. normalise.py is the only place the spelling
    # tables live; here we just call it. None means "unknown", which the contract allows.
    raw_tier = md.get("award_tier")
    md["award_tier_raw"] = str(raw_tier) if raw_tier else None
    md["award_tier"] = normalise.award_tier(raw_tier)
    md.setdefault("category", normalise.category_for(md.get("sector"), md.get("client")))
    md["effectiveness_type"] = normalise.effectiveness_type(md.get("effectiveness_type"))
    md["strategic_territory"] = normalise.strategic_territory(md.get("strategic_territory"))
    md["discipline"] = normalise.discipline(md.get("doc_kind")) if source == "playbook" else None
    md["lions_category"] = normalise.lions_category(md.get("lions_category"))
    md["section_role"] = role or normalise.section_role(source, heading, level)
    md["bucket"] = normalise.bucket(source, md["section_role"], md.get("doc_kind"))
    return contract.apply_defaults(md)      # status=active, verdict=none, scope=global, schema_version


def _mk(path: Path, meta: dict, *, source: str | None, doc_id: str, strategy: str, level: str,
        header: str, heading: str, text: str, idx: int, parent_id: str | None = None,
        rq: str | None = "", role: str | None = None) -> dict:
    cid = hashlib.sha1(f"{path.name}:{strategy}:{level}:{idx}:{heading}".encode()).hexdigest()[:12]
    md = {**meta, "source": source, "doc_id": doc_id, "level": level,
          "parent_id": parent_id, "strategy": strategy}
    md = _finalise_md(md, source=source, heading=heading, level=level, path=path, role=role)
    return {"id": cid, "source": path.name, "section": heading, "chunk_index": idx,
            "metadata": md, "text": text, "header": header, "retrieval_queries": rq or ""}


def _read(path: Path) -> tuple[dict, str, str | None, str]:
    meta, body = parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
    source = infer_source(path, meta)
    return meta, body, source, strategy_for(source, meta)


def chunk_file(path: Path) -> list[dict]:
    """Chunks for ONE file. D&AD entries return [] here because they are only useful in
    groups — see chunk_corpus(), which is what the build calls."""
    meta, body, source, strategy = _read(path)
    if strategy in ("skip", "group"):
        return []
    rq_block, body = extract_rq(body)            # any heading level, before the H1/H2 split
    sections = split_sections(body)
    title = _title(meta, sections, path)
    doc_id = str(meta.get("framework_id") or meta.get("id") or path.stem)
    header = context_header(source, meta, title, strategy)

    # pass 1: retrieval queries (frontmatter key and/or a section), body sections
    rq_parts = [rq_block] if rq_block else []
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
        # Sections split at paragraph boundaries, never inside a table (table rows have
        # no blank line between them, so a table is always one paragraph).
        i = 0
        prev_role: str | None = None
        for h, t in body_secs:
            role = normalise.section_role(source, h, "chunk")
            if role == "other" and prev_role in _INHERITABLE_ROLES:
                role = prev_role            # e.g. the "# Brand Audit Template" H1 inside SECTION 7: OUTPUT TEMPLATE
            prev_role = role
            if role in normalise.NOT_EMBEDDED_ROLES:
                continue                    # identity card + bibliography: no retrievable lesson
            for piece in _split_paragraphs(t, SECTION_MAX_WORDS):
                if len(piece.split()) < MIN_WORDS:
                    continue
                chunks.append(_mk(path, meta, level="chunk", heading=h, text=piece, idx=i, role=role, **common)); i += 1
        return chunks

    # windows (anything that explicitly asks for it)
    i = 0
    for h, t in body_secs:
        for w in _windows(t, WINDOW_WORDS, WINDOW_OVERLAP):
            if len(w.split()) < MIN_WORDS:
                continue
            chunks.append(_mk(path, meta, level="chunk", heading=h, text=w, idx=i, **common)); i += 1
    return chunks


# Roles a following unrecognised sub-heading may inherit (playbook templates and examples
# often contain their own H1/H2 titles).
_INHERITABLE_ROLES = frozenset({"output_template", "worked_example", "thought_process", "process"})


# ---- D&AD groups (corpus-level) -------------------------------------------------
_FIRST_SENTENCE = re.compile(r"^(.*?[.!?])(\s|$)", re.S)


def _first_sentence(text: str, cap: int = 220) -> str:
    m = _FIRST_SENTENCE.match(text.strip())
    s = (m.group(1) if m else text.strip())
    return s[:cap].strip()


def _dandad_entry(path: Path) -> dict | None:
    meta, body, source, _ = _read(path)
    if source != "dandad":
        return None
    rq, body = extract_rq(body)
    sections = dict(split_sections(body))
    overview = next((t for h, t in sections.items() if h.lower().startswith("overview")), "")
    if len(overview.split()) < MIN_WORDS:
        return None
    return {"path": path, "meta": meta, "title": _title(meta, list(sections.items()), path),
            "overview": overview.strip(), "rq": rq.strip(),
            # "Book Design, Typography" -> group under "Book Design"; the full label stays in `sector`
            "discipline": str(meta.get("sector") or "Uncategorised").split(",")[0].strip() or "Uncategorised",
            "sector_full": str(meta.get("sector") or "").strip(),
            "year": str(meta.get("year") or "").strip()}


def chunk_dandad_groups(paths: list[Path]) -> list[dict]:
    """D&AD entries are ~180 words with no client and no strategy; alone they are noise.
    Grouped by discipline + year they become a useful reference: ONE parent per group
    ("what award-winning Book Design looked like in 2026") and one child per entry.
    The parent text here is deterministic (titles + first sentences); the enrichment
    pass (step 2) may replace it with a written summary. Stage is `production`: these
    serve the production clan, and brief retrieval excludes them by default."""
    entries = [e for e in (_dandad_entry(p) for p in paths) if e]
    groups: dict[tuple[str, str], list[dict]] = {}
    for e in entries:
        groups.setdefault((e["discipline"], e["year"]), []).append(e)

    chunks: list[dict] = []
    for (disc, year), members in sorted(groups.items()):
        members.sort(key=lambda e: e["title"])
        first = members[0]
        gid = f"dandad:{normalise.snake(disc)}:{year or 'undated'}"
        strategy = "group"
        base_meta = {"source": "dandad", "sector": disc, "year": year or None, "doc_kind": "dandad_group",
                     "disciplines": sorted({e["sector_full"] for e in members if e["sector_full"]})[:20],
                     "stage": "production", "award_tier": None,
                     "tags": sorted({t for e in members for t in (e["meta"].get("tags") or []) if isinstance(t, str)})[:12]}
        title = f"D&AD {disc} winners ({year})" if year else f"D&AD {disc} winners"
        header = " · ".join(p for p in [title, "D&AD", disc, f"{len(members)} entries"] if p)
        bullets = "\n".join(f"- {e['title']}: {_first_sentence(e['overview'])}" for e in members)
        rq = "\n".join(e["rq"] for e in members if e["rq"])[:4000]
        pid = hashlib.sha1(f"{gid}:parent".encode()).hexdigest()[:12]
        pmd = _finalise_md({**base_meta, "doc_id": gid, "level": "parent", "parent_id": None,
                            "strategy": strategy, "group_size": str(len(members))},
                           source="dandad", heading=title, level="parent", path=first["path"], role="whole")
        chunks.append({"id": pid, "source": gid, "section": title, "chunk_index": 0, "metadata": pmd,
                       "text": f"{title}. {len(members)} Pencil-winning entries.\n\n{bullets}",
                       "header": header, "retrieval_queries": rq})
        for i, e in enumerate(members, 1):
            tier = e["meta"].get("award_tier")
            cmd = _finalise_md({**e["meta"], "source": "dandad", "doc_id": gid, "level": "child", "parent_id": pid,
                                "strategy": strategy, "stage": "production", "award_tier": tier, "entry_id": str(e["meta"].get("framework_id") or e["path"].stem)},
                               source="dandad", heading="Overview", level="child", path=e["path"], role="overview")
            cid = hashlib.sha1(f"{gid}:{e['path'].name}".encode()).hexdigest()[:12]
            chunks.append({"id": cid, "source": e["path"].name, "section": e["title"], "chunk_index": i, "metadata": cmd,
                           "text": e["overview"], "header": f"{e['title']} · D&AD {tier or ''} · {disc}".replace("  ", " "),
                           "retrieval_queries": e["rq"]})
    return chunks


def chunk_corpus(paths: list[Path]) -> list[dict]:
    """What the build calls: per-file chunks for everything, plus D&AD grouped across files."""
    chunks: list[dict] = []
    dandad: list[Path] = []
    for p in paths:
        _, _, source, strategy = _read(p)
        if strategy == "group":
            dandad.append(p)
        else:
            chunks.extend(chunk_file(p))
    if dandad:
        chunks.extend(chunk_dandad_groups(dandad))
    return chunks


# doc_ids whose Retrieval Queries must NOT be embedded (golden-set holdout). Set by the
# build from golden/holdout.json. Empty = embed everything (the production default).
RQ_HOLDOUT: set[str] = set()


def embed_text_of(chunk: dict, cap: int = 16000) -> str:
    """What we embed: context header, section, body, retrieval queries (unless held out)."""
    parts = [chunk.get("header") or "", chunk["section"], chunk["text"]]
    if chunk.get("retrieval_queries") and (chunk.get("metadata") or {}).get("doc_id") not in RQ_HOLDOUT:
        parts.append(chunk["retrieval_queries"])
    return "\n".join(p for p in parts if p)[:cap]
