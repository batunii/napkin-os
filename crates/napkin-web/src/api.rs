// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! The shell API — what the React app calls, in place of Tauri commands.
//!
//! Every route is tenant-addressed (`/api/t/{tenant}/…`) and every handler
//! takes [`Tenant`], which is the session's identity rather than the path's.
//! The two agreeing is checked once, in the extractor.

use std::sync::Arc;
use std::time::Duration;

use axum::body::Bytes;
use axum::extract::{Path, Query, State};
use axum::http::{header, StatusCode};
use axum::response::sse::{Event, KeepAlive, Sse};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use futures_core::Stream;
use napkin_host::{export, library, proxy, DocId, InstalledApp, OpenResult, RecentDoc};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use tokio_stream::StreamExt;

use crate::error::{ApiError, ApiResult};
use crate::state::{AppCtx, Workspace};
use crate::store::doc_id;
use crate::tenant::{Tenant, TenantId};

type Ctx = State<Arc<AppCtx>>;

pub fn router() -> Router<Arc<AppCtx>> {
    Router::new()
        .route("/session", get(session))
        .route("/t/{tenant}/apps", get(list_apps))
        .route("/t/{tenant}/apps/from/{doc}", post(install_from_document))
        .route("/t/{tenant}/recent", get(recent))
        .route("/t/{tenant}/home", get(home))
        .route("/t/{tenant}/documents", post(new_document))
        .route("/t/{tenant}/documents/upload", post(upload_document))
        .route("/t/{tenant}/d/{doc}", get(open_document))
        .route("/t/{tenant}/d/{doc}/human-html", get(human_html))
        .route("/t/{tenant}/d/{doc}/entry/{which}", get(entry))
        .route("/t/{tenant}/d/{doc}/edit-mode", post(edit_mode))
        .route("/t/{tenant}/d/{doc}/preview-html", post(preview_html))
        .route("/t/{tenant}/d/{doc}/patch", post(patch))
        .route("/t/{tenant}/d/{doc}/download", get(download))
        .route("/t/{tenant}/d/{doc}/export", post(export_document))
        .route("/t/{tenant}/export/{handle}", get(export_download))
        .route("/t/{tenant}/agent/endpoint", get(agent_endpoint))
        .route("/t/{tenant}/agent/prompt", post(agent_prompt))
        .route("/t/{tenant}/events", get(events))
}

/// An opened document, plus what the shell needs to render it: the capability
/// token for its app frame, and the origin that frame should load from
/// (`null` = this one, until the sandbox moves to its own hostname).
#[derive(Serialize)]
struct OpenView {
    #[serde(flatten)]
    open: OpenResult,
    token: String,
    sandbox_origin: Option<String>,
}

fn view(ctx: &AppCtx, tenant: &TenantId, doc: &DocId, open: OpenResult) -> OpenView {
    OpenView {
        open,
        token: ctx.tokens.mint(tenant, doc),
        sandbox_origin: ctx.sandbox_origin.clone(),
    }
}

/// Open `doc` and describe it. The session is cached, so this is cheap on the
/// second call and every tab of the same document shares one archive.
fn open_view(ctx: &AppCtx, tenant: &TenantId, ws: &Workspace, doc: DocId) -> ApiResult<OpenView> {
    let session = ws.session(&doc)?;
    let open = session.open(doc.clone())?;
    Ok(view(ctx, tenant, &doc, open))
}

// ── Session ──────────────────────────────────────────────────────────────────

#[derive(Serialize)]
struct SessionInfo {
    tenant: String,
    sandbox_origin: Option<String>,
    quota: crate::meter::Usage,
}

/// The shell's first call: who am I, and where do app frames live.
async fn session(State(ctx): Ctx, Tenant(tenant): Tenant) -> Json<SessionInfo> {
    Json(SessionInfo {
        tenant: tenant.to_string(),
        sandbox_origin: ctx.sandbox_origin.clone(),
        quota: ctx.meter.usage(&tenant),
    })
}

// ── The app library ──────────────────────────────────────────────────────────

async fn list_apps(State(ctx): Ctx, Tenant(tenant): Tenant) -> Json<Vec<InstalledApp>> {
    Json(library::scan_apps(&*ctx.workspace(&tenant).store))
}

async fn recent(State(ctx): Ctx, Tenant(tenant): Tenant) -> Json<Vec<RecentDoc>> {
    Json(library::scan_recent(&*ctx.workspace(&tenant).store))
}

/// Install a template the user has already uploaded — the web's answer to
/// "open a .clan, then install it", where the desktop passes a file path.
async fn install_from_document(
    State(ctx): Ctx,
    Tenant(tenant): Tenant,
    Path((_t, doc)): Path<(String, String)>,
) -> ApiResult<Json<InstalledApp>> {
    let ws = ctx.workspace(&tenant);
    let bytes = ws.store.read(&DocId::new(doc))?;
    Ok(Json(library::install_app(&*ws.store, bytes)?))
}

#[derive(Deserialize)]
struct NewDocument {
    app_id: String,
    #[serde(default)]
    title: Option<String>,
}

async fn new_document(
    State(ctx): Ctx,
    Tenant(tenant): Tenant,
    Json(body): Json<NewDocument>,
) -> ApiResult<Json<OpenView>> {
    let ws = ctx.workspace(&tenant);
    let doc = library::create_instance(&*ws.store, &body.app_id, body.title)?;
    Ok(Json(open_view(&ctx, &tenant, &ws, doc)?))
}

/// Accept an uploaded `.clan` as a document of this tenant and open it.
///
/// Templates are stored the same way: the shell sees `is_template` and offers
/// to install, exactly as the desktop does when a template is opened.
async fn upload_document(
    State(ctx): Ctx,
    Tenant(tenant): Tenant,
    body: Bytes,
) -> ApiResult<Json<OpenView>> {
    if body.is_empty() {
        return Err(ApiError::new(400, "empty upload"));
    }
    let ws = ctx.workspace(&tenant);
    let doc = doc_id(&format!("upload-{}", uuid::Uuid::new_v4().simple()));
    ws.store.write(&doc, &body)?;
    // Refuse anything that is not a readable archive, rather than leaving a
    // corrupt document in the library.
    if let Err(e) = clan_sdk::ClanFile::from_bytes(body.to_vec()) {
        return Err(ApiError::new(400, format!("not a readable .clan: {e}")));
    }
    Ok(Json(open_view(&ctx, &tenant, &ws, doc)?))
}

async fn home(State(ctx): Ctx, Tenant(tenant): Tenant) -> ApiResult<Json<OpenView>> {
    let ws = ctx.workspace(&tenant);
    let doc = library::ensure_home(&*ws.store)?;
    Ok(Json(open_view(&ctx, &tenant, &ws, doc)?))
}

// ── One document ─────────────────────────────────────────────────────────────

/// Resolve `{doc}` for this tenant, giving back its workspace and session.
fn doc_session(
    ctx: &AppCtx,
    tenant: &TenantId,
    doc: &str,
) -> ApiResult<(Arc<Workspace>, Arc<napkin_host::Session>, DocId)> {
    let ws = ctx.workspace(tenant);
    let id = DocId::new(doc);
    let session = ws.session(&id)?;
    Ok((ws, session, id))
}

async fn open_document(
    State(ctx): Ctx,
    Tenant(tenant): Tenant,
    Path((_t, doc)): Path<(String, String)>,
) -> ApiResult<Json<OpenView>> {
    let ws = ctx.workspace(&tenant);
    Ok(Json(open_view(&ctx, &tenant, &ws, DocId::new(doc))?))
}

async fn human_html(
    State(ctx): Ctx,
    Tenant(tenant): Tenant,
    Path((_t, doc)): Path<(String, String)>,
) -> ApiResult<Response> {
    let (_ws, session, _) = doc_session(&ctx, &tenant, &doc)?;
    let html = session.human_html()?;
    Ok(([(header::CONTENT_TYPE, "text/html; charset=utf-8")], html).into_response())
}

async fn entry(
    State(ctx): Ctx,
    Tenant(tenant): Tenant,
    Path((_t, doc, which)): Path<(String, String, String)>,
) -> ApiResult<Response> {
    let path = match which.as_str() {
        "data" => "shared/data.yaml",
        "chain" => "agent/decision-chain.yaml",
        "state" => "agent/state.yaml",
        "context" => "agent/context.md",
        other => return Err(ApiError::new(404, format!("no such entry: {other}"))),
    };
    let (_ws, session, _) = doc_session(&ctx, &tenant, &doc)?;
    let text = session.entry_string(path)?;
    Ok(([(header::CONTENT_TYPE, "text/plain; charset=utf-8")], text).into_response())
}

#[derive(Deserialize)]
struct EditMode {
    active: bool,
}

async fn edit_mode(
    State(ctx): Ctx,
    Tenant(tenant): Tenant,
    Path((_t, doc)): Path<(String, String)>,
    Json(body): Json<EditMode>,
) -> ApiResult<Json<Value>> {
    let (_ws, session, _) = doc_session(&ctx, &tenant, &doc)?;
    session.set_edit_mode(body.active);
    Ok(Json(serde_json::json!({ "ok": true })))
}

/// The composed page the app frame will load, pushed by the shell.
///
/// Same handshake as the desktop: the shell splices the edit bridge into the
/// human view and hands the result to the host, which serves it back to the
/// frame at `/s/{token}/document`.
async fn preview_html(
    State(ctx): Ctx,
    Tenant(tenant): Tenant,
    Path((_t, doc)): Path<(String, String)>,
    body: String,
) -> ApiResult<Json<Value>> {
    let (_ws, session, _) = doc_session(&ctx, &tenant, &doc)?;
    session.set_preview_html(body);
    Ok(Json(serde_json::json!({ "ok": true })))
}

#[derive(Deserialize)]
struct PatchBody {
    id: String,
    content: String,
}

async fn patch(
    State(ctx): Ctx,
    Tenant(tenant): Tenant,
    Path((_t, doc)): Path<(String, String)>,
    Json(body): Json<PatchBody>,
) -> ApiResult<Json<Value>> {
    let (_ws, session, _) = doc_session(&ctx, &tenant, &doc)?;
    session.save_patch(body.id, body.content)?;
    Ok(Json(serde_json::json!({ "ok": true })))
}

/// The single-file handoff: the packed archive, exactly as stored.
async fn download(
    State(ctx): Ctx,
    Tenant(tenant): Tenant,
    Path((_t, doc)): Path<(String, String)>,
) -> ApiResult<Response> {
    let (_ws, session, _) = doc_session(&ctx, &tenant, &doc)?;
    let bytes = session.raw_bytes()?;
    let name = format!(
        "{}.clan",
        filename_stem(&session.title().unwrap_or_default())
    );
    Ok(attachment("application/zip", &name, bytes))
}

#[derive(Deserialize)]
struct ExportRequestBody {
    #[serde(default = "default_kind")]
    kind: String,
    #[serde(default)]
    provenance: bool,
    #[serde(default)]
    no_brand: bool,
}

fn default_kind() -> String {
    "pdf".into()
}

/// Compose an export and tell the shell to come and get it.
///
/// The event mirrors the desktop's: the shell decides what to do about it. On
/// the desktop that is a save dialog; in a browser it is a fetch of the handle.
async fn export_document(
    State(ctx): Ctx,
    Tenant(tenant): Tenant,
    Path((_t, doc)): Path<(String, String)>,
    Json(body): Json<ExportRequestBody>,
) -> ApiResult<Json<Value>> {
    let kind = if body.kind == "pdf" { "pdf" } else { "html" };
    let (_ws, session, _) = doc_session(&ctx, &tenant, &doc)?;
    let (html, filename) = session.compose_export(body.provenance, body.no_brand)?;
    let tmp = export::write_temp_html(&html)?;
    let handle = ctx.exports.stash(&tenant, tmp, filename.clone());
    ctx.events.publish(
        &tenant,
        &napkin_host::HostEvent::ExportRequest {
            kind: kind.to_string(),
            filename,
            tmp_html: handle.clone(),
        },
    );
    Ok(Json(serde_json::json!({ "ok": true, "handle": handle })))
}

#[derive(Deserialize)]
struct ExportKind {
    #[serde(default = "default_kind")]
    kind: String,
}

/// Claim a composed export, rendering it on the way out. Single use.
async fn export_download(
    State(ctx): Ctx,
    Tenant(tenant): Tenant,
    Path((_t, handle)): Path<(String, String)>,
    Query(q): Query<ExportKind>,
) -> ApiResult<Response> {
    let kind = if q.kind == "pdf" { "pdf" } else { "html" };
    let (tmp, filename) = ctx
        .exports
        .take(&tenant, &handle)
        .ok_or_else(|| ApiError::new(404, "no such export (already claimed, or expired)"))?;

    // finish_export writes to a destination and consumes the source; give it a
    // temp destination and hand the bytes to the browser.
    let dest = std::env::temp_dir().join(format!("napkin-export-{handle}.{kind}"));
    let dest_str = dest.display().to_string();
    export::finish_export(kind, &tmp, &dest_str)?;
    let bytes = std::fs::read(&dest).map_err(|e| ApiError::new(500, e.to_string()))?;
    let _ = std::fs::remove_file(&dest);

    let ct = if kind == "pdf" {
        "application/pdf"
    } else {
        "text/html; charset=utf-8"
    };
    Ok(attachment(ct, &format!("{filename}.{kind}"), bytes))
}

// ── The agent ────────────────────────────────────────────────────────────────

async fn agent_endpoint(State(ctx): Ctx, Tenant(_t): Tenant) -> Json<Value> {
    let (url, _, _) = napkin_host::resolve_proxy(&*ctx.config, "agent");
    Json(serde_json::json!({ "endpoint": url }))
}

#[derive(Deserialize)]
struct Prompt {
    text: String,
}

async fn agent_prompt(
    State(ctx): Ctx,
    Tenant(tenant): Tenant,
    Json(body): Json<Prompt>,
) -> ApiResult<Json<Value>> {
    spend(&ctx, &tenant)?;
    Ok(Json(proxy::agent_prompt(&*ctx.config, &body.text).await))
}

/// Charge one agent call to the tenant, or refuse with a 429 the shell shows.
pub fn spend(ctx: &AppCtx, tenant: &TenantId) -> ApiResult<()> {
    ctx.meter.try_spend(tenant).map(|_| ()).map_err(|u| {
        ApiError::new(
            429,
            format!(
                "agent quota reached for this session ({}/{})",
                u.used, u.cap
            ),
        )
    })
}

// ── Host → shell ─────────────────────────────────────────────────────────────

/// One SSE stream per tenant, carrying the same events the desktop delivers
/// as Tauri events.
async fn events(
    State(ctx): Ctx,
    Tenant(tenant): Tenant,
) -> Sse<impl Stream<Item = Result<Event, std::convert::Infallible>>> {
    let rx = ctx.events.subscribe(&tenant);
    let stream = tokio_stream::wrappers::BroadcastStream::new(rx).filter_map(|msg| {
        // A lagging receiver has missed events; the next one still arrives.
        let msg = msg.ok()?;
        Some(Ok(Event::default()
            .event(msg.name)
            .data(msg.data.to_string())))
    });
    Sse::new(stream).keep_alive(KeepAlive::new().interval(Duration::from_secs(15)))
}

// ── Helpers ──────────────────────────────────────────────────────────────────

fn filename_stem(title: &str) -> String {
    let t = title.trim();
    let base: String = (if t.is_empty() { "document" } else { t })
        .chars()
        .map(|c| {
            if c.is_alphanumeric() || c == '.' || c == '-' {
                c
            } else {
                '-'
            }
        })
        .collect();
    base
}

fn attachment(content_type: &str, filename: &str, bytes: Vec<u8>) -> Response {
    (
        StatusCode::OK,
        [
            (header::CONTENT_TYPE, content_type.to_string()),
            (
                header::CONTENT_DISPOSITION,
                format!("attachment; filename=\"{filename}\""),
            ),
        ],
        bytes,
    )
        .into_response()
}
