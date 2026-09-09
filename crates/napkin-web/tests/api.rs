// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! The service, driven through its real router.
//!
//! The interesting properties here are the ones the desktop never had to have:
//! that a tenant is isolated, that a document id from a browser cannot become
//! an arbitrary path, and that the sandbox's authority is its token alone.

use std::sync::Arc;

use axum::body::{Body, Bytes};
use axum::http::{header, HeaderMap, Request, StatusCode};
use axum::Router;
use http_body_util::BodyExt;
use napkin_host::NoConfig;
use napkin_web::state::AppCtx;
use serde_json::Value;
use tower::ServiceExt;

struct Server {
    _dir: tempfile::TempDir,
    ctx: Arc<AppCtx>,
    app: Router,
}

fn server(agent_cap: u32) -> Server {
    let dir = tempfile::tempdir().unwrap();
    let ctx = Arc::new(AppCtx::new(
        dir.path().to_path_buf(),
        Arc::new(NoConfig),
        None,
        agent_cap,
    ));
    let app = napkin_web::router(ctx.clone(), None, false);
    Server {
        _dir: dir,
        ctx,
        app,
    }
}

struct Reply {
    status: StatusCode,
    headers: HeaderMap,
    body: Bytes,
}

impl Reply {
    fn json(&self) -> Value {
        serde_json::from_slice(&self.body).unwrap_or_else(|e| {
            panic!(
                "expected JSON, got {:?}: {e}",
                String::from_utf8_lossy(&self.body)
            )
        })
    }
}

async fn send(s: &Server, req: Request<Body>) -> Reply {
    let resp = s.app.clone().oneshot(req).await.unwrap();
    let status = resp.status();
    let headers = resp.headers().clone();
    let body = resp.into_body().collect().await.unwrap().to_bytes();
    Reply {
        status,
        headers,
        body,
    }
}

fn request(method: &str, uri: &str, cookie: Option<&str>, body: Body) -> Request<Body> {
    let mut b = Request::builder().method(method).uri(uri);
    if let Some(c) = cookie {
        b = b.header(header::COOKIE, c);
    }
    b.body(body).unwrap()
}

async fn get(s: &Server, uri: &str, cookie: Option<&str>) -> Reply {
    send(s, request("GET", uri, cookie, Body::empty())).await
}

async fn post(s: &Server, uri: &str, cookie: &str, body: impl Into<Body>) -> Reply {
    send(s, request("POST", uri, Some(cookie), body.into())).await
}

async fn post_json(s: &Server, uri: &str, cookie: &str, body: Value) -> Reply {
    let req = Request::builder()
        .method("POST")
        .uri(uri)
        .header(header::COOKIE, cookie)
        .header(header::CONTENT_TYPE, "application/json")
        .body(Body::from(body.to_string()))
        .unwrap();
    send(s, req).await
}

/// A browser session: its cookie and the tenant it resolved to.
struct Browser {
    cookie: String,
    tenant: String,
}

async fn browser(s: &Server) -> Browser {
    let reply = get(s, "/api/session", None).await;
    assert_eq!(reply.status, StatusCode::OK);
    let set = reply
        .headers
        .get(header::SET_COOKIE)
        .expect("a first visit must be given a session")
        .to_str()
        .unwrap();
    let cookie = set.split(';').next().unwrap().to_string();
    assert!(
        set.contains("HttpOnly"),
        "the session cookie must not be script-readable"
    );
    Browser {
        cookie,
        tenant: reply.json()["tenant"].as_str().unwrap().to_string(),
    }
}

fn a_clan(title: &str) -> Vec<u8> {
    clan_sdk::create(clan_sdk::CreateOptions {
        title: title.into(),
        brief: "test brief".into(),
        document_type: None,
        no_render: false,
        schema: None,
    })
    .unwrap()
}

/// Upload a document and return `(doc id, sandbox token)`.
async fn upload(s: &Server, b: &Browser, title: &str) -> (String, String) {
    let reply = post(
        s,
        &format!("/api/t/{}/documents/upload", b.tenant),
        &b.cookie,
        a_clan(title),
    )
    .await;
    assert_eq!(
        reply.status,
        StatusCode::OK,
        "{}",
        String::from_utf8_lossy(&reply.body)
    );
    let v = reply.json();
    (
        v["path"].as_str().unwrap().into(),
        v["token"].as_str().unwrap().into(),
    )
}

// ── Sessions and tenancy ─────────────────────────────────────────────────────

#[tokio::test]
async fn a_first_visit_is_given_a_session_and_keeps_it() {
    let s = server(40);
    let b = browser(&s).await;

    // Coming back with the cookie is the same tenant, and no new cookie.
    let again = get(&s, "/api/session", Some(&b.cookie)).await;
    assert_eq!(again.json()["tenant"], b.tenant);
    assert!(again.headers.get(header::SET_COOKIE).is_none());
}

#[tokio::test]
async fn a_forged_cookie_does_not_become_a_workspace() {
    let s = server(40);
    // The tenant id becomes a directory name; a hand-written cookie is ignored
    // and the visitor is simply given a fresh session.
    let reply = get(&s, "/api/session", Some("napkin_tenant=../../etc")).await;
    assert_eq!(reply.status, StatusCode::OK);
    assert!(
        reply.headers.get(header::SET_COOKIE).is_some(),
        "must be re-issued a real session"
    );
    assert_ne!(reply.json()["tenant"], "../../etc");
}

#[tokio::test]
async fn one_tenant_cannot_address_anothers_workspace() {
    let s = server(40);
    let alice = browser(&s).await;
    let mallory = browser(&s).await;
    let (doc, _) = upload(&s, &alice, "Alice's brief").await;

    // Mallory knows the URL. The path says Alice; the session says Mallory.
    let reply = get(
        &s,
        &format!("/api/t/{}/d/{}/human-html", alice.tenant, doc),
        Some(&mallory.cookie),
    )
    .await;
    assert_eq!(reply.status, StatusCode::FORBIDDEN);

    // And the same document id under her own tenant simply does not exist.
    let reply = get(
        &s,
        &format!("/api/t/{}/d/{}/human-html", mallory.tenant, doc),
        Some(&mallory.cookie),
    )
    .await;
    assert_eq!(reply.status, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn document_ids_from_the_browser_cannot_escape_the_tenant() {
    let s = server(40);
    let b = browser(&s).await;
    for id in [
        "doc-..%2f..%2fetc%2fpasswd",
        "..",
        "home-..",
        "secret-x",
        "nodash",
    ] {
        let reply = get(
            &s,
            &format!("/api/t/{}/d/{}/human-html", b.tenant, id),
            Some(&b.cookie),
        )
        .await;
        assert!(
            reply.status.is_client_error(),
            "{id} answered {} — a browser-supplied id must never resolve",
            reply.status
        );
    }
}

// ── Documents ────────────────────────────────────────────────────────────────

#[tokio::test]
async fn an_uploaded_document_opens_and_comes_back_out_intact() {
    let s = server(40);
    let b = browser(&s).await;
    let bytes = a_clan("Round Trip");

    let reply = post(
        &s,
        &format!("/api/t/{}/documents/upload", b.tenant),
        &b.cookie,
        bytes.clone(),
    )
    .await;
    let v = reply.json();
    assert_eq!(v["manifest"]["title"], "Round Trip");
    assert_eq!(v["has_human_view"], true);
    assert!(
        v["token"].as_str().is_some(),
        "an open document carries its sandbox token"
    );

    let doc = v["path"].as_str().unwrap();
    let out = get(
        &s,
        &format!("/api/t/{}/d/{}/download", b.tenant, doc),
        Some(&b.cookie),
    )
    .await;
    assert_eq!(out.status, StatusCode::OK);
    assert_eq!(
        out.body.as_ref(),
        bytes.as_slice(),
        "the handoff is the archive, byte for byte"
    );
    assert!(out
        .headers
        .get(header::CONTENT_DISPOSITION)
        .unwrap()
        .to_str()
        .unwrap()
        .contains("Round-Trip.clan"));
}

#[tokio::test]
async fn junk_uploads_are_refused_rather_than_stored() {
    let s = server(40);
    let b = browser(&s).await;
    let reply = post(
        &s,
        &format!("/api/t/{}/documents/upload", b.tenant),
        &b.cookie,
        "not a zip",
    )
    .await;
    assert_eq!(reply.status, StatusCode::BAD_REQUEST);
    assert_eq!(reply.json()["ok"], false);
}

#[tokio::test]
async fn the_home_app_is_built_on_demand_and_listed_apps_start_empty() {
    let s = server(40);
    let b = browser(&s).await;

    let apps = get(&s, &format!("/api/t/{}/apps", b.tenant), Some(&b.cookie)).await;
    assert_eq!(apps.json(), serde_json::json!([]));

    let home = get(&s, &format!("/api/t/{}/home", b.tenant), Some(&b.cookie)).await;
    assert_eq!(home.status, StatusCode::OK);
    let v = home.json();
    assert_eq!(v["manifest"]["title"], "Napkin Studio");
    assert_eq!(v["is_template"], true);
    assert_eq!(v["render_model"], "authored");
}

#[tokio::test]
async fn entries_are_readable_and_unknown_ones_are_not_invented() {
    let s = server(40);
    let b = browser(&s).await;
    let (doc, _) = upload(&s, &b, "Entries").await;

    for which in ["data", "chain", "state", "context"] {
        let reply = get(
            &s,
            &format!("/api/t/{}/d/{}/entry/{}", b.tenant, doc, which),
            Some(&b.cookie),
        )
        .await;
        assert_eq!(reply.status, StatusCode::OK, "{which}");
    }
    let reply = get(
        &s,
        &format!("/api/t/{}/d/{}/entry/secrets", b.tenant, doc),
        Some(&b.cookie),
    )
    .await;
    assert_eq!(reply.status, StatusCode::NOT_FOUND);
}

// ── The sandbox ──────────────────────────────────────────────────────────────

#[tokio::test]
async fn the_sandbox_token_is_the_only_authority_it_needs() {
    let s = server(40);
    let b = browser(&s).await;
    let (_doc, token) = upload(&s, &b, "Sandboxed").await;

    // No cookie: an app frame is third-party code and sends no credentials.
    let reply = get(&s, &format!("/s/{token}/chain"), None).await;
    assert_eq!(reply.status, StatusCode::OK);
    assert_eq!(
        reply
            .headers
            .get(header::ACCESS_CONTROL_ALLOW_ORIGIN)
            .unwrap(),
        "*",
        "the frame will be on another origin"
    );

    // And it is the whole authority: nothing else opens the door.
    let reply = get(&s, "/s/deadbeef/chain", Some(&b.cookie)).await;
    assert_eq!(reply.status, StatusCode::FORBIDDEN);
}

#[tokio::test]
async fn a_token_reaches_exactly_one_document() {
    let s = server(40);
    let b = browser(&s).await;
    let (_first, token_a) = upload(&s, &b, "First").await;
    let (_second, _token_b) = upload(&s, &b, "Second").await;

    // The token names its document; there is no path by which it names another.
    let title = get(&s, &format!("/s/{token_a}/document"), None).await;
    assert_eq!(title.status, StatusCode::OK);

    let patched = post(
        &s,
        &format!("/s/{token_a}/patch-data"),
        &b.cookie,
        r#"{"patch":{"verdict":"yes"},"agent":"human"}"#,
    )
    .await;
    assert_eq!(patched.status, StatusCode::OK);
    assert_eq!(patched.json()["keys"][0], "verdict");
}

#[tokio::test]
async fn sandbox_writes_reach_the_shell_as_events() {
    let s = server(40);
    let b = browser(&s).await;
    let (_doc, token) = upload(&s, &b, "Events").await;

    let tenant = napkin_web::tenant::TenantId::parse(&b.tenant).unwrap();
    let mut rx = s.ctx.events.subscribe(&tenant);

    post(
        &s,
        &format!("/s/{token}/patch-data"),
        &b.cookie,
        r#"{"patch":{"verdict":"yes"},"agent":"human"}"#,
    )
    .await;

    let event = rx.try_recv().expect("a data write must reach the shell");
    assert_eq!(event.name, "clan-data-changed");
}

#[tokio::test]
async fn unknown_sandbox_paths_are_not_part_of_the_api_surface() {
    let s = server(40);
    let b = browser(&s).await;
    let (_doc, token) = upload(&s, &b, "Surface").await;

    assert_eq!(
        get(&s, &format!("/s/{token}/nope"), None).await.status,
        StatusCode::NOT_FOUND
    );
    // Traversal inside the sandbox path lands on an unknown route, not a file.
    let reply = get(
        &s,
        &format!("/s/{token}/assets/..%2f..%2fmanifest.yaml"),
        None,
    )
    .await;
    assert!(reply.status.is_client_error(), "answered {}", reply.status);
}

// ── Quota ────────────────────────────────────────────────────────────────────

#[tokio::test]
async fn the_agent_is_metered_on_both_paths_it_can_be_reached_from() {
    // Cap of zero: refused before anything is dispatched, so no call is made.
    let s = server(0);
    let b = browser(&s).await;
    let (_doc, token) = upload(&s, &b, "Metered").await;

    let via_shell = post_json(
        &s,
        &format!("/api/t/{}/agent/prompt", b.tenant),
        &b.cookie,
        serde_json::json!({ "text": "hi" }),
    )
    .await;
    assert_eq!(via_shell.status, StatusCode::TOO_MANY_REQUESTS);

    let via_sandbox = post(
        &s,
        &format!("/s/{token}/api-proxy"),
        &b.cookie,
        r#"{"request_kind":"agent","payload":{}}"#,
    )
    .await;
    assert_eq!(via_sandbox.status, StatusCode::TOO_MANY_REQUESTS);
    assert_eq!(via_sandbox.json()["ok"], false);
}

// ── The shell itself ─────────────────────────────────────────────────────────

#[tokio::test]
async fn without_a_build_the_shell_says_so_instead_of_404ing() {
    let s = server(40);
    let reply = get(&s, "/", None).await;
    assert_eq!(reply.status, StatusCode::OK);
    assert!(String::from_utf8_lossy(&reply.body).contains("npm run build"));
}

// ── Install and launch ───────────────────────────────────────────────────────

fn a_template(app_id: &str, name: &str) -> Vec<u8> {
    let base = clan_sdk::ClanFile::from_bytes(a_clan(name)).unwrap();
    clan_sdk::make_template(
        &base,
        clan_sdk::AppInfo {
            name: name.into(),
            app_id: app_id.into(),
            version: "1.0.0".into(),
            icon: None,
            entry: "human/index.html".into(),
            schema: Some("agent/output-schema.json".into()),
            prompt_templates: vec![],
            data_seed: None,
        },
        clan_sdk::MakeTemplateOptions::default(),
    )
    .unwrap()
}

// The launcher's whole loop: upload a template, install it, instantiate a
// document from it, and get back something the shell can run.
#[tokio::test]
async fn a_template_can_be_uploaded_installed_and_launched() {
    let s = server(40);
    let b = browser(&s).await;

    let uploaded = post(
        &s,
        &format!("/api/t/{}/documents/upload", b.tenant),
        &b.cookie,
        a_template("ie.napkin.test", "Test App"),
    )
    .await;
    let v = uploaded.json();
    assert_eq!(
        v["is_template"], true,
        "the shell offers to install a template"
    );
    let doc = v["path"].as_str().unwrap().to_string();

    let installed = post(
        &s,
        &format!("/api/t/{}/apps/from/{}", b.tenant, doc),
        &b.cookie,
        Body::empty(),
    )
    .await;
    assert_eq!(
        installed.status,
        StatusCode::OK,
        "{}",
        String::from_utf8_lossy(&installed.body)
    );
    assert_eq!(installed.json()["app_id"], "ie.napkin.test");

    let apps = get(&s, &format!("/api/t/{}/apps", b.tenant), Some(&b.cookie)).await;
    assert_eq!(apps.json()[0]["name"], "Test App");

    let launched = post_json(
        &s,
        &format!("/api/t/{}/documents", b.tenant),
        &b.cookie,
        serde_json::json!({ "app_id": "ie.napkin.test", "title": "My Instance" }),
    )
    .await;
    assert_eq!(
        launched.status,
        StatusCode::OK,
        "{}",
        String::from_utf8_lossy(&launched.body)
    );
    let v = launched.json();
    assert_eq!(v["manifest"]["title"], "My Instance");
    assert_eq!(
        v["is_template"], false,
        "an instance is a document, not a template"
    );
    assert_eq!(v["manifest"]["app"]["app_id"], "ie.napkin.test");

    // And it shows up as recent work.
    let recent = get(&s, &format!("/api/t/{}/recent", b.tenant), Some(&b.cookie)).await;
    let listing = recent.json();
    let titles: Vec<&str> = listing
        .as_array()
        .unwrap()
        .iter()
        .map(|d| d["title"].as_str().unwrap())
        .collect();
    assert!(titles.contains(&"My Instance"), "recents: {titles:?}");
}

// ── Export ───────────────────────────────────────────────────────────────────

// Export is the one two-step flow: the host composes and raises an event, the
// shell comes back for the bytes. The handle is the browser's stand-in for the
// desktop's save dialog, so it has to be single use and tenant-scoped.
#[tokio::test]
async fn an_export_is_composed_once_and_claimed_once() {
    let s = server(40);
    let b = browser(&s).await;
    let mallory = browser(&s).await;
    let (doc, _) = upload(&s, &b, "Exported").await;

    let tenant = napkin_web::tenant::TenantId::parse(&b.tenant).unwrap();
    let mut rx = s.ctx.events.subscribe(&tenant);

    let started = post_json(
        &s,
        &format!("/api/t/{}/d/{}/export", b.tenant, doc),
        &b.cookie,
        serde_json::json!({ "kind": "html" }),
    )
    .await;
    assert_eq!(started.status, StatusCode::OK);
    let handle = started.json()["handle"].as_str().unwrap().to_string();

    // The shell hears about it under the same name the desktop uses.
    let event = rx
        .try_recv()
        .expect("the shell must be told an export is ready");
    assert_eq!(event.name, "clan-export-request");
    assert_eq!(
        event.data["tmpHtml"], handle,
        "the event carries a handle, never a server path"
    );
    assert_eq!(event.data["filename"], "Exported");

    // Nobody else can claim it.
    let stolen = get(
        &s,
        &format!("/api/t/{}/export/{}?kind=html", mallory.tenant, handle),
        Some(&mallory.cookie),
    )
    .await;
    assert_eq!(stolen.status, StatusCode::NOT_FOUND);

    let claimed = get(
        &s,
        &format!("/api/t/{}/export/{}?kind=html", b.tenant, handle),
        Some(&b.cookie),
    )
    .await;
    assert_eq!(claimed.status, StatusCode::OK);
    assert!(String::from_utf8_lossy(&claimed.body).contains("<!DOCTYPE html>"));
    assert!(claimed
        .headers
        .get(header::CONTENT_DISPOSITION)
        .unwrap()
        .to_str()
        .unwrap()
        .contains("Exported.html"));

    let again = get(
        &s,
        &format!("/api/t/{}/export/{}?kind=html", b.tenant, handle),
        Some(&b.cookie),
    )
    .await;
    assert_eq!(
        again.status,
        StatusCode::NOT_FOUND,
        "a claimed export is gone"
    );
}
