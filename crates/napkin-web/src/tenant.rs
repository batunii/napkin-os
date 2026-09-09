// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! Who the request belongs to.
//!
//! For the demo a tenant is an anonymous browser session: a cookie, minted on
//! first contact, with no account behind it. Everything downstream is already
//! written as if there were one — every route is tenant-scoped, every store is
//! rooted at the tenant, and the [`Tenant`] extractor is the single place that
//! decides identity. Real auth replaces the body of [`resolve`] and the URL
//! shape does not move.

use axum::extract::{FromRequestParts, Path};
use axum::http::request::Parts;
use axum::http::{header, HeaderValue, StatusCode};
use axum::middleware::Next;
use axum::response::Response;
use std::collections::HashMap;
use std::fmt;

pub const COOKIE_NAME: &str = "napkin_tenant";

#[derive(Clone, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct TenantId(String);

impl TenantId {
    /// Accept only what we mint: a plain UUID. The id becomes a directory
    /// name, so a cookie the user has edited must never be taken at face value.
    pub fn parse(s: &str) -> Option<Self> {
        let ok = s.len() == 36
            && s.chars().all(|c| c.is_ascii_hexdigit() || c == '-')
            && s.split('-').map(str::len).eq([8, 4, 4, 4, 12]);
        ok.then(|| Self(s.to_string()))
    }

    pub fn mint() -> Self {
        Self(uuid::Uuid::new_v4().to_string())
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl fmt::Display for TenantId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

/// The tenant this request acts as. Handlers take this rather than reading the
/// `{tenant}` path segment, so a request can never act on a workspace its
/// session does not own.
pub struct Tenant(pub TenantId);

impl<S: Send + Sync> FromRequestParts<S> for Tenant {
    type Rejection = Response;

    async fn from_request_parts(parts: &mut Parts, state: &S) -> Result<Self, Self::Rejection> {
        let session = parts.extensions.get::<TenantId>().cloned().ok_or_else(|| {
            deny(
                StatusCode::INTERNAL_SERVER_ERROR,
                "tenant layer not installed",
            )
        })?;

        // A `{tenant}` in the path is addressing, not authority: it must agree
        // with the session or the request is a confused deputy.
        if let Ok(Path(params)) =
            Path::<HashMap<String, String>>::from_request_parts(parts, state).await
        {
            if let Some(addressed) = params.get("tenant") {
                if addressed != session.as_str() {
                    return Err(deny(StatusCode::FORBIDDEN, "not your workspace"));
                }
            }
        }
        Ok(Tenant(session))
    }
}

fn deny(status: StatusCode, msg: &str) -> Response {
    let body = serde_json::json!({ "ok": false, "error": msg }).to_string();
    Response::builder()
        .status(status)
        .header(header::CONTENT_TYPE, "application/json")
        .body(body.into())
        .unwrap()
}

fn cookie_value(parts: &Parts, name: &str) -> Option<String> {
    parts
        .headers
        .get_all(header::COOKIE)
        .iter()
        .filter_map(|v| v.to_str().ok())
        .flat_map(|s| s.split(';'))
        .filter_map(|kv| kv.trim().split_once('='))
        .find(|(k, _)| *k == name)
        .map(|(_, v)| v.to_string())
}

/// Resolve (or mint) the session's tenant and put it in the request extensions;
/// set the cookie on the way out when it is new.
pub async fn layer(
    axum::extract::State(secure): axum::extract::State<bool>,
    request: axum::extract::Request,
    next: Next,
) -> Response {
    let (mut parts, body) = request.into_parts();
    let existing = cookie_value(&parts, COOKIE_NAME)
        .as_deref()
        .and_then(TenantId::parse);
    let is_new = existing.is_none();
    let tenant = existing.unwrap_or_else(TenantId::mint);
    parts.extensions.insert(tenant.clone());

    let mut response = next
        .run(axum::extract::Request::from_parts(parts, body))
        .await;

    if is_new {
        // A year: long enough that a demo link survives being reopened, and the
        // cookie is the only thing standing between a visitor and their work.
        let secure = if secure { "; Secure" } else { "" };
        let cookie = format!(
            "{COOKIE_NAME}={tenant}; Path=/; HttpOnly; SameSite=Lax; Max-Age=31536000{secure}"
        );
        if let Ok(v) = HeaderValue::from_str(&cookie) {
            response.headers_mut().append(header::SET_COOKIE, v);
        }
    }
    response
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_well_formed_uuids_are_accepted_as_tenants() {
        let minted = TenantId::mint();
        assert_eq!(TenantId::parse(minted.as_str()), Some(minted));

        // A tenant id becomes a directory name; these must never get that far.
        for bad in [
            "../../etc",
            "..",
            "a/b",
            "",
            "not-a-uuid",
            "0000000000000000000000000000000000000",
            "zzzzzzzz-zzzz-zzzz-zzzz-zzzzzzzzzzzz",
        ] {
            assert!(TenantId::parse(bad).is_none(), "{bad} must be refused");
        }
    }
}
