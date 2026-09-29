// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! The Napkin Studio OS app library: installed template apps, the documents
//! instantiated from them, and the home page (which is itself a CLAN app).
//!
//! Each thing that makes a document comes twice: a `*_change` that says what
//! would be written and writes nothing, and a helper of the old name that
//! applies it through the store for a shell that just wants it done. Home and
//! installed templates are library documents rather than instances, but they
//! go through the same funnel.

use clan_sdk::{
    create, instantiate, make_template, spinoff, AppInfo, ClanBuilder, ClanFile, CreateOptions,
    InstantiateOptions, MakeTemplateOptions, SpinoffOptions,
};
use serde::Serialize;

use crate::document::Document;
use crate::error::{HostError, HostResult};
use crate::store::{Change, DocId, DocStore};

#[derive(Serialize)]
pub struct InstalledApp {
    pub app_id: String,
    pub name: String,
    pub version: String,
    pub path: String,
    pub icon: Option<String>,
}

/// An installed app that will accept the open document as a spin-off source —
/// what "Continue in…" lists. Carries the declared graft so the shell can show
/// where the document's data will land without opening the template itself.
#[derive(Serialize)]
pub struct SpinoffTarget {
    pub app_id: String,
    pub name: String,
    pub version: String,
    pub icon: Option<String>,
    /// The dotted key the source's data is grafted under, when the app declares
    /// one. `None` means it folds in at the root.
    pub map: Option<String>,
}

#[derive(Serialize)]
pub struct RecentDoc {
    pub title: String,
    pub path: String,
    pub app_id: Option<String>,
    pub updated_at: String,
}

/// Scan the app library for installed template apps. Shared by the shell's
/// `list_apps` command and the `clan://apps` route (a home CLAN app).
pub fn scan_apps(store: &dyn DocStore) -> Vec<InstalledApp> {
    let mut out = Vec::new();
    for id in store.app_candidates() {
        let Ok(bytes) = store.read(&id) else { continue };
        let Ok(clan) = ClanFile::from_bytes(bytes) else {
            continue;
        };
        let m = clan.manifest();
        if m.document_type.as_deref() != Some("template") {
            continue;
        }
        if let Some(a) = &m.app {
            out.push(InstalledApp {
                app_id: a.app_id.clone(),
                name: a.name.clone(),
                version: a.version.clone(),
                path: id.to_string(),
                icon: displayable_icon(&clan, a.icon.as_deref()),
            });
        }
    }
    out
}

/// What a page can show for an app's icon. The manifest names an archive
/// member (spec §28), which no page can fetch into, so an image member is
/// handed over inline as a `data:` URI; a `data:` or http(s) value passes
/// through; anything else is dropped (the home screen falls back to the
/// app's initial). Capped, so a listing never carries a large picture.
pub fn displayable_icon(clan: &ClanFile, icon: Option<&str>) -> Option<String> {
    use base64::Engine as _;
    const MAX_ICON_BYTES: usize = 64 * 1024;
    let icon = icon?.trim();
    if icon.starts_with("data:image/")
        || icon.starts_with("https://")
        || icon.starts_with("http://")
    {
        return Some(icon.to_string());
    }
    let ext = icon.rsplit('.').next()?.to_ascii_lowercase();
    let media = match ext.as_str() {
        "svg" => "image/svg+xml",
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "webp" => "image/webp",
        _ => return None,
    };
    clan.manifest().file_by_path(icon)?;
    let bytes = clan.read_entry(icon).ok()?;
    if bytes.len() > MAX_ICON_BYTES {
        return None;
    }
    let b64 = base64::engine::general_purpose::STANDARD.encode(bytes);
    Some(format!("data:{media};base64,{b64}"))
}

/// Recent document instances, newest first.
pub fn scan_recent(store: &dyn DocStore) -> Vec<RecentDoc> {
    let mut out = Vec::new();
    for id in store.documents() {
        let Ok(bytes) = store.read(&id) else { continue };
        let Ok(clan) = ClanFile::from_bytes(bytes) else {
            continue;
        };
        let m = clan.manifest();
        out.push(RecentDoc {
            title: m.title.clone(),
            path: id.to_string(),
            app_id: m.app.as_ref().map(|a| a.app_id.clone()),
            updated_at: m.updated_at.clone(),
        });
    }
    out.sort_by(|a, b| b.updated_at.cmp(&a.updated_at));
    out.truncate(12);
    out
}

/// Install a template app from its packed bytes into the library.
pub fn install_app(store: &dyn DocStore, bytes: Vec<u8>) -> HostResult<InstalledApp> {
    let (app, change) = install_change(store, bytes)?;
    store.apply(&change)?;
    Ok(app)
}

/// What installing `bytes` would write, without writing it. Reinstalling
/// replaces the copy that is there, and says which version it expected.
pub fn install_change(store: &dyn DocStore, bytes: Vec<u8>) -> HostResult<(InstalledApp, Change)> {
    let clan = ClanFile::from_bytes(bytes.clone())?;
    let m = clan.manifest();
    if m.document_type.as_deref() != Some("template") {
        return Err(HostError::bad_request(
            "not a template app (document_type must be 'template')",
        ));
    }
    let a = m
        .app
        .clone()
        .ok_or_else(|| HostError::bad_request("template has no app block"))?;
    let dest = store.app_template(&a.app_id);
    let change = if store.exists(&dest) {
        Change::replace(dest.clone(), store.version(&dest)?, bytes)
    } else {
        Change::create(dest.clone(), bytes)
    };
    Ok((
        InstalledApp {
            app_id: a.app_id,
            name: a.name,
            version: a.version,
            path: dest.to_string(),
            icon: displayable_icon(&clan, a.icon.as_deref()),
        },
        change,
    ))
}

/// The installed template for `app_id`, or a "not installed" error carrying
/// the store's status.
fn load_template(store: &dyn DocStore, app_id: &str) -> HostResult<ClanFile> {
    let tpl_id = store.app_template(app_id);
    store
        .read(&tpl_id)
        .and_then(|b| Ok(ClanFile::from_bytes(b)?))
        .map_err(|e| HostError::new(e.status, format!("app not installed: {e}")))
}

/// A new document's id: the library's name for it, from the first characters
/// of its manifest id so the name is stable and readable.
fn allocate(store: &dyn DocStore, app_id: &str, bytes: &[u8]) -> HostResult<DocId> {
    let id_short = ClanFile::from_bytes(bytes.to_vec())?
        .manifest()
        .id
        .chars()
        .take(8)
        .collect::<String>();
    store.new_document(app_id, &id_short)
}

/// Instantiate a working document from an installed app and return its id.
/// Shared by the shell's `new_document_from_app` and the `clan://launch` route.
pub fn create_instance(
    store: &dyn DocStore,
    app_id: &str,
    title: Option<String>,
) -> HostResult<DocId> {
    let change = instance_change(store, app_id, title)?;
    store.apply(&change)?;
    Ok(change.doc)
}

/// The new document [`create_instance`] would write, without writing it.
pub fn instance_change(
    store: &dyn DocStore,
    app_id: &str,
    title: Option<String>,
) -> HostResult<Change> {
    let template = load_template(store, app_id)?;
    let bytes = instantiate(
        &template,
        InstantiateOptions {
            title: title.unwrap_or_default(),
            document_type: None,
            fresh_data: true,
            instance_id: None,
        },
    )?;
    let out = allocate(store, app_id, &bytes)?;
    Ok(Change::create(out, bytes))
}

/// Which installed apps will take `source_app_id` as a spin-off source.
///
/// An app has to *declare* `app.spinoff` to be offered — an app with no such
/// block is not refusing, it simply has not said what a foreign document's data
/// would mean inside it, and guessing is how data lands in the wrong shape. Of
/// the apps that do declare one, an empty `accepts` takes anything.
pub fn spinoff_targets(store: &dyn DocStore, source_app_id: Option<&str>) -> Vec<SpinoffTarget> {
    let mut out = Vec::new();
    for id in store.app_candidates() {
        let Ok(bytes) = store.read(&id) else { continue };
        let Ok(clan) = ClanFile::from_bytes(bytes) else {
            continue;
        };
        let m = clan.manifest();
        if m.document_type.as_deref() != Some("template") {
            continue;
        }
        let Some(app) = &m.app else { continue };
        let Some(spec) = &app.spinoff else { continue };
        let accepted = spec.accepts.is_empty()
            || source_app_id.is_some_and(|id| spec.accepts.iter().any(|a| a == id));
        if !accepted {
            continue;
        }
        out.push(SpinoffTarget {
            app_id: app.app_id.clone(),
            name: app.name.clone(),
            version: app.version.clone(),
            icon: displayable_icon(&clan, app.icon.as_deref()),
            map: spec.map.clone(),
        });
    }
    out
}

/// Branch `source` into a new document of `target_app_id`, carrying its data
/// and its decisions across, and return the new document's id.
///
/// The counterpart to [`create_instance`]: that one starts a document empty,
/// this one starts it from work already done somewhere else.
pub fn spinoff_document(
    store: &dyn DocStore,
    source: &DocId,
    target_app_id: &str,
    title: Option<String>,
    map: Option<String>,
) -> HostResult<DocId> {
    // The target first: "that app is not installed" is the better answer
    // when both are wrong.
    let template = load_template(store, target_app_id)?;
    let source_clan = store
        .read(source)
        .and_then(|b| Ok(ClanFile::from_bytes(b)?))
        .map_err(|e| HostError::new(e.status, format!("source document: {e}")))?;
    let change = spinoff_from(
        store,
        &template,
        &source_clan,
        source,
        target_app_id,
        title,
        map,
    )?;
    store.apply(&change)?;
    Ok(change.doc)
}

/// The new document [`spinoff_document`] would write from the snapshot
/// `source`, without writing it.
pub fn spinoff_change(
    store: &dyn DocStore,
    source: &Document,
    target_app_id: &str,
    title: Option<String>,
    map: Option<String>,
) -> HostResult<Change> {
    let template = load_template(store, target_app_id)?;
    spinoff_from(
        store,
        &template,
        source.clan(),
        source.id(),
        target_app_id,
        title,
        map,
    )
}

fn spinoff_from(
    store: &dyn DocStore,
    template: &ClanFile,
    source: &ClanFile,
    source_id: &DocId,
    target_app_id: &str,
    title: Option<String>,
    map: Option<String>,
) -> HostResult<Change> {
    let bytes = spinoff(
        template,
        source,
        SpinoffOptions {
            title: title.unwrap_or_default(),
            map,
            instance_id: None,
            // A DocId is the store's own address for the file; recording it is
            // what lets a restore find the parent again.
            source_uri: Some(format!("clan-store:{source_id}")),
        },
    )?;
    let out = allocate(store, target_app_id, &bytes)?;
    Ok(Change::create(out, bytes))
}

// ── The home page, as a CLAN file ───────────────────────────────────────────
//
// The launcher itself is an authored CLAN app rendered by the host like any
// other. Its content drives the host purely through the clan:// API: it lists
// installed apps (GET clan://apps) and launches one (POST clan://launch), which
// instantiates another .clan and tells the shell to open it. This proves a
// click inside one CLAN file can reliably launch another.

pub const HOME_APP_HTML: &str = include_str!("../assets/home_app.html");

/// Bumped whenever `HOME_APP_HTML` changes, so a new build rebuilds the home
/// app rather than reusing the stale one already in the library.
pub const HOME_VERSION: &str = "v8";

/// Build the home CLAN template (idempotent) and return its id.
pub fn ensure_home(store: &dyn DocStore) -> HostResult<DocId> {
    let (id, change) = home_change(store)?;
    if let Some(change) = change {
        store.apply(&change)?;
    }
    Ok(id)
}

/// Where the home app lives, and the change that builds it when this version
/// of it has not been built yet.
pub fn home_change(store: &dyn DocStore) -> HostResult<(DocId, Option<Change>)> {
    let id = store.home(HOME_VERSION)?;
    if store.exists(&id) {
        return Ok((id, None));
    }
    let base = create(CreateOptions {
        title: "Napkin Studio".into(),
        brief: "Napkin Studio home".into(),
        document_type: None,
        no_render: false,
        schema: None,
    })?;
    let clan = ClanFile::from_bytes(base)?;
    // Swap in the authored home HTML.
    let mut b = ClanBuilder::new(clan.manifest().clone());
    for (p, by) in clan.read_all_entries()? {
        if p == "manifest.yaml" || p == "human/index.html" {
            continue;
        }
        b.add_entry(p, by);
    }
    b.add_entry("human/index.html", HOME_APP_HTML.as_bytes().to_vec());
    let with_html = ClanFile::from_bytes(b.build()?)?;
    let tpl = make_template(
        &with_html,
        AppInfo {
            name: "Napkin Studio".into(),
            app_id: "ie.napkin.home".into(),
            version: "1.0.0".into(),
            icon: None,
            entry: "human/index.html".into(),
            schema: Some("agent/output-schema.json".into()),
            prompt_templates: vec![],
            data_seed: None,
            spinoff: None,
        },
        MakeTemplateOptions::default(),
    )?;
    let change = Change::create(id.clone(), tpl);
    Ok((id, Some(change)))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::session::Session;
    use crate::store::{Library, PartStore, Version};
    use clan_sdk::{MakeTemplateOptions as MtOpts, SpinoffSpec};
    use std::collections::HashMap;
    use std::sync::{Arc, Mutex};

    /// An in-memory library.
    ///
    /// Deliberately not `FsStore`: that reads `NAPKIN_APPS_DIR` at construction,
    /// so a developer who has it set would have these tests install fixture apps
    /// into their real library. Nothing here needs a filesystem anyway.
    #[derive(Default)]
    struct MemStore {
        files: Mutex<HashMap<String, Vec<u8>>>,
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
            let mut v: Vec<DocId> = self
                .files
                .lock()
                .unwrap()
                .keys()
                .filter(|k| k.starts_with("apps/"))
                .map(DocId::new)
                .collect();
            // Deterministic order, so assertions on the list are stable.
            v.sort();
            v
        }
        fn app_template(&self, app_id: &str) -> DocId {
            DocId::new(format!("apps/{app_id}/app.clan"))
        }
        fn documents(&self) -> Vec<DocId> {
            self.files
                .lock()
                .unwrap()
                .keys()
                .filter(|k| k.starts_with("docs/"))
                .map(DocId::new)
                .collect()
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

    fn app_info(name: &str, app_id: &str, spinoff: Option<SpinoffSpec>) -> AppInfo {
        AppInfo {
            name: name.into(),
            app_id: app_id.into(),
            version: "0.1.0".into(),
            icon: None,
            entry: "human/index.html".into(),
            schema: Some("agent/output-schema.json".into()),
            prompt_templates: vec![],
            data_seed: None,
            spinoff,
        }
    }

    fn blank(title: &str) -> ClanFile {
        let bytes = create(CreateOptions {
            title: title.into(),
            brief: "fixture".into(),
            document_type: None,
            no_render: false,
            schema: None,
        })
        .unwrap();
        ClanFile::from_bytes(bytes).unwrap()
    }

    fn install_template_app(
        store: &dyn DocStore,
        name: &str,
        app_id: &str,
        spinoff: Option<SpinoffSpec>,
    ) {
        let tpl = make_template(
            &blank(name),
            app_info(name, app_id, spinoff),
            MtOpts::default(),
        )
        .unwrap();
        install_app(store, tpl).unwrap();
    }

    /// A filled source document: an instance of `app_id` carrying real data.
    fn source_document(store: &dyn DocStore, app_id: &str) -> DocId {
        install_template_app(store, "Source App", app_id, None);
        let id = create_instance(store, app_id, Some("Acme Brief".into())).unwrap();
        let clan = ClanFile::from_bytes(store.read(&id).unwrap()).unwrap();
        let mut b = ClanBuilder::new(clan.manifest().clone());
        for (path, bytes) in clan.read_all_entries().unwrap() {
            if path == "manifest.yaml" || path == "shared/data.yaml" {
                continue;
            }
            b.add_entry(path, bytes);
        }
        b.add_entry(
            "shared/data.yaml",
            b"project_name: Acme\ninsight: people forget\n".to_vec(),
        );
        let base = store.version(&id).unwrap();
        store
            .apply(&Change::replace(id.clone(), base, b.build().unwrap()))
            .unwrap();
        id
    }

    fn film_spec() -> SpinoffSpec {
        SpinoffSpec {
            accepts: vec!["ie.napkin.brief".into()],
            map: Some("brief".into()),
            lift: [("project_name".to_string(), "project.name".to_string())]
                .into_iter()
                .collect(),
            pin_source_decisions: true,
            upstream: false,
        }
    }

    /// An app whose manifest names an icon member, the way the packers write one.
    fn template_with_icon(icon_path: &str, bytes: &[u8]) -> Vec<u8> {
        let base = blank("Iconic");
        let mut b = ClanBuilder::new(base.manifest().clone());
        for (path, data) in base.read_all_entries().unwrap() {
            b.add_entry(path, data);
        }
        b.add_entry(icon_path, bytes.to_vec());
        b.manifest_mut().files.push(clan_sdk::FileEntry {
            id: "app-icon".into(),
            path: icon_path.into(),
            role: "app-icon".into(),
            content_type: "image/svg+xml".into(),
            priority: None,
            sha256: None,
        });
        let clan = ClanFile::from_bytes(b.build().unwrap()).unwrap();
        let mut info = app_info("Iconic", "ie.napkin.iconic", None);
        info.icon = Some(icon_path.into());
        make_template(&clan, info, MtOpts::default()).unwrap()
    }

    #[test]
    fn an_icon_member_is_listed_inline_for_the_home_screen() {
        use base64::Engine as _;
        let store = MemStore::default();
        let svg = b"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 44 44'><circle cx='22' cy='22' r='9'/></svg>";
        let installed = install_app(&store, template_with_icon("app/icon.svg", svg)).unwrap();
        let listed = scan_apps(&store);
        let icon = listed[0].icon.as_deref().expect("the icon is listed");
        let b64 = icon
            .strip_prefix("data:image/svg+xml;base64,")
            .expect("as a data: URI");
        assert_eq!(
            base64::engine::general_purpose::STANDARD
                .decode(b64)
                .unwrap(),
            svg
        );
        // Install says the same as the listing.
        assert_eq!(installed.icon.as_deref(), Some(icon));
    }

    #[test]
    fn an_icon_that_is_not_an_image_member_is_not_listed() {
        let clan = ClanFile::from_bytes(template_with_icon("app/icon.svg", b"<svg/>")).unwrap();
        // Missing member, wrong kind, passthrough.
        assert_eq!(displayable_icon(&clan, Some("app/missing.svg")), None);
        assert_eq!(displayable_icon(&clan, Some("human/index.html")), None);
        assert_eq!(displayable_icon(&clan, None), None);
        assert_eq!(
            displayable_icon(&clan, Some("https://example.com/i.png")).as_deref(),
            Some("https://example.com/i.png")
        );
    }

    #[test]
    fn spinoff_document_carries_data_and_decisions_through_the_store() {
        let store = MemStore::default();
        let source = source_document(&store, "ie.napkin.brief");
        install_template_app(
            &store,
            "Advertising Studio",
            "ie.napkin.film",
            Some(film_spec()),
        );

        let out = spinoff_document(&store, &source, "ie.napkin.film", Some("FORM".into()), None)
            .expect("spin-off should succeed");

        let clan = ClanFile::from_bytes(store.read(&out).unwrap()).unwrap();
        let data: serde_yaml::Value =
            serde_yaml::from_slice(&clan.read_entry("shared/data.yaml").unwrap()).unwrap();
        // Grafted where the TARGET app said, not where the source kept it.
        assert_eq!(data["brief"]["insight"].as_str(), Some("people forget"));
        assert_eq!(data["project"]["name"].as_str(), Some("Acme"));

        let m = clan.manifest();
        assert_eq!(m.title, "FORM");
        assert_eq!(m.app.as_ref().unwrap().app_id, "ie.napkin.film");
        // Two parents: which app this is, and what authorised it.
        assert_eq!(m.lineage.as_ref().unwrap().parents.len(), 2);
        // A new document, not a rewrite of the source.
        assert_ne!(out, source);
        assert!(store.exists(&source));
    }

    #[test]
    fn spinoff_document_refuses_a_source_the_target_does_not_accept() {
        let store = MemStore::default();
        let source = source_document(&store, "ie.napkin.ooh");
        install_template_app(
            &store,
            "Advertising Studio",
            "ie.napkin.film",
            Some(film_spec()),
        );

        let err = spinoff_document(&store, &source, "ie.napkin.film", None, None)
            .expect_err("a source outside `accepts` must be refused");
        // A clean status the shell can report — not a panic, not a 500.
        assert_eq!(err.status, 422);
        assert!(err.message.contains("ie.napkin.ooh"), "{}", err.message);
    }

    #[test]
    fn spinoff_document_reports_an_uninstalled_target() {
        let store = MemStore::default();
        let source = source_document(&store, "ie.napkin.brief");
        let err = spinoff_document(&store, &source, "ie.napkin.absent", None, None)
            .expect_err("an uninstalled target must be an error");
        assert!(err.message.contains("app not installed"), "{}", err.message);
    }

    #[test]
    fn spinoff_targets_lists_only_apps_that_accept_this_source() {
        let store = MemStore::default();
        install_template_app(
            &store,
            "Advertising Studio",
            "ie.napkin.film",
            Some(film_spec()),
        );
        install_template_app(
            &store,
            "OOH Studio",
            "ie.napkin.ooh",
            Some(SpinoffSpec {
                accepts: vec!["ie.napkin.something-else".into()],
                ..Default::default()
            }),
        );
        // Declares a spin-off but names no sources — takes anything.
        install_template_app(
            &store,
            "Case Study",
            "ie.napkin.case",
            Some(SpinoffSpec::default()),
        );
        // Declares nothing: not offered, because it has never said what a
        // foreign document's data would mean inside it.
        install_template_app(&store, "Brief Maker", "ie.napkin.brief", None);

        let ids: Vec<String> = spinoff_targets(&store, Some("ie.napkin.brief"))
            .into_iter()
            .map(|t| t.app_id)
            .collect();
        assert!(ids.contains(&"ie.napkin.film".to_string()), "{ids:?}");
        assert!(ids.contains(&"ie.napkin.case".to_string()), "{ids:?}");
        assert!(!ids.contains(&"ie.napkin.ooh".to_string()), "{ids:?}");
        assert!(!ids.contains(&"ie.napkin.brief".to_string()), "{ids:?}");
    }

    #[test]
    fn spinoff_targets_surfaces_the_declared_graft() {
        let store = MemStore::default();
        install_template_app(
            &store,
            "Advertising Studio",
            "ie.napkin.film",
            Some(film_spec()),
        );
        let targets = spinoff_targets(&store, Some("ie.napkin.brief"));
        let film = targets
            .iter()
            .find(|t| t.app_id == "ie.napkin.film")
            .unwrap();
        assert_eq!(film.map.as_deref(), Some("brief"));
        assert_eq!(film.name, "Advertising Studio");
    }

    #[test]
    fn an_app_less_document_matches_only_the_permissive_targets() {
        let store = MemStore::default();
        install_template_app(
            &store,
            "Advertising Studio",
            "ie.napkin.film",
            Some(film_spec()),
        );
        install_template_app(
            &store,
            "Case Study",
            "ie.napkin.case",
            Some(SpinoffSpec::default()),
        );
        let ids: Vec<String> = spinoff_targets(&store, None)
            .into_iter()
            .map(|t| t.app_id)
            .collect();
        assert_eq!(ids, vec!["ie.napkin.case".to_string()]);
    }

    #[test]
    fn the_open_documents_app_id_drives_the_target_list() {
        let store = Arc::new(MemStore::default());
        let source = source_document(&*store, "ie.napkin.brief");
        install_template_app(
            &*store,
            "Advertising Studio",
            "ie.napkin.film",
            Some(film_spec()),
        );

        let session = Session::new(store.clone());
        session.open(source).unwrap();
        assert_eq!(session.app_id().as_deref(), Some("ie.napkin.brief"));
        let ids: Vec<String> = spinoff_targets(&**session.store(), session.app_id().as_deref())
            .into_iter()
            .map(|t| t.app_id)
            .collect();
        assert_eq!(ids, vec!["ie.napkin.film".to_string()]);
    }
}
