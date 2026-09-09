// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! Napkin Studio OS — the desktop shell.
//!
//! All of the host logic lives in `napkin-host`: opening a document, the
//! `clan://` API, the app library, export, the inference proxy. This binary is
//! the adapter that binds it to Tauri — commands in, a custom URI scheme in,
//! [`HostEvent`]s out as Tauri events — so the same handlers can serve HTTP
//! from a server binary without either copy drifting from the other.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

use std::sync::{Arc, Mutex, OnceLock};

use napkin_host::{
    export, library, proxy, session::Session, DocId, FsConfig, FsStore, HostRequest, HostResponse,
    InstalledApp, OpenResult,
};
use serde_json::Value;
use tauri::{Emitter, Manager, State};

/// The host, once the per-user directories are known. Built lazily because
/// resolving them needs an `AppHandle`, which only exists after the builder runs.
struct HostCtx {
    session: Session,
    config: FsConfig,
}

struct AppState {
    host: OnceLock<HostCtx>,
    // A `.clan` path the OS handed us at launch (double-click / "Open with"),
    // waiting for the frontend to pull it via `take_launch_file`.
    pending_open: Mutex<Option<String>>,
}

impl AppState {
    fn new(pending_open: Option<String>) -> Self {
        Self {
            host: OnceLock::new(),
            pending_open: Mutex::new(pending_open),
        }
    }

    fn host(&self, app: &tauri::AppHandle) -> &HostCtx {
        self.host.get_or_init(|| {
            let data_dir = app.path().app_data_dir().unwrap_or_default();
            let config_dir = app.path().app_config_dir().unwrap_or_default();
            HostCtx {
                session: Session::new(Arc::new(FsStore::new(data_dir))),
                config: FsConfig::new(config_dir),
            }
        })
    }
}

/// The host for this app handle, built on first use. The reference borrows
/// from `app`, whose state manager owns it for the life of the process.
fn host(app: &tauri::AppHandle) -> &HostCtx {
    let state: State<'_, AppState> = app.state();
    state.inner().host(app)
}

/// Pick the first `.clan` file path out of a set of process arguments.
/// Works for both our own launch args and the argv a second instance is
/// started with; the executable path and any flags are ignored since they
/// don't end in `.clan`.
fn clan_path_from_args<I: IntoIterator<Item = String>>(args: I) -> Option<String> {
    args.into_iter()
        .find(|a| a.to_lowercase().ends_with(".clan"))
}

// ── Commands ─────────────────────────────────────────────────────────────────

#[tauri::command]
fn open_clan(app: tauri::AppHandle, path: String) -> Result<OpenResult, String> {
    host(&app)
        .session
        .open(DocId::new(path))
        .map_err(String::from)
}

/// Returns (and clears) the `.clan` path the app was launched with, if any.
/// The frontend calls this once on mount to open a double-clicked file.
#[tauri::command]
fn take_launch_file(state: State<AppState>) -> Option<String> {
    state.pending_open.lock().unwrap().take()
}

#[tauri::command]
fn get_human_html(app: tauri::AppHandle) -> Result<String, String> {
    host(&app).session.human_html().map_err(String::from)
}

#[tauri::command]
fn get_data(app: tauri::AppHandle) -> Result<String, String> {
    host(&app)
        .session
        .entry_string("shared/data.yaml")
        .map_err(String::from)
}

#[tauri::command]
fn get_chain(app: tauri::AppHandle) -> Result<String, String> {
    host(&app)
        .session
        .entry_string("agent/decision-chain.yaml")
        .map_err(String::from)
}

#[tauri::command]
fn get_agent_state(app: tauri::AppHandle) -> Result<String, String> {
    host(&app)
        .session
        .entry_string("agent/state.yaml")
        .map_err(String::from)
}

#[tauri::command]
fn get_context(app: tauri::AppHandle) -> Result<String, String> {
    host(&app)
        .session
        .entry_string("agent/context.md")
        .map_err(String::from)
}

#[tauri::command]
fn set_edit_mode(app: tauri::AppHandle, active: bool) {
    host(&app).session.set_edit_mode(active);
}

#[tauri::command]
fn update_preview_html(app: tauri::AppHandle, html: String) {
    host(&app).session.set_preview_html(html);
}

#[tauri::command]
fn save_patch(app: tauri::AppHandle, id: String, content: String) -> Result<(), String> {
    host(&app)
        .session
        .save_patch(id, content)
        .map_err(String::from)
}

#[tauri::command]
fn list_apps(app: tauri::AppHandle) -> Result<Vec<InstalledApp>, String> {
    Ok(library::scan_apps(&**host(&app).session.store()))
}

#[tauri::command]
fn install_app(app: tauri::AppHandle, src_path: String) -> Result<InstalledApp, String> {
    let bytes = std::fs::read(&src_path).map_err(|e| e.to_string())?;
    library::install_app(&**host(&app).session.store(), bytes).map_err(String::from)
}

#[tauri::command]
fn new_document_from_app(
    app: tauri::AppHandle,
    app_id: String,
    title: Option<String>,
) -> Result<OpenResult, String> {
    let ctx = host(&app);
    let id =
        library::create_instance(&**ctx.session.store(), &app_id, title).map_err(String::from)?;
    ctx.session.open(id).map_err(String::from)
}

/// Open the home CLAN app as the current document (the React shell then pulls
/// its rendered HTML via `get_human_html`).
#[tauri::command]
fn open_home(app: tauri::AppHandle) -> Result<OpenResult, String> {
    let ctx = host(&app);
    let id = library::ensure_home(&**ctx.session.store()).map_err(String::from)?;
    ctx.session.open(id).map_err(String::from)
}

/// Save (export) the current open `.clan` to a chosen path — the single-file
/// handoff. Edits already persist in place on every write; this copies the
/// artifact somewhere shareable. The frontend supplies the path from a native
/// save dialog.
#[tauri::command]
fn save_clan_to(app: tauri::AppHandle, path: String) -> Result<(), String> {
    let bytes = host(&app).session.raw_bytes().map_err(String::from)?;
    std::fs::write(&path, bytes).map_err(|e| e.to_string())
}

/// Host-owned export: compose a standalone document from the open `.clan` via
/// the SDK, then hand it to the same shell save-dialog + `finish_export` flow
/// the app-pushed path uses. This is the uniform export every studio inherits —
/// the app no longer needs to build the HTML.
#[tauri::command]
fn export_current(
    app: tauri::AppHandle,
    kind: String,
    provenance: bool,
    no_brand: bool,
) -> Result<(), String> {
    let kind = if kind == "pdf" { "pdf" } else { "html" };
    let (html, filename) = host(&app)
        .session
        .compose_export(provenance, no_brand)
        .map_err(String::from)?;
    let tmp = export::write_temp_html(&html).map_err(String::from)?;
    app.emit(
        "clan-export-request",
        serde_json::json!({ "kind": kind, "filename": filename, "tmpHtml": tmp }),
    )
    .map_err(|e| e.to_string())
}

#[tauri::command]
fn finish_export(kind: String, tmp_html: String, dest: String) -> Result<String, String> {
    export::finish_export(&kind, &tmp_html, &dest).map_err(String::from)
}

/// The endpoint the "agent" kind currently resolves to (for display in the UI).
#[tauri::command]
fn agent_endpoint(app: tauri::AppHandle) -> String {
    napkin_host::resolve_proxy(&host(&app).config, "agent").0
}

/// Home-screen prompt → the unified proxy with `request_kind = "agent"`.
#[tauri::command]
async fn agent_prompt(app: tauri::AppHandle, text: String) -> Result<Value, String> {
    Ok(proxy::agent_prompt(&host(&app).config, &text).await)
}

// ── clan:// adapter ──────────────────────────────────────────────────────────

fn to_tauri(resp: HostResponse) -> tauri::http::Response<Vec<u8>> {
    let mut builder = tauri::http::Response::builder().status(resp.status);
    for (k, v) in &resp.headers {
        builder = builder.header(k, v);
    }
    builder.body(resp.body).unwrap()
}

/// Hand the shell whatever the route asked for. Event names are the host's, so
/// the frontend listener list and the route table stay in step by construction.
fn emit_events(app: &tauri::AppHandle, resp: &HostResponse) {
    for e in &resp.events {
        let _ = app.emit(e.name(), e.payload());
    }
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
        for key in [
            "WEBKIT_DISABLE_DMABUF_RENDERER",
            "WEBKIT_DISABLE_COMPOSITING_MODE",
        ] {
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
        // The full clan:// API surface. One handler, one routing table (in
        // napkin-host). Network routes are spawned so the WebView loop never blocks.
        .register_asynchronous_uri_scheme_protocol("clan", |ctx, request, responder| {
            let app = ctx.app_handle().clone();
            let req = HostRequest::new(
                request.uri().path().to_string(),
                request.uri().query().unwrap_or("").to_string(),
                request.body().clone(),
            );

            if napkin_host::is_async(&req.path) {
                tauri::async_runtime::spawn(async move {
                    let ctx = host(&app);
                    let resp = napkin_host::handle_async(&ctx.session, &ctx.config, req).await;
                    emit_events(&app, &resp);
                    responder.respond(to_tauri(resp));
                });
                return;
            }

            let ctx = host(&app);
            let resp = napkin_host::handle(&ctx.session, &ctx.config, req);
            emit_events(&app, &resp);
            responder.respond(to_tauri(resp));
        })
        .plugin(tauri_plugin_dialog::init())
        .manage(AppState::new(launch_file))
        .invoke_handler(tauri::generate_handler![
            open_clan,
            get_human_html,
            get_data,
            get_chain,
            get_agent_state,
            get_context,
            save_patch,
            set_edit_mode,
            update_preview_html,
            take_launch_file,
            list_apps,
            install_app,
            new_document_from_app,
            agent_prompt,
            agent_endpoint,
            open_home,
            save_clan_to,
            finish_export,
            export_current
        ])
        .run(tauri::generate_context!())
        .expect("error while running Napkin Studio OS");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clan_path_from_args_picks_the_clan_file() {
        let argv = ["/usr/bin/napkin", "--flag", "/home/u/My Brief.CLAN"];
        assert_eq!(
            clan_path_from_args(argv.iter().map(|s| s.to_string())),
            Some("/home/u/My Brief.CLAN".to_string())
        );
    }

    #[test]
    fn clan_path_from_args_ignores_everything_else() {
        let argv = ["/usr/bin/napkin", "--clan", "notes.txt"];
        assert_eq!(
            clan_path_from_args(argv.iter().map(|s| s.to_string())),
            None
        );
    }
}
