// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! Napkin Studio OS, served over HTTP.
//!
//! The same `napkin-host` the desktop shell runs, behind two front doors:
//!
//!   * `/api/t/{tenant}/…` — the shell API, in place of Tauri commands. Cookie
//!     session, one workspace per tenant, events over SSE.
//!   * `/s/{token}/…` — the app sandbox: the `clan://` table verbatim, reached
//!     with a capability token instead of a custom URI scheme.
//!
//! Everything else is the built React app.

pub mod api;
pub mod error;
pub mod events;
pub mod exports;
pub mod meter;
pub mod sandbox;
pub mod state;
pub mod store;
pub mod tenant;
pub mod tokens;

use std::path::Path;
use std::sync::Arc;

use axum::response::Html;
use axum::Router;
use tower_http::services::{ServeDir, ServeFile};
use tower_http::trace::TraceLayer;

use crate::state::AppCtx;

/// Shown instead of the shell when there is no build to serve — the mistake is
/// forgetting `npm run build`, and a blank 404 does not say so.
const NO_BUILD: &str = r#"<!doctype html><meta charset=utf-8>
<title>Napkin Studio OS — no build</title>
<style>body{font:15px/1.6 system-ui;margin:10vh auto;max-width:34rem;padding:0 1.5rem;color:#e2e8f0;background:#0f1117}
code{background:#1e2d45;padding:.15em .4em;border-radius:4px}</style>
<h1>No shell build found</h1>
<p>The API is running, but there is no built React app to serve.</p>
<p>Build it, then reload:</p>
<pre><code>cd app &amp;&amp; npm install &amp;&amp; npm run build</code></pre>
<p>Or point the server at an existing build with <code>NAPKIN_WEB_STATIC</code>.</p>"#;

fn shell_service(static_dir: Option<&Path>) -> Router<Arc<AppCtx>> {
    match static_dir {
        // Any unknown path falls back to index.html: the shell owns its routes.
        Some(dir) => Router::new().fallback_service(
            ServeDir::new(dir).not_found_service(ServeFile::new(dir.join("index.html"))),
        ),
        None => Router::new().fallback(|| async { Html(NO_BUILD) }),
    }
}

/// Assemble the whole service. Kept separate from `main` so tests can drive the
/// real router rather than a rehearsal of it.
pub fn router(ctx: Arc<AppCtx>, static_dir: Option<&Path>, secure_cookie: bool) -> Router {
    Router::new()
        // The shell API is the only part that runs on a cookie session.
        .nest(
            "/api",
            api::router().layer(axum::middleware::from_fn_with_state(
                secure_cookie,
                tenant::layer,
            )),
        )
        // The sandbox authenticates with its token and nothing else, so the
        // session layer deliberately does not apply to it.
        .nest("/s", sandbox::router())
        .merge(shell_service(static_dir))
        .layer(TraceLayer::new_for_http())
        .with_state(ctx)
}

/// Find the built shell without being told: next to the working directory in a
/// deployment, or up the tree in a checkout.
pub fn find_static() -> Option<std::path::PathBuf> {
    ["dist", "app/dist", "../app/dist", "../../app/dist"]
        .into_iter()
        .map(std::path::PathBuf::from)
        .find(|p| p.join("index.html").is_file())
}
