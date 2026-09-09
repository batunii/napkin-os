// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! The `clan://` API surface — the provenance-native rendering environment an
//! app inside a `.clan` file talks to.
//!
//! Exact-path routing (`uri.contains` would confuse `/patch` with
//! `/patch-data`). Every handler here is a pure function of the request, the
//! session and the config: it returns a [`HostResponse`] carrying the bytes to
//! send back plus any [`HostEvent`]s the shell must act on. That is what makes
//! the same routing table serve a custom URI scheme on the desktop and plain
//! HTTP on the web.

use serde_json::Value;

use crate::config::{agent_base_url, HostConfig};
use crate::error::HostError;
use crate::event::HostEvent;
#[cfg(feature = "native")]
use crate::export::write_temp_html;
use crate::library::{create_instance, scan_apps, scan_recent};
#[cfg(feature = "native")]
use crate::proxy::api_proxy;
use crate::session::{Session, TRUSTED_CAPABILITIES};

pub struct HostRequest {
    pub path: String,
    pub query: String,
    pub body: Vec<u8>,
}

impl HostRequest {
    pub fn new(path: impl Into<String>, query: impl Into<String>, body: Vec<u8>) -> Self {
        Self {
            path: path.into(),
            query: query.into(),
            body,
        }
    }

    fn body_str(&self) -> String {
        String::from_utf8(self.body.clone()).unwrap_or_default()
    }

    /// The request body as JSON, or `Null` if it is absent or malformed —
    /// routes that use this validate the fields they need instead.
    fn body_json(&self) -> Value {
        serde_json::from_str(&self.body_str()).unwrap_or(Value::Null)
    }
}

pub struct HostResponse {
    pub status: u16,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
    pub events: Vec<HostEvent>,
}

/// Every route the sandbox can reach answers with CORS open: the app runs in an
/// opaque-origin frame and sends no credentials, so the token in its URL — not
/// the origin — is the authority.
fn cors() -> Vec<(String, String)> {
    vec![("Access-Control-Allow-Origin".into(), "*".into())]
}

impl HostResponse {
    pub fn new(status: u16, content_type: &str, body: Vec<u8>) -> Self {
        let mut headers = cors();
        headers.push(("Content-Type".into(), content_type.into()));
        Self {
            status,
            headers,
            body,
            events: Vec::new(),
        }
    }

    pub fn json(status: u16, value: &Value) -> Self {
        Self::new(
            status,
            "application/json",
            serde_json::to_vec(value).unwrap_or_default(),
        )
    }

    pub fn error(status: u16, msg: &str) -> Self {
        Self::json(status, &serde_json::json!({ "ok": false, "error": msg }))
    }

    pub fn ok_json() -> Self {
        Self::json(200, &serde_json::json!({ "ok": true }))
    }

    /// 200 with an empty body and no content type — what the fire-and-forget
    /// routes (`/patch`, `/snapshot`) have always returned.
    pub fn empty() -> Self {
        Self {
            status: 200,
            headers: cors(),
            body: Vec::new(),
            events: Vec::new(),
        }
    }

    /// 404 with no body and no CORS header — an unknown path is not part of
    /// the API surface at all.
    pub fn unknown_route() -> Self {
        Self {
            status: 404,
            headers: Vec::new(),
            body: Vec::new(),
            events: Vec::new(),
        }
    }

    pub fn with_event(mut self, e: HostEvent) -> Self {
        self.events.push(e);
        self
    }
}

impl From<HostError> for HostResponse {
    fn from(e: HostError) -> Self {
        HostResponse::error(e.status, &e.message)
    }
}

/// Parse `k=v&k=v` query strings (small, dependency-free).
pub fn query_param(query: &str, key: &str) -> Option<String> {
    query.split('&').find_map(|kv| {
        let (k, v) = kv.split_once('=')?;
        if k == key {
            Some(v.replace('+', " "))
        } else {
            None
        }
    })
}

/// True for the routes that must be awaited. The desktop shell spawns these so
/// the WebView loop never blocks; everything else it answers inline.
///
/// Without the `native` feature there are none: the one network route belongs
/// to the page, which holds the credentials, and never reaches the host.
pub fn is_async(path: &str) -> bool {
    cfg!(feature = "native") && path == "/api-proxy"
}

/// Dispatch any route, including the async ones.
pub async fn handle_async(
    session: &Session,
    cfg: &dyn HostConfig,
    req: HostRequest,
) -> HostResponse {
    #[cfg(feature = "native")]
    if req.path == "/api-proxy" {
        return match api_proxy(session, cfg, &req.body_str()).await {
            Ok(v) => HostResponse::json(200, &v),
            Err(e) => e.into(),
        };
    }
    handle(session, cfg, req)
}

/// Dispatch every synchronous route. `/api-proxy` is the one route this cannot
/// serve — see [`handle_async`].
pub fn handle(session: &Session, cfg: &dyn HostConfig, req: HostRequest) -> HostResponse {
    let path = req.path.as_str();
    match path {
        #[cfg(feature = "native")]
        "/api-proxy" => HostResponse::error(500, "/api-proxy must be dispatched asynchronously"),
        // In a browser build the page owns inference — it has the credentials
        // and the network — so it answers this before the host ever sees it.
        #[cfg(not(feature = "native"))]
        "/api-proxy" => HostResponse::error(501, "inference is handled by the page in this build"),

        "/edit-mode" => HostResponse::new(
            200,
            "text/plain",
            if session.edit_mode() {
                b"true".to_vec()
            } else {
                b"false".to_vec()
            },
        ),

        "/document" => HostResponse::new(200, "text/html", session.preview_html().into_bytes()),

        "/snapshot" => {
            if let Ok(html) = String::from_utf8(req.body.clone()) {
                let _ = session.snapshot(&html);
            }
            HostResponse::empty()
        }

        "/patch" => match session.handle_patch_request(&req.body_str()) {
            Some(payload) => HostResponse::empty().with_event(HostEvent::PatchSaved(payload)),
            None => HostResponse::empty(),
        },

        "/patch-data" => match session.patch_data(&req.body_str()) {
            Ok(v) => HostResponse::json(200, &v).with_event(HostEvent::DataChanged(v)),
            Err(e) => e.into(),
        },

        "/fork" => match session.fork(&req.body_str()) {
            Ok(v) => HostResponse::json(200, &v),
            Err(e) => e.into(),
        },

        "/upload-asset" => {
            let name = query_param(&req.query, "name").unwrap_or_default();
            let agent = query_param(&req.query, "agent");
            match session.upload_asset(&name, agent.as_deref(), req.body) {
                Ok(v) => HostResponse::json(200, &v),
                Err(e) => e.into(),
            }
        }

        "/chain" => match session.chain_json() {
            Ok(v) => HostResponse::json(200, &v),
            Err(e) => e.into(),
        },

        // Launcher routes — let a home CLAN app list and launch apps.
        "/apps" => HostResponse::json(200, &serde_json::json!(scan_apps(&**session.store()))),
        "/recent" => HostResponse::json(200, &serde_json::json!(scan_recent(&**session.store()))),

        "/open" => match req.body_json().get("path").and_then(|x| x.as_str()) {
            Some(p) => HostResponse::ok_json().with_event(HostEvent::OpenDocument(p.to_string())),
            None => HostResponse::error(400, "missing path"),
        },

        "/agent-endpoint" => {
            HostResponse::json(200, &serde_json::json!({ "endpoint": agent_base_url(cfg) }))
        }

        "/launch" => {
            let v = req.body_json();
            let app_id = v.get("app_id").and_then(|x| x.as_str()).unwrap_or("");
            let title = v.get("title").and_then(|x| x.as_str()).map(String::from);
            if app_id.is_empty() {
                return HostResponse::error(400, "missing app_id");
            }
            match create_instance(&**session.store(), app_id, title) {
                Ok(id) => {
                    // Tell the shell to open the freshly created .clan.
                    HostResponse::json(
                        200,
                        &serde_json::json!({ "ok": true, "path": id.to_string() }),
                    )
                    .with_event(HostEvent::OpenDocument(id.to_string()))
                }
                Err(e) => HostResponse::error(422, &e.message),
            }
        }

        // The host owns the file dialog; ask the shell to run it.
        "/open-file" => HostResponse::ok_json().with_event(HostEvent::OpenFileRequest),

        "/set-title" => match req.body_json().get("title").and_then(|x| x.as_str()) {
            Some(t) if !t.trim().is_empty() => match session.set_title(t) {
                Ok(r) => {
                    HostResponse::json(200, &r).with_event(HostEvent::TitleChanged(t.to_string()))
                }
                Err(e) => e.into(),
            },
            _ => HostResponse::error(400, "missing title"),
        },

        // A doc (e.g. a locked brief) asks the shell to export/save it.
        "/request-save" => HostResponse::ok_json().with_event(HostEvent::RequestSave),

        "/export" => {
            // LEGACY / imperative-fallback path. The app builds its own
            // standalone HTML in JS and pushes it here. Prefer the host-owned
            // export (composes via the SDK from the file's data — bindings,
            // assets, brand chrome, provenance) so export is uniform and works
            // headless. This path remains for apps whose print layout is
            // genuinely code (e.g. brief-maker's report builder).
            let v = req.body_json();
            let kind = v.get("kind").and_then(|x| x.as_str()).unwrap_or("html");
            let kind = if kind == "pdf" { "pdf" } else { "html" };
            let filename = v
                .get("filename")
                .and_then(|x| x.as_str())
                .unwrap_or("brief");
            let html = v.get("html").and_then(|x| x.as_str()).unwrap_or("");
            if html.trim().is_empty() {
                return HostResponse::error(400, "missing html");
            }
            // Native: stash it and let the shell pick a destination.
            #[cfg(feature = "native")]
            {
                match write_temp_html(html) {
                    Ok(tmp) => HostResponse::ok_json().with_event(HostEvent::ExportRequest {
                        kind: kind.to_string(),
                        filename: filename.to_string(),
                        tmp_html: tmp,
                    }),
                    Err(e) => HostResponse::error(500, &e.message),
                }
            }
            // Browser: there is nowhere to stash it, so hand the document back
            // and let the page turn it into a download.
            #[cfg(not(feature = "native"))]
            {
                HostResponse::json(
                    200,
                    &serde_json::json!({
                        "ok": true, "kind": kind, "filename": filename, "html": html,
                    }),
                )
            }
        }

        "/set-context" => {
            let v = req.body_json();
            let md = v.get("markdown").and_then(|x| x.as_str()).unwrap_or("");
            let append = v.get("append").and_then(|x| x.as_bool()).unwrap_or(false);
            if md.trim().is_empty() {
                return HostResponse::error(400, "missing markdown");
            }
            match session.set_context(md, append) {
                Ok(r) => HostResponse::json(200, &r),
                Err(e) => e.into(),
            }
        }

        // Scoped host capabilities — only for trusted (signed) apps.
        "/capabilities" => {
            let trusted = session.trusted();
            let allowed: Vec<&str> = if trusted {
                TRUSTED_CAPABILITIES.to_vec()
            } else {
                vec![]
            };
            HostResponse::json(
                200,
                &serde_json::json!({ "trusted": trusted, "allowed": allowed }),
            )
        }

        "/notify" => {
            if !session.trusted() {
                return HostResponse::error(
                    403,
                    "capability 'notify' requires a signed (trusted) app",
                );
            }
            let v = req.body_json();
            HostResponse::ok_json().with_event(HostEvent::Notify {
                title: v
                    .get("title")
                    .and_then(|x| x.as_str())
                    .unwrap_or("Napkin")
                    .to_string(),
                body: v
                    .get("body")
                    .and_then(|x| x.as_str())
                    .unwrap_or("")
                    .to_string(),
            })
        }

        "/set-theme" => {
            // Recolor the viewer chrome — a scoped capability only a signed
            // (trusted) app may use.
            if !session.trusted() {
                return HostResponse::error(
                    403,
                    "capability 'set-theme' requires a signed (trusted) app",
                );
            }
            // Pass the theme object straight through to the shell.
            HostResponse::ok_json().with_event(HostEvent::ThemeChanged(req.body_json()))
        }

        p if p.starts_with("/assets/") => {
            let rel = p.strip_prefix("/assets/").unwrap_or("");
            match session.serve_asset(rel) {
                Ok((ct, bytes)) => {
                    let mut resp = HostResponse::new(200, &ct, bytes);
                    resp.headers
                        .push(("Cache-Control".into(), "no-cache".into()));
                    resp
                }
                Err(e) => e.into(),
            }
        }

        _ => HostResponse::unknown_route(),
    }
}
