// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! No operation writes. Each one is handed a snapshot and a store it may read,
//! and returns the changes it wants; the store below panics if anything calls
//! `apply` while it is sealed. Only the apply step — unsealed, one change at a
//! time — ever lands, and what lands is exactly what the operation returned.
//!
//! `PartStore::apply` is the only write method a store has, so this is the
//! whole write surface: there is nothing else an operation could call.

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};

use napkin_host::ops::edit;
use napkin_host::{
    library, Base, Change, Ctx, DocId, Document, HostError, HostResult, Library, PartStore,
    Session, Version,
};

/// An in-memory store whose one write method refuses to run while sealed.
#[derive(Default)]
struct SealedStore {
    files: Mutex<HashMap<DocId, Vec<u8>>>,
    unsealed: AtomicBool,
    applied: AtomicUsize,
}

impl SealedStore {
    /// Put a fixture in place without going through `apply`, so the seal
    /// only ever sees what the host itself asks for.
    fn seed(&self, id: &DocId, bytes: Vec<u8>) {
        self.files.lock().unwrap().insert(id.clone(), bytes);
    }

    /// The apply step: the only place this test lets a change land.
    fn apply_all(&self, changes: &[Change]) {
        self.unsealed.store(true, Ordering::SeqCst);
        for c in changes {
            self.apply(c).unwrap();
        }
        self.unsealed.store(false, Ordering::SeqCst);
    }
}

impl PartStore for SealedStore {
    fn read(&self, id: &DocId) -> HostResult<Vec<u8>> {
        self.files
            .lock()
            .unwrap()
            .get(id)
            .cloned()
            .ok_or_else(|| HostError::not_found(format!("no such document: {id}")))
    }

    fn exists(&self, id: &DocId) -> bool {
        self.files.lock().unwrap().contains_key(id)
    }

    fn apply(&self, change: &Change) -> HostResult<Version> {
        assert!(
            self.unsealed.load(Ordering::SeqCst),
            "an operation wrote {} directly instead of returning a Change",
            change.doc
        );
        self.applied.fetch_add(1, Ordering::SeqCst);
        self.files
            .lock()
            .unwrap()
            .insert(change.doc.clone(), change.bytes.clone());
        Ok(change.archive_version())
    }
}

impl Library for SealedStore {
    fn app_candidates(&self) -> Vec<DocId> {
        let mut v: Vec<DocId> = self
            .files
            .lock()
            .unwrap()
            .keys()
            .filter(|k| k.as_str().starts_with("apps/"))
            .cloned()
            .collect();
        v.sort();
        v
    }
    fn app_template(&self, app_id: &str) -> DocId {
        DocId::new(format!("apps/{app_id}"))
    }
    fn documents(&self) -> Vec<DocId> {
        Vec::new()
    }
    fn new_document(&self, app_id: &str, id_short: &str) -> HostResult<DocId> {
        Ok(DocId::new(format!("docs/{app_id}-{id_short}")))
    }
    fn home(&self, version_tag: &str) -> HostResult<DocId> {
        Ok(DocId::new(format!("home-{version_tag}")))
    }
    fn fork_branch(&self, parent: &DocId, agent: &str) -> DocId {
        DocId::new(format!("{parent}.{agent}"))
    }
}

fn blank(title: &str) -> Vec<u8> {
    clan_sdk::create(clan_sdk::CreateOptions {
        title: title.into(),
        brief: "fixture".into(),
        document_type: None,
        no_render: false,
        schema: None,
    })
    .unwrap()
}

fn template(app_id: &str) -> Vec<u8> {
    let base = clan_sdk::ClanFile::from_bytes(blank("Studio")).unwrap();
    clan_sdk::make_template(
        &base,
        clan_sdk::AppInfo {
            name: "Studio".into(),
            app_id: app_id.into(),
            version: "0.1.0".into(),
            icon: None,
            entry: "human/index.html".into(),
            schema: Some("agent/output-schema.json".into()),
            prompt_templates: vec![],
            data_seed: None,
            spinoff: Some(clan_sdk::SpinoffSpec::default()),
        },
        clan_sdk::MakeTemplateOptions::default(),
    )
    .unwrap()
}

fn fixture() -> (Arc<SealedStore>, Document) {
    let store = Arc::new(SealedStore::default());
    let id = DocId::new("docs/source");
    store.seed(&id, blank("Source"));
    store.seed(
        &store.app_template("ie.napkin.film"),
        template("ie.napkin.film"),
    );
    let doc = Document::load(&*store, id).unwrap();
    (store, doc)
}

#[test]
fn document_operations_return_changes_and_write_nothing() {
    let (store, doc) = fixture();
    let ctx = Ctx::local();
    let before = store.read(doc.id()).unwrap();

    let outcomes = vec![
        edit::snapshot(&ctx, &doc, "<html><body>view</body></html>").unwrap(),
        edit::save_patch(&ctx, &doc, "heading-0", "Edited").unwrap(),
        edit::patch_data(
            &ctx,
            &doc,
            edit::PatchData::parse(r#"{"patch":{"verdict":"yes"},"agent":"human"}"#).unwrap(),
        )
        .unwrap(),
        edit::upload_asset(&ctx, &doc, "note.txt", Some("human"), b"hello".to_vec()).unwrap(),
        edit::set_title(&ctx, &doc, "Renamed").unwrap(),
        edit::set_context(&ctx, &doc, "# Context", false).unwrap(),
        edit::fork(&ctx, &doc, &*store, &["a".into(), "b".into()]).unwrap(),
    ];

    // Sealed throughout, and nothing moved.
    assert_eq!(store.applied.load(Ordering::SeqCst), 0);
    assert_eq!(store.read(doc.id()).unwrap(), before);

    for out in &outcomes {
        assert!(
            !out.changes.is_empty(),
            "each of these edits changes something"
        );
        for c in &out.changes {
            if c.doc == *doc.id() {
                // Based on the snapshot it was computed from — the version the
                // W2-A4 check will compare against what is on disk.
                assert_eq!(c.base, Base::At(doc.version().clone()));
            } else {
                assert_eq!(c.base, Base::Absent, "a fork branch is a new document");
            }
        }
    }

    // The apply step, and only it, lands them.
    let patch_data = &outcomes[2];
    store.apply_all(&patch_data.changes);
    let after = Document::load(&*store, doc.id().clone()).unwrap();
    assert_eq!(after.version(), &patch_data.changes[0].archive_version());
    assert_ne!(after.version(), doc.version());
    // The change says which decision it appends, and it is the one in the file.
    assert_eq!(patch_data.changes[0].decisions.len(), 1);
    assert_eq!(patch_data.changes[0].decisions[0].agent, "human");
}

#[test]
fn library_operations_return_changes_and_write_nothing() {
    let (store, doc) = fixture();

    let (app, install) = library::install_change(&*store, template("ie.napkin.brief")).unwrap();
    assert_eq!(install.base, Base::Absent);
    // Reinstalling over an existing copy names the version it replaces.
    let (_, reinstall) = library::install_change(&*store, template("ie.napkin.film")).unwrap();
    assert!(matches!(reinstall.base, Base::At(_)));

    let instance = library::instance_change(&*store, "ie.napkin.film", Some("New".into())).unwrap();
    let spun = library::spinoff_change(&*store, &doc, "ie.napkin.film", None, None).unwrap();
    let (home_id, home) = library::home_change(&*store).unwrap();
    let home = home.expect("no home yet, so it must be built");

    assert_eq!(store.applied.load(Ordering::SeqCst), 0);
    assert!(!store.exists(&instance.doc) && !store.exists(&spun.doc) && !store.exists(&home_id));
    // A spin-off arrives with its source's decisions, and says so.
    assert_eq!(spun.base, Base::Absent);

    store.apply_all(&[install, instance.clone(), spun.clone(), home]);
    assert_eq!(store.applied.load(Ordering::SeqCst), 4);
    assert!(store.exists(&instance.doc) && store.exists(&spun.doc) && store.exists(&home_id));
    assert_eq!(app.app_id, "ie.napkin.brief");
    // Built once: a second look finds it and proposes nothing.
    assert!(library::home_change(&*store).unwrap().1.is_none());
}

/// The session is the shells' apply step. Every write it makes is one `apply`
/// of a change an operation returned, and its snapshot moves to the version the
/// store reports — never to something it computed on the side.
#[test]
fn a_session_writes_only_through_apply() {
    let (store, doc) = fixture();
    let session = Session::new(store.clone());
    session.open(doc.id().clone()).unwrap();
    let opened_at = session.current_version().unwrap();

    store.unsealed.store(true, Ordering::SeqCst);
    session
        .patch_data(r#"{"patch":{"verdict":"yes"},"agent":"human"}"#)
        .unwrap();
    assert_eq!(store.applied.load(Ordering::SeqCst), 1);
    let now = session.current_version().unwrap();
    assert_ne!(now, opened_at);
    assert_eq!(now, store.version(doc.id()).unwrap());

    // An unchanged edit is no write at all.
    let reply = session
        .patch_data(r#"{"patch":{"verdict":"yes"},"agent":"human"}"#)
        .unwrap();
    assert_eq!(reply["noop"], true);
    assert_eq!(store.applied.load(Ordering::SeqCst), 1);

    // Reads never apply anything, even with the seal off.
    session.human_html().unwrap();
    session.chain_json().unwrap();
    session.compose_export(true, false).unwrap();
    assert_eq!(store.applied.load(Ordering::SeqCst), 1);
}
