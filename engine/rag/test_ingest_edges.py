"""S1 edge cases from docs/brief-maker/edge-cases.md (2026-10-03): input that was silently dropped or misread.
Each test is one register row: Word content (EC-064/065), text encodings (EC-063), email parts (EC-060-062),
duplicate fact versions (EC-072), the facts: wrapper (EC-005), a malformed fact row (EC-071), empty input
(EC-066)."""
import email.message
import zipfile
from pathlib import Path

import pytest

import brief_ingest as bi
import research_facts as rf

docx = pytest.importorskip("docx")

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _docx_with_body(tmp_path: Path, body_xml: str, extra_parts: "dict | None" = None) -> Path:
    """A minimal .docx whose body is `body_xml` (w: elements), plus optional extra parts (footnotes...)."""
    base = tmp_path / "base.docx"
    docx.Document().save(str(base))
    out = tmp_path / "edge.docx"
    with zipfile.ZipFile(base) as zin, zipfile.ZipFile(out, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "word/document.xml":
                text = data.decode()
                start = text.index("<w:body>") + len("<w:body>")
                end = text.index("<w:sectPr")
                text = text[:start] + body_xml + text[end:]
                data = text.encode()
            zout.writestr(item, data)
        for name, xml in (extra_parts or {}).items():
            zout.writestr(name, xml)
    return out


def _p(text: str) -> str:
    return f'<w:p><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'


def test_word_tracked_insertions_content_controls_text_boxes_and_nested_tables_are_read(tmp_path):
    body = (_p("Plain paragraph.")
            + '<w:p><w:r><w:t>Budget is </w:t></w:r><w:ins w:id="1" w:author="a"><w:r><w:t>EUR 140k</w:t></w:r></w:ins>'
              '<w:del w:id="2" w:author="a"><w:r><w:delText>EUR 90k</w:delText></w:r></w:del></w:p>'
            + '<w:sdt><w:sdtContent>' + _p("Inside a content control.") + '</w:sdtContent></w:sdt>'
            + '<w:p><w:r><w:t>Before box. </w:t></w:r><w:r><w:pict><v:shape xmlns:v="urn:schemas-microsoft-com:vml">'
              '<v:textbox><w:txbxContent>' + _p("Text box words.") + '</w:txbxContent></v:textbox></v:shape></w:pict></w:r></w:p>'
            + '<w:tbl><w:tr><w:tc>' + _p("Outer cell")
            + '<w:tbl><w:tr><w:tc>' + _p("Nested cell") + '</w:tc></w:tr></w:tbl>'
            + '</w:tc></w:tr></w:tbl>')
    text = bi.docx_text(_docx_with_body(tmp_path, body))
    for want in ("Plain paragraph.", "Budget is EUR 140k", "Inside a content control.", "Text box words.",
                 "Outer cell", "Nested cell"):
        assert want in text, want
    assert "EUR 90k" not in text                                   # deleted text stays deleted


def test_a_field_code_hyperlink_keeps_its_address(tmp_path):
    body = ('<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r>'
            '<w:r><w:instrText xml:space="preserve"> HYPERLINK "https://example.ie/report" </w:instrText></w:r>'
            '<w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>the report</w:t></w:r>'
            '<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>')
    assert "the report <https://example.ie/report>" in bi.docx_text(_docx_with_body(tmp_path, body))


def test_word_footnotes_are_read_once_and_labelled(tmp_path):
    foot = (f'<w:footnotes xmlns:w="{W}"><w:footnote w:id="-1" w:type="separator"><w:p><w:r><w:t>---</w:t></w:r></w:p></w:footnote>'
            f'<w:footnote w:id="1">{_p("Source: CSO 2025.")}</w:footnote></w:footnotes>')
    path = _docx_with_body(tmp_path, _p("Main text."), {"word/footnotes.xml": foot})
    text = bi.docx_text(path)
    assert "Main text." in text and "Source: CSO 2025." in text and "---" not in text
    assert text.index("Main text.") < text.index("Source: CSO 2025.") and "[Footnotes]" in text


@pytest.mark.parametrize("encoding,bom", [("utf-16", b""), ("utf-8-sig", b""), ("cp1252", b"")])
def test_text_files_in_other_encodings_keep_their_characters(tmp_path, encoding, bom):
    p = tmp_path / "brief.txt"
    p.write_bytes(bom + "Budget €140k, café launch".encode(encoding))
    text, _ = bi.ingest(p)
    assert "€140k" in text and "café" in text and "�" not in text and "\x00" not in text


def _eml(tmp_path, msg) -> Path:
    p = tmp_path / "brief.eml"
    p.write_bytes(msg.as_bytes())
    return p


def test_an_email_keeps_its_forwarded_message_and_its_attachments(tmp_path):
    inner = email.message.EmailMessage()
    inner["Subject"] = "Original brief"
    inner.set_content("The client wants 18-34s.")
    outer = email.message.EmailMessage()
    outer["Subject"] = "Fwd: brief"
    outer.set_content("See below and attached.")
    outer.add_attachment(inner)                                          # message/rfc822
    outer.add_attachment(b"Budget: EUR 140k", maintype="text", subtype="plain", filename="budget.txt")
    text, mime = bi.ingest(_eml(tmp_path, outer))
    assert mime == "message/rfc822"
    for want in ("See below and attached.", "The client wants 18-34s.", "Budget: EUR 140k", "budget.txt"):
        assert want in text, want


def test_an_email_with_an_unknown_charset_or_only_an_attachment_does_not_crash(tmp_path):
    msg = email.message.EmailMessage()
    msg["Subject"] = "odd"
    msg.set_content("hello")
    raw = msg.as_bytes().replace(b'charset="utf-8"', b'charset="x-nonsense"')
    p = tmp_path / "odd.eml"
    p.write_bytes(raw)
    assert "hello" in bi.ingest(p)[0]
    only = email.message.EmailMessage()
    only["Subject"] = "attachment only"
    only.add_attachment(b"Just the attachment", maintype="text", subtype="plain", filename="a.txt")
    assert "Just the attachment" in bi.ingest(_eml(tmp_path, only))[0]


def test_of_two_versions_of_a_fact_the_newer_is_used():
    usable, skipped = rf.current([{"id": "f_1", "version": 1, "value": "old"}, {"id": "f_1", "version": 2, "value": "new"}])
    assert [f["value"] for f in usable] == ["new"] and skipped and skipped[0]["id"] == "f_1"


def test_a_wrapped_facts_dict_is_unwrapped_not_silently_lost():
    usable, _ = rf.current({"facts": [{"id": "f_1", "value": 1}, {"id": "f_2", "value": 2}]})
    assert [f["id"] for f in usable] == ["f_1", "f_2"]


def test_a_malformed_fact_row_is_skipped_with_its_reason_not_a_crash():
    usable, skipped = rf.current([{"id": ["x"], "value": 1}, {"id": "f_ok", "value": 2}])
    assert [f["id"] for f in usable] == ["f_ok"] and any("id" in s["why"] for s in skipped)


def test_an_empty_input_stops_before_any_model_call(monkeypatch):
    import parse_brief as pb
    monkeypatch.setattr(pb, "_json_call", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no model call")))
    with pytest.raises(pb.EmptyInput):
        pb.run(None, raw_text="   \n  ", source_name="empty")
