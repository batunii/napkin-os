"""reading.py: a PDF or a deck goes to the model whole when its pages hold what the text layer misses."""
import base64

from napkin import reading


def pdf(pages):
    return {"media_type": "application/pdf",
            "data": base64.b64encode(b"%PDF-1.7 " + b" ".join(b"<< /Type /Page >>" for _ in range(pages))
                                     + b" << /Type /Pages >>").decode()}


def test_what_is_read_as_pages_and_what_keeps_to_its_text():
    assert reading.should_read("scan.pdf", "", pdf(3), None)[0], "no text layer: scanned or all pictures"
    assert reading.should_read("Q3 pitch deck.pdf", "x" * 90000, pdf(30), None)[0], "a deck by its name"
    ok, why = reading.should_read("report.pdf", "x" * 2000, pdf(10), None)
    assert ok and "mostly pictures" in why, "200 characters a page"
    ok, why = reading.should_read("report.pdf", "x" * 60000, pdf(20), None)
    assert not ok and "text-heavy" in why
    assert not reading.should_read("huge.pdf", "", pdf(150), None)[0], "over the hundred pages one call reads"
    assert reading.should_read("brief.docx", "words", None, [{"media_type": "image/png", "data": "x"}])[0]
    assert not reading.should_read("notes.txt", "words", None, None)[0]


class Model:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def structured(self, purpose, system, payload, schema, **kw):
        self.calls.append((purpose, payload, kw))
        if self.fail:
            raise RuntimeError("no")
        return {"text": "Page 1\n[Bar chart: own-label share 10%, 16%, 24%]", "pages": 1}


def test_a_research_attachment_gets_its_reading_and_a_failure_keeps_its_text():
    m = Model()
    inp = {"attachments": [{"name": "deck.pdf", "text": "Glenmore", "document": pdf(1)},
                           {"name": "notes.txt", "text": "plain notes"}]}
    seen = []
    assert reading.read_inputs(m, inp, lambda what, **kw: seen.append(kw)) == []
    assert inp["attachments"][0]["text"].startswith("Page 1\n[Bar chart")
    assert inp["attachments"][1]["text"] == "plain notes" and len(m.calls) == 1
    purpose, _, kw = m.calls[0]
    assert purpose == "read_document" and kw["vision"] and kw["images"][0]["media_type"] == "application/pdf"
    assert [s["read"] for s in seen] == [True, False]
    bad = {"attachments": [{"name": "deck.pdf", "text": "Glenmore", "document": pdf(1)}]}
    notes = reading.read_inputs(Model(fail=True), bad)
    assert notes and bad["attachments"][0]["text"] == "Glenmore", "the text layer stands"
