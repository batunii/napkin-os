// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! The Napkin Studio OS app library: installed template apps, the documents
//! instantiated from them, and the home page (which is itself a CLAN app).

use clan_sdk::{
    create, instantiate, make_template, AppInfo, ClanBuilder, ClanFile, CreateOptions,
    InstantiateOptions, MakeTemplateOptions,
};
use serde::Serialize;

use crate::error::{HostError, HostResult};
use crate::store::{DocId, DocStore};

#[derive(Serialize)]
pub struct InstalledApp {
    pub app_id: String,
    pub name: String,
    pub version: String,
    pub path: String,
    pub icon: Option<String>,
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
                icon: a.icon.clone(),
            });
        }
    }
    out
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
    let dest = store.install_template(&a.app_id, &bytes)?;
    Ok(InstalledApp {
        app_id: a.app_id,
        name: a.name,
        version: a.version,
        path: dest.to_string(),
        icon: a.icon,
    })
}

/// Instantiate a working document from an installed app and return its id.
/// Shared by the shell's `new_document_from_app` and the `clan://launch` route.
pub fn create_instance(
    store: &dyn DocStore,
    app_id: &str,
    title: Option<String>,
) -> HostResult<DocId> {
    let tpl_id = store.app_template(app_id);
    let template = store
        .read(&tpl_id)
        .and_then(|b| Ok(ClanFile::from_bytes(b)?))
        .map_err(|e| HostError::new(e.status, format!("app not installed: {e}")))?;
    let bytes = instantiate(
        &template,
        InstantiateOptions {
            title: title.unwrap_or_default(),
            document_type: None,
            fresh_data: true,
            instance_id: None,
        },
    )?;
    let id_short = ClanFile::from_bytes(bytes.clone())?
        .manifest()
        .id
        .chars()
        .take(8)
        .collect::<String>();
    let out = store.new_document(app_id, &id_short)?;
    store.write(&out, &bytes)?;
    Ok(out)
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
pub const HOME_VERSION: &str = "v7";

/// Build the home CLAN template (idempotent) and return its id.
pub fn ensure_home(store: &dyn DocStore) -> HostResult<DocId> {
    let id = store.home(HOME_VERSION)?;
    if store.exists(&id) {
        return Ok(id);
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
        },
        MakeTemplateOptions::default(),
    )?;
    store.write(&id, &tpl)?;
    Ok(id)
}
