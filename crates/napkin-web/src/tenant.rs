// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! Who the request belongs to.
//!
//! Two modes ([`Identity`]):
//! - **accounts** (`NAPKIN_AUTH=local|cognito`): a person signs in as
//!   `user@agency` ([`crate::auth`]); the agency's workspace is the tenant and
//!   the person is the actor every write records. A request to the API with
//!   no valid session is answered 401 `{signin: true}`.
//! - **anonymous** (`NAPKIN_AUTH=none`, the default for tests and demos): a
//!   cookie minted on first contact, with no account behind it; the tenant is
//!   its own one person.
//!
//! Every route is tenant-scoped and every store is rooted at the tenant; the
//! [`Tenant`] and [`Acting`] extractors are the only places that decide identity.

use axum::extract::{FromRequestParts, Path};
use axum::http::request::Parts;
use axum::http::{header, HeaderValue, StatusCode};
use axum::middleware::Next;
use axum::response::Response;
use std::collections::HashMap;
use std::fmt;
use std::sync::Arc;

use crate::auth::{Account, Provider, Sessions, SESSION_COOKIE};

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

    /// An agency's workspace: the same id for everyone in it, on every server
    /// (a name-based UUID, so it is still a safe directory name).
    pub fn of_agency(agency: &str) -> Self {
        Self(uuid::Uuid::new_v5(&uuid::Uuid::NAMESPACE_URL, format!("napkin:agency:{agency}").as_bytes()).to_string())
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

impl TenantId {
    /// Who a request from this tenant runs as inside the host. The demo's
    /// tenant is one anonymous person, so the actor is that person and the org
    /// scope is their workspace. Real auth changes what goes in here, not who
    /// builds it: the host never derives either from a request body.
    pub fn ctx(&self) -> napkin_host::Ctx {
        self.ctx_for(self.as_str())
    }

    /// The same workspace, acted in by one person (`engineer@napkin`).
    pub fn ctx_for(&self, person: &str) -> napkin_host::Ctx {
        let actor = napkin_host::Actor::human(person)
            .or_else(|_| napkin_host::Actor::human(self.as_str()))
            .expect("a parsed tenant id is a well-formed actor id");
        napkin_host::Ctx::new(actor).with_scope(napkin_host::Scope {
            org: Some(self.0.clone()),
            brand: None,
        })
    }
}

/// How this server knows who someone is.
pub struct Identity {
    pub mode: Mode,
    /// Set the cookies `Secure` (behind TLS).
    pub secure: bool,
}

pub enum Mode {
    Anonymous,
    Accounts { provider: Provider, sessions: Sessions },
}

impl Identity {
    pub fn anonymous(secure: bool) -> Self {
        Self { mode: Mode::Anonymous, secure }
    }

    pub fn cookie(&self, name: &str, value: &str, max_age: u64) -> String {
        let secure = if self.secure { "; Secure" } else { "" };
        format!("{name}={value}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}{secure}")
    }
}

/// The person acting in this request: the signed-in account's name, or the
/// anonymous tenant itself.
#[derive(Clone, Debug)]
pub struct Person(pub String);

/// The request's tenant and person as the host's context: who writes, in
/// which workspace. Writes take this, so the decision chain names the person.
pub struct Acting(pub napkin_host::Ctx);

impl<S: Send + Sync> FromRequestParts<S> for Acting {
    type Rejection = Response;

    async fn from_request_parts(parts: &mut Parts, state: &S) -> Result<Self, Self::Rejection> {
        let Tenant(t) = Tenant::from_request_parts(parts, state).await?;
        let person = parts.extensions.get::<Person>().map(|p| p.0.clone());
        Ok(Acting(match person {
            Some(p) => t.ctx_for(&p),
            None => t.ctx(),
        }))
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

/// Resolve who the request belongs to and put it in the request extensions.
///
/// Anonymous: the tenant cookie (minted when new). Accounts: the signed
/// session cookie names the person; their agency is the tenant. Without one,
/// only sign-in itself and the health check are answered.
pub async fn layer(
    axum::extract::State(identity): axum::extract::State<Arc<Identity>>,
    request: axum::extract::Request,
    next: Next,
) -> Response {
    let (mut parts, body) = request.into_parts();
    match &identity.mode {
        Mode::Anonymous => {
            let existing = cookie_value(&parts, COOKIE_NAME)
                .as_deref()
                .and_then(TenantId::parse);
            let is_new = existing.is_none();
            let tenant = existing.unwrap_or_else(TenantId::mint);
            parts.extensions.insert(tenant.clone());
            parts.extensions.insert(Person(tenant.to_string()));
            let mut response = next
                .run(axum::extract::Request::from_parts(parts, body))
                .await;
            if is_new {
                // A year: long enough that a demo link survives being reopened,
                // and the cookie is the only thing between a visitor and their work.
                let cookie = identity.cookie(COOKIE_NAME, tenant.as_str(), 31_536_000);
                if let Ok(v) = HeaderValue::from_str(&cookie) {
                    response.headers_mut().append(header::SET_COOKIE, v);
                }
            }
            response
        }
        Mode::Accounts { sessions, .. } => {
            let account = cookie_value(&parts, SESSION_COOKIE).and_then(|c| sessions.read(&c));
            let path = parts.uri.path().to_string();
            match account {
                Some(a) => {
                    parts.extensions.insert(TenantId::of_agency(&a.agency));
                    parts.extensions.insert(Person(a.username.clone()));
                    parts.extensions.insert(a);
                }
                None if path.starts_with("/auth/") || path == "/healthz" => {}
                None => {
                    let body = serde_json::json!({ "ok": false, "error": "sign in", "signin": true }).to_string();
                    return Response::builder()
                        .status(StatusCode::UNAUTHORIZED)
                        .header(header::CONTENT_TYPE, "application/json")
                        .body(body.into())
                        .unwrap();
                }
            }
            next.run(axum::extract::Request::from_parts(parts, body)).await
        }
    }
}

/// The signed-in account, when there is one (accounts mode).
pub fn account_of(parts_ext: &axum::http::Extensions) -> Option<Account> {
    parts_ext.get::<Account>().cloned()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_tenant_acts_as_a_human_scoped_to_its_own_workspace() {
        let t = TenantId::mint();
        let ctx = t.ctx();
        assert_eq!(ctx.actor.as_str(), format!("human:{t}"));
        assert_eq!(ctx.scope.org.as_deref(), Some(t.as_str()));
    }

    #[test]
    fn an_agency_is_one_workspace_and_its_people_act_as_themselves() {
        let a = TenantId::of_agency("napkin");
        assert_eq!(a, TenantId::of_agency("napkin"));
        assert_ne!(a, TenantId::of_agency("javelin"));
        assert!(TenantId::parse(a.as_str()).is_some(), "still a safe directory name");
        let ctx = a.ctx_for("engineer@napkin");
        assert_eq!(ctx.actor.as_str(), "human:engineer@napkin");
        assert_eq!(ctx.scope.org.as_deref(), Some(a.as_str()));
    }

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
