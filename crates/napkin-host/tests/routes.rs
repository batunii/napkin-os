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
        "/decisions",
        "/apps",
        "/recent",
        "/capabilities",
        "/spinoff-targets",
        "/upstream",
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

// The decision view follows the chain: a person's patch-data is on top, and
// it needs no attention.
#[test]
fn decisions_lists_the_chain_newest_first() {
    let f = fixture();
    post(
        &f,
        "/patch-data",
        r#"{"patch":{"verdict":"yes"},"agent":"human"}"#,
    );
    let v = json(&get(&f, "/decisions"));
    let decisions = v["decisions"].as_array().unwrap();
    assert_eq!(decisions.len(), 1, "{v}");
    assert_eq!(decisions[0]["who"]["kind"], "person");
    // No targets on a patch-data: the fields it changed stand in.
    assert_eq!(decisions[0]["targets"][0]["label"], "Verdict");
    assert_eq!(decisions[0]["who"]["name"], "You");
    assert_eq!(v["lock"]["can_lock"], true);
    assert!(v["attention"].as_array().unwrap().is_empty());
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

// The host assembles no prompt: prompts are the middleware's (M1), and the
// browser-side model call that needed one is gone.
#[test]
fn the_host_hands_out_no_prompt() {
    let f = fixture();
    let resp = post(&f, "/agent-prompt", r#"{"payload":{"task":"draft_brief"}}"#);
    assert_eq!(resp.status, 404);
}

// A document with nothing installed to receive it offers nothing — and says so
// with an empty list rather than an error, because "no next step" is a normal
// state, not a failure.
#[test]
fn spinoff_targets_is_an_empty_list_when_no_app_accepts_the_document() {
    let f = fixture();
    let resp = get(&f, "/spinoff-targets");
    assert_eq!(resp.status, 200);
    assert_eq!(json(&resp), serde_json::json!([]));
}

#[test]
fn spinoff_validates_its_body_and_reports_an_unknown_app() {
    let f = fixture();

    let resp = post(&f, "/spinoff", "{}");
    assert_eq!(resp.status, 400);
    assert!(resp.events.is_empty(), "a rejected request opens nothing");

    // Well-formed, but naming an app that is not installed: a clean status,
    // not a panic, and still nothing opened.
    let resp = post(&f, "/spinoff", r#"{"app_id":"ie.napkin.absent"}"#);
    assert_eq!(resp.status, 404);
    assert!(json(&resp)["error"]
        .as_str()
        .unwrap()
        .contains("app not installed"));
    assert!(resp.events.is_empty());
}

/// The whole path a "Continue in…" click takes: the document is listed as a
/// spin-off source, the branch is made, and the shell is told to open it.
#[test]
fn spinoff_offers_a_target_then_branches_into_it() {
    // FsStore resolves its app library from NAPKIN_APPS_DIR when that is set,
    // and installing a fixture app into a developer's real library would be
    // rude. Setting env vars from a test races every other test in the binary,
    // so skip instead.
    if std::env::var_os("NAPKIN_APPS_DIR").is_some() {
        eprintln!("skipped: NAPKIN_APPS_DIR points the library outside the temp dir");
        return;
    }

    let dir = tempfile::tempdir().unwrap();
    let store = Arc::new(FsStore::new(dir.path().to_path_buf()));

    // An installed app that takes anything as a source.
    let base = clan_sdk::ClanFile::from_bytes(
        clan_sdk::create(clan_sdk::CreateOptions {
            title: "Advertising Studio".into(),
            brief: "fixture".into(),
            document_type: None,
            no_render: false,
            schema: None,
        })
        .unwrap(),
    )
    .unwrap();
    let template = clan_sdk::make_template(
        &base,
        clan_sdk::AppInfo {
            home: None,
            name: "Advertising Studio".into(),
            app_id: "ie.napkin.film".into(),
            version: "0.1.0".into(),
            icon: None,
            entry: "human/index.html".into(),
            schema: Some("agent/output-schema.json".into()),
            prompt_templates: vec![],
            data_seed: None,
            spinoff: Some(clan_sdk::SpinoffSpec {
                map: Some("brief".into()),
                ..Default::default()
            }),
        },
        clan_sdk::MakeTemplateOptions::default(),
    )
    .unwrap();
    napkin_host::install_app(&*store, template).unwrap();

    // A source document with something worth carrying.
    let src = DocId::from(dir.path().join("source.clan"));
    std::fs::write(
        src.as_str(),
        clan_sdk::create(clan_sdk::CreateOptions {
            title: "Acme Brief".into(),
            brief: "test brief".into(),
            document_type: None,
            no_render: false,
            schema: None,
        })
        .unwrap(),
    )
    .unwrap();
    let session = Session::new(store.clone());
    session.open(src).unwrap();
    let f = Fixture { _dir: dir, session };

    // Offered…
    let targets = json(&get(&f, "/spinoff-targets"));
    assert_eq!(targets[0]["app_id"], "ie.napkin.film");
    assert_eq!(targets[0]["map"], "brief");

    // …and taken. The shell is handed the new document to open.
    let resp = post(
        &f,
        "/spinoff",
        r#"{"app_id":"ie.napkin.film","title":"FORM"}"#,
    );
    assert_eq!(resp.status, 200, "{}", String::from_utf8_lossy(&resp.body));
    let path = json(&resp)["path"].as_str().unwrap().to_string();
    match resp.events.as_slice() {
        [HostEvent::OpenDocument(opened)] => assert_eq!(*opened, path),
        other => panic!("expected exactly one OpenDocument event, got {other:?}"),
    }

    // The branch is a real, separate file that knows both its parents.
    let made = clan_sdk::ClanFile::open(&path).unwrap();
    assert_eq!(made.manifest().title, "FORM");
    assert_eq!(
        made.manifest().app.as_ref().unwrap().app_id,
        "ie.napkin.film"
    );
    assert_eq!(made.manifest().lineage.as_ref().unwrap().parents.len(), 2);
}

// An app on the middleware asks on open whether one is configured, so it can
// say so plainly. Presence only: never the endpoint or its secret.
#[test]
fn the_middleware_route_says_only_whether_one_is_configured() {
    let f = fixture();
    let resp = get(&f, "/middleware");
    assert_eq!(resp.status, 200);
    let v = json(&resp);
    assert_eq!(v["configured"], false);
    assert_eq!(v["api"], "napkin.middleware/1");
    assert_eq!(v.as_object().unwrap().len(), 2, "{v}");
}

// ── A research document spun off into a brief (Contract 4 §5, §7.4, §8.1) ──

mod upstream {
    use std::collections::HashMap;
    use std::sync::{Arc, Mutex};

    use clan_sdk::{ClanFile, DecisionChain, SpinoffSpec};
    use napkin_host::library::{self, Backref};
    use napkin_host::{
        dispatch, Actor, Change, Ctx, DocId, DocStore, HostError, HostRequest, HostResponse,
        HostResult, Library, NoConfig, PartStore, Scope, Session, Version,
    };
    use serde_json::{json, Value};

    const RESEARCH: &str = "ie.napkin.campaign-research";
    const BRIEF: &str = "ie.napkin.brief-maker";
    const DECK: &str = "ie.napkin.deck";

    /// An in-memory library, so installing fixture apps never touches a real
    /// `NAPKIN_APPS_DIR`. Instances live under `docs/`, which is what the
    /// library lists and a hoisted ancestor is found by.
    #[derive(Default)]
    struct MemStore {
        files: Mutex<HashMap<String, Vec<u8>>>,
    }

    impl MemStore {
        fn remove(&self, id: &DocId) {
            self.files.lock().unwrap().remove(id.as_str());
        }
    }

    impl PartStore for MemStore {
        fn read(&self, id: &DocId) -> HostResult<Vec<u8>> {
            self.files
                .lock()
                .unwrap()
                .get(id.as_str())
                .cloned()
                .ok_or_else(|| HostError::not_found(format!("no such document: {id}")))
        }
        fn exists(&self, id: &DocId) -> bool {
            self.files.lock().unwrap().contains_key(id.as_str())
        }
        fn apply(&self, change: &Change) -> HostResult<Version> {
            self.files
                .lock()
                .unwrap()
                .insert(change.doc.to_string(), change.bytes.clone());
            Ok(change.archive_version())
        }
    }

    impl Library for MemStore {
        fn app_candidates(&self) -> Vec<DocId> {
            let mut v: Vec<DocId> = self.keys("apps/");
            v.sort();
            v
        }
        fn app_template(&self, app_id: &str) -> DocId {
            DocId::new(format!("apps/{app_id}/app.clan"))
        }
        fn documents(&self) -> Vec<DocId> {
            self.keys("docs/")
        }
        fn new_document(&self, app_id: &str, id_short: &str) -> HostResult<DocId> {
            Ok(DocId::new(format!("docs/{app_id}-{id_short}.clan")))
        }
        fn home(&self, version_tag: &str) -> HostResult<DocId> {
            Ok(DocId::new(format!("home-{version_tag}.clan")))
        }
        fn fork_branch(&self, parent: &DocId, agent: &str) -> DocId {
            DocId::new(format!("{parent}.{agent}.clan"))
        }
    }

    impl MemStore {
        fn keys(&self, prefix: &str) -> Vec<DocId> {
            self.files
                .lock()
                .unwrap()
                .keys()
                .filter(|k| k.starts_with(prefix))
                .map(DocId::new)
                .collect()
        }
    }

    fn install(store: &dyn DocStore, name: &str, app_id: &str, spinoff: Option<SpinoffSpec>) {
        let base = ClanFile::from_bytes(
            clan_sdk::create(clan_sdk::CreateOptions {
                title: name.into(),
                brief: "fixture".into(),
                document_type: None,
                no_render: false,
                schema: None,
            })
            .unwrap(),
        )
        .unwrap();
        let tpl = clan_sdk::make_template(
            &base,
            clan_sdk::AppInfo {
                home: None,
                name: name.into(),
                app_id: app_id.into(),
                version: "0.6.0".into(),
                icon: None,
                entry: "human/index.html".into(),
                schema: Some("agent/output-schema.json".into()),
                prompt_templates: vec![],
                data_seed: None,
                spinoff,
            },
            clan_sdk::MakeTemplateOptions::default(),
        )
        .unwrap();
        library::install_app(store, tpl).unwrap();
    }

    fn carries(accepts: &str) -> SpinoffSpec {
        SpinoffSpec {
            accepts: vec![accepts.into()],
            upstream: true,
            ..Default::default()
        }
    }

    /// One document open in its own session, as a shell holds it.
    struct Doc {
        session: Session,
        id: DocId,
        doc: String,
    }

    impl Doc {
        fn open(store: &Arc<MemStore>, id: DocId) -> Self {
            let session = Session::new(store.clone());
            session.open(id.clone()).unwrap();
            let doc = session.clan_context_for_agent()["id"]
                .as_str()
                .unwrap()
                .to_string();
            Doc { session, id, doc }
        }

        fn call(&self, ctx: &Ctx, path: &str, body: &str) -> HostResponse {
            dispatch(
                ctx,
                &self.session,
                &NoConfig,
                HostRequest::new(path, "", body.as_bytes().to_vec()),
            )
        }

        fn post(&self, path: &str, body: &str) -> Value {
            let resp = self.call(self.session.ctx(), path, body);
            assert_eq!(resp.status, 200, "{path}: {}", String::from_utf8_lossy(&resp.body));
            serde_json::from_slice(&resp.body).unwrap()
        }

        fn get(&self, path: &str) -> Value {
            self.post(path, "")
        }

        fn stored(&self, store: &MemStore) -> ClanFile {
            ClanFile::from_bytes(store.read(&self.id).unwrap()).unwrap()
        }
    }

    fn chain(clan: &ClanFile) -> DecisionChain {
        DecisionChain::from_yaml(&clan.read_entry("agent/decision-chain.yaml").unwrap()).unwrap()
    }

    fn reasoning(decided: &str, cites: &[&str]) -> Value {
        json!({ "decided": decided,
                "because": [{ "point": "the cited material says so", "cites": cites }],
                "rejected": [{ "option": "leave it open", "why": "the material settles it" }],
                "certainty": { "level": "high", "why": "a verbatim quote" },
                "would_change_if": "the client says otherwise" })
    }

    fn pin(id: &str, key: &str, value: f64) -> Value {
        json!({
            "id": id, "entity": "brand/orchard-hill", "key": key, "value": value,
            "unit": "proportion", "as_of": "2026-06-30", "retrieved_at": "2026-09-12",
            "sources": ["src_4f2a"], "confidence": "high", "licence": "open",
            "status": "active", "version": 2, "supersedes": null,
            "origin": format!("fact://brand/orchard-hill/{key}@2"),
            "decision": "d_01JA0D02PIN", "pinned_at": "2026-09-21T09:00:04Z", "pin_reason": "research",
            "layer": "brand", "market": "IE", "method": "report",
        })
    }

    /// A research document as its lenses leave it: two pins, two proposed
    /// findings, and a contest over the first pin's key with a pin frozen for
    /// the other value — written the way it arrives, as a middleware change.
    fn research(store: &Arc<MemStore>) -> Doc {
        install(&**store, "Campaign Research", RESEARCH, None);
        let id = library::create_instance(&**store, RESEARCH, Some("Lúnasa 0.0 launch".into())).unwrap();
        let r = Doc::open(store, id);
        let clan = r.session.clan_context_for_agent();
        let doc = r.doc.clone();
        let mut newer = pin("f_01JA0B9Z9Z", "product.share", 0.19);
        newer["version"] = json!(3);
        let reply = json!({
            "api": "napkin.middleware/1", "task": "research_lens", "handler": "research_lens@1.0",
            "job": { "id": "job_1", "state": "done" }, "result": {},
            "change": {
                "doc": doc, "base_version": clan["version"],
                "read": { "selection.contested": null },
                "data_patch": {
                    "campaign": { "name": "Lúnasa 0.0" },
                    "selection": { "contested": [ {
                        "id": "ct_share", "key": "brand/orchard-hill:product.share@IE", "status": "open",
                        "values": [
                            { "value": 0.23, "fact_id": "f_01JA0B3P4Q", "from": "pinned", "sources": ["src_4f2a"] },
                            { "value": 0.19, "fact_id": "f_01JA0B9Z9Z", "from": "market_structure/IE",
                              "sources": ["src_77aa"], "pin": newer } ] } ] } },
                "facts_append": [ pin("f_01JA0B3P4Q", "product.share", 0.23), pin("f_01JA0B3P5R", "product.growth", 0.44) ],
                "findings_append": [
                    { "id": "fi_01JA0F2B", "statement": "Orchard Hill is growing from a small share",
                      "cites": ["f_01JA0B3P4Q", "f_01JA0B3P5R"], "method": "synthesis",
                      "status": "proposed", "derived_by": "synthesise_findings@1.0",
                      "confidence": "medium", "derived_at": "2026-09-23T10:00:01Z", "decision": "d_01JA0D06SYN" },
                    { "id": "fi_01JA0F3C", "statement": "The category rewards a clear alcohol-free claim",
                      "cites": ["f_01JA0B3P5R"], "method": "synthesis",
                      "status": "proposed", "derived_by": "synthesise_findings@1.0",
                      "confidence": "medium", "derived_at": "2026-09-23T10:00:02Z", "decision": "d_01JA0D06SYN" } ],
                "decisions": [
                    { "id": "d_01JA0D02PIN", "kind": "pin", "agent": "research_lens@1.0", "action": "research_merge",
                      "rationale": "pinned", "targets": [format!("{doc}#facts[f_01JA0B3P4Q]")],
                      "cites": ["src_4f2a"], "reasoning": reasoning("Pinned.", &["src_4f2a"]) },
                    { "id": "d_01JA0D06SYN", "kind": "finding", "agent": "research_lens@1.0",
                      "action": "proposed findings", "rationale": "the pins agree",
                      "targets": [format!("{doc}#findings[fi_01JA0F2B]"), format!("{doc}#findings[fi_01JA0F3C]")],
                      "cites": ["f_01JA0B3P4Q", "f_01JA0B3P5R"],
                      "reasoning": reasoning("Proposed findings.", &["f_01JA0B3P4Q"]) } ]
            },
            "trace": {}
        });
        let env = json!({ "ok": true, "status": 200, "endpoint": "http://m", "data": reply, "error": null });
        let (out, _) = r.session.settle_middleware(r.session.ctx(), env);
        assert_eq!(out["data"]["change"]["applied"], true, "{out}");
        r
    }

    /// `from` spun off into `app`, through the route, and opened.
    fn spin(store: &Arc<MemStore>, from: &Doc, app: &str) -> Doc {
        let v = from.post("/spinoff", &json!({ "app_id": app, "title": "Lúnasa brief" }).to_string());
        Doc::open(store, DocId::new(v["path"].as_str().unwrap()))
    }

    fn setup() -> (Arc<MemStore>, Doc, Doc) {
        let store = Arc::new(MemStore::default());
        let research = research(&store);
        install(&*store, "Brief Maker", BRIEF, Some(carries(RESEARCH)));
        let brief = spin(&store, &research, BRIEF);
        (store, research, brief)
    }

    /// Reject both findings in `d`, so nothing about them is left open.
    fn reject_findings(d: &Doc) {
        for fi in ["fi_01JA0F2B", "fi_01JA0F3C"] {
            d.post(
                "/verdict",
                &json!({ "target": format!("findings[{fi}]"), "polarity": "bad", "rationale": "Not what the panel says" })
                    .to_string(),
            );
        }
    }

    fn resolve(d: &Doc, chosen: &str) -> Value {
        d.post(
            "/resolve",
            &json!({ "contest": "ct_share", "chosen": chosen, "rationale": "The retail panel" }).to_string(),
        )
    }

    #[test]
    fn a_spun_off_brief_carries_the_research_and_starts_current() {
        let (store, research, brief) = setup();
        let data = brief.session.read(|d| Ok(napkin_host::ops::read::data_json(d))).unwrap();
        // The research, frozen under its id; the pins projected at the root.
        assert_eq!(data["upstream"][&research.doc]["campaign"]["name"], "Lúnasa 0.0");
        assert!(data["upstream"][&research.doc].get("projection").is_none());
        assert_eq!(data["projection"]["pins"]["f_01JA0B3P4Q"]["value"], 0.23);
        assert_eq!(data["projection"]["findings"]["fi_01JA0F2B"]["status"], "proposed");
        let facts = brief.stored(&store).read_entry("shared/facts.yaml").unwrap();
        assert_eq!(
            data["projection"]["built_from"]["facts_sha256"],
            clan_sdk::hash::sha256_prefixed(&facts)
        );
        // Stamped as every other host stamp, so the brief's schema takes the
        // next write (a manifest stamp's nanoseconds and offset it refuses).
        let built_at = data["projection"]["built_from"]["built_at"].as_str().unwrap();
        assert!(built_at.ends_with('Z') && built_at.len() == 20, "{built_at}");

        let up = brief.get("/upstream");
        assert_eq!(up["document_id"], brief.doc);
        assert_eq!(up["carried"]["document_id"], research.doc);
        let entry = &up["upstream"][0];
        assert_eq!(entry["document_id"], research.doc);
        assert_eq!(entry["direct"], true);
        assert_eq!(entry["in_store"], true);
        assert_eq!(entry["status"], "current", "{up}");
        assert_eq!(entry["title"], "Lúnasa 0.0 launch");
        assert_eq!(entry["app_id"], RESEARCH);
        assert_eq!(entry["locked"], false);
        assert_eq!(entry["decisions_since"], 0);
        assert_eq!(
            entry["changed"],
            json!({ "data": false, "facts": false, "findings": false, "sources": false })
        );
        for list in ["pins", "findings", "contests"] {
            assert_eq!(entry[list], json!([]), "{list}");
        }
    }

    #[test]
    fn the_agent_is_sent_an_index_of_upstream_not_the_frozen_copy() {
        let (_store, research, brief) = setup();
        let clan = brief.session.clan_context_for_agent();
        let index = &clan["data"]["upstream"][&research.doc];
        assert_eq!(index["direct"], true);
        let keys: Vec<&str> = index["keys"].as_array().unwrap().iter().filter_map(Value::as_str).collect();
        assert!(keys.contains(&"campaign") && keys.contains(&"selection"), "{keys:?}");
        assert!(index.get("campaign").is_none(), "the frozen data stays home");
        assert_eq!(
            index["open_contests"],
            json!([{ "id": "ct_share", "key": "brand/orchard-hill:product.share@IE",
                     "fact_ids": ["f_01JA0B3P4Q", "f_01JA0B9Z9Z"] }])
        );
        // The carried pins and findings arrive where they were merged.
        assert_eq!(clan["findings"].as_array().unwrap().len(), 2);

        // Resolved here, the contest is no longer open for the agent either.
        resolve(&brief, "f_01JA0B9Z9Z");
        let clan = brief.session.clan_context_for_agent();
        assert_eq!(clan["data"]["upstream"][&research.doc]["open_contests"], json!([]));
        // The view still holds the copy whole.
        let data = brief.session.read(|d| Ok(napkin_host::ops::read::data_json(d))).unwrap();
        assert_eq!(data["upstream"][&research.doc]["selection"]["contested"][0]["status"], "open");
    }

    #[test]
    fn the_research_hears_what_the_brief_resolved_and_locked_and_nothing_else_changes() {
        let (store, research, brief) = setup();
        let before = research.stored(&store);
        let members = |c: &ClanFile| {
            ["shared/data.yaml", "shared/facts.yaml", "shared/findings.yaml"]
                .map(|p| c.read_entry(p).unwrap())
        };

        let reply = resolve(&brief, "f_01JA0B9Z9Z");
        let resolved = reply["decision"].as_str().unwrap().to_string();
        let backrefs = reply["backrefs"].as_array().unwrap();
        assert_eq!(backrefs.len(), 1, "{reply}");
        assert_eq!(backrefs[0]["document_id"], research.doc);

        let after = research.stored(&store);
        let d = &chain(&after).decisions[0];
        assert_eq!(d.kind.as_deref(), Some("backref"));
        assert_eq!(d.action, "resolved");
        assert_eq!(d.agent, "napkin-host");
        assert_eq!(d.actor.as_deref(), Some("human:local"));
        assert_eq!(d.id.as_deref(), backrefs[0]["decision"].as_str());
        assert_eq!(d.targets, vec![format!("{}#selection.contested[ct_share]", research.doc)]);
        assert_eq!(d.rationale, "Resolved in \"Lúnasa brief\": f_01JA0B9Z9Z.");
        let from = &d.extra["from"];
        assert_eq!(from["document_id"].as_str(), Some(brief.doc.as_str()));
        assert_eq!(from["decision"].as_str(), Some(resolved.as_str()));
        assert!(from["version"].as_str().unwrap().starts_with("sha256:"));
        // Only the chain moved: not the data, not the members, not the revision.
        assert_eq!(members(&after), members(&before));
        assert_eq!(after.manifest().id, before.manifest().id);
        // A pointer, not a resolution: the research's own contest is open.
        let r = research.session.read(|d| Ok(napkin_host::ops::read::data_json(d))).unwrap();
        assert_eq!(r["selection"]["contested"][0]["status"], "open");
        // Nor does it make the research "changed" for the brief.
        assert_eq!(brief.get("/upstream")["upstream"][0]["status"], "current");

        // A resolve of the brief's own contest (it has none) owes nothing; a
        // verdict owes nothing either.
        reject_findings(&brief);
        assert_eq!(chain(&research.stored(&store)).decisions.len(), chain(&after).decisions.len());

        let reply = brief.post("/approve", "{}");
        let lock = reply["decision"].as_str().unwrap().to_string();
        assert_eq!(reply["backrefs"], json!([{ "document_id": research.doc,
            "decision": chain(&research.stored(&store)).decisions[0].id }]));
        let now = research.stored(&store);
        let d = &chain(&now).decisions[0];
        assert_eq!(d.action, "used");
        assert_eq!(d.targets, vec![research.doc.clone()]);
        let approved = chain(&brief.stored(&store)).decisions[0].version.clone().unwrap();
        assert_eq!(d.rationale, format!("Used in locked \"Lúnasa brief\" at {approved}."));
        assert_eq!(d.extra["from"]["decision"].as_str(), Some(lock.as_str()));
        assert_eq!(d.extra["from"]["version"].as_str(), Some(approved.as_str()));
        assert_eq!(members(&now), members(&before));
    }

    #[test]
    fn a_locked_research_still_takes_the_briefs_backref() {
        let store = Arc::new(MemStore::default());
        let research = research(&store);
        resolve(&research, "f_01JA0B3P4Q");
        reject_findings(&research);
        research.post("/approve", "{}");
        install(&*store, "Brief Maker", BRIEF, Some(carries(RESEARCH)));
        let brief = spin(&store, &research, BRIEF);
        assert_eq!(brief.get("/upstream")["upstream"][0]["locked"], true);

        // The contest was settled and the findings rejected before the hop;
        // the brief's copies arrive rejected, so nothing is left open.
        let findings = brief.session.clan_context_for_agent()["findings"].clone();
        assert!(findings.as_array().unwrap().iter().all(|f| f["status"] == "rejected"), "{findings}");
        let reply = brief.post("/approve", "{}");
        assert_eq!(reply["backrefs"].as_array().unwrap().len(), 1, "{reply}");
        let now = research.stored(&store);
        assert_eq!(chain(&now).decisions[0].action, "used");
        // Still locked by its own approve, which the backref did not touch.
        assert!(chain(&now).decisions.iter().any(|d| d.kind.as_deref() == Some("approve")
            && d.targets == vec![research.doc.clone()]));
    }

    #[test]
    fn upstream_reports_what_changed_in_the_research_since_the_brief_started() {
        let (_store, research, brief) = setup();
        // The brief cites one finding in a field; then the research moves on.
        brief.post(
            "/patch-data",
            r#"{"patch":{"insight":{"value":"Growing from small","finding_ids":["fi_01JA0F2B"]}},"agent":"human"}"#,
        );
        research.post(
            "/verdict",
            r#"{"target":"findings[fi_01JA0F2B]","polarity":"bad","rationale":"Share is flat, not growing"}"#,
        );
        resolve(&research, "f_01JA0B9Z9Z");

        let up = brief.get("/upstream");
        let entry = &up["upstream"][0];
        assert_eq!(entry["status"], "changed", "{up}");
        assert_eq!(
            entry["changed"],
            json!({ "data": true, "facts": true, "findings": true, "sources": false })
        );
        assert_eq!(entry["decisions_since"], 2);
        assert_eq!(
            entry["pins"],
            json!([
                { "id": "f_01JA0B3P4Q", "change": "replaced", "label": "Product share · IE", "replaced_by": "f_01JA0B9Z9Z" },
                { "id": "f_01JA0B9Z9Z", "change": "added", "label": "Product share · IE" },
            ])
        );
        assert_eq!(
            entry["findings"],
            json!([{ "id": "fi_01JA0F2B", "change": "rejected",
                     "statement": "Orchard Hill is growing from a small share",
                     "reason": "Share is flat, not growing",
                     "cited_by": [format!("{}#insight", brief.doc)] }])
        );
        assert_eq!(
            entry["contests"],
            json!([{ "id": "ct_share", "change": "resolved", "key": "brand/orchard-hill:product.share@IE",
                     "chosen": "f_01JA0B9Z9Z", "resolved_here": false }])
        );

        // Resolved here too, and the brief knows it did.
        resolve(&brief, "f_01JA0B9Z9Z");
        let up = brief.get("/upstream");
        assert_eq!(up["upstream"][0]["contests"][0]["resolved_here"], true);
    }

    #[test]
    fn a_deck_from_the_brief_tells_both_ancestors_and_compares_only_its_parent() {
        let (store, research, brief) = setup();
        install(&*store, "Deck", DECK, Some(carries(BRIEF)));
        let deck = spin(&store, &brief, DECK);
        let up = deck.get("/upstream");
        let entries = up["upstream"].as_array().unwrap();
        assert_eq!(entries.len(), 2, "{up}");
        assert_eq!(entries[0]["document_id"], brief.doc, "the direct parent first");
        assert_eq!(entries[0]["status"], "current");
        assert_eq!(entries[1]["document_id"], research.doc);
        assert_eq!(entries[1]["direct"], false);
        assert_eq!(entries[1]["status"], "not_compared");
        assert_eq!(entries[1]["title"], "Lúnasa 0.0 launch");

        // The contest the deck carries from the research is settled in the
        // deck, and the research — a hoisted ancestor — hears of it.
        let reply = resolve(&deck, "f_01JA0B9Z9Z");
        assert_eq!(reply["backrefs"][0]["document_id"], research.doc, "{reply}");
        reject_findings(&deck);
        let reply = deck.post("/approve", "{}");
        let told: Vec<&str> = reply["backrefs"]
            .as_array()
            .unwrap()
            .iter()
            .filter_map(|b| b["document_id"].as_str())
            .collect();
        assert_eq!(told.len(), 2, "{reply}");
        assert!(told.contains(&brief.doc.as_str()) && told.contains(&research.doc.as_str()));
        assert_eq!(chain(&brief.stored(&store)).decisions[0].action, "used");
    }

    #[test]
    fn a_parent_the_store_does_not_hold_is_unknown_and_gets_no_backref() {
        let (store, research, brief) = setup();
        store.remove(&research.id);
        let up = brief.get("/upstream");
        assert_eq!(
            up["upstream"][0],
            json!({ "document_id": research.doc, "direct": true, "in_store": false, "status": "unknown" })
        );
        let reply = resolve(&brief, "f_01JA0B9Z9Z");
        assert_eq!(reply["backrefs"], json!([]));
    }

    #[test]
    fn a_verified_finding_is_told_to_the_parent_only_when_it_holds_it() {
        let (store, research, brief) = setup();
        let child = brief.session.read(|d| napkin_host::Document::from_bytes(d.id().clone(), d.bytes().to_vec())).unwrap();
        let ctx = Ctx::local();
        let held = Backref::Verified { finding: "fi_01JA0F3C".into() };
        let changes = library::backref_changes(&*store, &ctx, &child, "d_01JB0VERIFY", &held).unwrap();
        assert_eq!(changes.len(), 1);
        let (written, change) = &changes[0];
        assert_eq!(written.document_id, research.doc);
        assert_eq!(change.doc, research.id);
        let d = &change.decisions[0];
        assert_eq!(d.action, "verified");
        assert_eq!(d.targets, vec![format!("{}#findings[fi_01JA0F3C]", research.doc)]);
        assert_eq!(d.rationale, "Verified in \"Lúnasa brief\".");
        let foreign = Backref::Verified { finding: "fi_01JAXXXX".into() };
        assert!(library::backref_changes(&*store, &ctx, &child, "d_01JB0VERIFY", &foreign)
            .unwrap()
            .is_empty());
    }

    #[test]
    fn an_upstream_app_takes_no_map() {
        let store = Arc::new(MemStore::default());
        let research = research(&store);
        install(&*store, "Brief Maker", BRIEF, Some(carries(RESEARCH)));
        let resp = research.call(
            research.session.ctx(),
            "/spinoff",
            &json!({ "app_id": BRIEF, "map": "research" }).to_string(),
        );
        assert_eq!(resp.status, 422, "{}", String::from_utf8_lossy(&resp.body));
        assert!(resp.events.is_empty());
    }

    #[test]
    fn a_spinoff_stays_in_the_tenant_it_starts_in() {
        let store = Arc::new(MemStore::default());
        let research = research(&store);
        install(&*store, "Brief Maker", BRIEF, Some(carries(RESEARCH)));
        let tenant = |org: &str| {
            Ctx::new(Actor::human("u-1").unwrap()).with_scope(Scope {
                org: Some(org.into()),
                brand: None,
            })
        };
        // A person of tenant A writes to the research: it is A's now.
        let resp = research.call(&tenant("org-a"), "/patch-data", r#"{"patch":{"tone":"warm"},"agent":"human"}"#);
        assert_eq!(resp.status, 200);

        let body = json!({ "app_id": BRIEF }).to_string();
        let resp = research.call(&tenant("org-b"), "/spinoff", &body);
        assert_eq!(resp.status, 403, "{}", String::from_utf8_lossy(&resp.body));
        assert!(resp.events.is_empty(), "nothing is opened");
        assert_eq!(store.keys("docs/").len(), 1, "and nothing is written");

        let resp = research.call(&tenant("org-a"), "/spinoff", &body);
        assert_eq!(resp.status, 200, "{}", String::from_utf8_lossy(&resp.body));
    }

    #[test]
    fn an_upstream_target_is_listed_as_one() {
        let store = Arc::new(MemStore::default());
        let research = research(&store);
        install(&*store, "Brief Maker", BRIEF, Some(carries(RESEARCH)));
        let targets = research.get("/spinoff-targets");
        assert_eq!(targets[0]["app_id"], BRIEF);
        assert_eq!(targets[0]["upstream"], true);
        assert_eq!(targets[0]["map"], Value::Null);
    }
}

// A document nobody spun off has nothing upstream, and says so plainly.
#[test]
fn upstream_on_a_document_that_carries_nothing_is_empty() {
    let f = fixture();
    let resp = get(&f, "/upstream");
    assert_eq!(resp.status, 200);
    let v = json(&resp);
    assert_eq!(v["carried"], serde_json::Value::Null);
    assert_eq!(v["upstream"], serde_json::json!([]));
    assert!(v["document_id"].as_str().is_some());
}
