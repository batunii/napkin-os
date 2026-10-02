// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! The text of a Word document or a PowerPoint deck, so the agent reads a
//! client's `.docx` brief or `.pptx` deck as it reads a PDF.
//!
//! Both are zip archives of XML. A `.docx` keeps its body in
//! `word/document.xml`: runs of text in `<w:t>`, paragraphs closed by
//! `</w:p>`, tabs and breaks as `<w:tab/>` and `<w:br/>`. A `.pptx` keeps one
//! `ppt/slides/slideN.xml` per slide, text in `<a:t>`, paragraphs closed by
//! `</a:p>`; slides are read in their number order, each headed "Slide N".
//! No XML library: the scan only needs the text between those tags. Anything
//! that does not open as such an archive yields None, like any binary file.

use std::io::{Cursor, Read};

/// The text of `bytes` as a `.docx` (`kind` "docx") or `.pptx` ("pptx").
pub fn text(kind: &str, bytes: &[u8]) -> Option<String> {
    let mut zip = zip::ZipArchive::new(Cursor::new(bytes)).ok()?;
    match kind {
        "docx" => {
            let xml = entry(&mut zip, "word/document.xml")?;
            Some(scan(&xml, "w"))
        }
        "pptx" => {
            let mut slides: Vec<(u32, String)> = (0..zip.len())
                .filter_map(|i| {
                    let name = zip.by_index(i).ok()?.name().to_string();
                    let n = name.strip_prefix("ppt/slides/slide")?.strip_suffix(".xml")?.parse().ok()?;
                    Some((n, name))
                })
                .collect();
            slides.sort();
            let mut out = Vec::new();
            for (n, name) in slides {
                let body = scan(&entry(&mut zip, &name)?, "a");
                if !body.trim().is_empty() {
                    out.push(format!("Slide {n}\n{}", body.trim()));
                }
            }
            Some(out.join("\n\n"))
        }
        _ => None,
    }
}

/// The pictures inside a `.docx` (`word/media/`) or a `.pptx` (`ppt/media/`), in name order, as
/// (media type, bytes): the charts, photos and screenshots a deck's meaning often lives in. At most
/// `max` of them, each at most `max_bytes`; other files there (EMF, video) are left out.
pub fn pictures(kind: &str, bytes: &[u8], max: usize, max_bytes: usize) -> Vec<(&'static str, Vec<u8>)> {
    let dir = match kind {
        "docx" => "word/media/",
        "pptx" => "ppt/media/",
        _ => return Vec::new(),
    };
    let Ok(mut zip) = zip::ZipArchive::new(Cursor::new(bytes)) else {
        return Vec::new();
    };
    let mut names: Vec<String> = zip.file_names().filter(|n| n.starts_with(dir)).map(str::to_string).collect();
    names.sort();
    let mut out = Vec::new();
    for name in names {
        if out.len() >= max {
            break;
        }
        let mt = match name.rsplit('.').next().map(|e| e.to_ascii_lowercase()).as_deref() {
            Some("png") => "image/png",
            Some("jpg") | Some("jpeg") => "image/jpeg",
            Some("gif") => "image/gif",
            Some("webp") => "image/webp",
            _ => continue,
        };
        let Ok(mut f) = zip.by_name(&name) else { continue };
        if f.size() as usize > max_bytes || f.size() < 2048 {
            continue; // too big to send, or a bullet glyph or a logo crumb
        }
        let mut buf = Vec::new();
        if f.read_to_end(&mut buf).is_ok() {
            out.push((mt, buf));
        }
    }
    out
}

fn entry<R: Read + std::io::Seek>(zip: &mut zip::ZipArchive<R>, name: &str) -> Option<String> {
    let mut f = zip.by_name(name).ok()?;
    let mut s = String::new();
    f.read_to_string(&mut s).ok()?;
    Some(s)
}

/// The text runs of one part, `ns` being the namespace prefix of its text
/// tags (`w` for Word, `a` for slides).
fn scan(xml: &str, ns: &str) -> String {
    let (open, close, para) = (format!("<{ns}:t"), format!("</{ns}:t>"), format!("</{ns}:p>"));
    let (tab, br) = (format!("<{ns}:tab"), format!("<{ns}:br"));
    let mut out = String::new();
    let mut i = 0;
    while let Some(off) = xml[i..].find('<') {
        let at = i + off;
        let rest = &xml[at..];
        if rest.starts_with(&para) {
            out.push('\n');
        } else if rest.starts_with(&tab) && tag_ends(rest, tab.len()) {
            out.push('\t');
        } else if rest.starts_with(&br) && tag_ends(rest, br.len()) {
            out.push('\n');
        } else if rest.starts_with(&open) && tag_ends(rest, open.len()) {
            // <w:t> or <w:t xml:space="preserve">: the text runs to </w:t>
            let Some(gt) = rest.find('>') else { break };
            if rest.as_bytes()[gt - 1] != b'/' {
                let body = &rest[gt + 1..];
                let end = body.find(&close).unwrap_or(body.len());
                out.push_str(&unescape(&body[..end]));
                i = at + gt + 1 + end;
                continue;
            }
        }
        i = at + 1;
    }
    // runs of blank lines down to one
    let mut tidy = String::new();
    let mut blank = 0;
    for line in out.lines() {
        if line.trim().is_empty() {
            blank += 1;
            if blank > 1 {
                continue;
            }
        } else {
            blank = 0;
        }
        tidy.push_str(line.trim_end());
        tidy.push('\n');
    }
    tidy.trim().to_string()
}

/// The tag name ends at `n`: `<w:t>` and `<w:t xml:…>`, never `<w:tbl>`.
fn tag_ends(rest: &str, n: usize) -> bool {
    matches!(rest.as_bytes().get(n), Some(b'>') | Some(b' ') | Some(b'/'))
}

fn unescape(s: &str) -> String {
    if !s.contains('&') {
        return s.to_string();
    }
    let mut out = String::with_capacity(s.len());
    let mut rest = s;
    while let Some(amp) = rest.find('&') {
        out.push_str(&rest[..amp]);
        let tail = &rest[amp..];
        let Some(semi) = tail.find(';').filter(|&k| k <= 10) else {
            out.push('&');
            rest = &tail[1..];
            continue;
        };
        let ent = &tail[1..semi];
        let ch = match ent {
            "amp" => Some('&'),
            "lt" => Some('<'),
            "gt" => Some('>'),
            "quot" => Some('"'),
            "apos" => Some('\''),
            _ if ent.starts_with("#x") => u32::from_str_radix(&ent[2..], 16).ok().and_then(char::from_u32),
            _ if ent.starts_with('#') => ent[1..].parse().ok().and_then(char::from_u32),
            _ => None,
        };
        match ch {
            Some(c) => out.push(c),
            None => out.push_str(&tail[..=semi]),
        }
        rest = &tail[semi + 1..];
    }
    out.push_str(rest);
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    fn archive(files: &[(&str, &str)]) -> Vec<u8> {
        let mut buf = Cursor::new(Vec::new());
        {
            let mut z = zip::ZipWriter::new(&mut buf);
            let opt = zip::write::SimpleFileOptions::default();
            for (name, body) in files {
                z.start_file(*name, opt).unwrap();
                z.write_all(body.as_bytes()).unwrap();
            }
            z.finish().unwrap();
        }
        buf.into_inner()
    }

    #[test]
    fn a_word_document_reads_as_its_paragraphs() {
        let xml = r#"<w:document><w:body><w:p><w:r><w:t>Brewline &amp; the </w:t></w:r><w:r><w:t xml:space="preserve">summer launch</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>Budget:</w:t><w:tab/><w:t>&#8364;1.2m</w:t></w:r></w:p></w:tc></w:tr></w:tbl><w:p><w:r><w:t>Line one</w:t><w:br/><w:t>line two</w:t></w:r></w:p></w:body></w:document>"#;
        let t = text("docx", &archive(&[("word/document.xml", xml)])).unwrap();
        assert_eq!(t, "Brewline & the summer launch\nBudget:\t€1.2m\nLine one\nline two");
    }

    #[test]
    fn a_deck_reads_slide_by_slide_in_order() {
        let s = |t: &str| format!("<p:sld><a:p><a:r><a:t>{t}</a:t></a:r></a:p></p:sld>");
        let t = text("pptx", &archive(&[("ppt/slides/slide10.xml", &s("Ten")), ("ppt/slides/slide2.xml", &s("Two")),
                                       ("ppt/slides/slide1.xml", &s("One"))])).unwrap();
        assert_eq!(t, "Slide 1\nOne\n\nSlide 2\nTwo\n\nSlide 10\nTen");
    }

    #[test]
    fn what_is_not_an_office_archive_has_no_text() {
        assert!(text("docx", b"not a zip").is_none());
        assert!(text("docx", &archive(&[("other.xml", "<x/>")])).is_none());
    }
}

#[cfg(test)]
mod real_files {
    /// A file made by Word-compatible tools: `NAPKIN_OFFICE_PROBE=<path.docx|path.pptx> cargo test -- --ignored`.
    #[test]
    #[ignore]
    fn reads_a_real_file() {
        let p = std::env::var("NAPKIN_OFFICE_PROBE").unwrap();
        let kind = p.rsplit('.').next().unwrap().to_string();
        println!("{}", super::text(&kind, &std::fs::read(&p).unwrap()).unwrap());
    }
}
