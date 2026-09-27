"""
brief_ingest.py — reading a client brief into text (split out of parse_brief.py, 2026-09-27).

  ingest(path)         .txt / .md / .docx / .pdf / .eml / images -> (text, mime); scanned PDFs
                       and images go through the vision model and are tagged in
                       _INGEST_NOTES["transcribed"] so run() can say so (audit critic-G8)
  docx_text(path)      a .docx in document order, tables where they sit (audit critic-G1)
  ingest_email_text    an .eml body as text
  segment(text)        the sentence / line segments the no-loss ledger counts

Re-exported by parse_brief; new code should import from here.
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

from brief_llm import PROVIDERS, _HTTP_UA


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

# image input → faithful text, via a NIM vision model (no-loss capture stays intact)
_IMAGE_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp"}


def _vision_transcribe(image_bytes: bytes, mime: str, label: str = "image") -> str:
    """Transcribe one image to text with a vision model (default NIM nemotron-nano-vl).
    Returns '' and warns on failure rather than crashing the run."""
    # Vision endpoint is independent of the main model: point it at NIM (default) or a
    # local Ollama (keyless) via BRIEF_VISION_BASE/MODEL — e.g. gemma3 for image->text.
    base = os.environ.get("BRIEF_VISION_BASE", PROVIDERS["nim"][0])
    model = os.environ.get("BRIEF_VISION_MODEL", "nvidia/llama-3.1-nemotron-nano-vl-8b-v1")
    key = os.environ.get("BRIEF_VISION_API_KEY") or os.environ.get("NVIDIA_API_KEY")
    is_local = "localhost" in base or "127.0.0.1" in base   # e.g. Ollama — no key needed
    if not key and not is_local:
        print(f"[i] {label}: image ingest needs a vision key (NVIDIA_API_KEY / BRIEF_VISION_API_KEY) "
              "or a local endpoint (BRIEF_VISION_BASE=http://localhost:11434/v1) — skipping.",
              file=sys.stderr)
        return ""
    headers = {"Content-Type": "application/json", "User-Agent": _HTTP_UA}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    payload = {
        "model": model, "temperature": 0.0,
        "max_tokens": int(os.environ.get("BRIEF_MAX_TOKENS", "4000")),
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": _VISION_PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(image_bytes).decode()}"}},
        ]}],
    }
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions", data=json.dumps(payload).encode(),
        headers=headers, method="POST")
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                content = json.loads(r.read())["choices"][0]["message"].get("content") or ""
                return re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:200]
            if e.code in (429, 500, 502, 503) and attempt < 2:
                import time
                time.sleep(5 * (attempt + 1)); continue
            print(f"[i] {label}: vision model HTTP {e.code}: {detail}", file=sys.stderr)
            return ""
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt < 2:
                import time
                time.sleep(3 * (attempt + 1)); continue
            print(f"[i] {label}: vision model unreachable: {e}", file=sys.stderr)
            return ""
    return ""


def _pdf_vision_transcribe(path: Path, max_pages: int = 20) -> str:
    """Render an image-only / slide-deck PDF page by page and transcribe each."""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        print(f"[i] {path.name}: looks image-only but PyMuPDF isn't installed "
              "(pip install pymupdf) — can't transcribe.", file=sys.stderr)
        return ""
    doc = fitz.open(str(path))
    n = min(len(doc), max_pages)
    if len(doc) > max_pages:
        print(f"[i] {path.name}: image-PDF — transcribing first {max_pages} of {len(doc)} pages.",
              file=sys.stderr)
    parts = []
    for i in range(n):
        png = doc[i].get_pixmap(dpi=150).tobytes("png")
        t = _vision_transcribe(png, "image/png", f"{path.name} p{i + 1}")
        if t:
            parts.append(f"--- page {i + 1} ---\n{t}")
    return "\n\n".join(parts)


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
            parts.append(Paragraph(child, d).text)
        elif tag == "tbl":
            for row in Table(child, d).rows:
                parts.append(" | ".join(c.text for c in row.cells))
    return "\n".join(parts)


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
        out = []
        with pdfplumber.open(str(path)) as pdf:
            for page in pdf.pages:
                out.append(page.extract_text() or "")
        text = "\n".join(out)
        # Image-only / slide-deck PDFs carry little or no text layer — fall back to
        # rendering each page and transcribing it with the vision model.
        if len(text.strip()) < max(200, 40 * max(1, len(out))):
            print(f"[i] {path.name}: thin text layer — transcribing pages with the vision model.",
                  file=sys.stderr)
            transcribed = _pdf_vision_transcribe(path)
            if transcribed:
                _INGEST_NOTES["transcribed"] = f"scanned PDF, {len(out)} pages, vision model"
            text = transcribed or text
        return text, "application/pdf"
    if suffix in _IMAGE_MIME:
        mime = _IMAGE_MIME[suffix]
        _INGEST_NOTES["transcribed"] = f"image ({suffix}), vision model"
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
    '_pdf_vision_transcribe',
    '_strip_html',
    '_vision_transcribe',
    'docx_text',
    'ingest',
    'ingest_email_text',
    'segment',
)
