// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! The app sandbox: the `clan://` surface, over HTTP.
//!
//! `/s/{token}/patch-data` is `clan://localhost/patch-data`. The path after the
//! token is handed to `napkin_host::handle_async` verbatim, which is why the
//! routing table did not have to be rewritten to get here — and why a route
//! added for one shell appears in the other.
//!
//! Authority is the token, not a cookie. Nothing here reads the session: an app
//! frame is third-party code on (eventually) its own origin, sends no
//! credentials, and can reach exactly the one document its token names.

use std::sync::Arc;

use axum::body::Bytes;
use axum::extract::{Path, RawQuery, State};
use axum::http::{header, HeaderName, HeaderValue, Method, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::any;
use axum::Router;
use napkin_host::{HostRequest, HostResponse};

use crate::state::AppCtx;

pub fn router() -> Router<Arc<AppCtx>> {
    Router::new()
        .route("/{token}/{*rest}", any(dispatch))
        // A token with nothing after it addresses no route; answer like the
        // host does for an unknown path rather than 404ing at the router.
        .route("/{token}", any(dispatch_root))
}

async fn dispatch_root(
    state: State<Arc<AppCtx>>,
    Path(token): Path<String>,
    query: RawQuery,
    method: Method,
    body: Bytes,
) -> Response {
    dispatch(state, Path((token, String::new())), query, method, body).await
}

async fn dispatch(
    State(ctx): State<Arc<AppCtx>>,
    Path((token, rest)): Path<(String, String)>,
    RawQuery(query): RawQuery,
    method: Method,
    body: Bytes,
) -> Response {
    // The app frame's cross-origin POSTs are simple requests today (no custom
    // headers), but answer preflight anyway so an app that sets one still works.
    if method == Method::OPTIONS {
        return preflight();
    }

    let Some(grant) = ctx.tokens.resolve(&token) else {
        // Deliberately indistinguishable from an expired one: a caller probing
        // tokens learns nothing about which documents exist.
        return refuse(StatusCode::FORBIDDEN, "invalid or expired sandbox token");
    };

    let path = format!("/{}", rest.trim_start_matches('/'));

    // The one route that spends money is metered before it is dispatched.
    if path == "/api-proxy" {
        if let Err(usage) = ctx.meter.try_spend(&grant.tenant) {
            return refuse(
                StatusCode::TOO_MANY_REQUESTS,
                &format!(
                    "agent quota reached for this session ({}/{})",
                    usage.used, usage.cap
                ),
            );
        }
    }

    let workspace = ctx.workspace(&grant.tenant);
    let session = match workspace.session(&grant.doc) {
        Ok(s) => s,
        Err(e) => return refuse(status_of(e.status), &e.message),
    };

    let req = HostRequest::new(path, query.unwrap_or_default(), body.to_vec());
    let resp = napkin_host::handle_async(&session, &*ctx.config, req).await;
    ctx.events.publish_all(&grant.tenant, &resp.events);
    into_response(resp)
}

fn status_of(code: u16) -> StatusCode {
    StatusCode::from_u16(code).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR)
}

fn into_response(resp: HostResponse) -> Response {
    let mut out = Response::builder().status(status_of(resp.status));
    for (k, v) in &resp.headers {
        if let (Ok(name), Ok(value)) = (HeaderName::try_from(k.as_str()), HeaderValue::from_str(v))
        {
            out = out.header(name, value);
        }
    }
    out.body(resp.body.into())
        .unwrap_or_else(|_| refuse(StatusCode::INTERNAL_SERVER_ERROR, "malformed host response"))
}

fn preflight() -> Response {
    (
        StatusCode::NO_CONTENT,
        [
            (header::ACCESS_CONTROL_ALLOW_ORIGIN, "*"),
            (header::ACCESS_CONTROL_ALLOW_METHODS, "GET, POST, OPTIONS"),
            (header::ACCESS_CONTROL_ALLOW_HEADERS, "Content-Type"),
            (header::ACCESS_CONTROL_MAX_AGE, "600"),
        ],
    )
        .into_response()
}

fn refuse(status: StatusCode, message: &str) -> Response {
    (
        status,
        [
            (header::ACCESS_CONTROL_ALLOW_ORIGIN, "*".to_string()),
            (header::CONTENT_TYPE, "application/json".to_string()),
        ],
        serde_json::json!({ "ok": false, "error": message }).to_string(),
    )
        .into_response()
}
