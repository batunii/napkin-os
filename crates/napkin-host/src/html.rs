// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! Pure HTML transforms applied to a `.clan`'s human view.
//!
//! Nothing here reads a file or knows what a shell is: given HTML (plus the
//! data layer, a stylesheet, a patch set) these produce HTML. They are the
//! legacy rendering pipeline (`{{binding}}` resolution, `data-adf-id`
//! injection, patch application) plus the two injectors every render uses.

use serde::Deserialize;

use crate::log::log;

pub fn inject_styles(html: &str, css: &str) -> String {
    if css.is_empty() {
        return html.to_string();
    }
    let style_tag = format!("<style>{}</style>", css);
    let lower = html.to_lowercase();
    if lower.contains("</head>") {
        html.replacen("</head>", &format!("{}</head>", style_tag), 1)
    } else if lower.contains("<body") {
        // Fragment with no <head>: prepend style block
        format!("{}\n{}", style_tag, html)
    } else {
        format!("{}\n{}", style_tag, html)
    }
}

pub fn inject_clan_data(html: &str, context_json: &str) -> String {
    // context_json is the full { data, manifest, assets } object.
    let script_tag = format!("<script>window.__CLAN__ = {};</script>", context_json);
    let lower = html.to_lowercase();
    if lower.contains("</head>") {
        html.replacen("</head>", &format!("{}</head>", script_tag), 1)
    } else if lower.contains("<body") {
        html.replacen("<body", &format!("{}<body", script_tag), 1)
    } else {
        format!("{}\n{}", script_tag, html)
    }
}

pub fn resolve_bindings(html: &str, data: &serde_yaml::Value) -> String {
    // Byte-indexed scan — no Vec<char> allocation over the document.
    let mut output = String::with_capacity(html.len());
    let mut rest = html;
    while let Some(start) = rest.find("{{") {
        output.push_str(&rest[..start]);
        let after = &rest[start + 2..];
        match after.find("}}") {
            Some(end) => {
                let key = after[..end].trim();
                output.push_str(&resolve_key(key, data));
                rest = &after[end + 2..];
            }
            None => {
                // Unterminated braces: keep the remainder verbatim.
                output.push_str(&rest[start..]);
                return output;
            }
        }
    }
    output.push_str(rest);
    output
}

pub fn resolve_key(key: &str, data: &serde_yaml::Value) -> String {
    let mut current = data;
    for part in key.split('.') {
        current = match current {
            serde_yaml::Value::Mapping(m) => {
                match m.get(serde_yaml::Value::String(part.to_string())) {
                    Some(v) => v,
                    None => return format!("{{{{{key}}}}}"),
                }
            }
            serde_yaml::Value::Sequence(s) => {
                match part.parse::<usize>().ok().and_then(|i| s.get(i)) {
                    Some(v) => v,
                    None => return format!("{{{{{key}}}}}"),
                }
            }
            _ => return format!("{{{{{key}}}}}"),
        };
    }
    match current {
        serde_yaml::Value::String(s) => s.clone(),
        serde_yaml::Value::Number(n) => n.to_string(),
        serde_yaml::Value::Bool(b) => b.to_string(),
        _ => String::new(),
    }
}

pub fn apply_patches(html: &str, patch_yaml: &str) -> String {
    #[derive(Deserialize)]
    struct Patches {
        #[serde(default)]
        patches: Vec<Patch>,
    }
    #[derive(Deserialize)]
    struct Patch {
        id: String,
        content: String,
    }
    let Ok(ps) = serde_yaml::from_str::<Patches>(patch_yaml) else {
        return html.to_string();
    };
    let mut result = html.to_string();

    for p in ps.patches {
        let marker = format!("data-adf-id=\"{}\"", p.id);
        let Some(attr_pos) = result.find(&marker) else {
            log(&format!("apply_patches: id={:?} NOT FOUND in HTML", p.id));
            continue;
        };

        // Walk back to the opening `<` of the tag that holds this attribute.
        let tag_open = result[..attr_pos].rfind('<').unwrap_or(0);

        // Extract the tag name (e.g. "h1", "p", "div").
        let tag_name: String = result[tag_open + 1..]
            .chars()
            .take_while(|c| c.is_ascii_alphanumeric())
            .collect::<String>()
            .to_lowercase();

        // Find the `>` that closes the opening tag.
        let Some(gt_rel) = result[attr_pos..].find('>') else {
            log(&format!(
                "apply_patches: id={:?} no closing > for opening tag",
                p.id
            ));
            continue;
        };
        let content_start = attr_pos + gt_rel + 1;

        // Find the matching closing tag, respecting nesting.
        let close_pos = find_closing_tag(&result, content_start, &tag_name);
        let Some(close_pos) = close_pos else {
            log(&format!(
                "apply_patches: id={:?} tag={tag_name:?} no matching closing tag",
                p.id
            ));
            continue;
        };

        log(&format!(
            "apply_patches: id={:?} tag={tag_name:?} content_start={content_start} close_pos={close_pos} \
             old={:?}… new={:?}…",
            p.id,
            &result[content_start..close_pos.min(content_start + 60)],
            &p.content[..p.content.len().min(60)],
        ));

        result = format!(
            "{}{}{}",
            &result[..content_start],
            p.content,
            &result[close_pos..]
        );
    }
    result
}

/// Find the absolute position of the matching closing `</tag>` in `html`,
/// starting the search at `from`. Tracks nested same-tag pairs so a `<div>` that
/// contains another `<div>` resolves to its own `</div>`, not the inner one.
pub fn find_closing_tag(html: &str, from: usize, tag: &str) -> Option<usize> {
    let lower = html.to_lowercase();
    let open_pat = format!("<{}", tag);
    let close_pat = format!("</{}", tag);
    let mut depth: i32 = 0;
    let mut pos = from;

    loop {
        let slice = &lower[pos..];

        // Find the next opening tag of the same type (could be a nested child).
        let next_open = slice.find(open_pat.as_str()).and_then(|rel| {
            let after = pos + rel + open_pat.len();
            // Confirm it is really this tag and not a prefix (e.g. <td vs <thead).
            matches!(
                lower.as_bytes().get(after),
                Some(b' ' | b'\t' | b'\n' | b'\r' | b'>') | None
            )
            .then_some(pos + rel)
        });

        // Find the next closing tag — require a word boundary after the name
        // so "</td" doesn't match "</tbody>" or "</th" match "</thead>".
        let next_close = {
            let mut found = None;
            let mut search_from = 0;
            while let Some(rel) = slice[search_from..].find(close_pat.as_str()) {
                let abs = pos + search_from + rel;
                let after = abs + close_pat.len();
                if matches!(
                    lower.as_bytes().get(after),
                    Some(b'>' | b' ' | b'\t' | b'\n' | b'\r') | None
                ) {
                    found = Some(abs);
                    break;
                }
                search_from += rel + 1;
            }
            found
        };

        match (next_open, next_close) {
            (Some(o), Some(c)) if o < c => {
                // Nested open before close: go deeper.
                depth += 1;
                pos = o + open_pat.len();
            }
            (_, Some(c)) => {
                if depth == 0 {
                    return Some(c); // This is the matching close.
                }
                depth -= 1;
                pos = c + close_pat.len();
            }
            _ => return None,
        }
    }
}

/// Inject `data-adf-id` on editable block elements that the agent didn't annotate.
/// IDs are stable: same HTML always produces the same IDs (tag-type + sequential index).
///
/// Behavior-equivalent to the historical implementation that rebuilt the whole
/// string once per tag type (h1..h6, p, li, td, th, in that order). The passes
/// are simulated over the ORIGINAL string as injection bookkeeping, so the
/// output is built exactly once. Semantics preserved from the old code:
/// - tag names match case-sensitively (`<P>` never gets an id); `<script` /
///   `</script>` match case-insensitively;
/// - any `<script` substring (even inside a quoted attribute value) opens a
///   script region until the next `</script>`; tags inside it are skipped;
/// - tag-like text inside a quoted attribute shares its host tag's closing
///   `>`; the id goes to the first tag type in EDITABLE pass order whose span
///   is not yet annotated (later passes then see `data-adf-id` and skip);
/// - an injection placed before the `>` of a `</script>` token splits that
///   token, so later passes treat the script as unclosed.
///
/// The one intentional difference: ASCII lowercasing. The old `to_lowercase()`
/// changed byte lengths for some Unicode (e.g. U+212A KELVIN SIGN) and could
/// panic when indexing the original string with shifted offsets.
pub fn auto_inject_adf_ids(html: &str) -> String {
    const EDITABLE: &[&str] = &["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "td", "th"];
    const CLOSE: &str = "</script>";
    // ASCII lowercasing keeps byte offsets aligned with the original string.
    let lower = html.to_ascii_lowercase();
    let bytes = html.as_bytes();

    // `<script` / `</script>` token positions, shared by all passes. Opens can
    // never be created or destroyed by injections (the injected attribute
    // contains no '<' or '>'); closes can be destroyed (see below), so each
    // carries the pass index it was destroyed in, if any.
    let opens = token_positions(&lower, "<script");
    let mut closes: Vec<(usize, Option<usize>)> = token_positions(&lower, CLOSE)
        .into_iter()
        .map(|c| (c, None))
        .collect();

    // '>' position -> (tag index, per-tag count). At most one injection per '>'.
    let mut injections: std::collections::BTreeMap<usize, (usize, usize)> =
        std::collections::BTreeMap::new();

    for (t, tag) in EDITABLE.iter().enumerate() {
        let pat = format!("<{}", tag);
        let mut count = 0usize;
        let mut pos = 0usize;
        while pos < html.len() {
            // Case-sensitive match on the original string, like the old code.
            let Some(rel) = html[pos..].find(pat.as_str()) else {
                break;
            };
            let start = pos + rel;
            let after_name = start + pat.len();

            // Inside an unclosed <script? (A close destroyed by an EARLIER
            // pass's injection no longer counts for this pass.)
            let in_script = opens
                .partition_point(|&o| o < start)
                .checked_sub(1)
                .is_some_and(|qi| {
                    let q = opens[qi];
                    !closes.iter().any(|&(c, destroyed)| {
                        c >= q && c + CLOSE.len() <= start && destroyed.map_or(true, |d| d >= t)
                    })
                });
            // Tag boundary: not part of a longer name (e.g. <pre> vs <p>).
            let next = bytes.get(after_name).copied().unwrap_or(0);
            if in_script || !matches!(next, b' ' | b'\t' | b'\n' | b'\r' | b'>') {
                pos = after_name;
                continue;
            }

            // The `>` ending this opening tag (may sit inside a quoted
            // attribute value; the old code was not quote-aware either).
            let Some(rel_end) = html[start..].find('>') else {
                break;
            };
            let gt = start + rel_end;
            let annotated =
                html[start..=gt].contains("data-adf-id") || injections.contains_key(&gt);
            if !annotated && bytes[gt - 1] != b'/' {
                injections.insert(gt, (t, count));
                count += 1;
                // Injecting right before the '>' of a "</script>" splits the
                // token; later passes must not see it as a script close.
                if let Some(close) = closes.iter_mut().find(|(c, _)| c + CLOSE.len() == gt + 1) {
                    close.1.get_or_insert(t);
                }
            }
            pos = gt + 1;
        }
    }

    let mut out = String::with_capacity(html.len() + 32 * injections.len());
    let mut prev = 0;
    for (&gt, &(t, n)) in &injections {
        out.push_str(&html[prev..gt]);
        out.push_str(&format!(" data-adf-id=\"auto-{}-{}\"", EDITABLE[t], n));
        prev = gt;
    }
    out.push_str(&html[prev..]);
    out
}

/// Byte offsets of every occurrence of `token` in `haystack`.
pub fn token_positions(haystack: &str, token: &str) -> Vec<usize> {
    let mut found = Vec::new();
    let mut from = 0;
    while let Some(rel) = haystack[from..].find(token) {
        found.push(from + rel);
        from += rel + 1;
    }
    found
}

pub fn strip_scripts(html: &str) -> String {
    let lower = html.to_lowercase();
    let mut result = String::with_capacity(html.len());
    let mut pos = 0;
    loop {
        match lower[pos..].find("<script") {
            None => {
                result.push_str(&html[pos..]);
                break;
            }
            Some(rel) => {
                result.push_str(&html[pos..pos + rel]);
                let after_open = pos + rel;
                match lower[after_open..].find("</script>") {
                    None => break,
                    Some(end_rel) => {
                        pos = after_open + end_rel + "</script>".len();
                    }
                }
            }
        }
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;

    // --- #19: resolve_bindings ---

    fn yaml(src: &str) -> serde_yaml::Value {
        serde_yaml::from_str(src).unwrap()
    }

    #[test]
    fn resolve_bindings_substitutes_keys() {
        let data = yaml("vendor: Acme\nmeta:\n  total: 42\nitems:\n  - first\n  - second\n");
        assert_eq!(
            resolve_bindings(
                "<p>{{vendor}} owes {{meta.total}} for {{items.1}}</p>",
                &data
            ),
            "<p>Acme owes 42 for second</p>"
        );
    }

    #[test]
    fn resolve_bindings_keeps_unknown_keys_verbatim() {
        let data = yaml("vendor: Acme\n");
        assert_eq!(
            resolve_bindings("{{nope}} {{vendor}}", &data),
            "{{nope}} Acme"
        );
    }

    #[test]
    fn resolve_bindings_preserves_unterminated_braces() {
        let data = yaml("vendor: Acme\n");
        assert_eq!(
            resolve_bindings("a {{vendor}} b {{open", &data),
            "a Acme b {{open"
        );
    }

    #[test]
    fn resolve_bindings_handles_multibyte_text() {
        let data = yaml("name: Zoë\n");
        assert_eq!(
            resolve_bindings("héllo «{{ name }}» — ✓", &data),
            "héllo «Zoë» — ✓"
        );
    }

    // --- #20: auto_inject_adf_ids ---

    #[test]
    fn auto_inject_ids_all_tag_types_in_one_pass() {
        let html = "<h1>A</h1><p>b</p><p>c</p><ul><li>d</li></ul><table><tr><td>e</td><th>f</th></tr></table>";
        let out = auto_inject_adf_ids(html);
        assert!(
            out.contains(r#"<h1 data-adf-id="auto-h1-0">A</h1>"#),
            "{out}"
        );
        assert!(out.contains(r#"<p data-adf-id="auto-p-0">b</p>"#), "{out}");
        assert!(out.contains(r#"<p data-adf-id="auto-p-1">c</p>"#), "{out}");
        assert!(
            out.contains(r#"<li data-adf-id="auto-li-0">d</li>"#),
            "{out}"
        );
        assert!(
            out.contains(r#"<td data-adf-id="auto-td-0">e</td>"#),
            "{out}"
        );
        assert!(
            out.contains(r#"<th data-adf-id="auto-th-0">f</th>"#),
            "{out}"
        );
    }

    #[test]
    fn auto_inject_ids_respects_existing_ids_and_boundaries() {
        let html = r#"<p data-adf-id="mine">keep</p><pre>not a p</pre><p class="x">tag</p>"#;
        let out = auto_inject_adf_ids(html);
        assert!(out.contains(r#"<p data-adf-id="mine">keep</p>"#), "{out}");
        assert!(
            out.contains("<pre>not a p</pre>"),
            "<pre> must not be treated as <p>: {out}"
        );
        assert!(
            out.contains(r#"<p class="x" data-adf-id="auto-p-0">tag</p>"#),
            "{out}"
        );
    }

    #[test]
    fn auto_inject_ids_skips_script_blocks() {
        let html = "<script>const t = `<p>${row}</p>`;</script><p>real</p>";
        let out = auto_inject_adf_ids(html);
        assert!(
            out.contains("`<p>${row}</p>`"),
            "script content must be untouched: {out}"
        );
        assert!(
            out.contains(r#"<p data-adf-id="auto-p-0">real</p>"#),
            "{out}"
        );
    }

    #[test]
    fn auto_inject_ids_is_stable_and_idempotent() {
        let html = "<p>a</p><p>b</p>";
        let once = auto_inject_adf_ids(html);
        assert_eq!(
            once,
            auto_inject_adf_ids(&once),
            "second pass must change nothing"
        );
        assert_eq!(once, auto_inject_adf_ids(html), "same input, same ids");
    }

    // --- #20 equivalence oracle: the historical multi-pass implementation ---
    //
    // Verbatim copy of the pre-rewrite code (one full pass per editable tag,
    // in EDITABLE order), kept as a behavioral oracle. The rewrite must be
    // byte-for-byte equivalent on every input the old code handled without
    // panicking.
    //
    // KNOWN ACCEPTABLE DIVERGENCE (the only intentional one): the oracle uses
    // `str::to_lowercase`, which changes byte length for some Unicode (e.g.
    // U+212A KELVIN SIGN 'K' lowercases to 1-byte 'k', U+0130 'İ' lowercases
    // to two chars) and then indexes the ORIGINAL string with offsets derived
    // from the shorter lowercase copy — it can panic or split char boundaries.
    // The production code uses `to_ascii_lowercase`, which preserves byte
    // offsets. Equivalence inputs below therefore avoid characters whose
    // lowercase mapping changes byte length ('K', 'İ', 'ſ', ...); see
    // `old_impl_panics_on_length_changing_unicode_new_does_not`.
    // Verbatim history: lints that would rewrite this code are suppressed,
    // because its value is being byte-for-byte what shipped, not being idiomatic.
    #[allow(clippy::unnecessary_map_or)]
    mod oracle_old_impl {
        pub fn old_auto_inject_adf_ids(html: &str) -> String {
            const EDITABLE: &[&str] = &["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "td", "th"];
            let mut result = html.to_string();
            for tag in EDITABLE {
                result = old_inject_ids_for_tag(&result, tag);
            }
            result
        }
        fn old_inject_ids_for_tag(html: &str, tag: &str) -> String {
            let mut out = String::with_capacity(html.len() + 64);
            let mut pos = 0;
            let mut count = 0usize;
            let open = format!("<{}", tag);
            let lower = html.to_lowercase();
            while pos < html.len() {
                if lower[pos..].starts_with("<script") {
                    let end = lower[pos..]
                        .find("</script>")
                        .map(|r| pos + r + "</script>".len())
                        .unwrap_or(html.len());
                    out.push_str(&html[pos..end]);
                    pos = end;
                    continue;
                }
                let Some(rel) = html[pos..].find(open.as_str()) else {
                    out.push_str(&html[pos..]);
                    break;
                };
                let tag_start = pos + rel;
                let after_name = tag_start + open.len();
                if lower[..tag_start]
                    .rfind("<script")
                    .map_or(false, |s| lower[s..tag_start].find("</script>").is_none())
                {
                    out.push_str(&html[pos..after_name]);
                    pos = after_name;
                    continue;
                }
                let next = html.as_bytes().get(after_name).copied().unwrap_or(0);
                if !matches!(next, b' ' | b'\t' | b'\n' | b'\r' | b'>') {
                    out.push_str(&html[pos..after_name]);
                    pos = after_name;
                    continue;
                }
                let Some(rel_end) = html[tag_start..].find('>') else {
                    out.push_str(&html[pos..]);
                    break;
                };
                let tag_end = tag_start + rel_end;
                let tag_src = &html[tag_start..=tag_end];
                out.push_str(&html[pos..tag_end]);
                if !tag_src.contains("data-adf-id") && !tag_src.ends_with("/>") {
                    out.push_str(&format!(" data-adf-id=\"auto-{}-{}\"", tag, count));
                    count += 1;
                }
                out.push('>');
                pos = tag_end + 1;
            }
            out
        }
    }
    use oracle_old_impl::old_auto_inject_adf_ids;

    #[track_caller]
    fn assert_matches_old(html: &str) {
        assert_eq!(
            auto_inject_adf_ids(html),
            old_auto_inject_adf_ids(html),
            "new impl diverges from old multi-pass impl on input: {html:?}"
        );
    }

    #[test]
    fn auto_inject_ids_matches_old_on_adversarial_html() {
        let cases: &[&str] = &[
            // Case sensitivity (old matched literal lowercase "<tag" only).
            "<P>upper</P>",
            "<Li>mixed</Li>",
            "<H1>h</H1><h1>h</h1>",
            "<TD>x</TD><td>y</td>",
            // Whitespace inside the opening tag.
            "<p\nclass='a'>x</p>",
            "<p\t>tab</p>",
            "<p\r\n>crlf</p>",
            "<li\n\n data-x>y</li>",
            // Attributes containing '>' inside quotes (neither impl is
            // quote-aware; equivalence is what matters).
            "<p class=\"a>b\">x</p>",
            "<h2 a='>'><p>z</p>",
            // Self-closing variants.
            "<td/>",
            "<td />",
            "<p/><p />and<p>real</p>",
            // Tag at/near EOF, unclosed tags.
            "<p>at-eof",
            "<p>unclosed <li>nested",
            "<p",
            "x<p",
            "text<p ",
            "<p attr",
            "a<p>b</p>c<p",
            // Scripts: nested editable tags, unclosed scripts, multiple blocks.
            "<script>var a = '<p>';</script><p>x</p>",
            "<script>nested <p> and unclosed",
            "<script><p>",
            "<script>",
            "</script><p>x</p>",
            "<script>a</script><script>b</script><p>c</p>",
            "<SCRIPT>const x='<p>';</SCRIPT><p>y</p>",
            "<script src=\"x\"></script><li>z</li>",
            "before<script>mid<li>tag</script><li>after</li>",
            // Prefix lookalikes: <pre> vs <p>, <thead> vs <th>, <h1x> vs <h1>.
            "<pre>not p</pre><p>p</p>",
            "<thead><th>h</th></thead>",
            "<h1>a</h1><h1x>b</h1x>",
            "<h10>not h1</h10>",
            "<lite><li>x</li></lite>",
            // Existing data-adf-id, both quote styles, and lookalike text.
            "<p data-adf-id=\"x\">a</p><p>b</p>",
            "<p data-adf-id='y'>a</p>",
            "<p title=\"data-adf-idx\">substring-counts</p>",
            // Comments — NEITHER impl is comment-aware; tags inside comments
            // get ids in both, equivalence (not comment handling) is asserted.
            "<!-- <p>comment</p> --><p>real</p>",
            "<!--<li>--><li>x</li>",
            // Empty / plain text / stray brackets.
            "",
            "plain text without tags",
            "< p>not a tag</ p>",
            "<><p></p>",
            "<p<p>>",
            ">>>///<<<",
            // Multibyte UTF-8 around tags (length-stable under lowercasing).
            "é<p>déjà</p>✓",
            "É<P>x</P>✓",
            "«<li>é</li>»<h3>✓</h3>",
            // Tag-like text nested inside a quoted attribute (shares the
            // closing '>'): old gave the id to the first tag type in EDITABLE
            // order, not the leftmost tag start.
            "<td title=\"a<p>b\">",
            "<li title=\"a<h1>b\">",
            "<p title=\"x<li>y\">",
            "<p data-adf-id=\"x\" a=\"b<li>c\">",
            "<li data-adf-id=\"x\" a=\"<h1 data-adf-id='y'<td>z\">",
            // `<script` substring opening inside an attribute value.
            "<p title=\"<script\"><li>x</li>",
            "<p a=\"<script b=\"</script>\"><li>x",
            "<td a=\"</script>\"><li>x</li>",
        ];
        for case in cases {
            assert_matches_old(case);
        }
    }

    // Regression: REAL divergences found while comparing the single-pass
    // rewrite (#20) against the multi-pass original. Each was a behavior
    // change on inputs the old code handled fine; the implementation was
    // fixed to match the old behavior exactly.

    #[test]
    fn auto_inject_ids_matches_old_case_sensitive_tag_names() {
        // Old searched the ORIGINAL string for literal "<p"/"<li"/... — so
        // uppercase or mixed-case tags never received ids. The rewrite
        // matched tag names case-insensitively and injected into <P>.
        assert_eq!(auto_inject_adf_ids("<P>x</P>"), "<P>x</P>");
        assert_matches_old("<P>x</P>");
        assert_matches_old("<Li>x</Li>");
    }

    #[test]
    fn auto_inject_ids_matches_old_nested_tag_priority_in_attributes() {
        // When tag-like text in a quoted attribute shares the closing '>'
        // with its host tag, the old per-tag pass order (h1..h6, p, li, td,
        // th) decided which tag name the id used: <p> inside a <td> attribute
        // won because the p pass ran before the td pass (which then saw
        // data-adf-id in its span and skipped). The rewrite gave it to the
        // leftmost (host) tag instead.
        assert_eq!(
            auto_inject_adf_ids(r#"<td title="a<p>b">"#),
            r#"<td title="a<p data-adf-id="auto-p-0">b">"#
        );
        assert_matches_old(r#"<td title="a<p>b">"#);
        assert_matches_old(r#"<li title="a<h1>b">"#);
        // Host already annotated: old still injected into the nested tag.
        assert_matches_old(r#"<p data-adf-id="x" a="b<li>c">"#);
    }

    #[test]
    fn auto_inject_ids_matches_old_script_open_inside_attribute() {
        // Old treated ANY "<script" substring — even inside a quoted
        // attribute value — as opening a script region: every editable tag
        // until the next "</script>" was skipped. The rewrite only recognized
        // "<script" at positions it scanned outside consumed tag spans, so it
        // wrongly injected into the following <li>.
        assert_eq!(
            auto_inject_adf_ids(r#"<p title="<script"><li>x</li>"#),
            r#"<p title="<script" data-adf-id="auto-p-0"><li>x</li>"#
        );
        assert_matches_old(r#"<p title="<script"><li>x</li>"#);
    }

    #[test]
    fn auto_inject_ids_matches_old_injection_splitting_script_close() {
        // Cross-pass mutation quirk: when the first '>' after a tag start is
        // the '>' of a "</script>" token, the old impl's injection split that
        // token (`</script data-adf-id=...>`), so LATER passes saw the script
        // as unclosed and skipped subsequent tags. Equivalence requires
        // reproducing that.
        assert_matches_old(r#"<p a="<script b="</script>"><li>x"#);
    }

    #[test]
    fn old_impl_panics_on_length_changing_unicode_new_does_not() {
        // KNOWN ACCEPTABLE DIVERGENCE (the single intentional one): U+212A
        // KELVIN SIGN is 3 bytes but `to_lowercase` maps it to 1-byte 'k',
        // so the old impl's byte offsets into its lowercase copy drift and
        // it panics slicing out of range. The new impl uses
        // `to_ascii_lowercase` (offset-stable) and handles it sanely. Such
        // inputs are excluded from the equivalence corpus.
        let kelvin = "\u{212A}\u{212A}<p>x";
        assert_eq!(
            auto_inject_adf_ids(kelvin),
            "\u{212A}\u{212A}<p data-adf-id=\"auto-p-0\">x"
        );
        let old = std::panic::catch_unwind(|| old_auto_inject_adf_ids(kelvin));
        assert!(old.is_err(), "old impl is expected to panic on 'KK<p>x'");
    }

    #[test]
    fn auto_inject_ids_matches_old_on_random_tag_soup() {
        // Deterministic pseudo-random fuzz: seeded LCG (no extra deps) glues
        // adversarial fragments into a few hundred HTML soups and asserts
        // byte equality with the multi-pass oracle on each. Fragments avoid
        // characters whose Unicode lowercase changes byte length (see oracle
        // module comment) — that is the one known acceptable divergence.
        const FRAGMENTS: &[&str] = &[
            "<p>",
            "</p>",
            "<P>",
            "<p",
            "p>",
            "<p ",
            "<p\n",
            "<p\t>",
            "<p/>",
            "<p />",
            "<li>",
            "<Li>",
            "<li ",
            "</li>",
            "<LI>",
            "<li\n>",
            "<h1>",
            "<h1",
            "<h1x>",
            "<H1>",
            "<h2 class=\"a>b\">",
            "<h6\n class='c'>",
            "<h3>",
            "</h3>",
            "<h10>",
            "<td>",
            "<td/>",
            "<td />",
            "<th>",
            "<thead>",
            "<TD>",
            "</td>",
            "<pre>",
            "</pre>",
            "<script>",
            "</script>",
            "<script src=\"x\">",
            "<script",
            "</script",
            "<SCRIPT>",
            "</SCRIPT>",
            "<script b=\"",
            "data-adf-id=\"x\"",
            " data-adf-id='y'",
            "data-adf-idx",
            "<!-- <p> -->",
            "<!--",
            "-->",
            "text",
            " ",
            "\n",
            "\t",
            "\"",
            "'",
            ">",
            "<",
            "/>",
            "=",
            "/",
            "é",
            "✓",
            "É",
            "«»",
            "title=\"a<p>b\"",
            "a=\"<script\"",
            "a=\"</script>\"",
            "b=\"",
            "<p a=\"",
            "<td title=\"a<p>b\">",
            "<li title=\"<h1>\">",
        ];

        let mut state: u64 = 0x5DEECE66D;
        let mut next = move || {
            state = state
                .wrapping_mul(6364136223846793005)
                .wrapping_add(1442695040888963407);
            state >> 33
        };

        for round in 0..300 {
            let count = 1 + (next() as usize) % 32;
            let mut soup = String::new();
            for _ in 0..count {
                soup.push_str(FRAGMENTS[(next() as usize) % FRAGMENTS.len()]);
            }
            let new = auto_inject_adf_ids(&soup);
            let old = old_auto_inject_adf_ids(&soup);
            assert_eq!(
                new, old,
                "fuzz divergence (round {round}) on soup: {soup:?}"
            );
        }
    }
}
