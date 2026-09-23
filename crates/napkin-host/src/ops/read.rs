// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! Operations that only read a document: what the view renders, what the
//! agent is shown, what an export composes. Each is a function of one
//! snapshot, so any shell holding a [`Document`] can answer them.

use clan_sdk::{export_html, validate, ClanFile, ExportOptions};
use serde_json::Value;

use crate::document::Document;
use crate::error::{HostError, HostResult};
use crate::html::{
    apply_patches, auto_inject_adf_ids, inject_clan_data, inject_styles, resolve_bindings,
};
use crate::log::log;
use crate::session::{AppMeta, LineageInfo, ManifestInfo, OpenResult};

use super::{content_type_for, members};

/// What a shell is told about a document it has just opened.
pub fn describe(doc: &Document) -> OpenResult {
    let clan = doc.clan();
    let manifest = clan.manifest();
    let is_authored = manifest.view.as_ref().and_then(|v| v.source.as_deref()) == Some("app");
    let is_template = manifest.document_type.as_deref() == Some("template");

    let info = ManifestInfo {
        title: manifest.title.clone(),
        id: manifest.id.clone(),
        version: format!("{}.{}", manifest.clan_version, manifest.clan_version_minor),
        created_at: manifest.created_at.clone(),
        updated_at: manifest.updated_at.clone(),
        document_type: manifest.document_type.clone(),
        sha256: clan.sha256(),
        file_count: manifest.files.len(),
        lineage: manifest.lineage.as_ref().map(|l| LineageInfo {
            parent_id: l.parent_id.clone(),
            parent_uri: l.parent_uri.clone(),
            parent_sha256: l.parent_sha256.clone(),
            delta: l.delta.clone(),
        }),
        app: manifest.app.as_ref().map(|a| AppMeta {
            name: a.name.clone(),
            app_id: a.app_id.clone(),
            version: a.version.clone(),
            icon: a.icon.clone(),
        }),
    };

    OpenResult {
        path: doc.id().to_string(),
        manifest: info,
        validation: validate(clan).display(),
        has_human_view: clan.has_entry("human/index.html"),
        render_model: if is_authored {
            "authored".into()
        } else {
            "legacy".into()
        },
        is_template,
        trusted: doc.trusted(),
    }
}

/// One entry of the archive, as text.
pub fn entry_string(doc: &Document, path: &str) -> HostResult<String> {
    Ok(doc.clan().read_entry_string(path)?)
}

/// The view, ready to render: bindings resolved (legacy views), styles and
/// `window.__CLAN__` injected.
pub fn human_html(doc: &Document) -> HostResult<String> {
    log("get_human_html: called");
    let clan = doc.clan();
    let html = clan.read_entry_string("human/index.html")?;

    // Authored template apps (view.source == "app") render client-side
    // from window.__CLAN__.data. Legacy AI-generated views keep the
    // server-side {{binding}} + auto-id + patch pipeline.
    let authored = clan
        .manifest()
        .view
        .as_ref()
        .and_then(|v| v.source.as_deref())
        == Some("app");

    let data_value: serde_yaml::Value = clan
        .read_entry("shared/data.yaml")
        .ok()
        .and_then(|b| serde_yaml::from_slice(&b).ok())
        .unwrap_or(serde_yaml::Value::Null);

    let body = if authored {
        // Don't munge the authored markup — the app owns its rendering.
        html
    } else {
        let resolved = resolve_bindings(&html, &data_value);
        let with_ids = auto_inject_adf_ids(&resolved);
        if clan.has_entry("human/patches.yaml") {
            match clan.read_entry_string("human/patches.yaml") {
                Ok(yaml) => apply_patches(&with_ids, &yaml),
                Err(_) => with_ids,
            }
        } else {
            with_ids
        }
    };

    let css = clan
        .read_entry_string("human/styles.css")
        .unwrap_or_default();
    let styled_html = inject_styles(&body, &css);

    let context = build_clan_context(clan, &data_value);
    let context_json = serde_json::to_string(&context).unwrap_or_else(|_| "{}".to_string());
    Ok(inject_clan_data(&styled_html, &context_json))
}

/// `GET /assets/<rel>` — a binary asset from inside the archive.
pub fn serve_asset(doc: &Document, rel: &str) -> HostResult<(String, Vec<u8>)> {
    let full = format!("human/assets/{rel}");
    let bytes = doc
        .clan()
        .read_entry(&full)
        .map_err(|_| HostError::not_found(format!("asset not found: {rel}")))?;
    Ok((content_type_for(rel).to_string(), bytes))
}

/// Refuse an asset path that could escape `human/assets/`. Checked before a
/// document is even looked at, so a hostile path is a 400 whatever is open.
pub fn check_asset_path(rel: &str) -> HostResult<()> {
    if rel.is_empty() || rel.contains("..") || rel.contains('\\') || rel.starts_with('/') {
        return Err(HostError::bad_request("invalid asset path"));
    }
    Ok(())
}

/// `GET /chain` — the decision chain as JSON (lazy fetch for the view).
pub fn chain_json(doc: &Document) -> HostResult<Value> {
    let yaml = doc
        .clan()
        .read_entry("agent/decision-chain.yaml")
        .map_err(|e| HostError::not_found(e.to_string()))?;
    let v: serde_yaml::Value =
        serde_yaml::from_slice(&yaml).map_err(|e| HostError::internal(e.to_string()))?;
    serde_json::to_value(&v).map_err(|e| HostError::internal(e.to_string()))
}

/// Compose a standalone document via the SDK (bindings resolved, assets
/// inlined, scripts stripped, brand chrome + optional provenance). Returns
/// `(html, filename_stem)`.
pub fn compose_export(
    doc: &Document,
    provenance: bool,
    no_brand: bool,
) -> HostResult<(String, String)> {
    let title = doc.title().trim().to_string();
    let base: String = (if title.is_empty() { "document" } else { &title })
        .chars()
        .map(|c| {
            if c.is_alphanumeric() || c == '.' || c == '-' {
                c
            } else {
                '-'
            }
        })
        .collect();
    let html = export_html(
        doc.clan(),
        &ExportOptions {
            brand: !no_brand,
            provenance,
        },
    )?;
    Ok((html, base))
}

/// For each attachment carrying a `name`, read its cached extracted-text
/// sidecar and splice it in as `extracted_text`. Read at call time so the text
/// never has to live in the data layer or be re-extracted.
pub fn attach_extracted_text(doc: &Document, payload: &mut Value) {
    let Some(atts) = payload
        .get_mut("attachments")
        .and_then(|v| v.as_array_mut())
    else {
        return;
    };
    for a in atts.iter_mut() {
        let Some(name) = a.get("name").and_then(|v| v.as_str()).map(str::to_string) else {
            continue;
        };
        let sidecar = format!("human/assets/.extracted/{name}.txt");
        if let Ok(text) = doc.clan().read_entry_string(&sidecar) {
            if let Some(obj) = a.as_object_mut() {
                obj.insert("extracted_text".into(), Value::String(text));
            }
        }
    }
}

/// The provenance bundle an agent needs to fill the boxes coherently: the
/// schema (what boxes exist), current data (the brief so far), the decision
/// chain (what's been decided + by whom), the agent context, and lineage.
///
/// It also says which document and which version it was read from (`id`, the
/// manifest id, and `version`, the snapshot's) — what a middleware change must
/// name to be applied — and carries the facts and findings members (empty
/// lists when the document has none) and the parsed `app/pipeline.yaml` (null
/// when it has none), which the middleware resolves tasks against. Every key
/// an existing agent reads is unchanged.
pub fn clan_context_for_agent(doc: &Document) -> Value {
    let clan = doc.clan();
    let yaml_to_json = |p: &str| -> Value {
        clan.read_entry(p)
            .ok()
            .and_then(|b| serde_yaml::from_slice::<serde_yaml::Value>(&b).ok())
            .and_then(|y| serde_json::to_value(y).ok())
            .unwrap_or(Value::Null)
    };
    let schema: Value = clan
        .read_entry("agent/output-schema.json")
        .ok()
        .and_then(|b| serde_json::from_slice(&b).ok())
        .unwrap_or(Value::Null);
    let m = clan.manifest();
    serde_json::json!({
        "document_type": m.document_type,
        "app": m.app.as_ref().map(|a| serde_json::json!({ "name": a.name, "app_id": a.app_id, "version": a.version })),
        "schema": schema,
        "data": yaml_to_json("shared/data.yaml"),
        "decision_chain": yaml_to_json("agent/decision-chain.yaml"),
        "context": clan.read_entry_string("agent/context.md").unwrap_or_default(),
        "lineage": m.lineage.as_ref().map(|l| serde_json::json!({ "parent_id": l.parent_id, "delta": l.delta })),
        "id": m.id,
        "version": doc.version().as_str(),
        "facts": members::list_for_agent(clan, members::FACTS),
        "findings": members::list_for_agent(clan, members::FINDINGS),
        "pipeline": yaml_to_json("app/pipeline.yaml"),
    })
}

/// Build the `window.__CLAN__` context object the template/view reads:
/// `{ data, manifest, assets }`. The decision chain is intentionally omitted
/// here (it can be large) — the view fetches it lazily via `clan://chain`.
fn build_clan_context(clan: &ClanFile, data: &serde_yaml::Value) -> Value {
    let data_json: Value = serde_json::to_value(data).unwrap_or(Value::Null);
    let m = clan.manifest();

    // Map every human/assets/<rel> entry to a relative URL the iframe resolves
    // against its own clan:// origin.
    let mut assets = serde_json::Map::new();
    for f in &m.files {
        if let Some(rel) = f.path.strip_prefix("human/assets/") {
            assets.insert(rel.to_string(), Value::String(format!("/assets/{rel}")));
        }
    }

    let manifest_json = serde_json::json!({
        "id": m.id,
        "title": m.title,
        "document_type": m.document_type,
        "app": m.app.as_ref().map(|a| serde_json::json!({
            "name": a.name,
            "app_id": a.app_id,
            "version": a.version,
        })),
    });

    serde_json::json!({
        "data": data_json,
        "manifest": manifest_json,
        "assets": Value::Object(assets),
    })
}
