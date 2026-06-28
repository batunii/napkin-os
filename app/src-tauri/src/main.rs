// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::path::PathBuf;
use std::sync::Mutex;

use clan_sdk::{
    apply_patch_and_repack, create, fork, instantiate, make_template, patch_asset_with,
    patch_context, patch_data_with, validate, AppInfo, ClanBuilder, ClanFile, CreateOptions,
    DecisionEntry, InstantiateOptions, MakeTemplateOptions, PatchDataOptions,
};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use tauri::{Emitter, Manager, State};

// ── File logger (writes to /tmp/clan-debug.log) ──────────────────────────────
fn log(msg: &str) {
    use std::io::Write;
    let ts = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0);
    if let Ok(mut f) = std::fs::OpenOptions::new()
        .create(true).append(true).open("/tmp/clan-debug.log")
    {
        let _ = writeln!(f, "[{ts}] {msg}");
    }
}

struct AppState {
    current: Mutex<Option<LoadedClan>>,
    edit_mode: Mutex<bool>,
    preview_html: Mutex<String>,
    // A `.clan` path the OS handed us at launch (double-click / "Open with"),
    // waiting for the frontend to pull it via `take_launch_file`.
    pending_open: Mutex<Option<String>>,
}

/// Pick the first `.clan` file path out of a set of process arguments.
/// Works for both our own launch args and the argv a second instance is
/// started with; the executable path and any flags are ignored since they
/// don't end in `.clan`.
fn clan_path_from_args<I: IntoIterator<Item = String>>(args: I) -> Option<String> {
    args.into_iter()
        .find(|a| a.to_lowercase().ends_with(".clan"))
}

struct LoadedClan {
    path: PathBuf,
    // The ClanFile already holds the raw archive bytes (clan.raw_bytes()).
    clan: ClanFile,
    // True if the app is validly signed by Napkin's key → gets scoped host
    // capabilities. Untrusted files are limited to the safe clan:// subset.
    trusted: bool,
}

/// Napkin's app-signing public key (ed25519, base64). Safe to embed and ship
/// open-source: it can only VERIFY signatures, never forge them. Apps signed by
/// the matching private key are granted scoped host access.
const NAPKIN_PUBLIC_KEY: &str = "iE5TL/Am5Tu4jktPTXNp52HhgJWo8eLoDKgjtlyZ4fc=";

/// Scoped capabilities a trusted app may use (the allowlist — extend as needed).
/// Untrusted apps get none of these; they keep only the safe clan:// data/asset
/// /proxy routes.
const TRUSTED_CAPABILITIES: &[&str] = &["notify", "set-theme"];

#[derive(Serialize, Deserialize)]
struct ManifestInfo {
    title: String,
    id: String,
    version: String,
    created_at: String,
    updated_at: String,
    document_type: Option<String>,
    sha256: String,
    file_count: usize,
    lineage: Option<LineageInfo>,
    app: Option<AppMeta>,
}

/// App metadata surfaced to the shell (launcher cards, "running an app" chrome).
#[derive(Serialize, Deserialize, Clone)]
struct AppMeta {
    name: String,
    app_id: String,
    version: String,
    icon: Option<String>,
}

#[derive(Serialize, Deserialize)]
struct LineageInfo {
    parent_id: String,
    parent_uri: String,
    parent_sha256: Option<String>,
    delta: String,
}

#[derive(Serialize, Deserialize)]
struct OpenResult {
    path: String,
    manifest: ManifestInfo,
    validation: String,
    has_human_view: bool,
    /// `"authored"` for template apps / their instances (view.source == "app"),
    /// else `"legacy"` (AI-generated HTML). Drives which edit bridge the shell
    /// injects and how the view is rendered.
    render_model: String,
    /// `true` when this file is a template app (document_type == "template").
    is_template: bool,
    /// `true` when the app is validly signed by Napkin's key → scoped host
    /// capabilities are available to it.
    trusted: bool,
}

#[tauri::command]
fn open_clan(path: String, state: State<AppState>) -> Result<OpenResult, String> {
    do_open_clan(path, &state)
}

/// Returns (and clears) the `.clan` path the app was launched with, if any.
/// The frontend calls this once on mount to open a double-clicked file.
#[tauri::command]
fn take_launch_file(state: State<AppState>) -> Option<String> {
    state.pending_open.lock().unwrap().take()
}

fn do_open_clan(path: String, state: &AppState) -> Result<OpenResult, String> {
    let p = PathBuf::from(&path);
    let clan = ClanFile::open(&p).map_err(|e| e.to_string())?;
    let manifest = clan.manifest().clone();
    let report = validate(&clan);
    let has_human_view = clan.has_entry("human/index.html");
    let sha256 = clan.sha256();

    let is_authored = manifest
        .view
        .as_ref()
        .and_then(|v| v.source.as_deref())
        == Some("app");
    let is_template = manifest.document_type.as_deref() == Some("template");

    let info = ManifestInfo {
        title: manifest.title.clone(),
        id: manifest.id.clone(),
        version: format!("{}.{}", manifest.clan_version, manifest.clan_version_minor),
        created_at: manifest.created_at.clone(),
        updated_at: manifest.updated_at.clone(),
        document_type: manifest.document_type.clone(),
        sha256,
        file_count: manifest.files.len(),
        lineage: manifest.lineage.as_ref().map(|l| LineageInfo {
            parent_id: l.parent_id.clone(),
            parent_uri: l.parent_uri.clone(),
            parent_sha256: l.parent_sha256.clone(),
            delta: l.delta.clone(),
        }),
        app: manifest.app.as_ref().map(|a| AppMeta {
            name: a.name.clone(),
            app_id: a.app_id.clone(),
            version: a.version.clone(),
            icon: a.icon.clone(),
        }),
    };

    // The trust gate: is this app validly signed by Napkin's key?
    let trusted = clan_sdk::verify_app(&clan, NAPKIN_PUBLIC_KEY);

    // The ClanFile already read the file once; no second disk read needed.
    *state.current.lock().unwrap() = Some(LoadedClan {
        path: p.clone(),
        clan,
        trusted,
    });

    Ok(OpenResult {
        path: p.display().to_string(),
        manifest: info,
        validation: report.display(),
        has_human_view,
        render_model: if is_authored { "authored".into() } else { "legacy".into() },
        is_template,
        trusted,
    })
}

#[tauri::command]
fn get_human_html(state: State<AppState>) -> Result<String, String> {
    log("get_human_html: called");
    let guard = state.current.lock().unwrap();
    let loaded = guard.as_ref().ok_or("no file open")?;
    let html = loaded
        .clan
        .read_entry_string("human/index.html")
        .map_err(|e| e.to_string())?;

    // Authored template apps (view.source == "app") render client-side from
    // window.__CLAN__.data. Legacy AI-generated views keep the server-side
    // {{binding}} + auto-id + patch pipeline.
    let authored = loaded
        .clan
        .manifest()
        .view
        .as_ref()
        .and_then(|v| v.source.as_deref())
        == Some("app");

    let data_value: serde_yaml::Value = loaded
        .clan
        .read_entry("shared/data.yaml")
        .ok()
        .and_then(|b| serde_yaml::from_slice(&b).ok())
        .unwrap_or(serde_yaml::Value::Null);

    let body = if authored {
        // Don't munge the authored markup — the app owns its own rendering.
        html
    } else {
        // Legacy pipeline: resolve {{key}} bindings, auto-inject data-adf-id,
        // then apply human/patches.yaml.
        let resolved = resolve_bindings(&html, &data_value);
        let with_ids = auto_inject_adf_ids(&resolved);
        if loaded.clan.has_entry("human/patches.yaml") {
            match loaded.clan.read_entry_string("human/patches.yaml") {
                Ok(yaml) => apply_patches(&with_ids, &yaml),
                Err(_) => with_ids,
            }
        } else {
            with_ids
        }
    };

    let css = loaded
        .clan
        .read_entry_string("human/styles.css")
        .unwrap_or_default();
    let styled_html = inject_styles(&body, &css);

    let context = build_clan_context(&loaded.clan, &data_value);
    let context_json = serde_json::to_string(&context).unwrap_or_else(|_| "{}".to_string());
    Ok(inject_clan_data(&styled_html, &context_json))
}

/// Build the `window.__CLAN__` context object the template/view reads:
/// `{ data, manifest, assets }`. The decision chain is intentionally omitted
/// here (it can be large) — the view fetches it lazily via `clan://chain`.
fn build_clan_context(clan: &ClanFile, data: &serde_yaml::Value) -> Value {
    let data_json: Value = serde_json::to_value(data).unwrap_or(Value::Null);
    let m = clan.manifest();

    // Map every human/assets/<rel> entry to a relative URL the iframe resolves
    // against its own clan:// origin.
    let mut assets = serde_json::Map::new();
    for f in &m.files {
        if let Some(rel) = f.path.strip_prefix("human/assets/") {
            assets.insert(rel.to_string(), Value::String(format!("/assets/{rel}")));
        }
    }

    let manifest_json = serde_json::json!({
        "id": m.id,
        "title": m.title,
        "document_type": m.document_type,
        "app": m.app.as_ref().map(|a| serde_json::json!({
            "name": a.name,
            "app_id": a.app_id,
            "version": a.version,
        })),
    });

    serde_json::json!({
        "data": data_json,
        "manifest": manifest_json,
        "assets": Value::Object(assets),
    })
}

fn inject_styles(html: &str, css: &str) -> String {
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

fn inject_clan_data(html: &str, context_json: &str) -> String {
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

#[tauri::command]
fn get_data(state: State<AppState>) -> Result<String, String> {
    let guard = state.current.lock().unwrap();
    guard.as_ref().ok_or("no file open")?.clan
        .read_entry_string("shared/data.yaml").map_err(|e| e.to_string())
}

#[tauri::command]
fn get_chain(state: State<AppState>) -> Result<String, String> {
    let guard = state.current.lock().unwrap();
    guard.as_ref().ok_or("no file open")?.clan
        .read_entry_string("agent/decision-chain.yaml").map_err(|e| e.to_string())
}

#[tauri::command]
fn get_agent_state(state: State<AppState>) -> Result<String, String> {
    let guard = state.current.lock().unwrap();
    guard.as_ref().ok_or("no file open")?.clan
        .read_entry_string("agent/state.yaml").map_err(|e| e.to_string())
}

#[tauri::command]
fn get_context(state: State<AppState>) -> Result<String, String> {
    let guard = state.current.lock().unwrap();
    guard.as_ref().ok_or("no file open")?.clan
        .read_entry_string("agent/context.md").map_err(|e| e.to_string())
}

fn resolve_bindings(html: &str, data: &serde_yaml::Value) -> String {
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

fn resolve_key(key: &str, data: &serde_yaml::Value) -> String {
    let mut current = data;
    for part in key.split('.') {
        current = match current {
            serde_yaml::Value::Mapping(m) => match m.get(serde_yaml::Value::String(part.to_string())) {
                Some(v) => v,
                None => return format!("{{{{{key}}}}}"),
            },
            serde_yaml::Value::Sequence(s) => match part.parse::<usize>().ok().and_then(|i| s.get(i)) {
                Some(v) => v,
                None => return format!("{{{{{key}}}}}"),
            },
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

fn apply_patches(html: &str, patch_yaml: &str) -> String {
    #[derive(Deserialize)] struct Patches { #[serde(default)] patches: Vec<Patch> }
    #[derive(Deserialize)] struct Patch { id: String, content: String }
    let Ok(ps) = serde_yaml::from_str::<Patches>(patch_yaml) else { return html.to_string() };
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
            log(&format!("apply_patches: id={:?} no closing > for opening tag", p.id));
            continue;
        };
        let content_start = attr_pos + gt_rel + 1;

        // Find the matching closing tag, respecting nesting.
        let close_pos = find_closing_tag(&result, content_start, &tag_name);
        let Some(close_pos) = close_pos else {
            log(&format!("apply_patches: id={:?} tag={tag_name:?} no matching closing tag", p.id));
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
fn find_closing_tag(html: &str, from: usize, tag: &str) -> Option<usize> {
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
            matches!(lower.as_bytes().get(after), Some(b' ' | b'\t' | b'\n' | b'\r' | b'>') | None)
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
                if matches!(lower.as_bytes().get(after), Some(b'>' | b' ' | b'\t' | b'\n' | b'\r') | None) {
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
fn auto_inject_adf_ids(html: &str) -> String {
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
    let mut closes: Vec<(usize, Option<usize>)> =
        token_positions(&lower, CLOSE).into_iter().map(|c| (c, None)).collect();

    // '>' position -> (tag index, per-tag count). At most one injection per '>'.
    let mut injections: std::collections::BTreeMap<usize, (usize, usize)> =
        std::collections::BTreeMap::new();

    for (t, tag) in EDITABLE.iter().enumerate() {
        let pat = format!("<{}", tag);
        let mut count = 0usize;
        let mut pos = 0usize;
        while pos < html.len() {
            // Case-sensitive match on the original string, like the old code.
            let Some(rel) = html[pos..].find(pat.as_str()) else { break };
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
            let Some(rel_end) = html[start..].find('>') else { break };
            let gt = start + rel_end;
            let annotated =
                html[start..=gt].contains("data-adf-id") || injections.contains_key(&gt);
            if !annotated && bytes[gt - 1] != b'/' {
                injections.insert(gt, (t, count));
                count += 1;
                // Injecting right before the '>' of a "</script>" splits the
                // token; later passes must not see it as a script close.
                if let Some(close) =
                    closes.iter_mut().find(|(c, _)| c + CLOSE.len() == gt + 1)
                {
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
fn token_positions(haystack: &str, token: &str) -> Vec<usize> {
    let mut found = Vec::new();
    let mut from = 0;
    while let Some(rel) = haystack[from..].find(token) {
        found.push(from + rel);
        from += rel + 1;
    }
    found
}



#[tauri::command]
fn set_edit_mode(active: bool, state: State<AppState>) {
    *state.edit_mode.lock().unwrap() = active;
}

#[tauri::command]
fn update_preview_html(html: String, state: State<AppState>) {
    *state.preview_html.lock().unwrap() = html;
}

fn strip_scripts(html: &str) -> String {
    let lower = html.to_lowercase();
    let mut result = String::with_capacity(html.len());
    let mut pos = 0;
    loop {
        match lower[pos..].find("<script") {
            None => { result.push_str(&html[pos..]); break; }
            Some(rel) => {
                result.push_str(&html[pos..pos + rel]);
                let after_open = pos + rel;
                match lower[after_open..].find("</script>") {
                    None => break,
                    Some(end_rel) => { pos = after_open + end_rel + "</script>".len(); }
                }
            }
        }
    }
    result
}

fn do_snapshot(rendered_html: String, state: &AppState) -> Result<(), String> {
    let clean = strip_scripts(&rendered_html);
    log(&format!("snapshot: stripped len={}", clean.len()));

    let mut guard = state.current.lock().unwrap();
    let loaded = guard.as_mut().ok_or("no file open")?;

    let mut builder = ClanBuilder::new(loaded.clan.manifest().clone());
    for (path, bytes) in loaded.clan.read_all_entries().map_err(|e| e.to_string())? {
        if path == "manifest.yaml" || path == "human/index.html" { continue; }
        builder.add_entry(path, bytes);
    }
    builder.add_entry("human/index.html", clean.into_bytes());
    let new_bytes = builder.build().map_err(|e| e.to_string())?;
    std::fs::write(&loaded.path, &new_bytes).map_err(|e| e.to_string())?;
    loaded.clan = ClanFile::from_bytes(new_bytes).map_err(|e| e.to_string())?;
    log("snapshot: written to human/index.html");
    Ok(())
}

#[cfg(test)]
static SAVE_COUNT: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);

fn do_save_patch(id: String, content: String, state: &AppState) -> Result<(), String> {
    log(&format!("save_patch: id={id:?} content={:?}…", &content[..content.len().min(80)]));

    let mut guard = state.current.lock().unwrap();
    let loaded = guard.as_mut().ok_or("no file open")?;

    // No-op guard (F4): if a patch with this id already holds identical
    // content, skip the rewrite entirely. Backstops the client-side
    // skip-if-unchanged so a blur with no edit never churns the file.
    if let Ok(bytes) = loaded.clan.read_entry("human/patches.yaml") {
        if let Ok(existing) = clan_sdk::Patches::from_yaml(&bytes) {
            if existing.patches.iter().any(|p| p.id == id && p.content == content) {
                log(&format!("save_patch: no-op (id={id:?} unchanged), skipped"));
                return Ok(());
            }
        }
    }

    #[cfg(test)]
    SAVE_COUNT.fetch_add(1, std::sync::atomic::Ordering::SeqCst);

    let new_bytes = apply_patch_and_repack(&loaded.clan, id.clone(), content.clone())
        .map_err(|e| e.to_string())?;

    std::fs::write(&loaded.path, &new_bytes).map_err(|e| e.to_string())?;
    loaded.clan = ClanFile::from_bytes(new_bytes).map_err(|e| e.to_string())?;

    log(&format!("save_patch: done, file repacked. id={id:?}"));
    Ok(())
}

/// Handle a `clan://patch` request body. Saves the patch exactly once and
/// returns the payload for the informational `clan-patch-saved` event.
/// The frontend listener must treat that event as a notification only and
/// never call `save_patch` in response — doing so writes the file twice (#9).
fn handle_patch_request(body: &str, state: &AppState) -> Option<serde_json::Value> {
    let json = serde_json::from_str::<serde_json::Value>(body).ok()?;
    let id = json["id"].as_str()?;
    let content = json["content"].as_str()?;
    do_save_patch(id.to_string(), content.to_string(), state).ok()?;
    Some(serde_json::json!({ "id": id, "content": content }))
}

#[tauri::command]
fn save_patch(id: String, content: String, state: State<AppState>) -> Result<(), String> {
    do_save_patch(id, content, &*state)
}

// ── clan:// API surface (provenance-native rendering environment) ────────────
//
// Every mutating route reuses the established pattern: SDK fn -> fs::write ->
// reload ClanFile. Handlers return Ok(payload) or Err((status, message)); the
// scheme handler maps that to an HTTP response and emits UI events.

type RouteResult = std::result::Result<serde_json::Value, (u16, String)>;

fn json_resp(status: u16, value: &serde_json::Value) -> tauri::http::Response<Vec<u8>> {
    tauri::http::Response::builder()
        .header("Content-Type", "application/json")
        .header("Access-Control-Allow-Origin", "*")
        .status(status)
        .body(serde_json::to_vec(value).unwrap_or_default())
        .unwrap()
}

fn err_resp(status: u16, msg: &str) -> tauri::http::Response<Vec<u8>> {
    json_resp(status, &serde_json::json!({ "ok": false, "error": msg }))
}

/// Map an SDK error to an HTTP status: a namespace violation (writing a forked
/// branch through the direct path) is a 409; anything else a 422.
fn sdk_status(e: &clan_sdk::Error) -> u16 {
    match e {
        clan_sdk::Error::NamespaceViolation(_) => 409,
        _ => 422,
    }
}

/// `POST /patch-data` — structured write to shared/data.yaml with attribution,
/// recorded in the decision chain (the provenance-native human/AI co-author
/// write path).
fn do_patch_data(body: &str, state: &AppState) -> RouteResult {
    let json: Value = serde_json::from_str(body).map_err(|e| (400, format!("invalid JSON: {e}")))?;
    let patch = json
        .get("patch")
        .cloned()
        .ok_or((400, "missing 'patch'".to_string()))?;
    if !patch.is_object() {
        return Err((400, "'patch' must be an object".into()));
    }
    let keys: Vec<String> = patch
        .as_object()
        .map(|o| o.keys().cloned().collect())
        .unwrap_or_default();
    let append_keys: Vec<String> = json
        .get("append_keys")
        .and_then(|v| v.as_array())
        .map(|a| a.iter().filter_map(|x| x.as_str().map(String::from)).collect())
        .unwrap_or_default();
    // Attribution: when an agent (or "human") is named, record an attributed
    // decision over exactly the patched keys (F15).
    let decision = json.get("agent").and_then(|v| v.as_str()).map(|agent| DecisionEntry {
        agent_name: agent.to_string(),
        action: json
            .get("action")
            .and_then(|v| v.as_str())
            .unwrap_or("edit")
            .to_string(),
        rationale: json
            .get("rationale")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
        pinned: json.get("pinned").and_then(|v| v.as_bool()).unwrap_or(false),
        fields_changed: Some(keys.clone()),
    });

    let mut guard = state.current.lock().unwrap();
    let loaded = guard.as_mut().ok_or((409, "no file open".to_string()))?;

    // No-op guard: if applying the patch changes nothing, skip entirely — no
    // rewrite, no decision-chain entry. Stops redundant "edits" (e.g. opening a
    // field and saving without changing it) from polluting the provenance.
    if append_keys.is_empty() {
        let existing: Value = loaded
            .clan
            .read_entry("shared/data.yaml")
            .ok()
            .and_then(|b| serde_yaml::from_slice::<serde_yaml::Value>(&b).ok())
            .and_then(|y| serde_json::to_value(y).ok())
            .unwrap_or(Value::Object(Default::default()));
        let mut merged = existing.clone();
        json_merge(&mut merged, &patch);
        if merged == existing {
            return Ok(serde_json::json!({ "ok": true, "noop": true, "keys": [] }));
        }
    }

    let opts = PatchDataOptions {
        append_keys,
        decision,
    };
    let new_bytes = patch_data_with(&loaded.clan, &patch, opts, None)
        .map_err(|e| (sdk_status(&e), e.to_string()))?;
    std::fs::write(&loaded.path, &new_bytes).map_err(|e| (500, e.to_string()))?;
    loaded.clan = ClanFile::from_bytes(new_bytes).map_err(|e| (500, e.to_string()))?;
    Ok(serde_json::json!({ "ok": true, "keys": keys }))
}

/// RFC 7396 JSON Merge Patch applied in place — used only to test whether a
/// patch would actually change anything (the no-op guard).
fn json_merge(target: &mut Value, patch: &Value) {
    match patch {
        Value::Object(pm) => {
            if !target.is_object() {
                *target = Value::Object(Default::default());
            }
            let tm = target.as_object_mut().unwrap();
            for (k, v) in pm {
                if v.is_null() {
                    tm.remove(k);
                } else {
                    json_merge(tm.entry(k.clone()).or_insert(Value::Null), v);
                }
            }
        }
        _ => *target = patch.clone(),
    }
}

/// `POST /fork` — fork into ≥2 branch siblings written next to the parent.
/// Does NOT advance the open document.
fn do_fork(body: &str, state: &AppState) -> RouteResult {
    let json: Value = serde_json::from_str(body).map_err(|e| (400, format!("invalid JSON: {e}")))?;
    let agents: Vec<String> = json
        .get("agents")
        .and_then(|v| v.as_array())
        .map(|a| a.iter().filter_map(|x| x.as_str().map(String::from)).collect())
        .unwrap_or_default();
    if agents.len() < 2 {
        return Err((400, "fork needs at least 2 agents".into()));
    }
    let guard = state.current.lock().unwrap();
    let loaded = guard.as_ref().ok_or((409, "no file open".to_string()))?;
    let parent_dir = loaded
        .path
        .parent()
        .map(|p| p.to_path_buf())
        .unwrap_or_default();
    let stem = loaded
        .path
        .file_stem()
        .and_then(|s| s.to_str())
        .unwrap_or("doc")
        .to_string();
    let branches = fork(&loaded.clan, &agents).map_err(|e| (sdk_status(&e), e.to_string()))?;
    let mut written = Vec::new();
    for (agent_id, bytes) in &branches {
        let path = parent_dir.join(format!("{stem}.{agent_id}.clan"));
        if path.exists() {
            return Err((409, format!("refusing to overwrite {}", path.display())));
        }
        std::fs::write(&path, bytes).map_err(|e| (500, e.to_string()))?;
        written.push(serde_json::json!({ "agent": agent_id, "path": path.display().to_string() }));
    }
    Ok(serde_json::json!({ "ok": true, "branches": written }))
}

/// Reject asset names with path separators or traversal — the SDK does NOT
/// sanitize, so the host must.
fn sanitize_asset_name(name: &str) -> Option<String> {
    let n = name.trim();
    if n.is_empty() || n.contains("..") || n.contains('/') || n.contains('\\') {
        return None;
    }
    Some(n.to_string())
}

/// `POST /upload-asset?name=&agent=` — store a binary asset inside the archive.
fn do_upload_asset(name: &str, agent: Option<&str>, body: Vec<u8>, state: &AppState) -> RouteResult {
    let name = sanitize_asset_name(name).ok_or((400, "invalid asset name".to_string()))?;
    let mut guard = state.current.lock().unwrap();
    let loaded = guard.as_mut().ok_or((409, "no file open".to_string()))?;
    let decision = agent.map(|a| DecisionEntry {
        agent_name: a.to_string(),
        action: "upload-asset".into(),
        rationale: format!("added asset {name}"),
        pinned: false,
        fields_changed: None,
    });
    let new_bytes = patch_asset_with(&loaded.clan, &name, body, decision)
        .map_err(|e| (sdk_status(&e), e.to_string()))?;
    std::fs::write(&loaded.path, &new_bytes).map_err(|e| (500, e.to_string()))?;
    loaded.clan = ClanFile::from_bytes(new_bytes).map_err(|e| (500, e.to_string()))?;
    Ok(serde_json::json!({ "ok": true, "internal_path": format!("human/assets/{name}") }))
}

fn content_type_for(rel: &str) -> &'static str {
    match rel.rsplit('.').next().map(|e| e.to_ascii_lowercase()).as_deref() {
        Some("png") => "image/png",
        Some("jpg") | Some("jpeg") => "image/jpeg",
        Some("gif") => "image/gif",
        Some("webp") => "image/webp",
        Some("svg") => "image/svg+xml",
        Some("pdf") => "application/pdf",
        Some("css") => "text/css",
        Some("js") => "text/javascript",
        Some("json") => "application/json",
        Some("woff2") => "font/woff2",
        Some("woff") => "font/woff",
        _ => "application/octet-stream",
    }
}

/// `GET /assets/<rel>` — serve a binary asset from inside the artifact ZIP.
fn do_serve_asset(rel: &str, state: &AppState) -> std::result::Result<(String, Vec<u8>), (u16, String)> {
    if rel.is_empty() || rel.contains("..") || rel.contains('\\') || rel.starts_with('/') {
        return Err((400, "invalid asset path".into()));
    }
    let guard = state.current.lock().unwrap();
    let loaded = guard.as_ref().ok_or((409, "no file open".to_string()))?;
    let full = format!("human/assets/{rel}");
    let bytes = loaded
        .clan
        .read_entry(&full)
        .map_err(|_| (404, format!("asset not found: {rel}")))?;
    Ok((content_type_for(rel).to_string(), bytes))
}

/// Update the open document's title (e.g. to the AI-set brief name) and repack
/// in place. Title is not covered by the app signature, so trust is preserved.
fn do_set_title(title: &str, state: &AppState) -> RouteResult {
    let mut guard = state.current.lock().unwrap();
    let loaded = guard.as_mut().ok_or((409, "no file open".to_string()))?;
    let mut manifest = loaded.clan.manifest().clone();
    manifest.title = title.to_string();
    let mut builder = ClanBuilder::new(manifest);
    for (p, b) in loaded
        .clan
        .read_all_entries()
        .map_err(|e| (500, e.to_string()))?
    {
        if p == "manifest.yaml" {
            continue;
        }
        builder.add_entry(p, b);
    }
    let bytes = builder.build().map_err(|e| (500, e.to_string()))?;
    std::fs::write(&loaded.path, &bytes).map_err(|e| (500, e.to_string()))?;
    loaded.clan = ClanFile::from_bytes(bytes).map_err(|e| (500, e.to_string()))?;
    Ok(serde_json::json!({ "ok": true, "title": title }))
}

/// Replace or append `agent/context.md` — the running brief context downstream
/// agents read. Used by the first generate (full context) and human notes.
fn do_set_context(markdown: &str, append: bool, state: &AppState) -> RouteResult {
    let mut guard = state.current.lock().unwrap();
    let loaded = guard.as_mut().ok_or((409, "no file open".to_string()))?;
    let bytes = patch_context(&loaded.clan, markdown, append)
        .map_err(|e| (sdk_status(&e), e.to_string()))?;
    std::fs::write(&loaded.path, &bytes).map_err(|e| (500, e.to_string()))?;
    loaded.clan = ClanFile::from_bytes(bytes).map_err(|e| (500, e.to_string()))?;
    Ok(serde_json::json!({ "ok": true }))
}

/// `GET /chain` — the decision chain as JSON (lazy fetch for the view).
fn do_chain_json(state: &AppState) -> RouteResult {
    let guard = state.current.lock().unwrap();
    let loaded = guard.as_ref().ok_or((409, "no file open".to_string()))?;
    let yaml = loaded
        .clan
        .read_entry("agent/decision-chain.yaml")
        .map_err(|e| (404, e.to_string()))?;
    let v: serde_yaml::Value =
        serde_yaml::from_slice(&yaml).map_err(|e| (500, e.to_string()))?;
    serde_json::to_value(&v).map_err(|e| (500, e.to_string()))
}

// ── AI inference proxy: workspace config + host-side secrets ─────────────────
//
// Keys NEVER live in the artifact or the template. The template issues a
// `clan://api-proxy` call naming only a logical request_kind; the host resolves
// endpoint/model/secret from per-user config and makes the authenticated call.

#[derive(Deserialize, Default)]
struct WorkspaceConfig {
    #[serde(default)]
    proxies: std::collections::HashMap<String, ProxyConfig>,
    /// The single, uniform agent endpoint the home-screen prompt posts to.
    /// One place to change: localhost for testing, a hosted URL in production.
    #[serde(default)]
    agent_url: Option<String>,
}

#[derive(Deserialize, Clone)]
struct ProxyConfig {
    endpoint: String,
    #[serde(default)]
    auth_kind: Option<String>, // "x-api-key" (default) | "bearer"
    #[serde(default)]
    secret_ref: Option<String>,
}

fn config_dir(app: &tauri::AppHandle) -> std::result::Result<PathBuf, String> {
    app.path().app_config_dir().map_err(|e| e.to_string())
}

fn load_workspace_config(app: &tauri::AppHandle) -> std::result::Result<WorkspaceConfig, String> {
    let path = config_dir(app)?.join("workspace.yaml");
    let bytes = std::fs::read(&path)
        .map_err(|e| format!("no workspace.yaml at {}: {e}", path.display()))?;
    serde_yaml::from_slice(&bytes).map_err(|e| format!("invalid workspace.yaml: {e}"))
}

fn load_secret(app: &tauri::AppHandle, secret_ref: &str) -> std::result::Result<String, String> {
    let path = config_dir(app)?.join("secrets.yaml");
    let bytes = std::fs::read(&path).map_err(|e| format!("no secrets.yaml: {e}"))?;
    let map: std::collections::HashMap<String, String> =
        serde_yaml::from_slice(&bytes).map_err(|e| e.to_string())?;
    map.get(secret_ref)
        .cloned()
        .ok_or_else(|| format!("secret_ref '{secret_ref}' not found in secrets.yaml"))
}

/// Resolve a `request_kind` to its endpoint + auth. A kind configured in
/// `workspace.yaml` wins; otherwise everything falls back to the single uniform
/// agent URL (env / `agent_url` / default) so one value serves every kind in
/// dev. Returns `(url, auth_kind, key)`.
fn resolve_proxy(app: &tauri::AppHandle, kind: &str) -> (String, Option<String>, Option<String>) {
    if let Ok(cfg) = load_workspace_config(app) {
        if let Some(p) = cfg.proxies.get(kind) {
            let key = p.secret_ref.as_ref().and_then(|r| load_secret(app, r).ok());
            return (p.endpoint.clone(), p.auth_kind.clone(), key);
        }
    }
    (agent_base_url(app), None, None)
}

/// The one network primitive: POST `payload` verbatim to the endpoint resolved
/// for `request_kind`, add host-side auth, return a structured envelope. RAG,
/// model choice, prompt assembly — all backend concerns behind the endpoint.
async fn proxy_call(app: &tauri::AppHandle, request_kind: &str, payload: Value) -> Value {
    let (url, auth_kind, key) = resolve_proxy(app, request_kind);
    if url.trim().is_empty() {
        return serde_json::json!({ "ok": false, "status": 0, "error": format!("no endpoint configured for kind '{request_kind}'") });
    }
    let client = reqwest::Client::new();
    let mut rb = client.post(&url).json(&payload);
    if let Some(k) = &key {
        rb = match auth_kind.as_deref() {
            Some("bearer") => rb.bearer_auth(k),
            _ => rb.header("x-api-key", k),
        };
    }
    match rb.send().await {
        Ok(resp) => {
            let status = resp.status();
            let body = resp.text().await.unwrap_or_default();
            let data: Value = serde_json::from_str(&body).unwrap_or(Value::String(body));
            serde_json::json!({
                "ok": status.is_success(),
                "status": status.as_u16(),
                "endpoint": url,
                // Don't surface upstream error bodies verbatim to the sandbox.
                "data": if status.is_success() { data } else { Value::Null },
                "error": if status.is_success() { Value::Null } else { Value::String(format!("upstream returned {}", status.as_u16())) },
            })
        }
        Err(e) => serde_json::json!({
            "ok": false, "status": 0, "endpoint": url,
            "error": format!("could not reach {url}: {e}"),
        }),
    }
}

/// `POST /api-proxy {request_kind, payload}` (async) — the single, uniform
/// network route. Keys stay host-side; `payload` is forwarded verbatim.
async fn do_api_proxy(app: tauri::AppHandle, body: String) -> tauri::http::Response<Vec<u8>> {
    let req: Value = match serde_json::from_str(&body) {
        Ok(v) => v,
        Err(e) => return err_resp(400, &format!("invalid JSON: {e}")),
    };
    let kind = req
        .get("request_kind")
        .and_then(|v| v.as_str())
        .unwrap_or("agent")
        .to_string();
    let payload = req.get("payload").cloned().unwrap_or(Value::Null);
    // Enrich with the open artifact's intelligence layer so lineage, decisions,
    // schema, and context travel to the agent — not just what the iframe sent.
    let clan_ctx = clan_context_for_agent(app.state::<AppState>().inner());
    let outgoing = serde_json::json!({ "request_kind": kind, "payload": payload, "clan": clan_ctx });
    json_resp(200, &proxy_call(&app, &kind, outgoing).await)
}

/// The provenance bundle an agent needs to fill the boxes coherently: the
/// schema (what boxes exist), current data (the brief so far), the decision
/// chain (what's been decided + by whom), the agent context, and lineage. Built
/// host-side from the open document.
fn clan_context_for_agent(st: &AppState) -> Value {
    let guard = st.current.lock().unwrap();
    let Some(loaded) = guard.as_ref() else {
        return Value::Null;
    };
    let clan = &loaded.clan;
    let yaml_to_json = |p: &str| -> Value {
        clan.read_entry(p)
            .ok()
            .and_then(|b| serde_yaml::from_slice::<serde_yaml::Value>(&b).ok())
            .and_then(|y| serde_json::to_value(y).ok())
            .unwrap_or(Value::Null)
    };
    let schema: Value = clan
        .read_entry("agent/output-schema.json")
        .ok()
        .and_then(|b| serde_json::from_slice(&b).ok())
        .unwrap_or(Value::Null);
    let m = clan.manifest();
    serde_json::json!({
        "document_type": m.document_type,
        "app": m.app.as_ref().map(|a| serde_json::json!({ "name": a.name, "app_id": a.app_id, "version": a.version })),
        "schema": schema,
        "data": yaml_to_json("shared/data.yaml"),
        "decision_chain": yaml_to_json("agent/decision-chain.yaml"),
        "context": clan.read_entry_string("agent/context.md").unwrap_or_default(),
        "lineage": m.lineage.as_ref().map(|l| serde_json::json!({ "parent_id": l.parent_id, "delta": l.delta })),
    })
}

/// Parse `k=v&k=v` query strings (small, dependency-free).
fn query_param(query: &str, key: &str) -> Option<String> {
    query.split('&').find_map(|kv| {
        let (k, v) = kv.split_once('=')?;
        if k == key {
            Some(v.replace('+', " "))
        } else {
            None
        }
    })
}

// ── Launcher commands (Napkin Studio OS app library) ────────────────────────

#[derive(Serialize)]
struct InstalledApp {
    app_id: String,
    name: String,
    version: String,
    path: String,
    icon: Option<String>,
}

/// The per-user app library the launcher scans. Honors NAPKIN_APPS_DIR, else
/// `<app_data_dir>/apps`.
fn app_library_dir(app: &tauri::AppHandle) -> PathBuf {
    if let Some(d) = std::env::var_os("NAPKIN_APPS_DIR") {
        return PathBuf::from(d);
    }
    app.path()
        .app_data_dir()
        .map(|d| d.join("apps"))
        .unwrap_or_else(|_| PathBuf::from("apps"))
}

#[derive(Serialize)]
struct RecentDoc {
    title: String,
    path: String,
    app_id: Option<String>,
    updated_at: String,
}

/// Recent document instances (newest first) from the per-user documents dir.
fn scan_recent(app: &tauri::AppHandle) -> Vec<RecentDoc> {
    let dir = app
        .path()
        .app_data_dir()
        .map(|d| d.join("documents"))
        .unwrap_or_default();
    let mut out = Vec::new();
    if let Ok(rd) = std::fs::read_dir(&dir) {
        for e in rd.flatten() {
            let p = e.path();
            if p.extension().and_then(|x| x.to_str()) != Some("clan") {
                continue;
            }
            if let Ok(clan) = ClanFile::open(&p) {
                let m = clan.manifest();
                out.push(RecentDoc {
                    title: m.title.clone(),
                    path: p.display().to_string(),
                    app_id: m.app.as_ref().map(|a| a.app_id.clone()),
                    updated_at: m.updated_at.clone(),
                });
            }
        }
    }
    out.sort_by(|a, b| b.updated_at.cmp(&a.updated_at));
    out.truncate(12);
    out
}

/// Scan the app library for installed template apps. Shared by the `list_apps`
/// command (React shell) and the `clan://apps` route (a home CLAN app).
fn scan_apps(app: &tauri::AppHandle) -> Vec<InstalledApp> {
    let dir = app_library_dir(app);
    let mut out = Vec::new();
    if let Ok(rd) = std::fs::read_dir(&dir) {
        for e in rd.flatten() {
            let p = e.path();
            let candidate = if p.is_dir() {
                p.join("app.clan")
            } else if p.extension().and_then(|x| x.to_str()) == Some("clan") {
                p
            } else {
                continue;
            };
            if let Ok(clan) = ClanFile::open(&candidate) {
                let m = clan.manifest();
                if m.document_type.as_deref() == Some("template") {
                    if let Some(a) = &m.app {
                        out.push(InstalledApp {
                            app_id: a.app_id.clone(),
                            name: a.name.clone(),
                            version: a.version.clone(),
                            path: candidate.display().to_string(),
                            icon: a.icon.clone(),
                        });
                    }
                }
            }
        }
    }
    out
}

#[tauri::command]
fn list_apps(app: tauri::AppHandle) -> Result<Vec<InstalledApp>, String> {
    Ok(scan_apps(&app))
}

/// Instantiate a working document from an installed app and return its path.
/// Shared by `new_document_from_app` (command) and the `clan://launch` route.
fn create_instance_doc(
    app: &tauri::AppHandle,
    app_id: &str,
    title: Option<String>,
) -> std::result::Result<PathBuf, String> {
    let tpl_path = app_library_dir(app).join(app_id).join("app.clan");
    let template = ClanFile::open(&tpl_path).map_err(|e| format!("app not installed: {e}"))?;
    let bytes = instantiate(
        &template,
        InstantiateOptions {
            title: title.unwrap_or_default(),
            document_type: None,
            fresh_data: true,
            instance_id: None,
        },
    )
    .map_err(|e| e.to_string())?;
    let id_short = ClanFile::from_bytes(bytes.clone())
        .map_err(|e| e.to_string())?
        .manifest()
        .id
        .chars()
        .take(8)
        .collect::<String>();
    let docs = app
        .path()
        .app_data_dir()
        .map(|d| d.join("documents"))
        .map_err(|e| e.to_string())?;
    std::fs::create_dir_all(&docs).map_err(|e| e.to_string())?;
    let out = docs.join(format!("{}-{}.clan", app_id.replace('.', "-"), id_short));
    std::fs::write(&out, &bytes).map_err(|e| e.to_string())?;
    Ok(out)
}

#[tauri::command]
fn install_app(app: tauri::AppHandle, src_path: String) -> Result<InstalledApp, String> {
    let clan = ClanFile::open(&src_path).map_err(|e| e.to_string())?;
    let m = clan.manifest();
    if m.document_type.as_deref() != Some("template") {
        return Err("not a template app (document_type must be 'template')".into());
    }
    let a = m.app.clone().ok_or("template has no app block")?;
    let dir = app_library_dir(&app).join(&a.app_id);
    std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    let dest = dir.join("app.clan");
    std::fs::copy(&src_path, &dest).map_err(|e| e.to_string())?;
    Ok(InstalledApp {
        app_id: a.app_id,
        name: a.name,
        version: a.version,
        path: dest.display().to_string(),
        icon: a.icon,
    })
}

#[tauri::command]
fn new_document_from_app(
    app: tauri::AppHandle,
    app_id: String,
    title: Option<String>,
    state: State<AppState>,
) -> Result<OpenResult, String> {
    let out = create_instance_doc(&app, &app_id, title)?;
    do_open_clan(out.display().to_string(), &state)
}

// ── The home page, as a CLAN file ───────────────────────────────────────────
//
// The launcher itself is an authored CLAN app rendered by the host like any
// other. Its content drives the host purely through the clan:// API: it lists
// installed apps (GET clan://apps) and launches one (POST clan://launch), which
// instantiates another .clan and tells the shell to open it. This proves a
// click inside one CLAN file can reliably launch another.

const HOME_APP_HTML: &str = include_str!("home_app.html");

/// Build the home CLAN template (idempotent) and return its path. Stored in the
/// app data dir; rebuilt whenever the embedded HTML changes (keyed by a version
/// tag in the filename) so edits to home_app.html ship on next launch.
fn ensure_home_clan(app: &tauri::AppHandle) -> std::result::Result<PathBuf, String> {
    // Bump the suffix when HOME_APP_HTML changes to force a rebuild.
    let dir = app
        .path()
        .app_data_dir()
        .map_err(|e| e.to_string())?;
    std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    let path = dir.join("home-v6.clan");
    if path.exists() {
        return Ok(path);
    }
    let base = create(CreateOptions {
        title: "Napkin Studio".into(),
        brief: "Napkin Studio home".into(),
        document_type: None,
        no_render: false,
        schema: None,
    })
    .map_err(|e| e.to_string())?;
    let clan = ClanFile::from_bytes(base).map_err(|e| e.to_string())?;
    // Swap in the authored home HTML.
    let mut b = ClanBuilder::new(clan.manifest().clone());
    for (p, by) in clan.read_all_entries().map_err(|e| e.to_string())? {
        if p == "manifest.yaml" || p == "human/index.html" {
            continue;
        }
        b.add_entry(p, by);
    }
    b.add_entry("human/index.html", HOME_APP_HTML.as_bytes().to_vec());
    let with_html = ClanFile::from_bytes(b.build().map_err(|e| e.to_string())?)
        .map_err(|e| e.to_string())?;
    let tpl = make_template(
        &with_html,
        AppInfo {
            name: "Napkin Studio".into(),
            app_id: "ie.napkin.home".into(),
            version: "1.0.0".into(),
            icon: None,
            entry: "human/index.html".into(),
            schema: Some("agent/output-schema.json".into()),
            prompt_templates: vec![],
            data_seed: None,
        },
        MakeTemplateOptions::default(),
    )
    .map_err(|e| e.to_string())?;
    std::fs::write(&path, tpl).map_err(|e| e.to_string())?;
    Ok(path)
}

/// Open the home CLAN app as the current document (the React shell then pulls
/// its rendered HTML via `get_human_html`). Returns the OpenResult.
#[tauri::command]
fn open_home(app: tauri::AppHandle, state: State<AppState>) -> Result<OpenResult, String> {
    let path = ensure_home_clan(&app)?;
    do_open_clan(path.display().to_string(), &state)
}

/// Save (export) the current open `.clan` to a chosen path — the single-file
/// handoff. Edits already persist in place on every write; this copies the
/// artifact somewhere shareable. The frontend supplies the path from a native
/// save dialog.
#[tauri::command]
fn save_clan_to(path: String, state: State<AppState>) -> Result<(), String> {
    let guard = state.current.lock().unwrap();
    let loaded = guard.as_ref().ok_or("no file open")?;
    std::fs::write(&path, loaded.clan.raw_bytes()).map_err(|e| e.to_string())
}

// ── Home-screen agent prompt ─────────────────────────────────────────────────
//
// The home page sends the user's prompt to ONE uniform agent endpoint. The base
// URL has a single source of truth so it can point at a localhost dev server now
// and a hosted agent later with a one-value change — no code edits, no CSP
// fuss (the host makes the request, not the sandboxed webview).
//   precedence: NAPKIN_AGENT_URL env  →  workspace.yaml `agent_url`  →  default

const DEFAULT_AGENT_URL: &str = "http://localhost:8787";

fn agent_base_url(app: &tauri::AppHandle) -> String {
    if let Some(v) = std::env::var_os("NAPKIN_AGENT_URL") {
        if let Ok(s) = v.into_string() {
            if !s.trim().is_empty() {
                return s;
            }
        }
    }
    if let Ok(cfg) = load_workspace_config(app) {
        if let Some(u) = cfg.agent_url {
            if !u.trim().is_empty() {
                return u;
            }
        }
    }
    DEFAULT_AGENT_URL.to_string()
}

/// The endpoint the "agent" kind currently resolves to (for display in the UI).
#[tauri::command]
fn agent_endpoint(app: tauri::AppHandle) -> String {
    resolve_proxy(&app, "agent").0
}

/// Home-screen prompt → the unified proxy with `request_kind = "agent"`.
#[tauri::command]
async fn agent_prompt(app: tauri::AppHandle, text: String) -> Result<Value, String> {
    Ok(proxy_call(&app, "agent", serde_json::json!({ "input": text })).await)
}

fn main() {
    // On Linux the AppImage bundles an older WebKitGTK whose default DMABUF /
    // accelerated-compositing path fails on modern Mesa/Wayland systems — the web
    // content stays blank (and the bundled libwayland mismatch can even abort with
    // "EGL_BAD_PARAMETER" before the window appears; the AppImage build also strips
    // its bundled libwayland so the host's is used). Forcing the software path keeps
    // rendering working everywhere. Only set these if the user hasn't overridden them.
    #[cfg(target_os = "linux")]
    {
        for key in ["WEBKIT_DISABLE_DMABUF_RENDERER", "WEBKIT_DISABLE_COMPOSITING_MODE"] {
            if std::env::var_os(key).is_none() {
                std::env::set_var(key, "1");
            }
        }
    }

    // The OS launches us with the clicked file as an argument; stash it so the
    // frontend can pull it once it's ready.
    let launch_file = clan_path_from_args(std::env::args());

    tauri::Builder::default()
        // Must be the first plugin. When a second instance is started (e.g. the
        // user double-clicks another .clan file while the viewer is open), this
        // re-focuses our window and forwards the new path instead of opening a
        // duplicate window.
        .plugin(tauri_plugin_single_instance::init(|app, argv, _cwd| {
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.set_focus();
            }
            if let Some(path) = clan_path_from_args(argv) {
                let _ = app.emit("open-file", path);
            }
        }))
        // The full clan:// API surface. One async handler, exact-path routing
        // (uri.contains would confuse /patch with /patch-data). api-proxy is the
        // only network route and is spawned so the WebView loop never blocks.
        .register_asynchronous_uri_scheme_protocol("clan", |ctx, request, responder| {
            let app = ctx.app_handle().clone();
            let path = request.uri().path().to_string();
            let query = request.uri().query().unwrap_or("").to_string();

            // Network routes are spawned so the WebView loop never blocks.
            if path == "/api-proxy" {
                let body = String::from_utf8(request.body().clone()).unwrap_or_default();
                tauri::async_runtime::spawn(async move {
                    let resp = do_api_proxy(app, body).await;
                    responder.respond(resp);
                });
                return;
            }
            let state = app.state::<AppState>();
            let st: &AppState = state.inner();

            let resp: tauri::http::Response<Vec<u8>> = match path.as_str() {
                "/edit-mode" => {
                    let mode = *st.edit_mode.lock().unwrap();
                    tauri::http::Response::builder()
                        .header("Access-Control-Allow-Origin", "*")
                        .status(200)
                        .body(if mode { b"true".to_vec() } else { b"false".to_vec() })
                        .unwrap()
                }
                "/document" => {
                    let html = st.preview_html.lock().unwrap().clone();
                    tauri::http::Response::builder()
                        .header("Content-Type", "text/html")
                        .header("Access-Control-Allow-Origin", "*")
                        .status(200)
                        .body(html.into_bytes())
                        .unwrap()
                }
                "/snapshot" => {
                    if let Ok(html) = String::from_utf8(request.body().clone()) {
                        let _ = do_snapshot(html, st);
                    }
                    tauri::http::Response::builder()
                        .header("Access-Control-Allow-Origin", "*")
                        .status(200)
                        .body(Vec::new())
                        .unwrap()
                }
                "/patch" => {
                    if let Ok(body_str) = String::from_utf8(request.body().clone()) {
                        if let Some(payload) = handle_patch_request(&body_str, st) {
                            let _ = app.emit("clan-patch-saved", payload);
                        }
                    }
                    tauri::http::Response::builder()
                        .header("Access-Control-Allow-Origin", "*")
                        .status(200)
                        .body(Vec::new())
                        .unwrap()
                }
                "/patch-data" => {
                    let body = String::from_utf8(request.body().clone()).unwrap_or_default();
                    match do_patch_data(&body, st) {
                        Ok(v) => {
                            let _ = app.emit("clan-data-changed", v.clone());
                            json_resp(200, &v)
                        }
                        Err((c, m)) => err_resp(c, &m),
                    }
                }
                "/fork" => {
                    let body = String::from_utf8(request.body().clone()).unwrap_or_default();
                    match do_fork(&body, st) {
                        Ok(v) => json_resp(200, &v),
                        Err((c, m)) => err_resp(c, &m),
                    }
                }
                "/upload-asset" => {
                    let name = query_param(&query, "name").unwrap_or_default();
                    let agent = query_param(&query, "agent");
                    match do_upload_asset(&name, agent.as_deref(), request.body().clone(), st) {
                        Ok(v) => json_resp(200, &v),
                        Err((c, m)) => err_resp(c, &m),
                    }
                }
                "/chain" => match do_chain_json(st) {
                    Ok(v) => json_resp(200, &v),
                    Err((c, m)) => err_resp(c, &m),
                },
                // Launcher routes — let a home CLAN app list and launch apps.
                "/apps" => json_resp(200, &serde_json::json!(scan_apps(&app))),
                "/recent" => json_resp(200, &serde_json::json!(scan_recent(&app))),
                "/open" => {
                    let v: Value = serde_json::from_str(
                        &String::from_utf8(request.body().clone()).unwrap_or_default(),
                    )
                    .unwrap_or(Value::Null);
                    match v.get("path").and_then(|x| x.as_str()) {
                        Some(p) => {
                            let _ = app.emit("clan-open-document", p.to_string());
                            json_resp(200, &serde_json::json!({ "ok": true }))
                        }
                        None => err_resp(400, "missing path"),
                    }
                }
                "/agent-endpoint" => json_resp(
                    200,
                    &serde_json::json!({ "endpoint": agent_base_url(&app) }),
                ),
                "/launch" => {
                    let body = String::from_utf8(request.body().clone()).unwrap_or_default();
                    let v: Value = serde_json::from_str(&body).unwrap_or(Value::Null);
                    let app_id = v.get("app_id").and_then(|x| x.as_str()).unwrap_or("");
                    let title = v
                        .get("title")
                        .and_then(|x| x.as_str())
                        .map(String::from);
                    if app_id.is_empty() {
                        err_resp(400, "missing app_id")
                    } else {
                        match create_instance_doc(&app, app_id, title) {
                            Ok(p) => {
                                // Tell the shell to open the freshly created .clan.
                                let _ = app.emit("clan-open-document", p.display().to_string());
                                json_resp(
                                    200,
                                    &serde_json::json!({ "ok": true, "path": p.display().to_string() }),
                                )
                            }
                            Err(m) => err_resp(422, &m),
                        }
                    }
                }
                "/open-file" => {
                    // The host owns the file dialog; ask the shell to run it.
                    let _ = app.emit("clan-open-file-request", ());
                    json_resp(200, &serde_json::json!({ "ok": true }))
                }
                "/set-title" => {
                    let v: Value = serde_json::from_str(
                        &String::from_utf8(request.body().clone()).unwrap_or_default(),
                    )
                    .unwrap_or(Value::Null);
                    match v.get("title").and_then(|x| x.as_str()) {
                        Some(t) if !t.trim().is_empty() => match do_set_title(t, st) {
                            Ok(r) => {
                                let _ = app.emit("clan-title-changed", t.to_string());
                                json_resp(200, &r)
                            }
                            Err((c, m)) => err_resp(c, &m),
                        },
                        _ => err_resp(400, "missing title"),
                    }
                }
                "/request-save" => {
                    // A doc (e.g. a locked brief) asks the shell to export/save it.
                    let _ = app.emit("clan-request-save", ());
                    json_resp(200, &serde_json::json!({ "ok": true }))
                }
                "/set-context" => {
                    let v: Value = serde_json::from_str(
                        &String::from_utf8(request.body().clone()).unwrap_or_default(),
                    )
                    .unwrap_or(Value::Null);
                    let md = v.get("markdown").and_then(|x| x.as_str()).unwrap_or("");
                    let append = v.get("append").and_then(|x| x.as_bool()).unwrap_or(false);
                    if md.trim().is_empty() {
                        err_resp(400, "missing markdown")
                    } else {
                        match do_set_context(md, append, st) {
                            Ok(r) => json_resp(200, &r),
                            Err((c, m)) => err_resp(c, &m),
                        }
                    }
                }
                // Scoped host capabilities — only for trusted (signed) apps.
                "/capabilities" => {
                    let trusted = st
                        .current
                        .lock()
                        .unwrap()
                        .as_ref()
                        .map(|c| c.trusted)
                        .unwrap_or(false);
                    let allowed: Vec<&str> = if trusted {
                        TRUSTED_CAPABILITIES.to_vec()
                    } else {
                        vec![]
                    };
                    json_resp(200, &serde_json::json!({ "trusted": trusted, "allowed": allowed }))
                }
                "/notify" => {
                    let trusted = st
                        .current
                        .lock()
                        .unwrap()
                        .as_ref()
                        .map(|c| c.trusted)
                        .unwrap_or(false);
                    if !trusted {
                        err_resp(403, "capability 'notify' requires a signed (trusted) app")
                    } else {
                        let v: Value = serde_json::from_str(
                            &String::from_utf8(request.body().clone()).unwrap_or_default(),
                        )
                        .unwrap_or(Value::Null);
                        let _ = app.emit(
                            "napkin-notify",
                            serde_json::json!({
                                "title": v.get("title").and_then(|x| x.as_str()).unwrap_or("Napkin"),
                                "body": v.get("body").and_then(|x| x.as_str()).unwrap_or(""),
                            }),
                        );
                        json_resp(200, &serde_json::json!({ "ok": true }))
                    }
                }
                "/set-theme" => {
                    // Recolor the viewer chrome — a scoped capability only a
                    // signed (trusted) app may use.
                    let trusted = st
                        .current
                        .lock()
                        .unwrap()
                        .as_ref()
                        .map(|c| c.trusted)
                        .unwrap_or(false);
                    if !trusted {
                        err_resp(403, "capability 'set-theme' requires a signed (trusted) app")
                    } else {
                        let v: Value = serde_json::from_str(
                            &String::from_utf8(request.body().clone()).unwrap_or_default(),
                        )
                        .unwrap_or(Value::Null);
                        // Pass the theme object straight through to the shell.
                        let _ = app.emit("clan-theme-changed", v);
                        json_resp(200, &serde_json::json!({ "ok": true }))
                    }
                }
                p if p.starts_with("/assets/") => {
                    let rel = p.strip_prefix("/assets/").unwrap_or("");
                    match do_serve_asset(rel, st) {
                        Ok((ct, bytes)) => tauri::http::Response::builder()
                            .header("Content-Type", ct)
                            .header("Cache-Control", "no-cache")
                            .header("Access-Control-Allow-Origin", "*")
                            .status(200)
                            .body(bytes)
                            .unwrap(),
                        Err((c, m)) => err_resp(c, &m),
                    }
                }
                _ => tauri::http::Response::builder()
                    .status(404)
                    .body(Vec::new())
                    .unwrap(),
            };
            responder.respond(resp);
        })
        .plugin(tauri_plugin_dialog::init())
        .manage(AppState {
            current: Mutex::new(None),
            edit_mode: Mutex::new(false),
            preview_html: Mutex::new(String::new()),
            pending_open: Mutex::new(launch_file),
        })
        .invoke_handler(tauri::generate_handler![
            open_clan, get_human_html, get_data, get_chain, get_agent_state, get_context,
            save_patch, set_edit_mode, update_preview_html, take_launch_file,
            list_apps, install_app, new_document_from_app, agent_prompt, agent_endpoint,
            open_home, save_clan_to
        ])
        .run(tauri::generate_context!())
        .expect("error while running Napkin Studio OS");
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Serialises tests that assert on the global SAVE_COUNT, so one test's
    /// saves never land inside another's before/after delta window.
    static SAVE_TEST_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

    fn empty_state() -> AppState {
        AppState {
            current: Mutex::new(None),
            edit_mode: Mutex::new(false),
            preview_html: Mutex::new(String::new()),
            pending_open: Mutex::new(None),
        }
    }

    /// Create a real .clan file on disk and open it into a fresh AppState.
    fn open_temp_clan() -> (tempfile::TempDir, AppState) {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("test.clan");
        let bytes = clan_sdk::create(clan_sdk::CreateOptions {
            title: "Viewer Test".into(),
            brief: "test brief".into(),
            document_type: None,
            no_render: false,
            schema: None,
        })
        .unwrap();
        std::fs::write(&path, bytes).unwrap();

        let state = empty_state();
        do_open_clan(path.display().to_string(), &state).unwrap();
        (dir, state)
    }

    // Regression for #10: open_clan must not read the file from disk twice.
    // The loaded ClanFile's own raw bytes are the single source of truth and
    // must match what is on disk.
    #[test]
    fn open_clan_populates_state_from_single_read() {
        let (dir, state) = open_temp_clan();
        let path = dir.path().join("test.clan");

        let guard = state.current.lock().unwrap();
        let loaded = guard.as_ref().expect("state must hold the opened file");
        assert_eq!(loaded.clan.manifest().title, "Viewer Test");
        assert_eq!(
            loaded.clan.raw_bytes(),
            std::fs::read(&path).unwrap().as_slice(),
            "in-memory archive must match the file on disk"
        );
    }

    // Regression for #9: one clan://patch request must produce exactly one
    // save (one repack + one disk write), with the emitted payload echoing
    // the edit. The frontend listener must never save again.
    #[test]
    fn patch_request_saves_exactly_once() {
        let _guard = SAVE_TEST_LOCK.lock().unwrap();
        let (dir, state) = open_temp_clan();
        let path = dir.path().join("test.clan");

        let before = SAVE_COUNT.load(std::sync::atomic::Ordering::SeqCst);
        let payload = handle_patch_request(
            r#"{"id":"heading-0","content":"Edited Title"}"#,
            &state,
        )
        .expect("valid patch body must save and return a payload");
        let after = SAVE_COUNT.load(std::sync::atomic::Ordering::SeqCst);

        assert_eq!(after - before, 1, "a single edit must save exactly once");
        assert_eq!(payload["id"], "heading-0");
        assert_eq!(payload["content"], "Edited Title");

        // The patch landed on disk exactly once.
        let on_disk = ClanFile::open(&path).unwrap();
        let patches = on_disk.read_entry_string("human/patches.yaml").unwrap();
        assert_eq!(patches.matches("heading-0").count(), 1);
        assert!(patches.contains("Edited Title"));
    }

    #[test]
    fn patch_request_rejects_malformed_bodies() {
        let _guard = SAVE_TEST_LOCK.lock().unwrap();
        let (_dir, state) = open_temp_clan();
        let before = SAVE_COUNT.load(std::sync::atomic::Ordering::SeqCst);
        assert!(handle_patch_request("not json", &state).is_none());
        assert!(handle_patch_request(r#"{"id":"x"}"#, &state).is_none());
        let after = SAVE_COUNT.load(std::sync::atomic::Ordering::SeqCst);
        assert_eq!(after, before, "malformed bodies must not trigger saves");
    }

    // F4: re-saving identical content for the same id is a no-op — the blur
    // bridge can fire on focus-without-change, and that must not churn the file.
    #[test]
    fn resaving_identical_content_is_a_noop() {
        let _guard = SAVE_TEST_LOCK.lock().unwrap();
        let (dir, state) = open_temp_clan();
        let path = dir.path().join("test.clan");

        // First save lands.
        do_save_patch("heading-0".into(), "Same Title".into(), &state).unwrap();
        let after_first = std::fs::read(&path).unwrap();
        let count_after_first = SAVE_COUNT.load(std::sync::atomic::Ordering::SeqCst);

        // Identical re-save: no rewrite, no SAVE_COUNT increment, bytes unchanged.
        do_save_patch("heading-0".into(), "Same Title".into(), &state).unwrap();
        assert_eq!(
            SAVE_COUNT.load(std::sync::atomic::Ordering::SeqCst),
            count_after_first,
            "identical re-save must be skipped (F4)"
        );
        assert_eq!(after_first, std::fs::read(&path).unwrap(), "file must be byte-identical");

        // A genuine change still writes.
        do_save_patch("heading-0".into(), "Changed Title".into(), &state).unwrap();
        assert!(
            SAVE_COUNT.load(std::sync::atomic::Ordering::SeqCst) > count_after_first,
            "a real edit must still save"
        );
        let on_disk = ClanFile::open(&path).unwrap();
        assert!(on_disk
            .read_entry_string("human/patches.yaml")
            .unwrap()
            .contains("Changed Title"));
    }

    // --- #19: resolve_bindings ---

    fn yaml(src: &str) -> serde_yaml::Value {
        serde_yaml::from_str(src).unwrap()
    }

    #[test]
    fn resolve_bindings_substitutes_keys() {
        let data = yaml("vendor: Acme\nmeta:\n  total: 42\nitems:\n  - first\n  - second\n");
        assert_eq!(
            resolve_bindings("<p>{{vendor}} owes {{meta.total}} for {{items.1}}</p>", &data),
            "<p>Acme owes 42 for second</p>"
        );
    }

    #[test]
    fn resolve_bindings_keeps_unknown_keys_verbatim() {
        let data = yaml("vendor: Acme\n");
        assert_eq!(resolve_bindings("{{nope}} {{vendor}}", &data), "{{nope}} Acme");
    }

    #[test]
    fn resolve_bindings_preserves_unterminated_braces() {
        let data = yaml("vendor: Acme\n");
        assert_eq!(resolve_bindings("a {{vendor}} b {{open", &data), "a Acme b {{open");
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
        assert!(out.contains(r#"<h1 data-adf-id="auto-h1-0">A</h1>"#), "{out}");
        assert!(out.contains(r#"<p data-adf-id="auto-p-0">b</p>"#), "{out}");
        assert!(out.contains(r#"<p data-adf-id="auto-p-1">c</p>"#), "{out}");
        assert!(out.contains(r#"<li data-adf-id="auto-li-0">d</li>"#), "{out}");
        assert!(out.contains(r#"<td data-adf-id="auto-td-0">e</td>"#), "{out}");
        assert!(out.contains(r#"<th data-adf-id="auto-th-0">f</th>"#), "{out}");
    }

    #[test]
    fn auto_inject_ids_respects_existing_ids_and_boundaries() {
        let html = r#"<p data-adf-id="mine">keep</p><pre>not a p</pre><p class="x">tag</p>"#;
        let out = auto_inject_adf_ids(html);
        assert!(out.contains(r#"<p data-adf-id="mine">keep</p>"#), "{out}");
        assert!(out.contains("<pre>not a p</pre>"), "<pre> must not be treated as <p>: {out}");
        assert!(out.contains(r#"<p class="x" data-adf-id="auto-p-0">tag</p>"#), "{out}");
    }

    #[test]
    fn auto_inject_ids_skips_script_blocks() {
        let html = "<script>const t = `<p>${row}</p>`;</script><p>real</p>";
        let out = auto_inject_adf_ids(html);
        assert!(out.contains("`<p>${row}</p>`"), "script content must be untouched: {out}");
        assert!(out.contains(r#"<p data-adf-id="auto-p-0">real</p>"#), "{out}");
    }

    #[test]
    fn auto_inject_ids_is_stable_and_idempotent() {
        let html = "<p>a</p><p>b</p>";
        let once = auto_inject_adf_ids(html);
        assert_eq!(once, auto_inject_adf_ids(&once), "second pass must change nothing");
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
    mod oracle_old_impl {
        pub fn old_auto_inject_adf_ids(html: &str) -> String {
            const EDITABLE: &[&str] = &["h1","h2","h3","h4","h5","h6","p","li","td","th"];
            let mut result = html.to_string();
            for tag in EDITABLE { result = old_inject_ids_for_tag(&result, tag); }
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
                    let end = lower[pos..].find("</script>").map(|r| pos + r + "</script>".len()).unwrap_or(html.len());
                    out.push_str(&html[pos..end]); pos = end; continue;
                }
                let Some(rel) = html[pos..].find(open.as_str()) else { out.push_str(&html[pos..]); break; };
                let tag_start = pos + rel;
                let after_name = tag_start + open.len();
                if lower[..tag_start].rfind("<script").map_or(false, |s| lower[s..tag_start].find("</script>").is_none()) {
                    out.push_str(&html[pos..after_name]); pos = after_name; continue;
                }
                let next = html.as_bytes().get(after_name).copied().unwrap_or(0);
                if !matches!(next, b' ' | b'\t' | b'\n' | b'\r' | b'>') {
                    out.push_str(&html[pos..after_name]); pos = after_name; continue;
                }
                let Some(rel_end) = html[tag_start..].find('>') else { out.push_str(&html[pos..]); break; };
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
            "<p>", "</p>", "<P>", "<p", "p>", "<p ", "<p\n", "<p\t>", "<p/>", "<p />",
            "<li>", "<Li>", "<li ", "</li>", "<LI>", "<li\n>",
            "<h1>", "<h1", "<h1x>", "<H1>", "<h2 class=\"a>b\">", "<h6\n class='c'>",
            "<h3>", "</h3>", "<h10>",
            "<td>", "<td/>", "<td />", "<th>", "<thead>", "<TD>", "</td>",
            "<pre>", "</pre>",
            "<script>", "</script>", "<script src=\"x\">", "<script", "</script",
            "<SCRIPT>", "</SCRIPT>", "<script b=\"",
            "data-adf-id=\"x\"", " data-adf-id='y'", "data-adf-idx",
            "<!-- <p> -->", "<!--", "-->",
            "text", " ", "\n", "\t", "\"", "'", ">", "<", "/>", "=", "/",
            "é", "✓", "É", "«»",
            "title=\"a<p>b\"", "a=\"<script\"", "a=\"</script>\"", "b=\"", "<p a=\"",
            "<td title=\"a<p>b\">", "<li title=\"<h1>\">",
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
            assert_eq!(new, old, "fuzz divergence (round {round}) on soup: {soup:?}");
        }
    }

    // --- clan:// API routes ---

    #[test]
    fn patch_data_writes_data_and_attributed_decision() {
        let (dir, state) = open_temp_clan();
        let path = dir.path().join("test.clan");

        let body = r#"{"patch":{"verdict":"HubSpot"},"agent":"human","action":"set verdict","rationale":"best fit","pinned":true}"#;
        let out = do_patch_data(body, &state).expect("patch-data should succeed");
        assert_eq!(out["ok"], true);
        assert_eq!(out["keys"][0], "verdict");

        let on_disk = ClanFile::open(&path).unwrap();
        let data = on_disk.read_entry_string("shared/data.yaml").unwrap();
        assert!(data.contains("HubSpot"), "data layer updated: {data}");
        let chain = on_disk.read_entry_string("agent/decision-chain.yaml").unwrap();
        assert!(chain.contains("human"), "decision attributed to human: {chain}");
        assert!(chain.contains("verdict"), "fields_changed records the key: {chain}");
    }

    #[test]
    fn patch_data_rejects_non_object_patch() {
        let (_dir, state) = open_temp_clan();
        assert!(do_patch_data(r#"{"patch":"nope"}"#, &state).is_err());
        assert!(do_patch_data("not json", &state).is_err());
    }

    #[test]
    fn patch_data_noop_skips_unchanged_write() {
        let (dir, state) = open_temp_clan();
        let path = dir.path().join("test.clan");
        let body = r#"{"patch":{"verdict":"HubSpot"},"agent":"human","action":"set"}"#;
        do_patch_data(body, &state).unwrap();
        let chain1 = ClanFile::open(&path)
            .unwrap()
            .read_entry_string("agent/decision-chain.yaml")
            .unwrap();
        // Same value again → no-op: no rewrite, no new decision.
        let res = do_patch_data(body, &state).unwrap();
        assert_eq!(res["noop"], true, "unchanged patch must be a no-op");
        let chain2 = ClanFile::open(&path)
            .unwrap()
            .read_entry_string("agent/decision-chain.yaml")
            .unwrap();
        assert_eq!(chain1, chain2, "no-op must not append a decision");
    }

    #[test]
    fn serve_asset_rejects_traversal() {
        let (_dir, state) = open_temp_clan();
        assert_eq!(do_serve_asset("../manifest.yaml", &state).unwrap_err().0, 400);
        assert_eq!(do_serve_asset("/etc/passwd", &state).unwrap_err().0, 400);
        // A missing-but-safe path is a 404, not a 400.
        assert_eq!(do_serve_asset("logo.png", &state).unwrap_err().0, 404);
    }

    #[test]
    fn sanitize_asset_name_blocks_separators() {
        assert!(sanitize_asset_name("logo.png").is_some());
        assert!(sanitize_asset_name("../x").is_none());
        assert!(sanitize_asset_name("a/b.png").is_none());
        assert!(sanitize_asset_name("a\\b.png").is_none());
        assert!(sanitize_asset_name("  ").is_none());
    }
}
