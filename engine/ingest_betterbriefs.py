#!/usr/bin/env python3
"""
ingest_betterbriefs.py — turn the BetterBriefs research PDFs into briefing-template markdown.

    python3 ingest_betterbriefs.py                       # default paths below
    python3 ingest_betterbriefs.py --src <dir of PDFs> --out <corpus>/rag/briefing-template

The four BetterBriefs PDFs (global report, best-practice guide, booklet, "the issues with
briefs") are research on what makes a good brief. They belong in the `instructions`
bucket — "what a good brief contains" — which held only 76 template chunks. The chunker
already routes files in briefing-template/ as source `template`, so the only job here is
to produce markdown it can split: one `# title`, then one `## section` per PDF page.

Design notes
  * One section per page, headed "Page N — <first line>". PDF extraction loses the
    document's own headings, and a page is a unit a reader can find again from a citation.
  * Pages with under MIN_PAGE_CHARS of text (covers, blank, image-only) are skipped: they
    would become empty or near-empty chunks that match broadly.
  * Whitespace is normalised; nothing is summarised or rewritten — the text is the source.
  * Deterministic output (sorted files, stable names), so a rebuild is reproducible.
  * The output directory is the corpus, outside the repo; this script is the record of how
    those files were made.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

REF = Path("/Users/saieeshwar/Projects/Work/work/napkin/briefing/reference")
SRC = REF / "betterbriefs"
OUT = REF / "rag" / "briefing-template"
MIN_PAGE_CHARS = 200


def _clean(text: str) -> str:
    """Collapse runs of spaces, keep paragraph breaks, drop hyphenation at line ends."""
    text = re.sub(r"-\n(?=[a-z])", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def _title(pdf: Path) -> str:
    """A readable document title from the file name."""
    stem = re.sub(r"[_-]+", " ", pdf.stem).strip()
    return f"BetterBriefs — {stem[:1].upper() + stem[1:]}"


def convert(pdf: Path) -> tuple[str, int, int]:
    """(markdown, pages kept, pages total) for one PDF."""
    import pypdf
    reader = pypdf.PdfReader(str(pdf))
    parts, kept = [f"# {_title(pdf)}\n", f"Source: {pdf.name} (BetterBriefs research).\n"], 0
    for i, page in enumerate(reader.pages, 1):
        text = _clean(page.extract_text() or "")
        if len(text) < MIN_PAGE_CHARS:
            continue
        first = next((l.strip() for l in text.splitlines() if l.strip()), "")[:70]
        parts.append(f"## Page {i} — {first}\n\n{text}\n")
        kept += 1
    return "\n".join(parts), kept, len(reader.pages)


def main() -> None:
    """Convert every PDF in --src into a markdown file in --out."""
    ap = argparse.ArgumentParser(description="BetterBriefs PDFs -> briefing-template markdown")
    ap.add_argument("--src", default=str(SRC))
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for pdf in sorted(Path(a.src).glob("*.pdf")):
        md, kept, total = convert(pdf)
        dest = out / f"{_title(pdf).replace('/', '-')}.md"
        dest.write_text(md, encoding="utf-8")
        print(f"{pdf.name}: {kept}/{total} pages -> {dest.name} ({len(md):,} chars)")


if __name__ == "__main__":
    main()
