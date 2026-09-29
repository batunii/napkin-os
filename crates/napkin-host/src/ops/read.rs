// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! Operations that only read a document: what the view renders, what the
//! agent is shown, what an export composes. Each is a function of one
//! snapshot, so any shell holding a [`Document`] can answer them.

use std::borrow::Cow;
use std::collections::{BTreeMap, BTreeSet};

use clan_sdk::hash::sha256_prefixed;
use clan_sdk::{export_html, validate, ClanFile, DecisionChain, ExportOptions};
use serde_json::{json, Value};

use crate::document::Document;
use crate::error::{HostError, HostResult};
use crate::html::{
    apply_patches, auto_inject_adf_ids, inject_clan_data, inject_styles, resolve_bindings,
};
use crate::log::log;
use crate::session::{AppMeta, LineageInfo, ManifestInfo, OpenResult};
use crate::view;

use super::edit::UPSTREAM_KEY;
use super::members::{FACTS, FINDINGS, SOURCES};
use super::{content_type_for, decisions, members, review};

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
        has_human_view: doc.view_clan().has_entry("human/index.html"),
        render_model: if is_authored {
            "authored".into()
        } else {
            "legacy".into()
        },
        is_template,
        trusted: doc.trusted(),
        view_source: doc.view_source(),
        view_version: doc.view_version().map(String::from),
    }
}

/// One entry of the archive, as text.
pub fn entry_string(doc: &Document, path: &str) -> HostResult<String> {
    Ok(doc.clan().read_entry_string(path)?)
}

/// The view, ready to render: bindings resolved (legacy views), styles and
/// `window.__CLAN__` injected.
///
/// The markup and stylesheet come from the view the document is shown with —
/// the installed app's when [`crate::view`] chose it, else the document's own.
/// Data, manifest and assets are always the document's.
pub fn human_html(doc: &Document) -> HostResult<String> {
    log(&format!(
        "get_human_html: called ({:?} view)",
        doc.view_source()
    ));
    let clan = doc.clan();
    let view = doc.view_clan();
    let html = view.read_entry_string("human/index.html")?;

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

    let css = view
        .read_entry_string("human/styles.css")
        .unwrap_or_default();
    let styled_html = inject_styles(&body, &css);

    let context = build_clan_context(doc, &data_value);
    let context_json = serde_json::to_string(&context).unwrap_or_else(|_| "{}".to_string());
    Ok(inject_clan_data(&styled_html, &context_json))
}

/// `shared/data.yaml` as JSON — what a view holds as `window.__CLAN__.data`.
/// `null` when the document has none or it does not parse.
pub fn data_json(doc: &Document) -> Value {
    doc.clan()
        .read_entry("shared/data.yaml")
        .ok()
        .and_then(|b| serde_yaml::from_slice::<serde_yaml::Value>(&b).ok())
        .and_then(|y| serde_json::to_value(y).ok())
        .unwrap_or(Value::Null)
}

/// `GET /assets/<rel>` — a binary asset from inside the archive.
///
/// The document's own asset first: uploads share `human/assets/` with the
/// view's, and a document's upload must never be shadowed by an app's file of
/// the same name. With a library view, a name the document does not hold is
/// then looked up in the installed template, so an asset a newer view added
/// resolves.
pub fn serve_asset(doc: &Document, rel: &str) -> HostResult<(String, Vec<u8>)> {
    let full = format!("human/assets/{rel}");
    let bytes = doc
        .clan()
        .read_entry(&full)
        .or_else(|e| match doc.library_view() {
            Some(v) => v.template().read_entry(&full),
            None => Err(e),
        })
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
///
/// Composed from [`crate::view::served_archive`], so an export shows the view
/// the user saw — the installed app's when that is what the document is shown
/// with. Nothing is written.
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
    let swapped;
    let served = match view::served_archive(doc)? {
        Cow::Borrowed(_) => doc.clan(),
        Cow::Owned(bytes) => {
            swapped = ClanFile::from_bytes(bytes)?;
            &swapped
        }
    };
    let html = export_html(
        served,
        &ExportOptions {
            brand: !no_brand,
            provenance,
        },
    )?;
    Ok((html, base))
}

/// For each attachment carrying a `name`, read its cached extracted-text
/// sidecar and splice it in. Read at call time so the text never has to live
/// in the data layer or be re-extracted.
///
/// Two payload shapes carry attachments. An agent payload lists them at the
/// top level and reads the text as `extracted_text`. A `napkin.middleware/1`
/// task lists them under `input` and reads it as `text` (middleware-api §1);
/// an attachment there without it is recorded as unread and grounds nothing.
pub fn attach_extracted_text(doc: &Document, payload: &mut Value) {
    splice_text(doc, payload.get_mut("attachments"), "extracted_text");
    let under_input = payload
        .get_mut("input")
        .and_then(|i| i.get_mut("attachments"));
    splice_text(doc, under_input, "text");
    let under_input = payload
        .get_mut("input")
        .and_then(|i| i.get_mut("attachments"));
    splice_images(doc, under_input);
}

/// The picture types a middleware task takes as `image` (middleware-api §10.1,
/// Contract 5 §1.6): the set both model wires accept.
pub const IMAGE_TYPES: &[&str] = &["image/png", "image/jpeg", "image/gif", "image/webp"];

/// The most bytes one `image` may carry, decoded (middleware-api §10.1).
pub const IMAGE_MAX_BYTES: usize = 5 * 1024 * 1024;

/// For each middleware attachment that is a picture and carries no text, send
/// its bytes as `image: {media_type, data}` (base64) — the middleware
/// transcribes it (Contract 5 §1.6). The type is the attachment's
/// `media_type`, else its extension's. A picture of another type, or over
/// [`IMAGE_MAX_BYTES`], is not sent: the attachment goes without text or image
/// and the middleware records it unread, rather than the whole task being
/// refused for one oversized file.
///
/// TODO(O6, Contract 5 §9): an image-only PDF — a scanned deck, no text layer —
/// arrives here with no `text` and is recorded unread. The contract gives its
/// page rendering to the host's extraction (`extract_text` on upload): render
/// each page to PNG there, cache the pages beside the `.extracted/` sidecar,
/// and send them here as `images: [...]` (at most 20 per call). It needs a PDF
/// rasteriser in the host (pdfium or mupdf bindings, native only), which this
/// build does not carry yet.
fn splice_images(doc: &Document, attachments: Option<&mut Value>) {
    use base64::Engine as _;
    let Some(atts) = attachments.and_then(|v| v.as_array_mut()) else {
        return;
    };
    for a in atts.iter_mut() {
        if a.get("text").and_then(Value::as_str).is_some() || a.get("image").is_some() {
            continue;
        }
        let Some(name) = a.get("name").and_then(Value::as_str).map(str::to_string) else {
            continue;
        };
        let media_type = a
            .get("media_type")
            .and_then(Value::as_str)
            .map(str::to_string)
            .unwrap_or_else(|| content_type_for(&name).to_string());
        if !IMAGE_TYPES.contains(&media_type.as_str()) {
            continue;
        }
        let Ok(bytes) = doc.clan().read_entry(&format!("human/assets/{name}")) else {
            continue;
        };
        if bytes.len() > IMAGE_MAX_BYTES {
            continue;
        }
        if let Some(obj) = a.as_object_mut() {
            obj.insert(
                "image".into(),
                serde_json::json!({
                    "media_type": media_type,
                    "data": base64::engine::general_purpose::STANDARD.encode(&bytes),
                }),
            );
        }
    }
}

fn splice_text(doc: &Document, attachments: Option<&mut Value>, key: &str) {
    let Some(atts) = attachments.and_then(|v| v.as_array_mut()) else {
        return;
    };
    for a in atts.iter_mut() {
        let Some(name) = a.get("name").and_then(|v| v.as_str()).map(str::to_string) else {
            continue;
        };
        let sidecar = format!("human/assets/.extracted/{name}.txt");
        if let Ok(text) = doc.clan().read_entry_string(&sidecar) {
            if let Some(obj) = a.as_object_mut() {
                obj.insert(key.into(), Value::String(text));
            }
        }
    }
}

/// The provenance bundle an agent needs to fill the boxes coherently: the
/// schema (what boxes exist), current data (the brief so far), the decision
/// chain (what's been decided + by whom), the agent context, and lineage.
///
/// It also says which document and which version it was read from — `id`, the
/// document's identity (`document_id`, stable across revisions), `revision`,
/// the manifest id of this revision, and `version`, the snapshot's — which is
/// what a middleware change must name to be applied, and carries the facts and findings members (empty
/// lists when the document has none) and the parsed `app/pipeline.yaml` (null
/// when it has none), which the middleware resolves tasks against. Every key
/// an existing agent reads is unchanged.
///
/// A spun-off document's `data.upstream` — its ancestors' data, frozen, often
/// larger than the document itself — is sent as a small index instead
/// ([`upstream_index`], `napkin.middleware/1` §1). The carried pins and
/// findings arrive in `facts` and `findings`, where they were merged.
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
        "data": with_upstream_index(clan, yaml_to_json("shared/data.yaml")),
        "decision_chain": yaml_to_json("agent/decision-chain.yaml"),
        "context": clan.read_entry_string("agent/context.md").unwrap_or_default(),
        "lineage": m.lineage.as_ref().map(|l| serde_json::json!({ "parent_id": l.parent_id, "delta": l.delta })),
        "id": clan.document_id(),
        "revision": m.id,
        "version": doc.version().as_str(),
        "facts": members::list_for_agent(clan, members::FACTS),
        "findings": members::list_for_agent(clan, members::FINDINGS),
        "edits": members::edits_map(clan),
        "pipeline": yaml_to_json("app/pipeline.yaml"),
    })
}

/// `data` with its `upstream` replaced by [`upstream_index`]; unchanged when
/// it carries none.
fn with_upstream_index(clan: &ClanFile, mut data: Value) -> Value {
    if let Some(up) = data.get(UPSTREAM_KEY).filter(|v| v.is_object()) {
        let index = upstream_index(clan, up);
        data[UPSTREAM_KEY] = index;
    }
    data
}

/// What a middleware task is told of each ancestor a document carries
/// (`napkin.middleware/1` §1): whether it is the direct parent, the frozen
/// copy's top-level keys, and its contests still open — `open` in the copy
/// and not resolved in this chain (Contract 4 §7.2, item 1) — each with the
/// fact id of every value.
pub fn upstream_index(clan: &ClanFile, upstream: &Value) -> Value {
    let chain = chain_of(clan);
    let direct = clan.manifest().carried().map(|c| c.document_id.as_str());
    let mut out = serde_json::Map::new();
    for (id, copy) in upstream.as_object().into_iter().flatten() {
        let keys: Vec<&String> = copy.as_object().map(|o| o.keys().collect()).unwrap_or_default();
        let open: Vec<Value> = contests(copy)
            .filter(|c| c.get("status").and_then(Value::as_str) == Some("open"))
            .filter(|c| !resolved_in(&chain, id, str_of(c, "id").unwrap_or_default()))
            .map(|c| {
                let fact_ids: Vec<&str> = c
                    .get("values")
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                    .filter_map(|v| str_of(v, "fact_id"))
                    .collect();
                json!({ "id": c.get("id"), "key": c.get("key"), "fact_ids": fact_ids })
            })
            .collect();
        out.insert(
            id.clone(),
            json!({ "direct": direct == Some(id.as_str()), "keys": keys, "open_contests": open }),
        );
    }
    Value::Object(out)
}

/// `GET /upstream` — what changed in each ancestor since this document was
/// spun off (Contract 4 §8.1, item 6). `parent` finds an ancestor by
/// document id, as the store holds it for whoever asks, or `None`.
///
/// One entry per key of `data.upstream`, the direct parent first. Only the
/// direct parent is compared: `lineage.carried` records it, and a hoisted
/// ancestor's record is its own child's. The parent is `current` when each
/// carried hash is the hash of its entry now — absent on both sides is equal
/// — else `changed`, with its pins, findings and contests set against this
/// document's copies. A backref this document wrote changes only the
/// parent's chain, so it never makes it `changed`.
pub fn upstream_status(doc: &Document, parent: impl Fn(&str) -> Option<Document>) -> Value {
    let clan = doc.clan();
    let here = clan.document_id();
    let Some(carried) = clan.manifest().carried() else {
        return json!({ "document_id": here, "carried": null, "upstream": [] });
    };
    let data = data_json(doc);
    let mut ids: Vec<String> = data
        .get(UPSTREAM_KEY)
        .and_then(Value::as_object)
        .map(|o| o.keys().cloned().collect())
        .unwrap_or_default();
    if !ids.contains(&carried.document_id) {
        ids.push(carried.document_id.clone());
    }
    // Direct first; the rest keep key order.
    ids.sort_by_key(|id| *id != carried.document_id);

    let upstream: Vec<Value> = ids
        .iter()
        .map(|id| {
            let direct = *id == carried.document_id;
            let Some(p) = parent(id) else {
                return json!({ "document_id": id, "direct": direct, "in_store": false, "status": "unknown" });
            };
            let pm = p.clan().manifest();
            let app_id = pm.app.as_ref().map(|a| a.app_id.clone());
            if !direct {
                return json!({
                    "document_id": id, "direct": false, "in_store": true,
                    "title": pm.title, "app_id": app_id, "status": "not_compared",
                });
            }
            let mut entry = compare(doc, &data, carried, &p);
            entry["title"] = pm.title.clone().into();
            entry["app_id"] = json!(app_id);
            entry
        })
        .collect();
    json!({ "document_id": here, "carried": carried, "upstream": upstream })
}

/// The direct parent `p` now, against what `doc` carried from it.
fn compare(doc: &Document, data: &Value, carried: &clan_sdk::Carried, p: &Document) -> Value {
    let pc = p.clan();
    let now = |path: &str| pc.read_entry(path).ok().map(|b| sha256_prefixed(&b));
    let differs = |was: Option<&String>, path: &str| was.cloned() != now(path);
    let changed = json!({
        "data": differs(Some(&carried.data_sha256), "shared/data.yaml"),
        "facts": differs(carried.facts_sha256.as_ref(), FACTS.path),
        "findings": differs(carried.findings_sha256.as_ref(), FINDINGS.path),
        "sources": differs(carried.sources_sha256.as_ref(), SOURCES.path),
    });
    let is_changed = changed.as_object().unwrap().values().any(|v| v == true);
    let parent_chain = chain_of(pc);
    let decisions_since = carried.last_decision.as_deref().and_then(|last| {
        parent_chain
            .decisions
            .iter()
            .position(|d| d.id.as_deref() == Some(last))
    });
    let (pins, findings, contests) = if is_changed {
        (
            pin_changes(doc, p),
            finding_changes(doc, p),
            contest_changes(doc, data, carried, p),
        )
    } else {
        (Vec::new(), Vec::new(), Vec::new())
    };
    json!({
        "document_id": carried.document_id, "direct": true, "in_store": true,
        "version": p.version().as_str(),
        "locked": review::lock_of(&parent_chain, pc.document_id()).is_some(),
        "status": if is_changed { "changed" } else { "current" },
        "changed": changed,
        "decisions_since": decisions_since,
        "pins": pins, "findings": findings, "contests": contests,
    })
}

/// A member's entries by id, sorted — the order every `/upstream` list keeps.
fn by_id(clan: &ClanFile, m: members::Member) -> BTreeMap<String, Value> {
    let Value::Array(items) = members::list_for_agent(clan, m) else {
        return BTreeMap::new();
    };
    items
        .into_iter()
        .filter_map(|v| Some((str_of(&v, "id")?.to_string(), v)))
        .collect()
}

/// The parent's pins against this document's: `added`, `replaced` or
/// `changed`. A pin only this document holds is its own, and not listed.
fn pin_changes(doc: &Document, p: &Document) -> Vec<Value> {
    let ours = by_id(doc.clan(), FACTS);
    let mut out = Vec::new();
    for (id, theirs) in by_id(p.clan(), FACTS) {
        let label = decisions::fact_label(&theirs);
        let set = |v: &Value, k: &str| v.get(k).filter(|x| !x.is_null()).cloned();
        let change = match ours.get(&id) {
            None => Some("added"),
            Some(o) if set(&theirs, "replaced_by").is_some() && set(o, "replaced_by").is_none() => {
                Some("replaced")
            }
            Some(o) if ["value", "unit", "as_of", "status"].iter().any(|k| set(&theirs, k) != set(o, k)) => {
                Some("changed")
            }
            Some(_) => None,
        };
        let Some(change) = change else { continue };
        let mut entry = json!({ "id": id, "change": change, "label": label });
        if change == "replaced" {
            let by = &theirs["replaced_by"];
            entry["replaced_by"] = by.get("fact_id").unwrap_or(by).clone();
        }
        out.push(entry);
    }
    out
}

/// The parent's findings against this document's copies: `added`,
/// `verified` (there, and still proposed here) or `rejected` (there, and not
/// here), each with the fields here that cite it.
fn finding_changes(doc: &Document, p: &Document) -> Vec<Value> {
    let ours = by_id(doc.clan(), FINDINGS);
    let status = |f: &Value| str_of(f, "status").unwrap_or_default().to_string();
    let mut listed: Vec<(String, &str, Value)> = Vec::new();
    for (id, theirs) in by_id(p.clan(), FINDINGS) {
        let change = match ours.get(&id) {
            None => "added",
            Some(o) if status(&theirs) == "verified" && status(o) == "proposed" => "verified",
            Some(o) if status(&theirs) == "rejected" && status(o) != "rejected" => "rejected",
            Some(_) => continue,
        };
        listed.push((id, change, theirs));
    }
    let wanted: BTreeSet<String> = listed.iter().map(|(id, _, _)| id.clone()).collect();
    let citing = if wanted.is_empty() {
        BTreeMap::new()
    } else {
        decisions::fields_citing(doc, &wanted)
    };
    listed
        .into_iter()
        .map(|(id, change, theirs)| {
            let cited_by: Vec<&String> = citing.get(&id).into_iter().flatten().collect();
            let mut entry = json!({
                "id": id, "change": change,
                "statement": theirs.get("statement").cloned().unwrap_or(Value::Null),
                "cited_by": cited_by,
            });
            if change == "rejected" {
                entry["reason"] = theirs
                    .pointer("/rejection/reason")
                    .cloned()
                    .unwrap_or(Value::Null);
            }
            entry
        })
        .collect()
}

/// The parent's contests now against the ones frozen in this document's copy
/// of it: `opened` there since, or `resolved` there since it was carried open.
fn contest_changes(doc: &Document, data: &Value, carried: &clan_sdk::Carried, p: &Document) -> Vec<Value> {
    let up = &carried.document_id;
    let frozen = data.get(UPSTREAM_KEY).and_then(|u| u.get(up)).cloned().unwrap_or(Value::Null);
    let was: BTreeMap<&str, &Value> = contests(&frozen)
        .filter_map(|c| Some((str_of(c, "id")?, c)))
        .collect();
    let parent_data = data_json(p);
    let now: BTreeMap<&str, &Value> = contests(&parent_data)
        .filter_map(|c| Some((str_of(c, "id")?, c)))
        .collect();
    let chain = chain_of(doc.clan());
    let mut out = Vec::new();
    for (id, c) in now {
        let status = str_of(c, "status");
        let before = was.get(id).and_then(|f| str_of(f, "status"));
        let entry = match (before, status) {
            (None, Some("open")) => json!({ "id": id, "change": "opened", "key": c.get("key") }),
            (Some("open"), Some("resolved")) => json!({
                "id": id, "change": "resolved", "key": c.get("key"), "chosen": c.get("chosen"),
                "resolved_here": resolved_in(&chain, up, id),
            }),
            _ => continue,
        };
        out.push(entry);
    }
    out
}

/// A `resolve` in `chain`, not superseded, names the contest `ct` of
/// ancestor `up`: how a carried contest is settled (Contract 4 §7.2).
fn resolved_in(chain: &DecisionChain, up: &str, ct: &str) -> bool {
    let address = format!("{up}#selection.contested[{ct}]");
    chain.decisions.iter().any(|d| {
        d.kind.as_deref() == Some("resolve") && d.superseded_by.is_none() && d.targets.contains(&address)
    })
}

fn contests(data: &Value) -> impl Iterator<Item = &Value> {
    data.pointer("/selection/contested")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
}

fn str_of<'v>(v: &'v Value, key: &str) -> Option<&'v str> {
    v.get(key).and_then(Value::as_str)
}

fn chain_of(clan: &ClanFile) -> DecisionChain {
    clan.read_entry("agent/decision-chain.yaml")
        .ok()
        .and_then(|b| DecisionChain::from_yaml(&b).ok())
        .unwrap_or_default()
}

/// Build the `window.__CLAN__` context object the template/view reads:
/// `{ data, manifest, assets }`. The decision chain is intentionally omitted
/// here (it can be large) — the view fetches it lazily via `clan://chain`.
fn build_clan_context(doc: &Document, data: &serde_yaml::Value) -> Value {
    let clan = doc.clan();
    let data_json: Value = serde_json::to_value(data).unwrap_or(Value::Null);
    let m = clan.manifest();

    // Map every human/assets/<rel> entry to a relative URL the iframe resolves
    // against its own clan:// origin — the document's, then any a library
    // view adds (the same order `serve_asset` resolves them in).
    let mut assets = serde_json::Map::new();
    let library_files = doc
        .library_view()
        .map(|v| v.template().manifest().files.as_slice())
        .unwrap_or_default();
    for f in m.files.iter().chain(library_files) {
        if let Some(rel) = f.path.strip_prefix("human/assets/") {
            assets
                .entry(rel.to_string())
                .or_insert_with(|| Value::String(format!("/assets/{rel}")));
        }
    }

    // `id` is this revision's, fresh on every write; `document_id` is the
    // document's, the same on every revision, and the prefix of every address
    // a view writes (`<document_id>#campaign.problem`).
    let manifest_json = serde_json::json!({
        "id": m.id,
        "document_id": clan.document_id(),
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
        "edits": members::edits_map(clan),
    })
}
