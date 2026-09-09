// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! End-to-end coverage of the `clan://` routing table: a request in, a status,
//! a body and the events the shell is expected to act on out.
//!
//! These exercise the table itself — the part every shell shares — rather than
//! the operations underneath it, which `session`'s own tests cover.

use std::sync::Arc;

use napkin_host::{
    handle, DocId, FsStore, HostEvent, HostRequest, HostResponse, NoConfig, Session,
};

struct Fixture {
    _dir: tempfile::TempDir,
    session: Session,
}

fn fixture() -> Fixture {
    let dir = tempfile::tempdir().unwrap();
    let id = DocId::from(dir.path().join("test.clan"));
    let bytes = clan_sdk::create(clan_sdk::CreateOptions {
        title: "Route Test".into(),
        brief: "test brief".into(),
        document_type: None,
        no_render: false,
        schema: None,
    })
    .unwrap();
    std::fs::write(id.as_str(), bytes).unwrap();

    let session = Session::new(Arc::new(FsStore::new(dir.path().to_path_buf())));
    session.open(id).unwrap();
    Fixture { _dir: dir, session }
}

fn get(f: &Fixture, path: &str) -> HostResponse {
    handle(
        &f.session,
        &NoConfig,
        HostRequest::new(path, "", Vec::new()),
    )
}

fn post(f: &Fixture, path: &str, body: &str) -> HostResponse {
    handle(
        &f.session,
        &NoConfig,
        HostRequest::new(path, "", body.as_bytes().to_vec()),
    )
}

fn json(resp: &HostResponse) -> serde_json::Value {
    serde_json::from_slice(&resp.body).expect("route must answer with JSON")
}

#[test]
fn unknown_paths_are_404_with_no_cors() {
    let f = fixture();
    let resp = get(&f, "/nope");
    assert_eq!(resp.status, 404);
    assert!(resp.body.is_empty());
    assert!(
        resp.headers.is_empty(),
        "an unknown path is not part of the API surface"
    );
}

// The reason routing is exact rather than substring: /patch and /patch-data are
// different operations on different layers.
#[test]
fn patch_and_patch_data_are_distinct_routes() {
    let f = fixture();

    let resp = post(&f, "/patch", r#"{"id":"heading-0","content":"Edited"}"#);
    assert_eq!(resp.status, 200);
    assert!(matches!(resp.events.as_slice(), [HostEvent::PatchSaved(_)]));

    let resp = post(
        &f,
        "/patch-data",
        r#"{"patch":{"verdict":"yes"},"agent":"human"}"#,
    );
    assert_eq!(resp.status, 200);
    assert_eq!(json(&resp)["keys"][0], "verdict");
    assert!(matches!(
        resp.events.as_slice(),
        [HostEvent::DataChanged(_)]
    ));
}

#[test]
fn every_reachable_route_answers_with_cors_open() {
    let f = fixture();
    for path in [
        "/edit-mode",
        "/document",
        "/chain",
        "/apps",
        "/recent",
        "/capabilities",
    ] {
        let resp = get(&f, path);
        assert_eq!(resp.status, 200, "{path}");
        assert!(
            resp.headers
                .iter()
                .any(|(k, v)| k == "Access-Control-Allow-Origin" && v == "*"),
            "{path} must be reachable from the sandboxed frame",
        );
    }
}

#[test]
fn edit_mode_reflects_the_session() {
    let f = fixture();
    assert_eq!(get(&f, "/edit-mode").body, b"false");
    f.session.set_edit_mode(true);
    assert_eq!(get(&f, "/edit-mode").body, b"true");
}

// An unsigned document gets the safe subset and nothing else — the trust gate
// is the whole point of the capability routes.
#[test]
fn scoped_capabilities_are_refused_to_untrusted_apps() {
    let f = fixture();

    let caps = json(&get(&f, "/capabilities"));
    assert_eq!(caps["trusted"], false);
    assert_eq!(caps["allowed"].as_array().unwrap().len(), 0);

    for path in ["/notify", "/set-theme"] {
        let resp = post(&f, path, r#"{"title":"x","body":"y"}"#);
        assert_eq!(resp.status, 403, "{path}");
        assert!(resp.events.is_empty(), "{path} must not reach the shell");
    }
}

#[test]
fn routes_validate_their_bodies_before_touching_the_document() {
    let f = fixture();
    let cases = [
        ("/open", "{}", 400),
        ("/launch", "{}", 400),
        ("/set-title", r#"{"title":"  "}"#, 400),
        ("/set-context", r#"{"markdown":""}"#, 400),
        ("/export", r#"{"html":""}"#, 400),
        ("/fork", r#"{"agents":["only-one"]}"#, 400),
    ];
    for (path, body, want) in cases {
        let resp = post(&f, path, body);
        assert_eq!(resp.status, want, "{path}");
        assert_eq!(json(&resp)["ok"], false, "{path}");
        assert!(resp.events.is_empty(), "{path} must not reach the shell");
    }
}

// Shell-driven routes carry their request as an event rather than doing it —
// a handler cannot open a file dialog.
#[test]
fn shell_driven_routes_return_a_request_for_the_shell() {
    let f = fixture();

    let resp = post(&f, "/open", r#"{"path":"/tmp/other.clan"}"#);
    assert_eq!(resp.status, 200);
    assert_eq!(
        resp.events,
        vec![HostEvent::OpenDocument("/tmp/other.clan".into())]
    );

    assert_eq!(
        get(&f, "/open-file").events,
        vec![HostEvent::OpenFileRequest]
    );
    assert_eq!(
        get(&f, "/request-save").events,
        vec![HostEvent::RequestSave]
    );

    let resp = post(&f, "/set-title", r#"{"title":"Renamed"}"#);
    assert_eq!(resp.events, vec![HostEvent::TitleChanged("Renamed".into())]);
    assert_eq!(f.session.title().unwrap(), "Renamed");
}

#[test]
fn assets_are_served_from_inside_the_archive_and_traversal_is_refused() {
    let f = fixture();

    let resp = handle(
        &f.session,
        &NoConfig,
        HostRequest::new(
            "/upload-asset",
            "name=note.txt&agent=human",
            b"hello".to_vec(),
        ),
    );
    assert_eq!(resp.status, 200);
    assert_eq!(json(&resp)["internal_path"], "human/assets/note.txt");

    let resp = get(&f, "/assets/note.txt");
    assert_eq!(resp.status, 200);
    assert_eq!(resp.body, b"hello");

    assert_eq!(get(&f, "/assets/../manifest.yaml").status, 400);
}

// The one async route. Everything else must be answerable inline, because the
// desktop shell runs it on the WebView loop.
#[test]
fn only_the_network_route_is_async() {
    assert!(napkin_host::is_async("/api-proxy"));
    for path in ["/patch-data", "/document", "/assets/x.png", "/launch"] {
        assert!(!napkin_host::is_async(path), "{path}");
    }
}
