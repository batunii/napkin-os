"""Reading a document the way a person does: the PDF itself goes to the model, which sees every
page as a picture as well as its text, so a deck's charts, layout and image-only slides are read,
not just whatever text layer the file happens to carry (the owner, 2026-10-01: "Claude can
understand the images, so why can't we send it that?"). A Word file or a deck's pictures go as
images beside their text.

  should_read(name, text, document, images) -> (yes, why)   whether to send it, and why (logged)
  read(model, name, text, document, images)  -> the reading, or None (the text layer then stands)

A PDF is read when it is a deck (by name), has little text for its pages (image-heavy or scanned),
or has no text at all; a long text-heavy report is left to its text layer, which costs nothing.
At most MAX_PAGES pages go in one call (the API's limit is 100).
"""
from __future__ import annotations

import base64
import re

MAX_PAGES = 100
SPARSE = 1200        # characters of text layer per page below which a page is mostly picture
DECK = re.compile(r"(deck|presentation|slides?|pitch|pptx?|keynote|moodboard|creds|credentials)", re.I)

SYSTEM = """You read a document a client sent an advertising agency, exactly as a person reading it would.
Write it out page by page, in order, each page headed "Page N" (for a deck, "Slide N").
- Every word on the page as written: headings, body text, bullets, labels, captions, table cells, footnotes.
- For every chart, graph, diagram, photo, mood board or image of text: one line in square brackets saying what it
  shows, with every number, label and axis it shows, e.g. "[Bar chart: own-label share of UK water, 10% (2023),
  16% (2024), 24% (2025)]".
- Keep the page's structure: what is the headline, what is a footnote, what goes with which picture.
Never summarise, interpret, judge or add anything that is not on the page."""

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["text", "pages"],
          "properties": {"text": {"type": "string"}, "pages": {"type": "integer"}}}


def pdf_pages(data: bytes) -> int:
    """Pages in a PDF, counted from its page objects (no PDF library needed); 0 when unknown."""
    return len(re.findall(rb"/Type\s*/Page(?![a-zA-Z])", data))


def should_read(name: str, text: str | None, document: dict | None, images: list | None) -> tuple[bool, str]:
    if images:
        return True, f"{len(images)} picture(s) in it"
    if not document:
        return False, "nothing to see beyond its text"
    try:
        raw = base64.b64decode(document.get("data") or "", validate=True)
    except (ValueError, TypeError):
        return False, "the PDF did not decode"
    pages = pdf_pages(raw)
    chars = len((text or "").strip())
    if pages > MAX_PAGES:
        return False, f"{pages} pages, over the {MAX_PAGES} a model reads at once: its text layer stands"
    if not chars:
        return True, "no text layer: scanned or all pictures"
    if DECK.search(name or ""):
        return True, "a deck"
    if pages and chars / pages < SPARSE:
        return True, f"little text for its {pages} page(s) ({chars // max(pages, 1)} characters a page): mostly pictures"
    return False, f"a text-heavy document ({pages} page(s), {chars} characters): its text layer stands"


def read(model, name: str, text: str | None, document: dict | None = None, images: list | None = None,
         max_tokens: int = 32000) -> str | None:
    parts = ([document] if document else []) + list(images or [])[:20]
    if not parts:
        return None
    payload = {"name": name, "text_layer": (text or "")[:20000] or None,
               "note": "The text layer is what a PDF tool pulled out of the file; the pages themselves are attached."}
    out = model.structured("read_document", SYSTEM, payload, SCHEMA, max_tokens=max_tokens, images=parts, vision=True)
    t = (out.get("text") or "").strip()
    return t or None


def read_inputs(model, inp: dict, note=lambda *a, **k: None) -> list[str]:
    """For a task whose attachments carry `text` (the Research Tool's extract): read each one that
    carries its PDF or pictures and should be read, and put the reading in its `text`. Returns notes
    on any that could not be read (their text layer stands)."""
    notes, todo = [], []
    for a in inp.get("attachments") or []:
        if not isinstance(a, dict):
            continue
        doc = a.get("document") if isinstance(a.get("document"), dict) else None
        pics = [x for x in a.get("images") or [] if isinstance(x, dict)]
        text = a.get("text") if isinstance(a.get("text"), str) else None
        ok, why = should_read(str(a.get("name") or ""), text, doc, pics)
        note("read_document", name=str(a.get("name") or "")[:80], read=ok, why=why)
        if ok:
            todo.append((a, doc, pics, text))

    def one(job):
        a, doc, pics, text = job
        try:
            return job, read(model, str(a.get("name") or ""), text, doc, pics), None
        except Exception as e:  # noqa: BLE001 - the text layer stands
            return job, None, e
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=max(1, min(4, len(todo)))) as ex:  # every file at once
        done = list(ex.map(one, todo))
    for (a, doc, pics, text), t, err in done:
        if err is not None:
            notes.append(f"{a.get('name')} could not be read as pages ({getattr(err, 'kind', type(err).__name__)})")
            continue
        if t:
            a["text"] = t if not (text and text.strip()) else t + (
                "\n\n[The file's own text layer]\n" + text if len(text) > len(t) * 1.5 else "")
    return notes
