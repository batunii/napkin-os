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

/// The same document answers the same way on the server and in the browser.
///
/// napkin-wasm is `napkin_host::handle` over a session acting as the local
/// user; napkin-web is the same table behind a token, acting as the tenant.
/// For one `.clan` the two must agree on what an app reads — its decision
/// chain above all, which is what the Research Tool counts History and its
/// blockers from — and on how the shell describes it when opened. (They once
/// seemed not to: the web shell had opened home underneath the document, so
/// its frame read home's empty chain. That was the shell's bookkeeping, pinned
/// in app/tests/httpHost.test.ts; this pins the hosts.)
#[tokio::test]
async fn the_server_and_the_browser_host_answer_alike_for_one_document() {
    let s = server(40);
    let b = browser(&s).await;
    let (doc, token) = upload(&s, &b, "Parity").await;
    // Give it a history: a person's write, then an agent's.
    for body in [
        r#"{"patch":{"verdict":"yes"},"agent":"human","rationale":"the brief says so"}"#,
        r#"{"patch":{"notes":"checked"},"agent":"agent","rationale":"a second look"}"#,
    ] {
        let r = post(&s, &format!("/s/{token}/patch-data"), &b.cookie, body).await;
        assert_eq!(
            r.status,
            StatusCode::OK,
            "{}",
            String::from_utf8_lossy(&r.body)
        );
    }
    let web_open = get(&s, &format!("/api/t/{}/d/{doc}", b.tenant), Some(&b.cookie))
        .await
        .json();
    let bytes = get(
        &s,
        &format!("/api/t/{}/d/{doc}/download", b.tenant),
        Some(&b.cookie),
    )
    .await
    .body;

    // The browser's host, as napkin-wasm builds it, over the downloaded file.
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("parity.clan");
    std::fs::write(&path, &bytes).unwrap();
    let device = napkin_host::Session::with_ctx(
        Arc::new(napkin_host::FsStore::new(dir.path().to_path_buf())),
        napkin_host::Ctx::local(),
    );
    let device_open = serde_json::to_value(device.open(path.into()).unwrap()).unwrap();

    for key in [
        "validation",
        "has_human_view",
        "render_model",
        "is_template",
        "trusted",
    ] {
        assert_eq!(
            web_open[key], device_open[key],
            "open result differs on {key}"
        );
    }
    for key in [
        "title",
        "id",
        "version",
        "updated_at",
        "sha256",
        "file_count",
    ] {
        assert_eq!(
            web_open["manifest"][key], device_open["manifest"][key],
            "manifest differs on {key}"
        );
    }

    for route in ["/chain", "/capabilities", "/spinoff-targets"] {
        let web = get(&s, &format!("/s/{token}{route}"), None).await;
        let dev = napkin_host::handle(
            &device,
            &NoConfig,
            napkin_host::HostRequest::new(route, "", Vec::new()),
        );
        assert_eq!(web.status.as_u16(), dev.status, "{route} status");
        let web: Value = serde_json::from_slice(&web.body).unwrap();
        let dev: Value = serde_json::from_slice(&dev.body).unwrap();
        assert_eq!(web, dev, "{route} differs between the hosts");
    }
    let chain: Value = serde_json::from_slice(
        &napkin_host::handle(
            &device,
            &NoConfig,
            napkin_host::HostRequest::new("/chain", "", Vec::new()),
        )
        .body,
    )
    .unwrap();
    assert!(
        chain["decisions"].as_array().is_some_and(|d| d.len() >= 2),
        "the writes must be in the chain both hosts read: {chain}"
    );
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
async fn an_export_the_app_builds_reaches_the_browser_as_a_handle_it_can_fetch() {
    let s = server(40);
    let b = browser(&s).await;
    let (_doc, token) = upload(&s, &b, "Export").await;

    let tenant = napkin_web::tenant::TenantId::parse(&b.tenant).unwrap();
    let mut rx = s.ctx.events.subscribe(&tenant);

    // What Brief Maker and Research do: build the HTML, hand it to clan://export.
    let r = post(
        &s,
        &format!("/s/{token}/export"),
        &b.cookie,
        r#"{"kind":"html","filename":"brief","html":"<!DOCTYPE html><html><body><h1>Probe</h1></body></html>"}"#,
    )
    .await;
    assert_eq!(r.status, StatusCode::OK);

    let event = rx.try_recv().expect("the export must reach the shell");
    assert_eq!(event.name, "clan-export-request");
    let handle = event.data["tmpHtml"].as_str().unwrap().to_string();
    assert!(!handle.contains('/'), "the browser is given a handle, never a server path: {handle}");

    let dl = get(&s, &format!("/api/t/{}/export/{handle}?kind=html", b.tenant), Some(&b.cookie)).await;
    assert_eq!(dl.status, StatusCode::OK, "the shell's download of that handle must work");
    assert!(String::from_utf8_lossy(&dl.body).contains("Probe"));
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

fn middleware_task(task: &str) -> String {
    serde_json::json!({ "request_kind": "middleware",
                        "payload": { "task": task, "input": { "job_id": "job_1" } } })
    .to_string()
}

// Owner decision: only submitting work spends the quota. Polling a job is
// free; a body the meter cannot read as a poll is not.
#[tokio::test]
async fn only_task_submissions_spend_the_agent_quota() {
    let s = server(2);
    let b = browser(&s).await;
    let (_doc, token) = upload(&s, &b, "Intake").await;
    let tenant = s.ctx.tokens.resolve(&token).unwrap().tenant;
    let proxy = format!("/s/{token}/api-proxy");

    // No endpoint is configured, so each call is answered without a network
    // round trip — but a charged call is charged before it is dispatched.
    for _ in 0..5 {
        let r = post(&s, &proxy, &b.cookie, middleware_task("job_status")).await;
        assert_eq!(r.status, StatusCode::OK);
    }
    assert_eq!(s.ctx.meter.usage(&tenant).used, 0, "polls are free");

    let r = post(&s, &proxy, &b.cookie, middleware_task("start_campaign")).await;
    assert_eq!(r.status, StatusCode::OK);
    assert_eq!(s.ctx.meter.usage(&tenant).used, 1);
    let r = post(&s, &proxy, &b.cookie, middleware_task("answer_question")).await;
    assert_eq!(r.status, StatusCode::OK);
    assert_eq!(s.ctx.meter.usage(&tenant).used, 2);

    // The cap bites on submissions; polls of the job still get through.
    let r = post(&s, &proxy, &b.cookie, middleware_task("compose_report")).await;
    assert_eq!(r.status, StatusCode::TOO_MANY_REQUESTS);
    let r = post(&s, &proxy, &b.cookie, middleware_task("job_status")).await;
    assert_eq!(r.status, StatusCode::OK);
    assert_eq!(s.ctx.meter.usage(&tenant).used, 2);
}

#[tokio::test]
async fn a_malformed_proxy_body_is_charged() {
    let s = server(40);
    let b = browser(&s).await;
    let (_doc, token) = upload(&s, &b, "Malformed").await;
    let tenant = s.ctx.tokens.resolve(&token).unwrap().tenant;
    let proxy = format!("/s/{token}/api-proxy");

    let bodies = [
        r#"{"request_kind":"middleware","payload":{"task":"job_status""#.to_string(),
        r#"{"request_kind":"middleware","payload":{}}"#.to_string(),
        r#"{"payload":{"task":"job_status"}}"#.to_string(),
        "not json".to_string(),
    ];
    for (n, body) in bodies.iter().enumerate() {
        post(&s, &proxy, &b.cookie, body.clone()).await;
        assert_eq!(s.ctx.meter.usage(&tenant).used, n as u32 + 1, "{body}");
    }
}

// ── The shell itself ─────────────────────────────────────────────────────────

#[tokio::test]
async fn without_a_build_the_shell_says_so_instead_of_404ing() {
    let s = server(40);
    let reply = get(&s, "/", None).await;
    assert_eq!(reply.status, StatusCode::OK);
    assert!(String::from_utf8_lossy(&reply.body).contains("npm run build"));
}

/// A server with a (stand-in) shell build to serve.
fn server_with_shell() -> (Server, tempfile::TempDir) {
    let shell = tempfile::tempdir().unwrap();
    std::fs::write(
        shell.path().join("index.html"),
        "<!doctype html><title>shell</title>",
    )
    .unwrap();
    std::fs::write(shell.path().join("sw.js"), "// worker").unwrap();
    let mut s = server(40);
    s.app = napkin_web::router(s.ctx.clone(), Some(shell.path()), false);
    (s, shell)
}

#[tokio::test]
async fn the_public_viewer_is_the_shell_and_starts_no_session() {
    let (s, _shell) = server_with_shell();
    for path in ["/view", "/view/"] {
        let reply = get(&s, path, None).await;
        assert_eq!(reply.status, StatusCode::OK, "{path}");
        assert!(String::from_utf8_lossy(&reply.body).contains("<title>shell</title>"));
        assert!(
            reply.headers.get(header::SET_COOKIE).is_none(),
            "{path} must not mint a tenant for someone who only opened a file"
        );
    }
    // The installed app's worker is served beside it, also without a session.
    let sw = get(&s, "/sw.js", None).await;
    assert_eq!(sw.status, StatusCode::OK);
    assert!(sw.headers.get(header::SET_COOKIE).is_none());
}

// ── Install and launch ───────────────────────────────────────────────────────

fn a_template(app_id: &str, name: &str) -> Vec<u8> {
    a_template_with(app_id, name, None)
}

fn a_template_with(app_id: &str, name: &str, spinoff: Option<clan_sdk::SpinoffSpec>) -> Vec<u8> {
    let base = clan_sdk::ClanFile::from_bytes(a_clan(name)).unwrap();
    clan_sdk::make_template(
        &base,
        clan_sdk::AppInfo {
            home: None,
            name: name.into(),
            app_id: app_id.into(),
            version: "1.0.0".into(),
            icon: None,
            entry: "human/index.html".into(),
            schema: Some("agent/output-schema.json".into()),
            prompt_templates: vec![],
            data_seed: None,
            spinoff,
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

// ── Client review, through the sandbox ───────────────────────────────────────
//
// The server serves the host's own routes behind the token (Contract 4 §8.1
// item 6, §8.2): which parts a client's words were about is the host's own
// word match — the middleware is never asked — and every decision is the
// tenant's: its person as the recorder, its workspace as the scope.

const SMP: &str = "single_minded_proposition";
const SAID: &str =
    "Honestly this isn't the brief we talked about. The proposition doesn't feel like us.";

/// A middleware that counts what it is asked, and answers nothing useful.
/// What it was asked is kept in memory for the test to read, and nowhere
/// else.
async fn ellis() -> (String, Arc<std::sync::Mutex<Vec<Value>>>) {
    let asked = Arc::new(std::sync::Mutex::new(Vec::new()));
    let seen = asked.clone();
    let app = Router::new().route(
        "/v1/tasks",
        axum::routing::post(move |axum::Json(body): axum::Json<Value>| {
            let seen = seen.clone();
            async move {
                seen.lock().unwrap().push(body);
                axum::Json(serde_json::json!({ "error": { "type": "unknown_task", "message": "no" } }))
            }
        }),
    );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}/v1/tasks", listener.local_addr().unwrap());
    tokio::spawn(async move { axum::serve(listener, app).await.unwrap() });
    (url, asked)
}

/// A workspace whose `middleware` proxy is `endpoint`.
struct WithMiddleware(String);

impl napkin_host::HostConfig for WithMiddleware {
    fn workspace(&self) -> Option<napkin_host::WorkspaceConfig> {
        let mut ws = napkin_host::WorkspaceConfig::default();
        ws.proxies.insert(
            "middleware".into(),
            napkin_host::config::ProxyConfig {
                endpoint: self.0.clone(),
                auth_kind: None,
                secret_ref: None,
            },
        );
        Some(ws)
    }
    fn secret(&self, _: &str) -> Option<String> {
        None
    }
}

fn server_with(config: Arc<dyn napkin_host::HostConfig>) -> Server {
    let dir = tempfile::tempdir().unwrap();
    let ctx = Arc::new(AppCtx::new(dir.path().to_path_buf(), config, None, 40));
    let app = napkin_web::router(ctx.clone(), None, false);
    Server {
        _dir: dir,
        ctx,
        app,
    }
}

/// POST a sandbox route and insist on a 200.
async fn clan_ok(s: &Server, token: &str, route: &str, body: Value) -> Value {
    let r = post(s, &format!("/s/{token}{route}"), "", body.to_string()).await;
    assert_eq!(
        r.status,
        StatusCode::OK,
        "{route}: {}",
        String::from_utf8_lossy(&r.body)
    );
    r.json()
}

/// A brief with three parts, locked through the sandbox.
async fn a_locked_brief(s: &Server, b: &Browser) -> (String, String) {
    let (doc, token) = upload(s, b, "Brief").await;
    clan_ok(
        s,
        &token,
        "/patch-data",
        serde_json::json!({ "agent": "human", "rationale": "the brief", "patch": {
            SMP: "Summer tastes better without the hangover.",
            "audience": { "primary": "Adults 25-40" },
            "tone": "Warm, dry, a little wry",
        } }),
    )
    .await;
    clan_ok(s, &token, "/approve", serde_json::json!({})).await;
    (doc, token)
}

fn client_parts() -> Value {
    serde_json::json!([
        { "address": SMP, "label": "Single-minded proposition" },
        { "address": "audience", "label": "Audience" },
        { "address": "tone", "label": "Tone" },
    ])
}

#[tokio::test]
async fn a_clients_rejection_is_matched_by_the_host_and_made_good_through_the_sandbox() {
    let (url, asked) = ellis().await;
    let s = server_with(Arc::new(WithMiddleware(url)));
    let b = browser(&s).await;
    let (_doc, token) = a_locked_brief(&s, &b).await;
    let tenant = b.tenant.clone();

    let r = clan_ok(
        &s,
        &token,
        "/client-review",
        serde_json::json!({ "answer": "rejected", "client": { "name": "Jane Murphy" },
                            "channel": "pasted_email", "said": SAID, "parts": client_parts() }),
    )
    .await;
    assert_eq!(r["suggestions"]["status"], "found", "{r}");
    assert_eq!(
        r["clan"].is_object(),
        true,
        "the reply carries the document now"
    );
    assert_eq!(asked.lock().unwrap().len(), 0, "the middleware is never asked");

    // Every decision is the tenant's: its person records, its workspace scopes.
    let chain = get(&s, &format!("/s/{token}/chain"), None).await.json();
    let decisions = chain["decisions"].as_array().unwrap();
    let (suggestion, answer) = (&decisions[0], &decisions[1]);
    assert_eq!(answer["action"], "client_answer");
    assert_eq!(answer["actor"], format!("human:{tenant}"));
    assert_eq!(answer["scope"]["org"], tenant.as_str());
    assert_eq!(answer["said"], SAID, "verbatim");
    assert_eq!(suggestion["action"], "suggest_part");
    assert_eq!(suggestion["actor"], "process:host");
    assert_eq!(suggestion["handler"], "client_parts_match@1");
    assert_eq!(
        suggestion["quote"],
        "The proposition doesn't feel like us."
    );
    assert_eq!(suggestion["scope"]["org"], tenant.as_str());

    let view = get(&s, &format!("/s/{token}/decisions"), None).await.json();
    assert_eq!(
        view["client"]["suggestions"][0]["decision"],
        r["suggestions"]["decisions"][0]
    );
    assert_eq!(view["lock"]["locked"], true);
    assert_eq!(
        view["lock"]["can_lock"], false,
        "which parts is not known yet"
    );

    let c = clan_ok(
        &s,
        &token,
        "/client-review/confirm",
        serde_json::json!({ "suggestion": r["suggestions"]["decisions"][0], "confirm": true }),
    )
    .await;
    let part = c["decision"].as_str().unwrap().to_string();
    let u = clan_ok(
        &s,
        &token,
        "/client-review/reopen",
        serde_json::json!({ "answer": part }),
    )
    .await;
    // No words typed under the part: the document's words, not the quote.
    assert_eq!(
        u["reason"],
        "Jane Murphy asked: Honestly this isn't the brief we talked about. The proposition doesn't feel like us."
    );
    let path = u["address"]
        .as_str()
        .unwrap()
        .split_once('#')
        .unwrap()
        .1
        .to_string();
    assert_eq!(path, SMP);
    clan_ok(
        &s,
        &token,
        "/edit",
        serde_json::json!({ "path": path, "value": "Summer, but ours.", "rationale": u["reason"], "answers": part }),
    )
    .await;
    clan_ok(&s, &token, "/approve", serde_json::json!({})).await;
    let view = get(&s, &format!("/s/{token}/decisions"), None).await.json();
    assert_eq!(view["client"]["parts"][0]["answered"], true);
    assert_eq!(view["lock"]["reopened"], serde_json::json!([]));
    assert_eq!(
        view["client"]["available"], true,
        "locked again, a client can answer"
    );
}

#[tokio::test]
async fn without_a_middleware_the_answer_is_recorded_with_its_suggestions_and_parts_are_marked_by_hand() {
    let s = server(40);
    let b = browser(&s).await;
    let (_doc, token) = a_locked_brief(&s, &b).await;
    let r = clan_ok(
        &s,
        &token,
        "/client-review",
        serde_json::json!({ "answer": "rejected", "client": { "name": "Jane Murphy" },
                            "channel": "pasted_email", "said": SAID, "parts": client_parts() }),
    )
    .await;
    assert_eq!(r["suggestions"]["status"], "found", "the match needs no middleware");
    assert_eq!(r["suggestions"]["decisions"].as_array().unwrap().len(), 1);
    let m = clan_ok(
        &s,
        &token,
        "/client-review/confirm",
        serde_json::json!({ "review": r["decision"], "address": "tone", "answer": "accepted_with_changes" }),
    )
    .await;
    let view = get(&s, &format!("/s/{token}/decisions"), None).await.json();
    assert_eq!(view["client"]["parts"][0]["decision"], m["decision"]);
    assert_eq!(
        view["lock"]["can_lock"], true,
        "a change asked never blocks"
    );

    // Refused as the host refuses: a client answers a locked document only.
    let (_open, fresh) = upload(&s, &b, "Unlocked").await;
    let refused = post(
        &s,
        &format!("/s/{fresh}/client-review"),
        "",
        serde_json::json!({ "answer": "accepted", "client": { "name": "Jane" },
                            "channel": "none", "parts": [] })
        .to_string(),
    )
    .await;
    assert_eq!(refused.status, StatusCode::CONFLICT);
}

#[tokio::test]
async fn upstream_answers_on_the_server_as_on_the_device() {
    let s = server(40);
    let b = browser(&s).await;
    let (doc, token) = a_locked_brief(&s, &b).await;
    let web = get(&s, &format!("/s/{token}/upstream"), None).await;
    assert_eq!(web.status, StatusCode::OK);
    let web = web.json();
    assert_eq!(web["carried"], Value::Null, "not spun off");
    assert_eq!(web["upstream"], serde_json::json!([]));

    let bytes = get(
        &s,
        &format!("/api/t/{}/d/{doc}/download", b.tenant),
        Some(&b.cookie),
    )
    .await
    .body;
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("brief.clan");
    std::fs::write(&path, &bytes).unwrap();
    let device = napkin_host::Session::with_ctx(
        Arc::new(napkin_host::FsStore::new(dir.path().to_path_buf())),
        napkin_host::Ctx::local(),
    );
    device.open(path.into()).unwrap();
    let dev = napkin_host::handle(
        &device,
        &NoConfig,
        napkin_host::HostRequest::new("/upstream", "", Vec::new()),
    );
    assert_eq!(dev.status, 200);
    assert_eq!(web, serde_json::from_slice::<Value>(&dev.body).unwrap());
}

#[tokio::test]
async fn upstream_names_the_tenants_parent_and_what_changed_in_it() {
    let s = server(40);
    let b = browser(&s).await;
    let t = |p: &str| format!("/api/t/{}{p}", b.tenant);
    let spec = clan_sdk::SpinoffSpec {
        upstream: true,
        ..Default::default()
    };
    let up = post(
        &s,
        &t("/documents/upload"),
        &b.cookie,
        a_template_with("ie.napkin.child", "Child", Some(spec)),
    )
    .await;
    let template = up.json()["path"].as_str().unwrap().to_string();
    let installed = post(
        &s,
        &t(&format!("/apps/from/{template}")),
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

    let (parent, parent_token) = upload(&s, &b, "Parent").await;
    clan_ok(
        &s,
        &parent_token,
        "/patch-data",
        serde_json::json!({ "agent": "human", "rationale": "first", "patch": { "tone": "Warm" } }),
    )
    .await;
    let child = post_json(
        &s,
        &t(&format!("/d/{parent}/spinoff")),
        &b.cookie,
        serde_json::json!({ "app_id": "ie.napkin.child" }),
    )
    .await;
    assert_eq!(
        child.status,
        StatusCode::OK,
        "{}",
        String::from_utf8_lossy(&child.body)
    );
    let child_token = child.json()["token"].as_str().unwrap().to_string();

    let v = get(&s, &format!("/s/{child_token}/upstream"), None)
        .await
        .json();
    let parent_id = v["carried"]["document_id"].as_str().unwrap().to_string();
    assert_eq!(v["upstream"][0]["document_id"], parent_id.as_str());
    assert_eq!(
        v["upstream"][0]["in_store"], true,
        "the tenant's own store holds it"
    );
    assert_eq!(v["upstream"][0]["status"], "current");

    clan_ok(&s, &parent_token, "/patch-data",
            serde_json::json!({ "agent": "human", "rationale": "later", "patch": { "tone": "Bright" } })).await;
    let v = get(&s, &format!("/s/{child_token}/upstream"), None)
        .await
        .json();
    assert_eq!(v["upstream"][0]["status"], "changed");
    assert_eq!(v["upstream"][0]["changed"]["data"], true);
    assert_eq!(v["upstream"][0]["decisions_since"], 1);
}

// ── Accounts (NAPKIN_AUTH=local) ─────────────────────────────────────────────

fn accounts_server() -> (Server, napkin_web::auth::Local) {
    let dir = tempfile::tempdir().unwrap();
    let ctx = Arc::new(AppCtx::new(dir.path().to_path_buf(), Arc::new(NoConfig), None, 40));
    let users = dir.path().join("accounts.json");
    let identity = napkin_web::tenant::Identity {
        mode: napkin_web::tenant::Mode::Accounts {
            provider: napkin_web::auth::Provider::Local(napkin_web::auth::Local::new(users.clone())),
            sessions: napkin_web::auth::Sessions::new(&[9u8; 32]).unwrap(),
        },
        secure: false,
    };
    let app = napkin_web::router_with(ctx.clone(), None, Arc::new(identity));
    (Server { _dir: dir, ctx, app }, napkin_web::auth::Local::new(users))
}

fn json_body(v: Value) -> Body {
    Body::from(v.to_string())
}

fn json_req(uri: &str, cookie: Option<&str>, v: Value) -> Request<Body> {
    let mut b = Request::builder().method("POST").uri(uri).header(header::CONTENT_TYPE, "application/json");
    if let Some(c) = cookie {
        b = b.header(header::COOKIE, c);
    }
    b.body(json_body(v)).unwrap()
}

fn session_cookie(r: &Reply) -> String {
    r.headers
        .get_all(header::SET_COOKIE)
        .iter()
        .filter_map(|v| v.to_str().ok())
        .find(|v| v.starts_with("napkin_session="))
        .map(|v| v.split(';').next().unwrap().to_string())
        .expect("a session cookie")
}

#[tokio::test]
async fn a_person_signs_in_as_user_at_agency_and_chooses_their_password_first() {
    let (s, local) = accounts_server();
    local.add("engineer@napkin", "Temporary-1234", Some("Shrey")).unwrap();
    local.add("visionary@napkin", "Temporary-5678", Some("Laurance")).unwrap();

    // without a session, the API asks for one; sign-in itself is answered
    let r = send(&s, request("GET", "/api/session", None, Body::empty())).await;
    assert_eq!(r.status, StatusCode::UNAUTHORIZED);
    assert_eq!(r.json()["signin"], true);

    let r = send(&s, json_req("/api/auth/sign-in", None, serde_json::json!({"username": "engineer@napkin", "password": "nope"}))).await;
    assert_eq!(r.status, StatusCode::UNAUTHORIZED);

    // the temporary password leads to choosing one's own
    let r = send(&s, json_req("/api/auth/sign-in", None,
        serde_json::json!({"username": "Engineer@Napkin", "password": "Temporary-1234"}))).await;
    assert_eq!(r.json()["step"], "new_password");
    let challenge = r.json()["session"].as_str().unwrap().to_string();
    let r = send(&s, json_req("/api/auth/new-password", None,
        serde_json::json!({"username": "engineer@napkin", "session": challenge, "password": "short"}))).await;
    assert_eq!(r.status, StatusCode::UNAUTHORIZED, "a weak password is refused");
    let r = send(&s, json_req("/api/auth/new-password", None,
        serde_json::json!({"username": "engineer@napkin", "session": challenge, "password": "MyOwnPassword9"}))).await;
    assert_eq!(r.status, StatusCode::OK);
    let mine = session_cookie(&r);

    // signed in: the agency's workspace, the person named
    let r = send(&s, request("GET", "/api/session", Some(&mine), Body::empty())).await;
    let me = r.json();
    assert_eq!(me["user"]["username"], "engineer@napkin");
    assert_eq!(me["user"]["name"], "Shrey");
    let workspace = me["tenant"].as_str().unwrap().to_string();
    assert_eq!(workspace, napkin_web::tenant::TenantId::of_agency("napkin").to_string());

    // a colleague in the same agency works in the same workspace
    let r = send(&s, json_req("/api/auth/sign-in", None,
        serde_json::json!({"username": "visionary@napkin", "password": "Temporary-5678"}))).await;
    let c = r.json()["session"].as_str().unwrap().to_string();
    let r = send(&s, json_req("/api/auth/new-password", None,
        serde_json::json!({"username": "visionary@napkin", "session": c, "password": "Laurances-own-1"}))).await;
    let theirs = session_cookie(&r);
    let r = send(&s, request("GET", "/api/session", Some(&theirs), Body::empty())).await;
    assert_eq!(r.json()["tenant"], workspace.as_str());
    assert_eq!(r.json()["user"]["username"], "visionary@napkin");

    // a workspace that is not yours is refused, and a forged cookie is no session
    let other = napkin_web::tenant::TenantId::of_agency("javelin");
    let r = send(&s, request("GET", &format!("/api/t/{other}/apps"), Some(&mine), Body::empty())).await;
    assert_eq!(r.status, StatusCode::FORBIDDEN);
    let r = send(&s, request("GET", "/api/session", Some("napkin_session=e30.forged"), Body::empty())).await;
    assert_eq!(r.status, StatusCode::UNAUTHORIZED);

    // signing out clears the cookie
    let r = send(&s, json_req("/api/auth/sign-out", Some(&mine), serde_json::json!({}))).await;
    assert!(r.headers.get_all(header::SET_COOKIE).iter().any(|v| v.to_str().unwrap().contains("Max-Age=0")));
    let _ = &s.ctx;
}
