"""Image-heavy PDF pages and image files are read by Claude (2026-10-02): every flagged page, no page
limit, the text layer kept, a marker for a page nobody could read, a cache, Claude only."""
import json
import subprocess
from types import SimpleNamespace

import pytest

import brief_ingest as bi
import brief_llm as bl

fitz = pytest.importorskip("fitz")


def _pdf(tmp_path, pages):
    """A PDF whose pages are ('text', words) or ('image', None): an image page has a full-page picture."""
    doc = fitz.open()
    for kind, words in pages:
        pg = doc.new_page(width=400, height=400)
        if kind == "text":
            pg.insert_text((20, 40), words, fontsize=9)
        else:
            pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 50, 50), False)
            pix.clear_with(200)
            pg.insert_image(pg.rect, pixmap=pix)
    path = tmp_path / "deck.pdf"
    doc.save(str(path))
    return path


def test_a_page_is_flagged_for_little_text_or_mostly_pictures():
    page = lambda text, imgs: SimpleNamespace(extract_text=lambda: text, width=100, height=100, images=imgs)
    assert bi._page_needs_vision(page("x" * 50, []))                                       # little text
    assert bi._page_needs_vision(page("x" * 900, [{"x0": 0, "x1": 60, "top": 0, "bottom": 60}]))  # 36% picture
    assert not bi._page_needs_vision(page("x" * 900, [{"x0": 0, "x1": 10, "top": 0, "bottom": 10}]))


def test_flagged_pages_are_read_after_their_text_layer_every_page_no_limit(tmp_path, monkeypatch):
    long = "word " * 120
    path = _pdf(tmp_path, [("text", long)] + [("image", None)] * 24 + [("text", long)])
    seen = []
    monkeypatch.setattr(bi, "_vision_transcribe", lambda b, mime, label="": seen.append(label) or f"read {label}")
    text, mime = bi.ingest(path)
    assert mime == "application/pdf" and len(seen) == 24                     # all 24 image pages, no 20-page cap
    assert "[page 2, read from the page image]\nread deck.pdf p2" in text
    assert "read deck.pdf p25" in text and text.index("p2") < text.index("p25")
    assert "PDF, 24 of 26 pages read from page images by Claude" == bi._INGEST_NOTES["transcribed"]


def test_a_page_nobody_could_read_is_marked_not_dropped(tmp_path, monkeypatch):
    path = _pdf(tmp_path, [("text", "word " * 120), ("image", None)])
    monkeypatch.setattr(bi, "_vision_transcribe", lambda b, mime, label="": "")
    text, _ = bi.ingest(path)
    assert "[page 2: picture content could not be read]" in text
    assert "1 unreadable" in bi._INGEST_NOTES["transcribed"]


def test_transcriptions_are_cached_by_image(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIEF_VISION_CACHE", str(tmp_path))
    calls = []
    monkeypatch.setattr(bl, "transcribe_image", lambda data, mime, prompt, label="": calls.append(1) or "page text")
    assert bi._vision_transcribe(b"png-1", "image/png") == "page text"
    assert bi._vision_transcribe(b"png-1", "image/png") == "page text"
    assert bi._vision_transcribe(b"png-2", "image/png") == "page text"
    assert len(calls) == 2


def test_claude_reads_the_image_in_the_message_and_falls_back_to_the_next_model(monkeypatch):
    """Over the CLI the image goes in a stream-json user message (no tools to open a file); when the
    first vision model fails the next one answers. Every model is Claude."""
    monkeypatch.setenv("BRIEF_CLAUDE_TRANSPORT", "cli")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/claude")
    sent = []

    def fake_run(cmd, input=None, **kw):
        model = cmd[cmd.index("--model") + 1]
        msg = json.loads(input)
        sent.append((model, msg["message"]["content"][0]["type"], cmd[cmd.index("--input-format") + 1]))
        if model == bl.route_models("vision")[0]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")
        out = json.dumps({"type": "result", "subtype": "success", "result": "Trust 73%",
                          "usage": {"input_tokens": 1500, "output_tokens": 10}})
        return subprocess.CompletedProcess(cmd, 0, stdout=out + "\n", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert bl.transcribe_image(b"\x89PNG", "image/png", "prompt") == "Trust 73%"
    models = bl.route_models("vision")
    assert [s[0] for s in sent] == models[:2] and all(m.startswith("claude-") for m in models)
    assert all(kind == "image" and fmt == "stream-json" for _m, kind, fmt in sent)
