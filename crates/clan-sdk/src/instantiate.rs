// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! Template-as-application (spec §28, v1.2) — the Napkin Studio OS model.
//!
//! An *app* is a developer-authored CLAN file with `document_type: template`
//! whose presentation layer renders from `window.__CLAN__.data` instead of
//! being AI-generated. Two operations live here:
//!
//! - [`make_template`] promotes an authored `.clan` into a template app.
//! - [`instantiate`] produces a working *instance* from a template — the
//!   "new from template" / "create document = copy a template" primitive.
//!
//! Instantiation deliberately does NOT reuse [`crate::merge::fork`]: forking
//! requires ≥2 agents and sets up branch namespaces. An instance is a single
//! working document that is provenance-linked to its template via lineage.

use chrono::Utc;
use uuid::Uuid;

use crate::container::{ClanBuilder, ClanFile, MANIFEST_PATH};
use crate::error::{Error, Result};
use crate::manifest::{AppInfo, FileEntry, Lineage, ViewState};

const DATA_PATH: &str = "shared/data.yaml";
const APP_MANIFEST_PATH: &str = "app/manifest.yaml";

/// Options for [`instantiate`].
#[derive(Debug, Clone)]
pub struct InstantiateOptions {
    /// Title for the new instance. Defaults to the app name when empty.
    pub title: String,
    /// `document_type` of the instance. Defaults to `"document"`. Never
    /// `"template"` — an instance is not itself an app.
    pub document_type: Option<String>,
    /// `true` → reset the data layer to an empty mapping; `false` → copy the
    /// template's seed data (`app.data_seed` or `shared/data.yaml`).
    pub fresh_data: bool,
    /// Override the generated instance id (must be a UUID v4). Defaults to a
    /// fresh UUID.
    pub instance_id: Option<String>,
}

impl Default for InstantiateOptions {
    fn default() -> Self {
        Self {
            title: String::new(),
            document_type: None,
            fresh_data: true,
            instance_id: None,
        }
    }
}

/// Options for [`make_template`].
#[derive(Debug, Clone)]
pub struct MakeTemplateOptions {
    /// Also write a standalone `app/manifest.yaml` member so SDK-less tools
    /// and the launcher can read app metadata without parsing the manifest.
    pub embed_app_manifest: bool,
}

impl Default for MakeTemplateOptions {
    fn default() -> Self {
        Self {
            embed_app_manifest: true,
        }
    }
}

/// Produce a working instance `.clan` from a template `.clan`.
///
/// Copies the presentation layer, schema, and spec; resets or seeds the data
/// layer; records a `template` lineage edge back to the source app; sets
/// `view.source = "app"`; and drops `document_type: template`.
pub fn instantiate(template: &ClanFile, opts: InstantiateOptions) -> Result<Vec<u8>> {
    let tpl_manifest = template.manifest();

    if tpl_manifest.document_type.as_deref() != Some("template") {
        return Err(Error::OutputRejected(
            "instantiate expects a template app (document_type: template); \
             this file is not one"
                .into(),
        ));
    }
    let app = tpl_manifest.app.clone().ok_or_else(|| {
        Error::OutputRejected("template app has no `app` block in its manifest".into())
    })?;

    if let Some(id) = &opts.instance_id {
        if !crate::manifest::is_uuid_v4(id) {
            return Err(Error::OutputRejected(format!(
                "instance_id is not a valid UUID v4: {id}"
            )));
        }
    }

    let now = Utc::now().to_rfc3339();
    let id = opts.instance_id.unwrap_or_else(|| Uuid::new_v4().to_string());
    let title = if opts.title.trim().is_empty() {
        app.name.clone()
    } else {
        opts.title.clone()
    };
    let parent_sha = template.sha256();

    let mut manifest = tpl_manifest.clone();
    manifest.id = id;
    manifest.title = title;
    manifest.created_at = now.clone();
    manifest.updated_at = now.clone();
    // An instance is a document, not an app — but it keeps a lightweight app
    // ref so the viewer can show "this is a <app.name> v<app.version> document".
    manifest.document_type = Some(
        opts.document_type
            .unwrap_or_else(|| "document".to_string()),
    );
    manifest.app = Some(app.clone());
    manifest.fork = None;
    manifest.lineage = Some(Lineage {
        parent_id: tpl_manifest.id.clone(),
        parent_uri: format!("file:///unknown/{}.clan", tpl_manifest.id),
        parent_sha256: Some(parent_sha),
        delta: format!("instantiated from template {} v{}", app.name, app.version),
        parents: Vec::new(),
        merge: false,
    });
    manifest.view = Some(ViewState {
        present: true,
        renderable: true,
        stale: false,
        // Authored app view — protected from `clan render` like "agent".
        source: Some("app".into()),
    });

    // Resolve the seed data for sample mode.
    let seed_path = app.data_seed.as_deref().unwrap_or(DATA_PATH);
    let data_bytes = if opts.fresh_data {
        b"{}\n".to_vec()
    } else {
        template
            .read_entry(seed_path)
            .unwrap_or_else(|_| b"{}\n".to_vec())
    };

    let mut builder = ClanBuilder::new(manifest);
    for (path, bytes) in template.read_all_entries()? {
        if path == MANIFEST_PATH || path == DATA_PATH {
            continue;
        }
        builder.add_entry(path, bytes);
    }
    builder.add_entry(DATA_PATH, data_bytes);
    builder.build()
}

/// Promote an authored `.clan` into a template app: set
/// `document_type: template`, attach the `app` block, set `view.source = "app"`,
/// and (optionally) write a standalone `app/manifest.yaml` member.
pub fn make_template(
    clan: &ClanFile,
    app: AppInfo,
    opts: MakeTemplateOptions,
) -> Result<Vec<u8>> {
    let now = Utc::now().to_rfc3339();
    let mut manifest = clan.manifest().clone();

    // The entry the app renders from must exist in the archive.
    if !clan.has_entry(&app.entry) {
        return Err(Error::OutputRejected(format!(
            "app.entry {:?} does not exist in the archive",
            app.entry
        )));
    }

    manifest.document_type = Some("template".into());
    manifest.updated_at = now;
    manifest.view = Some(ViewState {
        present: clan.has_entry(&app.entry),
        renderable: true,
        stale: false,
        source: Some("app".into()),
    });

    let app_manifest_yaml = if opts.embed_app_manifest {
        let yaml = serde_yaml::to_string(&app)
            .map_err(|e| Error::OutputRejected(format!("failed to serialise app block: {e}")))?;
        // Register the member file if it isn't already in the registry.
        if manifest.file_by_path(APP_MANIFEST_PATH).is_none() {
            manifest.files.push(FileEntry {
                id: "app-manifest".into(),
                path: APP_MANIFEST_PATH.into(),
                role: "app-manifest".into(),
                content_type: "application/yaml".into(),
                priority: None,
                sha256: None,
            });
        }
        Some(yaml.into_bytes())
    } else {
        None
    };

    manifest.app = Some(app);

    let mut builder = ClanBuilder::new(manifest);
    for (path, bytes) in clan.read_all_entries()? {
        if path == MANIFEST_PATH || path == APP_MANIFEST_PATH {
            continue;
        }
        builder.add_entry(path, bytes);
    }
    if let Some(bytes) = app_manifest_yaml {
        builder.add_entry(APP_MANIFEST_PATH, bytes);
    }
    builder.build()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::create::{create, CreateOptions};

    /// Build a minimal template app: scaffold a doc, add an authored view,
    /// then promote it with `make_template`.
    fn make_test_template() -> ClanFile {
        let bytes = create(CreateOptions {
            title: "Brief Maker".into(),
            brief: "A template app".into(),
            document_type: None,
            no_render: false,
            schema: None,
        })
        .unwrap();
        let clan = ClanFile::from_bytes(bytes).unwrap();
        let app = AppInfo {
            name: "Brief Maker".into(),
            app_id: "ie.napkin.brief".into(),
            version: "1.0.0".into(),
            icon: None,
            entry: "human/index.html".into(),
            schema: Some("agent/output-schema.json".into()),
            prompt_templates: vec![],
            data_seed: None,
        };
        let tpl = make_template(&clan, app, MakeTemplateOptions::default()).unwrap();
        ClanFile::from_bytes(tpl).unwrap()
    }

    #[test]
    fn make_template_marks_app_and_validates() {
        let tpl = make_test_template();
        let m = tpl.manifest();
        assert_eq!(m.document_type.as_deref(), Some("template"));
        assert_eq!(m.app.as_ref().unwrap().app_id, "ie.napkin.brief");
        assert_eq!(m.view.as_ref().unwrap().source.as_deref(), Some("app"));
        assert!(tpl.has_entry(APP_MANIFEST_PATH), "app/manifest.yaml written");
        assert!(
            m.structural_problems().is_empty(),
            "{:?}",
            m.structural_problems()
        );
    }

    #[test]
    fn instantiate_links_lineage_and_resets_data() {
        let tpl = make_test_template();
        let inst_bytes = instantiate(
            &tpl,
            InstantiateOptions {
                title: "My Brief".into(),
                fresh_data: true,
                ..Default::default()
            },
        )
        .unwrap();
        let inst = ClanFile::from_bytes(inst_bytes).unwrap();
        let m = inst.manifest();

        assert_eq!(m.title, "My Brief");
        assert_eq!(m.document_type.as_deref(), Some("document"));
        assert_ne!(m.id, tpl.manifest().id, "instance gets a fresh id");
        let lineage = m.lineage.as_ref().expect("instance has lineage");
        assert_eq!(lineage.parent_id, tpl.manifest().id);
        assert_eq!(lineage.parent_sha256.as_deref(), Some(tpl.sha256().as_str()));
        assert_eq!(m.view.as_ref().unwrap().source.as_deref(), Some("app"));
        assert_eq!(inst.read_entry_string(DATA_PATH).unwrap().trim(), "{}");
        // The instance is a normal, valid document.
        assert!(m.structural_problems().is_empty());
    }

    #[test]
    fn instantiate_sample_mode_copies_seed_data() {
        // Promote a template whose data layer carries seed content.
        let bytes = create(CreateOptions {
            title: "Seeded".into(),
            brief: "seed".into(),
            document_type: None,
            no_render: false,
            schema: None,
        })
        .unwrap();
        let mut clan = ClanFile::from_bytes(bytes).unwrap();
        // Rebuild with seed data in shared/data.yaml.
        let mut b = ClanBuilder::new(clan.manifest().clone());
        for (p, by) in clan.read_all_entries().unwrap() {
            if p == DATA_PATH {
                continue;
            }
            b.add_entry(p, by);
        }
        b.add_entry(DATA_PATH, b"vendor: Acme\n".to_vec());
        clan = ClanFile::from_bytes(b.build().unwrap()).unwrap();

        let app = AppInfo {
            name: "Seeded".into(),
            app_id: "ie.napkin.seeded".into(),
            version: "1.0.0".into(),
            icon: None,
            entry: "human/index.html".into(),
            schema: None,
            prompt_templates: vec![],
            data_seed: None,
        };
        let tpl = ClanFile::from_bytes(
            make_template(&clan, app, MakeTemplateOptions::default()).unwrap(),
        )
        .unwrap();

        let inst = ClanFile::from_bytes(
            instantiate(
                &tpl,
                InstantiateOptions {
                    fresh_data: false,
                    ..Default::default()
                },
            )
            .unwrap(),
        )
        .unwrap();
        assert!(inst
            .read_entry_string(DATA_PATH)
            .unwrap()
            .contains("Acme"));
    }

    #[test]
    fn instantiate_rejects_non_template() {
        let bytes = create(CreateOptions {
            title: "Plain".into(),
            brief: "x".into(),
            document_type: None,
            no_render: false,
            schema: None,
        })
        .unwrap();
        let clan = ClanFile::from_bytes(bytes).unwrap();
        assert!(instantiate(&clan, InstantiateOptions::default()).is_err());
    }
}
