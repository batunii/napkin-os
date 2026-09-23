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

use serde_yaml::{Mapping, Value};

use crate::container::{ClanBuilder, ClanFile, MANIFEST_PATH};
use crate::decision::{Decision, DecisionChain};
use crate::error::{Error, Result};
use crate::manifest::{AppInfo, FileEntry, Lineage, ParentRef, ViewState};

const DATA_PATH: &str = "shared/data.yaml";
const APP_MANIFEST_PATH: &str = "app/manifest.yaml";
const CHAIN_PATH: &str = "agent/decision-chain.yaml";
const ASSET_PREFIX: &str = "human/assets/";

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
    let id = opts
        .instance_id
        .unwrap_or_else(|| Uuid::new_v4().to_string());
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
    manifest.document_type = Some(opts.document_type.unwrap_or_else(|| "document".to_string()));
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
pub fn make_template(clan: &ClanFile, app: AppInfo, opts: MakeTemplateOptions) -> Result<Vec<u8>> {
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

    // A template app is not a task — it carries a BARE agent context, not the
    // verbose "produce a rich rendering" brief that `create` seeds. The app
    // owns its presentation; an agent only writes structured data. The context
    // grows from here via the decision chain as the document is filled.
    let schema_path = app
        .schema
        .clone()
        .unwrap_or_else(|| "agent/output-schema.json".into());
    let app_context = format!(
        "# {name}\n\n\
         This is a Napkin app document. The presentation in `{entry}` renders from \
         `shared/data.yaml`; do not generate or edit HTML.\n\n\
         To contribute, write structured fields matching `{schema}` via `patch-data`. \
         Every write is attributed and appended to the decision chain — this context is \
         intentionally minimal and grows as the document is filled.\n",
        name = app.name,
        entry = app.entry,
        schema = schema_path,
    );

    manifest.app = Some(app);

    let mut builder = ClanBuilder::new(manifest);
    for (path, bytes) in clan.read_all_entries()? {
        // Replace the scaffold's verbose context with the bare app context.
        if path == MANIFEST_PATH || path == APP_MANIFEST_PATH || path == "agent/context.md" {
            continue;
        }
        builder.add_entry(path, bytes);
    }
    builder.add_entry("agent/context.md", app_context.into_bytes());
    if let Some(bytes) = app_manifest_yaml {
        builder.add_entry(APP_MANIFEST_PATH, bytes);
    }
    builder.build()
}

// ── Spin-off: branching one document into another app ───────────────────────

/// Options for [`spinoff`].
#[derive(Debug, Clone, Default)]
pub struct SpinoffOptions {
    /// Title for the new document. Defaults to the source's title.
    pub title: String,
    /// Overrides the target app's declared `spinoff.map`.
    pub map: Option<String>,
    /// Override the generated id (must be a UUID v4).
    pub instance_id: Option<String>,
    /// Where the source file lives, recorded in lineage. Defaults to the
    /// `file:///unknown/<id>.clan` form used elsewhere.
    pub source_uri: Option<String>,
}

/// Branch a finished document into a new document of a different app, carrying
/// its data and its decisions across.
///
/// This is not [`instantiate`] (a blank document from a template) and not
/// [`make_template`] (an app built from a document). It is the third edge: a
/// *working document* becomes the starting point of another app's document,
/// keeping the reasoning that produced it. A brief becomes a production; the
/// strategy that was approved during briefing is still legible — and still
/// attributed — while the film is being made.
///
/// The target app declares the shape of the graft in `app.spinoff`, because the
/// target is what knows its own schema. Both parents are recorded, so the new
/// file states which app it is *and* which exact document authorised it.
pub fn spinoff(template: &ClanFile, source: &ClanFile, opts: SpinoffOptions) -> Result<Vec<u8>> {
    let tpl_manifest = template.manifest();
    if tpl_manifest.document_type.as_deref() != Some("template") {
        return Err(Error::OutputRejected(
            "spinoff expects a template app as the target (document_type: template)".into(),
        ));
    }
    let app = tpl_manifest.app.clone().ok_or_else(|| {
        Error::OutputRejected("target template has no `app` block in its manifest".into())
    })?;
    let spec = app.spinoff.clone().unwrap_or_default();

    let src_manifest = source.manifest();
    let src_app_id = src_manifest.app.as_ref().map(|a| a.app_id.as_str());
    if !spec.accepts.is_empty() {
        let ok = src_app_id.is_some_and(|id| spec.accepts.iter().any(|a| a == id));
        if !ok {
            return Err(Error::OutputRejected(format!(
                "{} accepts a spin-off from {:?}, but the source is {}",
                app.name,
                spec.accepts,
                src_app_id.unwrap_or("an app-less document"),
            )));
        }
    }
    if let Some(id) = &opts.instance_id {
        if !crate::manifest::is_uuid_v4(id) {
            return Err(Error::OutputRejected(format!(
                "instance_id is not a valid UUID v4: {id}"
            )));
        }
    }

    let now = Utc::now().to_rfc3339();
    let id = opts
        .instance_id
        .unwrap_or_else(|| Uuid::new_v4().to_string());
    let title = if opts.title.trim().is_empty() {
        src_manifest.title.clone()
    } else {
        opts.title.clone()
    };
    let source_uri = opts
        .source_uri
        .unwrap_or_else(|| format!("file:///unknown/{}.clan", src_manifest.id));

    // ── the data layer: the source's facts, in the target's shape ──────────
    let mut src_data: Value = source
        .read_entry(DATA_PATH)
        .ok()
        .and_then(|b| serde_yaml::from_slice(&b).ok())
        .unwrap_or_else(|| Value::Mapping(Mapping::new()));
    let mut data = Value::Mapping(Mapping::new());

    // Lift the few fields that belong elsewhere in the target schema, taking
    // them out of the source before the rest is grafted wholesale.
    for (from, to) in &spec.lift {
        if let Some(v) = take_dotted(&mut src_data, from) {
            set_dotted(&mut data, to, v);
        }
    }
    match opts.map.as_deref().or(spec.map.as_deref()) {
        Some(path) if !path.is_empty() => set_dotted(&mut data, path, src_data),
        // No declared namespace: fold the source in at the root, without
        // overwriting anything a lift rule has already placed there.
        _ => {
            if let (Some(dst), Some(src)) = (data.as_mapping_mut(), src_data.as_mapping()) {
                for (k, v) in src {
                    if !dst.contains_key(k) {
                        dst.insert(k.clone(), v.clone());
                    }
                }
            }
        }
    }
    let data_bytes = serde_yaml::to_string(&data)
        .map_err(|e| Error::OutputRejected(format!("failed to serialise spun-off data: {e}")))?
        .into_bytes();

    // ── the decision chain: why these facts are what they are ─────────────
    let mut src_chain = source
        .read_entry(CHAIN_PATH)
        .ok()
        .and_then(|b| DecisionChain::from_yaml(&b).ok())
        .unwrap_or_default();
    let carried = src_chain.decisions.len();
    if spec.pin_source_decisions {
        for d in &mut src_chain.decisions {
            d.pinned = true;
        }
    }
    let mut chain = DecisionChain::default();
    chain.decisions.append(&mut src_chain.decisions);
    if let Ok(mut tpl_chain) = template
        .read_entry(CHAIN_PATH)
        .and_then(|b| DecisionChain::from_yaml(&b))
    {
        chain.decisions.append(&mut tpl_chain.decisions);
    }
    let mut marker = Decision::new(
        "napkin-spinoff",
        format!("spin off \"{}\" into {}", src_manifest.title, app.name),
        format!(
            "Carried the data layer and {carried} decision(s) from \"{}\" ({}). \
             Everything below this entry was decided in that document; it stays \
             binding here until something in this one supersedes it.",
            src_manifest.title,
            &src_manifest.id[..8.min(src_manifest.id.len())],
        ),
        now.clone(),
    );
    marker.pinned = true;
    chain.prepend(marker);
    let chain_bytes = chain.to_yaml()?;

    // ── the manifest: two parents, because there were two ─────────────────
    let mut manifest = tpl_manifest.clone();
    manifest.id = id;
    manifest.title = title;
    manifest.created_at = now.clone();
    manifest.updated_at = now;
    manifest.document_type = Some("document".to_string());
    manifest.app = Some(app.clone());
    manifest.fork = None;
    manifest.lineage = Some(Lineage {
        // The source document is the substantive parent — it is where the
        // content came from. The template is recorded alongside it.
        parent_id: src_manifest.id.clone(),
        parent_uri: source_uri.clone(),
        parent_sha256: Some(source.sha256()),
        delta: format!(
            "spun off from \"{}\" into {} v{}",
            src_manifest.title, app.name, app.version
        ),
        parents: vec![
            ParentRef {
                id: src_manifest.id.clone(),
                sha256: Some(source.sha256()),
                delta: Some("source document: data and decisions".into()),
                agent_id: None,
            },
            ParentRef {
                id: tpl_manifest.id.clone(),
                sha256: Some(template.sha256()),
                delta: Some(format!("template app: {} v{}", app.name, app.version)),
                agent_id: None,
            },
        ],
        // Not a `clan merge` product — two origins, one of them an app.
        merge: false,
    });
    manifest.view = Some(ViewState {
        present: true,
        renderable: true,
        stale: false,
        source: Some("app".into()),
    });

    // ── the entries: the target's app, the source's assets ────────────────
    let mut builder = ClanBuilder::new(manifest);
    let mut have: Vec<String> = Vec::new();
    for (path, bytes) in template.read_all_entries()? {
        if path == MANIFEST_PATH || path == DATA_PATH || path == CHAIN_PATH {
            continue;
        }
        have.push(path.clone());
        builder.add_entry(path, bytes);
    }
    // Mood boards, logos, reference stills — the material the decisions were
    // made about travels with them. The template's own assets win a collision.
    for (path, bytes) in source.read_all_entries()? {
        if path.starts_with(ASSET_PREFIX) && !have.contains(&path) {
            builder.add_entry(path, bytes);
        }
    }
    builder.add_entry(DATA_PATH, data_bytes);
    builder.add_entry(CHAIN_PATH, chain_bytes);
    builder.build()
}

/// Descend a dotted path, creating mappings as needed, and set a value.
fn set_dotted(root: &mut Value, path: &str, value: Value) {
    let keys: Vec<&str> = path.split('.').filter(|s| !s.is_empty()).collect();
    let Some((last, parents)) = keys.split_last() else {
        *root = value;
        return;
    };
    let mut cur = root;
    for k in parents {
        if !cur.is_mapping() {
            *cur = Value::Mapping(Mapping::new());
        }
        let key = Value::String((*k).to_string());
        let m = cur.as_mapping_mut().expect("just ensured mapping");
        if !m.contains_key(&key) {
            m.insert(key.clone(), Value::Mapping(Mapping::new()));
        }
        cur = m.get_mut(&key).expect("just inserted");
    }
    if !cur.is_mapping() {
        *cur = Value::Mapping(Mapping::new());
    }
    cur.as_mapping_mut()
        .expect("just ensured mapping")
        .insert(Value::String((*last).to_string()), value);
}

/// Remove and return the value at a dotted path, if it is there.
fn take_dotted(root: &mut Value, path: &str) -> Option<Value> {
    let keys: Vec<&str> = path.split('.').filter(|s| !s.is_empty()).collect();
    let (last, parents) = keys.split_last()?;
    let mut cur = root;
    for k in parents {
        cur = cur
            .as_mapping_mut()?
            .get_mut(Value::String((*k).to_string()))?;
    }
    cur.as_mapping_mut()?
        .remove(Value::String((*last).to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::create::{create, CreateOptions};
    use crate::manifest::SpinoffSpec;

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
            spinoff: None,
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
        assert!(
            tpl.has_entry(APP_MANIFEST_PATH),
            "app/manifest.yaml written"
        );
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
        assert_eq!(
            lineage.parent_sha256.as_deref(),
            Some(tpl.sha256().as_str())
        );
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
            spinoff: None,
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
        assert!(inst.read_entry_string(DATA_PATH).unwrap().contains("Acme"));
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

    /// A source document that has been through an agent: real data, real
    /// reasoning, and an asset the reasoning was about.
    fn filled_source(app_id: &str) -> ClanFile {
        let bytes = create(CreateOptions {
            title: "Acme Brief".into(),
            brief: "a creative brief".into(),
            document_type: None,
            no_render: false,
            schema: None,
        })
        .unwrap();
        let clan = ClanFile::from_bytes(bytes).unwrap();

        let mut manifest = clan.manifest().clone();
        manifest.app = Some(AppInfo {
            name: "Brief Maker".into(),
            app_id: app_id.into(),
            version: "1.0.0".into(),
            icon: None,
            entry: "human/index.html".into(),
            schema: None,
            prompt_templates: vec![],
            data_seed: None,
            spinoff: None,
        });
        let mut chain = DecisionChain::default();
        chain.decisions.push(Decision::new(
            "strategy-model",
            "set single_minded_proposition",
            "the only claim the product can actually support",
            "2026-01-01T00:00:00Z",
        ));
        let mut builder = ClanBuilder::new(manifest);
        for (path, bytes) in clan.read_all_entries().unwrap() {
            if path == MANIFEST_PATH || path == DATA_PATH || path == CHAIN_PATH {
                continue;
            }
            builder.add_entry(path, bytes);
        }
        builder.add_entry(
            DATA_PATH,
            b"project_name: Acme\ninsight: people forget\n".to_vec(),
        );
        builder.add_entry(CHAIN_PATH, chain.to_yaml().unwrap());
        builder.add_entry("human/assets/mood-1.png", b"\x89PNG fake".to_vec());
        ClanFile::from_bytes(builder.build().unwrap()).unwrap()
    }

    fn film_template(spec: Option<SpinoffSpec>) -> ClanFile {
        let bytes = create(CreateOptions {
            title: "Advertising Studio".into(),
            brief: "a production".into(),
            document_type: None,
            no_render: false,
            schema: None,
        })
        .unwrap();
        let source = ClanFile::from_bytes(bytes).unwrap();
        let app = AppInfo {
            name: "Advertising Studio".into(),
            app_id: "ie.napkin.film".into(),
            version: "0.1.0".into(),
            icon: None,
            entry: "human/index.html".into(),
            schema: None,
            prompt_templates: vec![],
            data_seed: None,
            spinoff: spec,
        };
        let bytes = make_template(&source, app, MakeTemplateOptions::default()).unwrap();
        ClanFile::from_bytes(bytes).unwrap()
    }

    fn spec_for_film() -> SpinoffSpec {
        SpinoffSpec {
            accepts: vec!["ie.napkin.brief".into()],
            map: Some("brief".into()),
            lift: [("project_name".to_string(), "project.name".to_string())]
                .into_iter()
                .collect(),
            pin_source_decisions: true,
        }
    }

    #[test]
    fn spinoff_grafts_data_and_carries_reasoning() {
        let source = filled_source("ie.napkin.brief");
        let template = film_template(Some(spec_for_film()));

        let out = spinoff(&template, &source, SpinoffOptions::default()).unwrap();
        let clan = ClanFile::from_bytes(out).unwrap();

        // The brief landed where the target app said it would, and the lifted
        // key went somewhere else entirely.
        let data: serde_yaml::Value =
            serde_yaml::from_slice(&clan.read_entry(DATA_PATH).unwrap()).unwrap();
        assert_eq!(data["brief"]["insight"], Value::from("people forget"));
        assert_eq!(data["project"]["name"], Value::from("Acme"));
        // A lifted key is moved, not copied.
        assert!(data["brief"].get("project_name").is_none());

        // The reasoning came with it, under a marker that says where from.
        let chain = DecisionChain::from_yaml(&clan.read_entry(CHAIN_PATH).unwrap()).unwrap();
        assert_eq!(chain.decisions[0].agent, "napkin-spinoff");
        assert!(chain
            .decisions
            .iter()
            .any(|d| { d.agent == "strategy-model" && d.rationale.contains("actually support") }));
        // Pinned, so compression can never eat the reasoning behind facts this
        // document is now built on.
        assert!(chain.decisions.iter().all(|d| d.pinned));

        // The material the decisions were about travels too.
        assert!(clan.has_entry("human/assets/mood-1.png"));

        // Two origins, both recorded: which app this is, and what authorised it.
        let lineage = clan.manifest().lineage.as_ref().unwrap();
        assert_eq!(lineage.parent_id, source.manifest().id);
        assert_eq!(lineage.parents.len(), 2);
        assert!(!lineage.merge, "a spin-off is not a merge product");
        assert_eq!(
            clan.manifest().app.as_ref().unwrap().app_id,
            "ie.napkin.film"
        );
        assert_eq!(clan.manifest().document_type.as_deref(), Some("document"));
    }

    #[test]
    fn spinoff_refuses_a_source_the_app_does_not_accept() {
        let source = filled_source("ie.napkin.ooh");
        let template = film_template(Some(spec_for_film()));
        let err = spinoff(&template, &source, SpinoffOptions::default()).unwrap_err();
        assert!(
            err.to_string().contains("ie.napkin.ooh"),
            "the refusal should name the source app: {err}"
        );
    }

    #[test]
    fn spinoff_without_a_declared_map_folds_in_at_the_root() {
        let source = filled_source("ie.napkin.brief");
        let template = film_template(None);
        let out = spinoff(&template, &source, SpinoffOptions::default()).unwrap();
        let clan = ClanFile::from_bytes(out).unwrap();
        let data: serde_yaml::Value =
            serde_yaml::from_slice(&clan.read_entry(DATA_PATH).unwrap()).unwrap();
        assert_eq!(data["insight"], Value::from("people forget"));
        // No spec at all still pins carried reasoning — Default agrees with serde.
        let chain = DecisionChain::from_yaml(&clan.read_entry(CHAIN_PATH).unwrap()).unwrap();
        assert!(chain.decisions.iter().all(|d| d.pinned));
    }

    #[test]
    fn spinoff_rejects_a_non_template_target() {
        let source = filled_source("ie.napkin.brief");
        let not_a_template = filled_source("ie.napkin.brief");
        let err = spinoff(&not_a_template, &source, SpinoffOptions::default()).unwrap_err();
        assert!(err.to_string().contains("template"), "{err}");
    }

    #[test]
    fn dotted_helpers_create_and_move() {
        let mut v = Value::Mapping(Mapping::new());
        set_dotted(&mut v, "a.b.c", Value::from(1));
        assert_eq!(v["a"]["b"]["c"], Value::from(1));
        assert_eq!(take_dotted(&mut v, "a.b.c"), Some(Value::from(1)));
        assert_eq!(take_dotted(&mut v, "a.b.c"), None);
        assert_eq!(take_dotted(&mut v, "nope.nothing"), None);
    }
}
