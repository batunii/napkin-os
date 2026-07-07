// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! Standalone document export — the OS-layer foundation for Export Studio.
//!
//! Export composes a *self-contained* HTML document from a `.clan`'s structured
//! members: no running app, no network, no live host bridge. It is the shared
//! counterpart to the per-template `buildExportHTML()` that authored apps ship
//! in JS today — the template declares *what* to present (a binding-based
//! export view), and the SDK owns the cross-cutting composition every studio
//! needs identically: `{{binding}}` resolution against `shared/data.yaml`,
//! asset inlining as data URIs, optional brand chrome, and an optional
//! attributed provenance appendix drawn from the decision chain.
//!
//! The output is deterministic (it stamps nothing that isn't already in the
//! file) and static (all `<script>` is stripped — the exported file must render
//! without the viewer). HTML→PDF conversion is deliberately *not* here: it is
//! an environment concern that shells out to a headless browser, so it lives in
//! the binary layers (the `clan` CLI and the desktop host), each of which calls
//! [`export_html`] for the document and converts the result.
//!
//! Source-view precedence:
//! 1. `human/export.html` — a print-oriented view authored for export.
//! 2. `human/index.html`  — the on-screen view, if no export view exists.
//! 3. the default-theme [`crate::render`] output, for agent-only files.

use base64::Engine;
use lol_html::{element, rewrite_str, RewriteStrSettings};

use crate::container::ClanFile;
use crate::decision::DecisionChain;
use crate::error::{Error, Result};

/// Knobs for [`export_html`]. Defaults produce a branded document without the
/// provenance appendix (the common "share this with a client" case).
#[derive(Debug, Clone)]
pub struct ExportOptions {
    /// Prepend the Napkin lockup and append a "powered by CLAN" footer.
    pub brand: bool,
    /// Append an attributed provenance appendix built from the decision chain.
    pub provenance: bool,
}

impl Default for ExportOptions {
    fn default() -> Self {
        Self {
            brand: true,
            provenance: false,
        }
    }
}

/// Compose a standalone HTML document from a `.clan` file's members.
///
/// The returned string is a complete document (doctype + head + body) with no
/// external dependencies: bindings resolved, assets inlined, scripts removed.
pub fn export_html(clan: &ClanFile, opts: &ExportOptions) -> Result<String> {
    let title = clan.manifest().title.clone();

    // 1. Pick the source view, honouring the export-view precedence.
    let source = if clan.has_entry("human/export.html") {
        clan.read_entry_string("human/export.html")?
    } else if clan.has_entry("human/index.html") {
        clan.read_entry_string("human/index.html")?
    } else {
        // Agent-only file: materialise the default theme, then export that.
        let rendered = ClanFile::from_bytes(crate::render::render(clan)?)?;
        rendered.read_entry_string("human/index.html")?
    };

    // 2. Resolve {{dotted.path}} bindings against shared/data.yaml. Done on the
    //    raw string so bindings in text nodes and attribute values both resolve.
    let data: serde_yaml::Value = clan
        .read_entry("shared/data.yaml")
        .ok()
        .and_then(|b| serde_yaml::from_slice(&b).ok())
        .unwrap_or(serde_yaml::Value::Null);
    let bound = resolve_bindings(&source, &data);

    // 3. Ensure we have a full document to work with (default-theme and authored
    //    export views already are; a bare fragment gets a minimal scaffold).
    let doc = ensure_document(&bound, &title);

    // 4. Single rewriting pass: strip scripts, inline assets, inject chrome.
    compose(&doc, clan, opts)
}

/// Replace every `{{path}}` / `{{a.b.c}}` token with the escaped scalar at that
/// dotted path in `data`. Missing paths and non-scalar values resolve to the
/// empty string (a composite field renders through its own markup, not a
/// binding). Unrecognised `{{...}}` shapes are left untouched.
fn resolve_bindings(html: &str, data: &serde_yaml::Value) -> String {
    let mut out = String::with_capacity(html.len());
    let mut rest = html;
    while let Some(pos) = rest.find("{{") {
        out.push_str(&rest[..pos]);
        let after = &rest[pos + 2..];
        match after.find("}}") {
            Some(close) if is_binding_path(after[..close].trim()) => {
                out.push_str(&escape(&scalar_at(data, after[..close].trim())));
                rest = &after[close + 2..];
            }
            // A `{{` that isn't a well-formed binding (prose, script, unbalanced):
            // emit it literally and keep scanning past it, never inside it.
            _ => {
                out.push_str("{{");
                rest = after;
            }
        }
    }
    out.push_str(rest);
    out
}

/// A binding path is a non-empty dotted run of identifier characters — the same
/// shape `render` emits (`{{key}}`) and templates author (`{{theme.accent}}`).
/// Rejecting anything else keeps stray `{{ ... }}` in prose or scripts intact.
fn is_binding_path(s: &str) -> bool {
    !s.is_empty()
        && s.chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-' | '$'))
}

/// Look up a dotted path and render it as a display scalar. Objects/sequences
/// return empty (they are presented by the template's own markup, not inlined).
fn scalar_at(data: &serde_yaml::Value, path: &str) -> String {
    let mut cur = data;
    for part in path.split('.') {
        match cur.as_mapping().and_then(|m| m.get(part)) {
            Some(v) => cur = v,
            None => return String::new(),
        }
    }
    match cur {
        serde_yaml::Value::String(s) => s.clone(),
        serde_yaml::Value::Bool(b) => b.to_string(),
        serde_yaml::Value::Number(n) => n.to_string(),
        _ => String::new(),
    }
}

/// Wrap a bare fragment in a minimal document. Full documents pass through.
fn ensure_document(html: &str, title: &str) -> String {
    let head = html.to_ascii_lowercase();
    if head.contains("<!doctype") || head.contains("<html") {
        return html.to_string();
    }
    format!(
        "<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n\
         <title>{}</title>\n</head>\n<body>\n{html}\n</body>\n</html>\n",
        escape(title)
    )
}

/// The one HTML-rewriting pass: remove scripts (static output), inline asset
/// references as data URIs, and inject brand chrome + provenance into `<body>`.
fn compose(doc: &str, clan: &ClanFile, opts: &ExportOptions) -> Result<String> {
    let brand_style = brand_style_block();
    let header = if opts.brand { brand_header(clan) } else { String::new() };

    let mut tail = String::new();
    if opts.provenance {
        tail.push_str(&provenance_appendix(clan));
    }
    if opts.brand {
        tail.push_str(&brand_footer(clan));
    }
    let inject_style = opts.brand || opts.provenance;

    let result = rewrite_str(
        doc,
        RewriteStrSettings {
            element_content_handlers: vec![
                // Static export: the viewer's bridges and the app's own JS must
                // not travel into a shared file.
                element!("script", |el| {
                    el.remove();
                    Ok(())
                }),
                // Inline every asset reference we can resolve from the ZIP.
                element!("img[src], source[src]", |el| {
                    inline_attr(el, "src", clan);
                    Ok(())
                }),
                element!("link[href], a[href]", |el| {
                    inline_attr(el, "href", clan);
                    Ok(())
                }),
                // Scoped brand styles go in <head> so the template's own CSS is
                // never overridden.
                element!("head", move |el| {
                    if inject_style {
                        el.append(&brand_style, lol_html::html_content::ContentType::Html);
                    }
                    Ok(())
                }),
                element!("body", move |el| {
                    if !header.is_empty() {
                        el.prepend(&header, lol_html::html_content::ContentType::Html);
                    }
                    if !tail.is_empty() {
                        el.append(&tail, lol_html::html_content::ContentType::Html);
                    }
                    Ok(())
                }),
            ],
            ..RewriteStrSettings::default()
        },
    )
    .map_err(|e| Error::OutputRejected(format!("export: HTML rewrite failed: {e}")))?;

    Ok(result)
}

/// Rewrite one asset-bearing attribute to a `data:` URI if it points at an
/// asset held inside this `.clan`. Anything else (external URLs, unresolved
/// names) is left untouched so the export degrades gracefully.
fn inline_attr(el: &mut lol_html::html_content::Element, attr: &str, clan: &ClanFile) {
    let Some(val) = el.get_attribute(attr) else {
        return;
    };
    let Some(name) = asset_name(&val) else {
        return;
    };
    for candidate in [format!("human/assets/{name}"), format!("assets/{name}")] {
        if let Ok(bytes) = clan.read_entry(&candidate) {
            let uri = format!(
                "data:{};base64,{}",
                content_type_for(&name),
                base64::engine::general_purpose::STANDARD.encode(&bytes)
            );
            let _ = el.set_attribute(attr, &uri);
            return;
        }
    }
}

/// Extract the asset name from a reference the host would serve at `/assets/`,
/// covering the clan scheme (both platforms) and plain relative paths.
fn asset_name(val: &str) -> Option<String> {
    let v = val.trim();
    for prefix in [
        "clan://localhost/assets/",
        "http://clan.localhost/assets/",
        "https://clan.localhost/assets/",
        "./assets/",
        "assets/",
        "human/assets/",
    ] {
        if let Some(rest) = v.strip_prefix(prefix) {
            let name = rest.split(['?', '#']).next().unwrap_or(rest);
            if !name.is_empty() && !name.contains("..") {
                return Some(name.to_string());
            }
        }
    }
    None
}

fn brand_style_block() -> String {
    "<style>\n\
     @page { margin: 18mm; }\n\
     .napkin-export-header { display:flex; align-items:center; gap:.6rem; \
     font-family: ui-sans-serif, system-ui, sans-serif; font-size:11px; \
     letter-spacing:.14em; text-transform:uppercase; color:#6b7280; \
     border-bottom:1px solid #e5e7eb; padding-bottom:10px; margin-bottom:22px; }\n\
     .napkin-export-header .dot { width:9px; height:9px; border-radius:2px; \
     background:#6366f1; display:inline-block; }\n\
     .napkin-export-footer { margin-top:34px; padding-top:12px; \
     border-top:1px solid #e5e7eb; font-family: ui-sans-serif, system-ui, sans-serif; \
     font-size:9px; color:#9ca3af; text-align:center; }\n\
     .napkin-provenance { margin-top:30px; page-break-inside:avoid; \
     font-family: ui-sans-serif, system-ui, sans-serif; }\n\
     .napkin-provenance h2 { font-size:10px; letter-spacing:.12em; \
     text-transform:uppercase; color:#6b7280; border-bottom:1px solid #e5e7eb; \
     padding-bottom:5px; }\n\
     .napkin-provenance ol { list-style:none; padding:0; margin:.6rem 0 0; }\n\
     .napkin-provenance li { border-left:3px solid #6366f1; padding:.35rem 0 .35rem .7rem; \
     margin-bottom:.5rem; font-size:11px; color:#374151; }\n\
     .napkin-provenance small { color:#9ca3af; }\n\
     </style>\n"
        .to_string()
}

fn brand_header(clan: &ClanFile) -> String {
    format!(
        "<div class=\"napkin-export-header\"><span class=\"dot\"></span>\
         <span>Napkin Studio OS</span><span style=\"flex:1\"></span>\
         <span>{}</span></div>\n",
        escape(&clan.manifest().title)
    )
}

fn brand_footer(clan: &ClanFile) -> String {
    let date = clan
        .manifest()
        .updated_at
        .split('T')
        .next()
        .unwrap_or("")
        .to_string();
    format!(
        "<div class=\"napkin-export-footer\">Generated with Napkin Studio OS · \
         powered by CLAN{}</div>\n",
        if date.is_empty() {
            String::new()
        } else {
            format!(" · {}", escape(&date))
        }
    )
}

/// Attributed provenance appendix, newest decisions first, bounded for size.
fn provenance_appendix(clan: &ClanFile) -> String {
    let chain = clan
        .read_entry("agent/decision-chain.yaml")
        .ok()
        .and_then(|b| DecisionChain::from_yaml(&b).ok())
        .unwrap_or_default();

    let mut items = String::new();
    for d in chain.decisions.iter().take(50) {
        let when = d.timestamp.split('T').next().unwrap_or(&d.timestamp);
        items.push_str(&format!(
            "<li><strong>{}</strong> — {}{}<br><small>{}</small></li>\n",
            escape(&d.agent),
            escape(&d.action),
            if d.rationale.is_empty() {
                String::new()
            } else {
                format!(": {}", escape(&d.rationale))
            },
            escape(when),
        ));
    }
    if items.is_empty() {
        items.push_str("<li><small>No decisions recorded.</small></li>\n");
    }
    format!(
        "<section class=\"napkin-provenance\"><h2>Provenance</h2><ol>\n{items}</ol></section>\n"
    )
}

fn escape(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
}

/// Extension → MIME, mirroring the desktop host's asset server.
fn content_type_for(rel: &str) -> &'static str {
    match rel.rsplit('.').next().map(|e| e.to_ascii_lowercase()).as_deref() {
        Some("png") => "image/png",
        Some("jpg") | Some("jpeg") => "image/jpeg",
        Some("gif") => "image/gif",
        Some("webp") => "image/webp",
        Some("svg") => "image/svg+xml",
        Some("css") => "text/css",
        Some("woff2") => "font/woff2",
        Some("woff") => "font/woff",
        _ => "application/octet-stream",
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::create::{create, CreateOptions};
    use crate::pack::patch_data;

    fn doc_with(data: serde_json::Value, no_render: bool) -> ClanFile {
        let bytes = create(CreateOptions {
            title: "Q3 <Report>".into(),
            brief: "brief".into(),
            document_type: None,
            no_render,
            schema: None,
        })
        .unwrap();
        let clan = ClanFile::from_bytes(bytes).unwrap();
        ClanFile::from_bytes(patch_data(&clan, &data, None).unwrap()).unwrap()
    }

    #[test]
    fn resolves_scalar_bindings_and_escapes_title() {
        // An authored view that binds data the way `render` and templates do.
        let clan = doc_with(serde_json::json!({"vendor": "Acme & Co"}), false);
        let injected = crate::pack::pack_html(
            &clan,
            "<p data-adf-id=\"v\">Vendor: {{vendor}}</p><p>literal {{ not a binding }}</p>",
            None,
            None,
            None,
            None,
        )
        .unwrap();
        let clan = ClanFile::from_bytes(injected).unwrap();
        let html = export_html(&clan, &ExportOptions::default()).unwrap();
        // The {{vendor}} binding resolves and is HTML-escaped…
        assert!(html.contains("Vendor: Acme &amp; Co"), "{html}");
        assert!(!html.contains("{{vendor}}"), "binding left unresolved: {html}");
        // …a non-binding `{{ ... }}` survives verbatim…
        assert!(html.contains("{{ not a binding }}"), "{html}");
        // …and the escaped title appears in the brand header.
        assert!(html.contains("Q3 &lt;Report&gt;"), "{html}");
    }

    #[test]
    fn binding_resolution_preserves_multibyte_utf8() {
        let clan = doc_with(serde_json::json!({"k": "v"}), false);
        let injected =
            crate::pack::pack_html(&clan, "<p>café — déjà vu … {{k}}</p>", None, None, None, None)
                .unwrap();
        let clan = ClanFile::from_bytes(injected).unwrap();
        let html = export_html(&clan, &ExportOptions::default()).unwrap();
        assert!(html.contains("café — déjà vu … v"), "utf-8 corrupted: {html}");
    }

    #[test]
    fn strips_scripts_for_static_output() {
        let clan = doc_with(serde_json::json!({"k": "v"}), false);
        // Authored views carry app JS; the export must not.
        let injected = crate::pack::pack_html(
            &clan,
            "<div>hi</div><script>window.x=1</script>",
            None,
            None,
            None,
            None,
        )
        .unwrap();
        let clan = ClanFile::from_bytes(injected).unwrap();
        let html = export_html(&clan, &ExportOptions::default()).unwrap();
        assert!(!html.to_lowercase().contains("<script"), "{html}");
    }

    #[test]
    fn agent_only_file_exports_via_default_render() {
        // No human view present; export must still produce a document.
        let clan = doc_with(serde_json::json!({"finding": "growth"}), true);
        assert!(!clan.has_entry("human/index.html"));
        let html = export_html(&clan, &ExportOptions::default()).unwrap();
        assert!(html.contains("growth"), "{html}");
        assert!(html.to_lowercase().contains("</html>"));
    }

    #[test]
    fn provenance_appendix_is_opt_in() {
        let clan = doc_with(serde_json::json!({"k": "v"}), false);
        // Check for the appendix SECTION, not the substring — the brand style
        // block always *defines* the .napkin-provenance class.
        let plain = export_html(&clan, &ExportOptions { brand: true, provenance: false }).unwrap();
        assert!(!plain.contains("<section class=\"napkin-provenance\">"), "{plain}");
        let withprov = export_html(&clan, &ExportOptions { brand: true, provenance: true }).unwrap();
        assert!(withprov.contains("<section class=\"napkin-provenance\">"), "{withprov}");
    }

    #[test]
    fn deterministic_for_same_file() {
        let clan = doc_with(serde_json::json!({"k": "v"}), false);
        let a = export_html(&clan, &ExportOptions::default()).unwrap();
        let b = export_html(&clan, &ExportOptions::default()).unwrap();
        assert_eq!(a, b);
    }
}
