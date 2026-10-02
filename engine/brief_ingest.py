"""
brief_ingest.py — reading a client brief into text (split out of parse_brief.py, 2026-09-27).

  ingest(path)         .txt / .md / .docx / .pdf / .eml / images -> (text, mime); every PDF page
                       with little text or mostly pictures, and every image, is also read by
                       Claude from its image (2026-10-02) and tagged in _INGEST_NOTES["transcribed"]
                       so run() can say so (audit critic-G8)
  docx_text(path)      a .docx in document order, tables where they sit (audit critic-G1)
                       and each hyperlink's address kept as 'text <url>' (2026-09-28)
  ingest_email_text    an .eml body as text
  segment(text)        the sentence / line segments the no-loss ledger counts

Re-exported by parse_brief; new code should import from here.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path



# ---------------------------------------------------------------------------
# 1. INGEST
# ---------------------------------------------------------------------------

_VISION_PROMPT = (
    "Transcribe this document image into clean, faithful text for an advertising-brief pipeline.\n"
    "Rules: (1) Capture ALL text verbatim — headings, body, bullets, labels, captions, table cells, "
    "prices, names, figures. Lose nothing. (2) Preserve reading order and structure (use markdown "
    "headings / bullets / tables to mirror the layout). (3) For a meaningful non-text visual (a chart, "
    "an org diagram, a product photo with a caption), add a short bracketed note of what it shows. "
    "(4) Do NOT summarise, interpret, or invent — transcribe only. Output only the transcription."
)

_IMAGE_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp"}

# A PDF page is read from its image as well as its text layer when it has little text or is mostly
# pictures (charts, scans, slides). Measured 2026-10-02 on the Samaritans tracker: 5 of 8 pages, the
# chart pages whose current figures the text layer did not hold.
PAGE_MIN_CHARS = 300
PAGE_IMAGE_SHARE = 0.3
VISION_WORKERS = 4


def _vision_cache_dir() -> Path:
    """Where page transcriptions are kept between runs, keyed by image and prompt (BRIEF_VISION_CACHE,
    default ~/.cache/napkin/vision): a rerun on the same document reads no page twice."""
    d = Path(os.environ.get("BRIEF_VISION_CACHE") or Path.home() / ".cache" / "napkin" / "vision")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _vision_transcribe(image_bytes: bytes, mime: str, label: str = "image") -> str:
    """Transcribe one image to text with Claude (brief_llm.transcribe_image, the 'vision' route), cached.
    Returns '' when no model could read it, so the caller can mark it unread."""
    import hashlib
    from brief_llm import transcribe_image
    key = hashlib.sha256(image_bytes + _VISION_PROMPT.encode()).hexdigest()
    hit = _vision_cache_dir() / f"{key}.txt"
    if hit.is_file():
        return hit.read_text(encoding="utf-8")
    text = transcribe_image(image_bytes, mime, _VISION_PROMPT, label)
    if text:
        hit.write_text(text, encoding="utf-8")
    return text


def _page_needs_vision(page) -> bool:
    """Does this pdfplumber page carry content its text layer may not hold: little text, or pictures
    covering PAGE_IMAGE_SHARE of it (charts, scans, slide art)?"""
    text = (page.extract_text() or "").strip()
    area = float(page.width * page.height) or 1.0
    pictures = sum(max(0.0, im["x1"] - im["x0"]) * max(0.0, im["bottom"] - im["top"]) for im in page.images)
    return len(text) < PAGE_MIN_CHARS or pictures / area >= PAGE_IMAGE_SHARE


def _pdf_pages_with_vision(path: Path, pages_text: list, needs: list) -> "tuple[list, int, int]":
    """Every page's text, with a Claude transcription of each flagged page's image added after it (the
    text layer is kept: nothing is replaced). All pages, in parallel, never a page limit. -> (texts,
    pages read from images, pages that could not be read). An unreadable page gets an inline marker so
    the brief raises it instead of losing it."""
    flagged = [i for i, n in enumerate(needs) if n]
    if not flagged:
        return pages_text, 0, 0
    try:
        import fitz  # PyMuPDF
    except ImportError:
        print(f"[!] {path.name}: {len(flagged)} page(s) carry images but PyMuPDF is not installed "
              "(pip install pymupdf); they are marked unread.", file=sys.stderr)
        out = list(pages_text)
        for i in flagged:
            out[i] += f"\n[page {i + 1}: picture content not read (no PDF renderer)]"
        return out, 0, len(flagged)
    doc = fitz.open(str(path))
    pngs = {i: doc[i].get_pixmap(dpi=150).tobytes("png") for i in flagged}
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=VISION_WORKERS) as ex:
        got = dict(zip(flagged, ex.map(lambda i: _vision_transcribe(pngs[i], "image/png", f"{path.name} p{i + 1}"),
                                       flagged)))
    out, read, unread = list(pages_text), 0, 0
    for i in flagged:
        if got[i]:
            out[i] = (out[i] + f"\n[page {i + 1}, read from the page image]\n" + got[i]).strip()
            read += 1
        else:
            out[i] += f"\n[page {i + 1}: picture content could not be read]"
            unread += 1
    return out, read, unread


def _strip_html(html: str) -> str:
    """Crude HTML→text: drop tags, unescape the common entities. Good enough for
    an email body when no text/plain part exists."""
    import html as _h
    text = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return _h.unescape(text)


def ingest_email_text(raw: str) -> str:
    """A copy-pasted email is just text. Keep it verbatim — Loop 1 is no-loss —
    but normalise CRLF so the segmenter sees clean lines."""
    return raw.replace("\r\n", "\n").replace("\r", "\n")


def docx_text(path: Path) -> str:
    """The text of a .docx in DOCUMENT ORDER: paragraphs and tables interleaved as they
    appear, a table row as its cells joined with ' | '. python-docx's `paragraphs` then
    `tables` put every table at the end, which moved the employer brief's budget sentence
    from char 483 to char 6,137, one past the judge clip, and renumbered every cited
    sentence (audit critic-G1). Shared with rag/labelset._doc_text so the label set and the
    pipeline read the same text."""
    try:
        import docx
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except ImportError:
        sys.exit("Need python-docx for .docx:  pip install python-docx")
    d = docx.Document(str(path))
    parts = []
    for child in d.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            parts.append(_para_text(Paragraph(child, d)))
        elif tag == "tbl":
            for row in Table(child, d).rows:
                parts.append(" | ".join("\n".join(_para_text(p) for p in c.paragraphs) for c in row.cells))
    return "\n".join(parts)


def _same_address(text: str, url: str) -> bool:
    """True when a link's visible text already is its address (scheme, mailto: and a
    trailing slash ignored), so repeating it would add nothing."""
    norm = lambda x: re.sub(r"^(https?://|mailto:)", "", str(x or "").strip(), flags=re.I).rstrip("/").lower()
    return norm(text) == norm(url)


def _para_text(p) -> str:
    """A paragraph's text with each external hyperlink's address kept as 'text <url>'.
    python-docx's Paragraph.text keeps a link's visible text and drops its target, so a
    fact cited by a link ('source here') lost its source (2026-09-28, for the brand and
    category research documents, which cite by hyperlink). A link whose text already is
    its address, and an internal bookmark link, is left as it was."""
    from docx.text.hyperlink import Hyperlink
    out = []
    for item in p.iter_inner_content():
        text = item.text
        if isinstance(item, Hyperlink) and item.address and not _same_address(text, item.url):
            text = f"{text} <{item.url}>"
        out.append(text)
    return "".join(out)


# Set by ingest() when the text came through the vision model (an image, or a PDF with a
# thin text layer), read by run() so the brief says it was transcribed (audit critic-G8:
# a transcript from an 8B vision model was treated as the client's verbatim words).
_INGEST_NOTES: dict = {}


def ingest(path: Path) -> tuple[str, str]:
    """Return (raw_text, mime) from .txt/.md, .docx, .pdf, or .eml. Sets _INGEST_NOTES
    ['transcribed'] when the text is a vision-model transcript."""
    _INGEST_NOTES.clear()
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md", ".text"}:
        return path.read_text(encoding="utf-8", errors="replace"), "text/plain"
    if suffix == ".eml":
        import email
        from email import policy
        msg = email.message_from_bytes(path.read_bytes(), policy=policy.default)
        body = msg.get_body(preferencelist=("plain", "html"))
        content = body.get_content() if body else (msg.get_content() or "")
        if body is not None and body.get_content_type() == "text/html":
            content = _strip_html(content)
        hdr = [f"{k}: {msg[k]}" for k in ("Subject", "From", "Date") if msg[k]]
        text = ("\n".join(hdr) + "\n\n" + content) if hdr else content
        return ingest_email_text(text), "message/rfc822"
    if suffix == ".docx":
        return docx_text(path), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if suffix == ".pdf":
        try:
            import pdfplumber
        except ImportError:
            sys.exit("Need pdfplumber for .pdf:  pip install pdfplumber")
        texts, needs = [], []
        with pdfplumber.open(str(path)) as pdf:
            for page in pdf.pages:
                texts.append(page.extract_text() or "")
                needs.append(_page_needs_vision(page))
        texts, read, unread = _pdf_pages_with_vision(path, texts, needs)
        if read or unread:
            print(f"[i] {path.name}: {read} of {len(texts)} page(s) also read from the page image by Claude"
                  + (f"; {unread} could not be read" if unread else ""), file=sys.stderr)
            _INGEST_NOTES["transcribed"] = (f"PDF, {read} of {len(texts)} pages read from page images by Claude"
                                            + (f", {unread} unreadable" if unread else ""))
        text = "\n".join(texts)
        return text, "application/pdf"
    if suffix in _IMAGE_MIME:
        mime = _IMAGE_MIME[suffix]
        _INGEST_NOTES["transcribed"] = f"image ({suffix}), read by Claude"
        return _vision_transcribe(path.read_bytes(), mime, path.name), mime
    sys.exit(f"Unsupported file type: {suffix}. Use .txt, .md, .docx, .pdf, .eml, "
             "or an image (.png/.jpg/.jpeg/.webp) — or paste with --text / '-' for stdin.")


# ---------------------------------------------------------------------------
# 2. SEGMENT
# ---------------------------------------------------------------------------

def segment(text: str) -> list[str]:
    """Coalesce soft-wrapped lines into blocks, then sentence-split. Each
    segment becomes a row in the no-loss ledger."""
    blocks: list[str] = []
    buf: list[str] = []

    def flush():
        """Join the buffered lines into one block, append it to `blocks` and clear the
        buffer; a no-op when the buffer is empty."""
        if buf:
            blocks.append(" ".join(buf).strip())
            buf.clear()

    for raw in text.splitlines():
        stripped = raw.strip()
        is_bullet = bool(re.match(r"^\s*[-*•]\s+", raw))
        is_label = bool(re.match(r"^[A-Za-z /]{3,30}\s*[:=]\s+\S", stripped))
        if not stripped:
            flush(); continue
        if is_bullet or is_label:
            flush()
        buf.append(stripped.lstrip("-*• \t"))
    flush()

    segs: list[str] = []
    for block in blocks:
        for piece in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'])", block):
            piece = piece.strip()
            if len(piece) >= 4:
                segs.append(piece)
    return segs


# Names parse_brief re-exports and forwards assignments for (see parse_brief._ForwardingModule).
MOVED_NAMES = (
    '_IMAGE_MIME',
    '_INGEST_NOTES',
    '_VISION_PROMPT',
    '_pdf_pages_with_vision', '_page_needs_vision',
    '_strip_html',
    '_vision_transcribe',
    'docx_text',
    'ingest',
    'ingest_email_text',
    'segment',
)
