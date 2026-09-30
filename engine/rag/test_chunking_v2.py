"""Chunker v2 behaviour: Cannes parent/child, D&AD groups, table-safe templates, role
inheritance, identity-card/bibliography skipped. Fixtures are inline so the tests run
without the (gitignored) corpus.

Run:  cd engine/rag && python3 -m pytest test_chunking_v2.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import chunking  # noqa: E402
from contract import validate  # noqa: E402

LONG = "This sentence is here so the section clears the minimum word count for a chunk. " * 2


def _write(root: Path, rel: str, text: str) -> Path:
    """Write text to root/rel, creating parent directories, and return the path."""
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


# ---- Cannes: parent + one child per entry-form answer -------------------------------
def test_cannes_is_parent_child_with_entry_answers(tmp_path):
    """A Cannes case yields one parent chunk and one child per entry-form answer, with award
    tier, Lions category and client-resolved category normalised and every chunk contract-valid."""
    f = _write(tmp_path, "cannes/cannes_0001.md",
               "---\nsource: cannes\nframework_id: cannes_0001\nframework_name: \"A CRITTER CAROL (2026)\"\n"
               "category: cannes_case\naward_tier: Silver Cannes Lions\nyear: 2026\nclient: APPLE\nagency: TBWA\n"
               "sector: general\nlions_category: Film\n---\n# A CRITTER CAROL (2026)\n**Award:** Silver Cannes Lions\n\n"
               f"## Why is this work relevant for Film?\n{LONG}\n\n## Describe the Impact\n{LONG}\n\n"
               "## Retrieval Queries\n- Apple holiday film craft\n")
    chunks = chunking.chunk_file(f)
    levels = [c["metadata"]["level"] for c in chunks]
    assert levels == ["parent", "child", "child"]
    parent, *children = chunks
    assert parent["metadata"]["section_role"] == "whole"
    assert all(c["metadata"]["section_role"] == "entry_answer" for c in children)
    assert all(c["metadata"]["parent_id"] == parent["id"] for c in children)
    assert parent["metadata"]["award_tier"] == "silver" and parent["metadata"]["award_tier_raw"] == "Silver Cannes Lions"
    assert parent["metadata"]["lions_category"] == "film"
    assert parent["metadata"]["category"] == "technology"     # sector is 'general'; client APPLE resolves it
    assert all(c["retrieval_queries"] for c in chunks)      # RQs attached to every chunk
    assert all(validate(c["metadata"]) == [] for c in chunks)


# ---- D&AD: grouped by discipline + year -------------------------------------------------
def _dandad(root: Path, n: int, disc: str, year: int, title: str) -> Path:
    """Write a minimal D&AD case file with the given discipline, year and title."""
    return _write(root, f"dandad/dandad_{n:04d}.md",
                  f"---\nsource: dandad\nframework_id: dandad_{n:04d}\nframework_name: \"{title} ({year})\"\n"
                  f"category: dandad_case\naward_tier: WOOD Pencil\nyear: {year}\nclient: not recorded\n"
                  f"agency: Someone\nsector: {disc}\ncountry: not recorded\ntags: [wood-pencil]\n---\n"
                  f"# {title} ({year})\n\n## Overview\nA {disc.lower()} piece. {LONG}\n\n"
                  f"## Retrieval Queries\n- Award-winning {disc} work\n")


def test_dandad_single_file_yields_nothing_but_corpus_groups_it(tmp_path):
    """A single D&AD file chunks to nothing on its own; chunk_corpus() groups files sharing
    discipline and year into one parent with a child per entry, correctly normalised."""
    a = _dandad(tmp_path, 1, "Book Design, Typography", 2026, "Alpha")
    b = _dandad(tmp_path, 2, "Book Design, Typography", 2026, "Beta")
    c = _dandad(tmp_path, 3, "Book Design, Typography", 2025, "Gamma")
    d = _dandad(tmp_path, 4, "Film Advertising", 2026, "Delta")
    assert chunking.chunk_file(a) == []
    chunks = chunking.chunk_corpus([a, b, c, d])
    parents = [x for x in chunks if x["metadata"]["level"] == "parent"]
    children = [x for x in chunks if x["metadata"]["level"] == "child"]
    assert len(parents) == 3 and len(children) == 4          # (BookDesign,2026) (BookDesign,2025) (Film,2026)
    g = next(p for p in parents if p["metadata"]["doc_id"] == "dandad:book_design:2026")
    assert "Alpha" in g["text"] and "Beta" in g["text"] and "Gamma" not in g["text"]
    assert g["metadata"]["stage"] == "production" and g["metadata"]["bucket"] == "exemplars"
    assert g["metadata"]["section_role"] == "whole" and g["metadata"]["as_of"] == "2026-01-01"
    kids = [x for x in children if x["metadata"]["parent_id"] == g["id"]]
    assert len(kids) == 2 and all(k["metadata"]["section_role"] == "overview" for k in kids)
    assert all(k["metadata"]["award_tier"] == "wood_pencil" for k in kids)
    assert all(k["metadata"]["category"] is None for k in kids)   # a design discipline is not a client category
    assert all(validate(x["metadata"]) == [] for x in chunks)


def test_chunk_corpus_mixes_sources(tmp_path):
    """chunk_corpus() handles a mix of playbook and D&AD files in one call, producing
    chunks from both sources."""
    pb = _write(tmp_path, "playbooks/01-x.md",
                "---\nsource: playbook\nframework_id: \"01\"\nframework_name: X\ncategory: \"Comms Planning\"\nyear: 1980\n---\n"
                f"# X\n\n## SECTION 2: STEP-BY-STEP APPLICATION PROCESS\n{LONG}\n")
    dd = _dandad(tmp_path, 9, "Typography", 2026, "Zeta")
    chunks = chunking.chunk_corpus([pb, dd])
    assert {c["metadata"]["source"] for c in chunks} == {"playbook", "dandad"}


# ---- templates: sections, tables intact -------------------------------------------------
def test_template_sections_keep_tables_whole(tmp_path):
    """A markdown table that spans well past SECTION_MAX_WORDS still stays in a single
    chunk, with its first and last rows together."""
    rows = "\n".join(f"| Principle {i} | Meaning for agencies number {i} is stated here. | Meaning for the OS number {i} is stated here. |"
                     for i in range(60))     # ~60 rows * ~15 words = far above SECTION_MAX_WORDS
    table = "| Principle | Agency | OS |\n|---|---|---|\n" + rows
    f = _write(tmp_path, "briefing-template/arch.md",
               f"# Architecture\n\n## Design Principles\n\nIntro paragraph. {LONG}\n\n{table}\n\n## Modules\n{LONG}\n")
    chunks = chunking.chunk_file(f)
    assert all(c["metadata"]["strategy"] == "sections" for c in chunks)
    table_chunks = [c for c in chunks if "| Principle 0 |" in c["text"]]
    assert len(table_chunks) == 1
    assert "| Principle 59 |" in table_chunks[0]["text"]      # first and last row in the same chunk
    assert all(c["metadata"]["bucket"] == "instructions" for c in chunks)
    assert all(c["metadata"]["section_role"] == "template_section" for c in chunks)


# ---- playbooks: role inheritance and non-embedded roles ---------------------------------------
def test_playbook_inner_titles_inherit_role_and_identity_card_is_dropped(tmp_path):
    """A playbook's inner H1 title inherits the section role of the section it falls under,
    and identity-card and bibliography sections are never emitted as chunks."""
    f = _write(tmp_path, "playbooks/22-brand-arch.md",
               "---\nsource: playbook\nframework_id: \"22\"\nframework_name: Brand Architecture\ncategory: 'Brand Strategy'\nyear: 1990\n---\n"
               "# Brand Architecture: The Complete Playbook\n\n"
               f"## SECTION 1: IDENTITY CARD\n| Field | Description |\n|---|---|\n| Name | Brand Architecture is a model. {LONG} |\n\n"
               f"## SECTION 7: OUTPUT TEMPLATE\n{LONG}\n\n"
               f"# Brand Architecture Strategy: [Project Name/Date]\n{LONG}\n\n"
               f"## SECTION 8: DECISION RULES\n{LONG}\n\n"
               f"## SECTION 9: SOURCE BIBLIOGRAPHY\n{LONG}\n")
    chunks = chunking.chunk_file(f)
    roles = [(c["section"], c["metadata"]["section_role"], c["metadata"]["bucket"]) for c in chunks]
    assert ("SECTION 7: OUTPUT TEMPLATE", "output_template", "craft") in roles
    assert ("Brand Architecture Strategy: [Project Name/Date]", "output_template", "craft") in roles   # inherited
    assert ("SECTION 8: DECISION RULES", "decision_rules", "rules") in roles
    assert not any(r in ("identity_card", "bibliography") for _, r, _ in roles)
    assert all(c["metadata"]["discipline"] == "brand_strategy" for c in chunks)


# ---- IPA meta docs are sections, role synthesis, bucket craft --------------------------------------
def test_ipa_meta_doc_is_synthesis(tmp_path):
    """An IPA pattern-analysis document chunks by sections and every chunk is tagged role
    synthesis, bucket craft."""
    f = _write(tmp_path, "ipa/IPA Pattern Analysis.md",
               f"# IPA Effectiveness Awards: Pattern Analysis\n\n## 1. Executive Summary\n{LONG}\n\n## 2. Sector Trends\n{LONG}\n")
    chunks = chunking.chunk_file(f)
    assert chunks and all(c["metadata"]["strategy"] == "sections" for c in chunks)
    assert all(c["metadata"]["section_role"] == "synthesis" and c["metadata"]["bucket"] == "craft" for c in chunks)


# ---- retrieval queries at any heading level -----------------------------------------------------
def test_extract_rq_finds_h3_and_h5_blocks_and_removes_them():
    """extract_rq() collects RETRIEVAL_QUERIES blocks at any heading level (H3 and H5 alike)
    and strips them out of the remaining body text."""
    body = ("## SECTION 9: SOURCE BIBLIOGRAPHY\nSome refs.\n\n### **RETRIEVAL_QUERIES**\n- How do I use the FCB grid?\n"
            "- Think feel matrix examples\n\n## Another\ntext\n\n##### RETRIEVAL QUERIES\n- late one\n")
    rq, rest = chunking.extract_rq(body)
    assert "How do I use the FCB grid?" in rq and "late one" in rq
    assert "RETRIEVAL" not in rest and "Some refs." in rest and "## Another" in rest


def test_playbook_rqs_attach_to_every_chunk_even_when_bibliography_is_dropped(tmp_path):
    """Retrieval queries defined under the (dropped) bibliography section still attach to
    every chunk produced from the file."""
    f = _write(tmp_path, "playbooks/01-fcb.md",
               "---\nsource: playbook\nframework_id: \"01\"\nframework_name: FCB Grid\ncategory: \"Comms Planning\"\nyear: 1980\n---\n"
               f"# FCB Grid\n\n## SECTION 2: STEP-BY-STEP APPLICATION PROCESS\n{LONG}\n\n"
               f"## SECTION 9: SOURCE BIBLIOGRAPHY\n{LONG}\n\n### RETRIEVAL_QUERIES\n- How do I use the FCB grid?\n")
    chunks = chunking.chunk_file(f)
    assert chunks and all("FCB grid" in c["retrieval_queries"] for c in chunks)
    assert not any(c["metadata"]["section_role"] == "bibliography" for c in chunks)
