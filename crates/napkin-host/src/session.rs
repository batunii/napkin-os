// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! One open document, and every operation that reads or mutates it.
//!
//! Every mutating operation follows the established pattern: SDK fn ->
//! `store.write` -> reload the `ClanFile`. The packed archive is the single
//! source of truth; nothing is cached beside it.

use std::sync::{Arc, Mutex};

use clan_sdk::{
    apply_patch_and_repack, export_html, fork, patch_asset_with, patch_context, patch_data_with,
    validate, ClanBuilder, ClanFile, DecisionEntry, ExportOptions, PatchDataOptions,
};
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::error::{HostError, HostResult};
use crate::html::{
    apply_patches, auto_inject_adf_ids, inject_clan_data, inject_styles, resolve_bindings,
    strip_scripts,
};
use crate::log::log;
use crate::store::{DocId, DocStore};

/// Napkin's app-signing public key (ed25519, base64). Safe to embed and ship
/// open-source: it can only VERIFY signatures, never forge them. Apps signed by
/// the matching private key are granted scoped host access.
pub const NAPKIN_PUBLIC_KEY: &str = "iE5TL/Am5Tu4jktPTXNp52HhgJWo8eLoDKgjtlyZ4fc=";

/// Scoped capabilities a trusted app may use (the allowlist — extend as needed).
/// Untrusted apps get none of these; they keep only the safe clan:// data/asset
/// /proxy routes.
pub const TRUSTED_CAPABILITIES: &[&str] = &["notify", "set-theme"];

/// Cap on extracted text we cache + send, to bound the agent's token cost
/// (~6k tokens). The full asset always stays in the archive; this only limits
/// what the agent reads.
const MAX_EXTRACT_CHARS: usize = 24_000;

#[derive(Serialize, Deserialize)]
pub struct ManifestInfo {
    pub title: String,
    pub id: String,
    pub version: String,
    pub created_at: String,
    pub updated_at: String,
    pub document_type: Option<String>,
    pub sha256: String,
    pub file_count: usize,
    pub lineage: Option<LineageInfo>,
    pub app: Option<AppMeta>,
}

/// App metadata surfaced to the shell (launcher cards, "running an app" chrome).
#[derive(Serialize, Deserialize, Clone)]
pub struct AppMeta {
    pub name: String,
    pub app_id: String,
    pub version: String,
    pub icon: Option<String>,
}

#[derive(Serialize, Deserialize)]
pub struct LineageInfo {
    pub parent_id: String,
    pub parent_uri: String,
    pub parent_sha256: Option<String>,
    pub delta: String,
}

#[derive(Serialize, Deserialize)]
pub struct OpenResult {
    /// The document's id in its store. On the desktop that is its path, which
    /// is what the shell hands back to `install_app` and `open`.
    pub path: String,
    pub manifest: ManifestInfo,
    pub validation: String,
    pub has_human_view: bool,
    /// `"authored"` for template apps / their instances (view.source == "app"),
    /// else `"legacy"` (AI-generated HTML). Drives which edit bridge the shell
    /// injects and how the view is rendered.
    pub render_model: String,
    /// `true` when this file is a template app (document_type == "template").
    pub is_template: bool,
    /// `true` when the app is validly signed by Napkin's key → scoped host
    /// capabilities are available to it.
    pub trusted: bool,
}

pub struct LoadedClan {
    pub id: DocId,
    // The ClanFile already holds the raw archive bytes (clan.raw_bytes()).
    pub clan: ClanFile,
    // True if the app is validly signed by Napkin's key → gets scoped host
    // capabilities. Untrusted files are limited to the safe clan:// subset.
    pub trusted: bool,
}

/// The document a shell (or, later, one browser session) currently has open,
/// together with the transient view state that belongs to it.
pub struct Session {
    store: Arc<dyn DocStore>,
    current: Mutex<Option<LoadedClan>>,
    edit_mode: Mutex<bool>,
    preview_html: Mutex<String>,
}

#[cfg(test)]
pub(crate) static SAVE_COUNT: std::sync::atomic::AtomicUsize =
    std::sync::atomic::AtomicUsize::new(0);

impl Session {
    pub fn new(store: Arc<dyn DocStore>) -> Self {
        Self {
            store,
            current: Mutex::new(None),
            edit_mode: Mutex::new(false),
            preview_html: Mutex::new(String::new()),
        }
    }

    pub fn store(&self) -> &Arc<dyn DocStore> {
        &self.store
    }

    /// Run `f` against the open document, or fail with "no file open".
    fn with<T>(&self, f: impl FnOnce(&LoadedClan) -> HostResult<T>) -> HostResult<T> {
        let guard = self.current.lock().unwrap();
        f(guard.as_ref().ok_or_else(HostError::no_file_open)?)
    }

    /// Replace the open document's bytes: write through the store, then reload
    /// so the in-memory archive and the stored one can never diverge.
    fn commit(loaded: &mut LoadedClan, store: &dyn DocStore, bytes: Vec<u8>) -> HostResult<()> {
        store.write(&loaded.id, &bytes)?;
        loaded.clan = ClanFile::from_bytes(bytes)?;
        Ok(())
    }

    pub fn is_open(&self) -> bool {
        self.current.lock().unwrap().is_some()
    }

    pub fn trusted(&self) -> bool {
        self.current
            .lock()
            .unwrap()
            .as_ref()
            .map(|c| c.trusted)
            .unwrap_or(false)
    }

    pub fn current_id(&self) -> Option<DocId> {
        self.current.lock().unwrap().as_ref().map(|c| c.id.clone())
    }

    /// The packed archive exactly as stored — the single-file handoff.
    pub fn raw_bytes(&self) -> HostResult<Vec<u8>> {
        self.with(|l| Ok(l.clan.raw_bytes().to_vec()))
    }

    pub fn title(&self) -> HostResult<String> {
        self.with(|l| Ok(l.clan.manifest().title.clone()))
    }

    // ── Opening ─────────────────────────────────────────────────────────────

    pub fn open(&self, id: DocId) -> HostResult<OpenResult> {
        let clan = ClanFile::from_bytes(self.store.read(&id)?)?;
        let manifest = clan.manifest().clone();
        let report = validate(&clan);
        let has_human_view = clan.has_entry("human/index.html");
        let sha256 = clan.sha256();

        let is_authored = manifest.view.as_ref().and_then(|v| v.source.as_deref()) == Some("app");
        let is_template = manifest.document_type.as_deref() == Some("template");

        let info = ManifestInfo {
            title: manifest.title.clone(),
            id: manifest.id.clone(),
            version: format!("{}.{}", manifest.clan_version, manifest.clan_version_minor),
            created_at: manifest.created_at.clone(),
            updated_at: manifest.updated_at.clone(),
            document_type: manifest.document_type.clone(),
            sha256,
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

        // The trust gate: is this app validly signed by Napkin's key?
        let trusted = clan_sdk::verify_app(&clan, NAPKIN_PUBLIC_KEY);

        let result = OpenResult {
            path: id.to_string(),
            manifest: info,
            validation: report.display(),
            has_human_view,
            render_model: if is_authored {
                "authored".into()
            } else {
                "legacy".into()
            },
            is_template,
            trusted,
        };

        // The ClanFile already read the bytes once; no second read needed.
        *self.current.lock().unwrap() = Some(LoadedClan { id, clan, trusted });
        Ok(result)
    }

    // ── Reading ─────────────────────────────────────────────────────────────

    /// One entry of the open archive, as text. Backs the shell's `get_data` /
    /// `get_chain` / `get_agent_state` / `get_context`.
    pub fn entry_string(&self, path: &str) -> HostResult<String> {
        self.with(|l| Ok(l.clan.read_entry_string(path)?))
    }

    pub fn human_html(&self) -> HostResult<String> {
        log("get_human_html: called");
        self.with(|loaded| {
            let html = loaded.clan.read_entry_string("human/index.html")?;

            // Authored template apps (view.source == "app") render client-side
            // from window.__CLAN__.data. Legacy AI-generated views keep the
            // server-side {{binding}} + auto-id + patch pipeline.
            let authored = loaded
                .clan
                .manifest()
                .view
                .as_ref()
                .and_then(|v| v.source.as_deref())
                == Some("app");

            let data_value: serde_yaml::Value = loaded
                .clan
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
                if loaded.clan.has_entry("human/patches.yaml") {
                    match loaded.clan.read_entry_string("human/patches.yaml") {
                        Ok(yaml) => apply_patches(&with_ids, &yaml),
                        Err(_) => with_ids,
                    }
                } else {
                    with_ids
                }
            };

            let css = loaded
                .clan
                .read_entry_string("human/styles.css")
                .unwrap_or_default();
            let styled_html = inject_styles(&body, &css);

            let context = build_clan_context(&loaded.clan, &data_value);
            let context_json = serde_json::to_string(&context).unwrap_or_else(|_| "{}".to_string());
            Ok(inject_clan_data(&styled_html, &context_json))
        })
    }

    /// `GET /assets/<rel>` — serve a binary asset from inside the artifact ZIP.
    pub fn serve_asset(&self, rel: &str) -> HostResult<(String, Vec<u8>)> {
        if rel.is_empty() || rel.contains("..") || rel.contains('\\') || rel.starts_with('/') {
            return Err(HostError::bad_request("invalid asset path"));
        }
        self.with(|loaded| {
            let full = format!("human/assets/{rel}");
            let bytes = loaded
                .clan
                .read_entry(&full)
                .map_err(|_| HostError::not_found(format!("asset not found: {rel}")))?;
            Ok((content_type_for(rel).to_string(), bytes))
        })
    }

    /// `GET /chain` — the decision chain as JSON (lazy fetch for the view).
    pub fn chain_json(&self) -> HostResult<Value> {
        self.with(|loaded| {
            let yaml = loaded
                .clan
                .read_entry("agent/decision-chain.yaml")
                .map_err(|e| HostError::not_found(e.to_string()))?;
            let v: serde_yaml::Value =
                serde_yaml::from_slice(&yaml).map_err(|e| HostError::internal(e.to_string()))?;
            serde_json::to_value(&v).map_err(|e| HostError::internal(e.to_string()))
        })
    }

    // ── Transient view state ────────────────────────────────────────────────

    pub fn set_edit_mode(&self, active: bool) {
        *self.edit_mode.lock().unwrap() = active;
    }

    pub fn edit_mode(&self) -> bool {
        *self.edit_mode.lock().unwrap()
    }

    pub fn set_preview_html(&self, html: String) {
        *self.preview_html.lock().unwrap() = html;
    }

    pub fn preview_html(&self) -> String {
        self.preview_html.lock().unwrap().clone()
    }

    // ── Writing ─────────────────────────────────────────────────────────────

    pub fn snapshot(&self, rendered_html: &str) -> HostResult<()> {
        let clean = strip_scripts(rendered_html);
        log(&format!("snapshot: stripped len={}", clean.len()));

        let mut guard = self.current.lock().unwrap();
        let loaded = guard.as_mut().ok_or_else(HostError::no_file_open)?;

        let mut builder = ClanBuilder::new(loaded.clan.manifest().clone());
        for (path, bytes) in loaded.clan.read_all_entries()? {
            if path == "manifest.yaml" || path == "human/index.html" {
                continue;
            }
            builder.add_entry(path, bytes);
        }
        builder.add_entry("human/index.html", clean.into_bytes());
        let new_bytes = builder.build()?;
        Self::commit(loaded, &*self.store, new_bytes)?;
        log("snapshot: written to human/index.html");
        Ok(())
    }

    pub fn save_patch(&self, id: String, content: String) -> HostResult<()> {
        log(&format!(
            "save_patch: id={id:?} content={:?}…",
            &content[..content.len().min(80)]
        ));

        let mut guard = self.current.lock().unwrap();
        let loaded = guard.as_mut().ok_or_else(HostError::no_file_open)?;

        // No-op guard (F4): if a patch with this id already holds identical
        // content, skip the rewrite entirely. Backstops the client-side
        // skip-if-unchanged so a blur with no edit never churns the file.
        if let Ok(bytes) = loaded.clan.read_entry("human/patches.yaml") {
            if let Ok(existing) = clan_sdk::Patches::from_yaml(&bytes) {
                if existing
                    .patches
                    .iter()
                    .any(|p| p.id == id && p.content == content)
                {
                    log(&format!("save_patch: no-op (id={id:?} unchanged), skipped"));
                    return Ok(());
                }
            }
        }

        #[cfg(test)]
        SAVE_COUNT.fetch_add(1, std::sync::atomic::Ordering::SeqCst);

        let new_bytes = apply_patch_and_repack(&loaded.clan, id.clone(), content.clone())?;
        Self::commit(loaded, &*self.store, new_bytes)?;

        log(&format!("save_patch: done, file repacked. id={id:?}"));
        Ok(())
    }

    /// Handle a `clan://patch` request body. Saves the patch exactly once and
    /// returns the payload for the informational `clan-patch-saved` event.
    /// The frontend listener must treat that event as a notification only and
    /// never call `save_patch` in response — doing so writes the file twice (#9).
    pub fn handle_patch_request(&self, body: &str) -> Option<Value> {
        let json = serde_json::from_str::<Value>(body).ok()?;
        let id = json["id"].as_str()?;
        let content = json["content"].as_str()?;
        self.save_patch(id.to_string(), content.to_string()).ok()?;
        Some(serde_json::json!({ "id": id, "content": content }))
    }

    /// `POST /patch-data` — structured write to shared/data.yaml with attribution,
    /// recorded in the decision chain (the provenance-native human/AI co-author
    /// write path).
    pub fn patch_data(&self, body: &str) -> HostResult<Value> {
        let json: Value = serde_json::from_str(body)
            .map_err(|e| HostError::bad_request(format!("invalid JSON: {e}")))?;
        let patch = json
            .get("patch")
            .cloned()
            .ok_or_else(|| HostError::bad_request("missing 'patch'"))?;
        if !patch.is_object() {
            return Err(HostError::bad_request("'patch' must be an object"));
        }
        let keys: Vec<String> = patch
            .as_object()
            .map(|o| o.keys().cloned().collect())
            .unwrap_or_default();
        let append_keys: Vec<String> = json
            .get("append_keys")
            .and_then(|v| v.as_array())
            .map(|a| {
                a.iter()
                    .filter_map(|x| x.as_str().map(String::from))
                    .collect()
            })
            .unwrap_or_default();
        // Attribution: when an agent (or "human") is named, record an attributed
        // decision over exactly the patched keys (F15).
        let decision = json
            .get("agent")
            .and_then(|v| v.as_str())
            .map(|agent| DecisionEntry {
                agent_name: agent.to_string(),
                action: json
                    .get("action")
                    .and_then(|v| v.as_str())
                    .unwrap_or("edit")
                    .to_string(),
                rationale: json
                    .get("rationale")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string(),
                pinned: json
                    .get("pinned")
                    .and_then(|v| v.as_bool())
                    .unwrap_or(false),
                fields_changed: Some(keys.clone()),
            });

        let mut guard = self.current.lock().unwrap();
        let loaded = guard.as_mut().ok_or_else(HostError::no_file_open)?;

        // No-op guard: if applying the patch changes nothing, skip entirely — no
        // rewrite, no decision-chain entry. Stops redundant "edits" (e.g. opening
        // a field and saving without changing it) from polluting the provenance.
        if append_keys.is_empty() {
            let existing: Value = loaded
                .clan
                .read_entry("shared/data.yaml")
                .ok()
                .and_then(|b| serde_yaml::from_slice::<serde_yaml::Value>(&b).ok())
                .and_then(|y| serde_json::to_value(y).ok())
                .unwrap_or(Value::Object(Default::default()));
            let mut merged = existing.clone();
            json_merge(&mut merged, &patch);
            if merged == existing {
                return Ok(serde_json::json!({ "ok": true, "noop": true, "keys": [] }));
            }
        }

        let opts = PatchDataOptions {
            append_keys,
            decision,
        };
        let new_bytes = patch_data_with(&loaded.clan, &patch, opts, None)?;
        Self::commit(loaded, &*self.store, new_bytes)?;
        Ok(serde_json::json!({ "ok": true, "keys": keys }))
    }

    /// `POST /fork` — fork into ≥2 branch siblings written next to the parent.
    /// Does NOT advance the open document.
    pub fn fork(&self, body: &str) -> HostResult<Value> {
        let json: Value = serde_json::from_str(body)
            .map_err(|e| HostError::bad_request(format!("invalid JSON: {e}")))?;
        let agents: Vec<String> = json
            .get("agents")
            .and_then(|v| v.as_array())
            .map(|a| {
                a.iter()
                    .filter_map(|x| x.as_str().map(String::from))
                    .collect()
            })
            .unwrap_or_default();
        if agents.len() < 2 {
            return Err(HostError::bad_request("fork needs at least 2 agents"));
        }
        self.with(|loaded| {
            let branches = fork(&loaded.clan, &agents)?;
            let mut written = Vec::new();
            for (agent_id, bytes) in &branches {
                let branch = self.store.fork_branch(&loaded.id, agent_id);
                if self.store.exists(&branch) {
                    return Err(HostError::conflict(format!(
                        "refusing to overwrite {branch}"
                    )));
                }
                self.store.write(&branch, bytes)?;
                written.push(serde_json::json!({ "agent": agent_id, "path": branch.to_string() }));
            }
            Ok(serde_json::json!({ "ok": true, "branches": written }))
        })
    }

    /// `POST /upload-asset?name=&agent=` — store a binary asset inside the archive.
    pub fn upload_asset(
        &self,
        name: &str,
        agent: Option<&str>,
        body: Vec<u8>,
    ) -> HostResult<Value> {
        let name = sanitize_asset_name(name)
            .ok_or_else(|| HostError::bad_request("invalid asset name"))?;
        let mut guard = self.current.lock().unwrap();
        let loaded = guard.as_mut().ok_or_else(HostError::no_file_open)?;
        let decision = agent.map(|a| DecisionEntry {
            agent_name: a.to_string(),
            action: "upload-asset".into(),
            rationale: format!("added asset {name}"),
            pinned: false,
            fields_changed: None,
        });
        // Extract text BEFORE the bytes are moved into the repack.
        let extracted = extract_text(&name, &body);
        let new_bytes = patch_asset_with(&loaded.clan, &name, body, decision)?;
        Self::commit(loaded, &*self.store, new_bytes)?;
        // Cache the extracted text as a sidecar INSIDE the .clan so it travels
        // with the document and is never re-extracted. No decision entry (it's a
        // cache, not an authored decision), and it stays out of the data layer.
        let mut extracted_chars = 0usize;
        if let Some(text) = extracted {
            extracted_chars = text.chars().count();
            let sidecar = format!("human/assets/.extracted/{name}.txt");
            if let Ok(nb) = patch_asset_with(&loaded.clan, &sidecar, text.into_bytes(), None) {
                Self::commit(loaded, &*self.store, nb)?;
            }
        }
        Ok(serde_json::json!({
            "ok": true,
            "internal_path": format!("human/assets/{name}"),
            "extracted_chars": extracted_chars
        }))
    }

    /// Update the open document's title (e.g. to the AI-set brief name) and
    /// repack in place. Title is not covered by the app signature, so trust is
    /// preserved.
    pub fn set_title(&self, title: &str) -> HostResult<Value> {
        let mut guard = self.current.lock().unwrap();
        let loaded = guard.as_mut().ok_or_else(HostError::no_file_open)?;
        let mut manifest = loaded.clan.manifest().clone();
        manifest.title = title.to_string();
        let mut builder = ClanBuilder::new(manifest);
        for (p, b) in loaded.clan.read_all_entries()? {
            if p == "manifest.yaml" {
                continue;
            }
            builder.add_entry(p, b);
        }
        let bytes = builder.build()?;
        Self::commit(loaded, &*self.store, bytes)?;
        Ok(serde_json::json!({ "ok": true, "title": title }))
    }

    /// Replace or append `agent/context.md` — the running brief context
    /// downstream agents read. Used by the first generate (full context) and
    /// human notes.
    pub fn set_context(&self, markdown: &str, append: bool) -> HostResult<Value> {
        let mut guard = self.current.lock().unwrap();
        let loaded = guard.as_mut().ok_or_else(HostError::no_file_open)?;
        let bytes = patch_context(&loaded.clan, markdown, append)?;
        Self::commit(loaded, &*self.store, bytes)?;
        Ok(serde_json::json!({ "ok": true }))
    }

    // ── Export ──────────────────────────────────────────────────────────────

    /// Compose a standalone document from the open `.clan` via the SDK
    /// (bindings resolved, assets inlined, scripts stripped, brand chrome +
    /// optional provenance). Returns `(html, filename_stem)`.
    pub fn compose_export(&self, provenance: bool, no_brand: bool) -> HostResult<(String, String)> {
        self.with(|loaded| {
            let title = loaded.clan.manifest().title.trim().to_string();
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
                &loaded.clan,
                &ExportOptions {
                    brand: !no_brand,
                    provenance,
                },
            )?;
            Ok((html, base))
        })
    }

    // ── The agent's view of the document ────────────────────────────────────

    /// For each attachment carrying a `name`, read its cached extracted-text
    /// sidecar from the open .clan and splice it in as `extracted_text`. Read at
    /// call time so the text never has to live in the data layer or be
    /// re-extracted.
    pub fn attach_extracted_text(&self, payload: &mut Value) {
        let guard = self.current.lock().unwrap();
        let Some(loaded) = guard.as_ref() else {
            return;
        };
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
            if let Ok(text) = loaded.clan.read_entry_string(&sidecar) {
                if let Some(obj) = a.as_object_mut() {
                    obj.insert("extracted_text".into(), Value::String(text));
                }
            }
        }
    }

    /// The provenance bundle an agent needs to fill the boxes coherently: the
    /// schema (what boxes exist), current data (the brief so far), the decision
    /// chain (what's been decided + by whom), the agent context, and lineage.
    /// Built host-side from the open document.
    pub fn clan_context_for_agent(&self) -> Value {
        let guard = self.current.lock().unwrap();
        let Some(loaded) = guard.as_ref() else {
            return Value::Null;
        };
        let clan = &loaded.clan;
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
        })
    }
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

/// RFC 7396 JSON Merge Patch applied in place — used only to test whether a
/// patch would actually change anything (the no-op guard).
fn json_merge(target: &mut Value, patch: &Value) {
    match patch {
        Value::Object(pm) => {
            if !target.is_object() {
                *target = Value::Object(Default::default());
            }
            let tm = target.as_object_mut().unwrap();
            for (k, v) in pm {
                if v.is_null() {
                    tm.remove(k);
                } else {
                    json_merge(tm.entry(k.clone()).or_insert(Value::Null), v);
                }
            }
        }
        _ => *target = patch.clone(),
    }
}

/// Reject asset names with path separators or traversal — the SDK does NOT
/// sanitize, so the host must.
pub fn sanitize_asset_name(name: &str) -> Option<String> {
    let n = name.trim();
    if n.is_empty() || n.contains("..") || n.contains('/') || n.contains('\\') {
        return None;
    }
    Some(n.to_string())
}

/// Pull readable text out of an uploaded asset so the agent sees document
/// *contents*, not just a filename. Plain-text family is decoded directly;
/// PDFs go through `pdf_extract` (wrapped in `catch_unwind` — malformed PDFs
/// can panic deep in the parser). Returns None for binary formats and empties.
fn extract_text(name: &str, bytes: &[u8]) -> Option<String> {
    let ext = name.rsplit('.').next().unwrap_or("").to_ascii_lowercase();
    let raw = match ext.as_str() {
        "txt" | "text" | "md" | "markdown" | "csv" | "tsv" | "json" | "yaml" | "yml" | "log" => {
            String::from_utf8_lossy(bytes).into_owned()
        }
        "pdf" => {
            let owned = bytes.to_vec();
            std::panic::catch_unwind(move || pdf_extract::extract_text_from_mem(&owned).ok())
                .ok()
                .flatten()?
        }
        _ => return None,
    };
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return None;
    }
    Some(clamp_chars(trimmed, MAX_EXTRACT_CHARS))
}

fn clamp_chars(s: &str, max: usize) -> String {
    if s.chars().count() <= max {
        return s.to_string();
    }
    let mut out: String = s.chars().take(max).collect();
    out.push_str("\n\n[… truncated for length …]");
    out
}

pub fn content_type_for(rel: &str) -> &'static str {
    match rel
        .rsplit('.')
        .next()
        .map(|e| e.to_ascii_lowercase())
        .as_deref()
    {
        Some("png") => "image/png",
        Some("jpg") | Some("jpeg") => "image/jpeg",
        Some("gif") => "image/gif",
        Some("webp") => "image/webp",
        Some("svg") => "image/svg+xml",
        Some("pdf") => "application/pdf",
        Some("css") => "text/css",
        Some("js") => "text/javascript",
        Some("json") => "application/json",
        Some("woff2") => "font/woff2",
        Some("woff") => "font/woff",
        _ => "application/octet-stream",
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::store::FsStore;

    /// Serialises tests that assert on the global SAVE_COUNT, so one test's
    /// saves never land inside another's before/after delta window.
    static SAVE_TEST_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

    /// Create a real .clan file on disk and open it into a fresh session.
    fn open_temp_clan() -> (tempfile::TempDir, Session, DocId) {
        let dir = tempfile::tempdir().unwrap();
        let id = DocId::from(dir.path().join("test.clan"));
        let bytes = clan_sdk::create(clan_sdk::CreateOptions {
            title: "Viewer Test".into(),
            brief: "test brief".into(),
            document_type: None,
            no_render: false,
            schema: None,
        })
        .unwrap();
        std::fs::write(id.as_str(), bytes).unwrap();

        let session = Session::new(Arc::new(FsStore::new(dir.path().to_path_buf())));
        session.open(id.clone()).unwrap();
        (dir, session, id)
    }

    fn saves() -> usize {
        SAVE_COUNT.load(std::sync::atomic::Ordering::SeqCst)
    }

    // Regression for #10: opening must not read the file from disk twice.
    // The loaded ClanFile's own raw bytes are the single source of truth and
    // must match what is on disk.
    #[test]
    fn open_populates_state_from_single_read() {
        let (_dir, session, id) = open_temp_clan();

        let guard = session.current.lock().unwrap();
        let loaded = guard.as_ref().expect("state must hold the opened file");
        assert_eq!(loaded.clan.manifest().title, "Viewer Test");
        assert_eq!(
            loaded.clan.raw_bytes(),
            std::fs::read(id.as_str()).unwrap().as_slice(),
            "in-memory archive must match the file on disk"
        );
    }

    // Regression for #9: one clan://patch request must produce exactly one
    // save (one repack + one disk write), with the emitted payload echoing
    // the edit. The frontend listener must never save again.
    #[test]
    fn patch_request_saves_exactly_once() {
        let _guard = SAVE_TEST_LOCK.lock().unwrap();
        let (_dir, session, id) = open_temp_clan();

        let before = saves();
        let payload = session
            .handle_patch_request(r#"{"id":"heading-0","content":"Edited Title"}"#)
            .expect("valid patch body must save and return a payload");
        assert_eq!(saves() - before, 1, "a single edit must save exactly once");
        assert_eq!(payload["id"], "heading-0");
        assert_eq!(payload["content"], "Edited Title");

        // The patch landed on disk exactly once.
        let on_disk = ClanFile::open(id.as_str()).unwrap();
        let patches = on_disk.read_entry_string("human/patches.yaml").unwrap();
        assert_eq!(patches.matches("heading-0").count(), 1);
        assert!(patches.contains("Edited Title"));
    }

    #[test]
    fn patch_request_rejects_malformed_bodies() {
        let _guard = SAVE_TEST_LOCK.lock().unwrap();
        let (_dir, session, _id) = open_temp_clan();
        let before = saves();
        assert!(session.handle_patch_request("not json").is_none());
        assert!(session.handle_patch_request(r#"{"id":"x"}"#).is_none());
        assert_eq!(saves(), before, "malformed bodies must not trigger saves");
    }

    // F4: re-saving identical content for the same id is a no-op — the blur
    // bridge can fire on focus-without-change, and that must not churn the file.
    #[test]
    fn resaving_identical_content_is_a_noop() {
        let _guard = SAVE_TEST_LOCK.lock().unwrap();
        let (_dir, session, id) = open_temp_clan();

        // First save lands.
        session
            .save_patch("heading-0".into(), "Same Title".into())
            .unwrap();
        let after_first = std::fs::read(id.as_str()).unwrap();
        let count_after_first = saves();

        // Identical re-save: no rewrite, no SAVE_COUNT increment, bytes unchanged.
        session
            .save_patch("heading-0".into(), "Same Title".into())
            .unwrap();
        assert_eq!(
            saves(),
            count_after_first,
            "identical re-save must be skipped (F4)"
        );
        assert_eq!(
            after_first,
            std::fs::read(id.as_str()).unwrap(),
            "file must be byte-identical"
        );

        // A genuine change still writes.
        session
            .save_patch("heading-0".into(), "Changed Title".into())
            .unwrap();
        assert!(saves() > count_after_first, "a real edit must still save");
        let on_disk = ClanFile::open(id.as_str()).unwrap();
        assert!(on_disk
            .read_entry_string("human/patches.yaml")
            .unwrap()
            .contains("Changed Title"));
    }

    // --- clan:// API routes ---

    #[test]
    fn patch_data_writes_data_and_attributed_decision() {
        let (_dir, session, id) = open_temp_clan();

        let body = r#"{"patch":{"verdict":"HubSpot"},"agent":"human","action":"set verdict","rationale":"best fit","pinned":true}"#;
        let out = session.patch_data(body).expect("patch-data should succeed");
        assert_eq!(out["ok"], true);
        assert_eq!(out["keys"][0], "verdict");

        let on_disk = ClanFile::open(id.as_str()).unwrap();
        let data = on_disk.read_entry_string("shared/data.yaml").unwrap();
        assert!(data.contains("HubSpot"), "data layer updated: {data}");
        let chain = on_disk
            .read_entry_string("agent/decision-chain.yaml")
            .unwrap();
        assert!(
            chain.contains("human"),
            "decision attributed to human: {chain}"
        );
        assert!(
            chain.contains("verdict"),
            "fields_changed records the key: {chain}"
        );
    }

    #[test]
    fn patch_data_rejects_non_object_patch() {
        let (_dir, session, _id) = open_temp_clan();
        assert!(session.patch_data(r#"{"patch":"nope"}"#).is_err());
        assert!(session.patch_data("not json").is_err());
    }

    #[test]
    fn patch_data_noop_skips_unchanged_write() {
        let (_dir, session, id) = open_temp_clan();
        let body = r#"{"patch":{"verdict":"HubSpot"},"agent":"human","action":"set"}"#;
        session.patch_data(body).unwrap();
        let chain1 = ClanFile::open(id.as_str())
            .unwrap()
            .read_entry_string("agent/decision-chain.yaml")
            .unwrap();
        // Same value again → no-op: no rewrite, no new decision.
        let res = session.patch_data(body).unwrap();
        assert_eq!(res["noop"], true, "unchanged patch must be a no-op");
        let chain2 = ClanFile::open(id.as_str())
            .unwrap()
            .read_entry_string("agent/decision-chain.yaml")
            .unwrap();
        assert_eq!(chain1, chain2, "no-op must not append a decision");
    }

    #[test]
    fn serve_asset_rejects_traversal() {
        let (_dir, session, _id) = open_temp_clan();
        assert_eq!(
            session.serve_asset("../manifest.yaml").unwrap_err().status,
            400
        );
        assert_eq!(session.serve_asset("/etc/passwd").unwrap_err().status, 400);
        // A missing-but-safe path is a 404, not a 400.
        assert_eq!(session.serve_asset("logo.png").unwrap_err().status, 404);
    }

    #[test]
    fn sanitize_asset_name_blocks_separators() {
        assert!(sanitize_asset_name("logo.png").is_some());
        assert!(sanitize_asset_name("../x").is_none());
        assert!(sanitize_asset_name("a/b.png").is_none());
        assert!(sanitize_asset_name("a\\b.png").is_none());
        assert!(sanitize_asset_name("  ").is_none());
    }

    // A route with nothing open answers 409 "no file open", not a panic.
    #[test]
    fn routes_without_an_open_document_are_a_conflict() {
        let dir = tempfile::tempdir().unwrap();
        let session = Session::new(Arc::new(FsStore::new(dir.path().to_path_buf())));
        let err = session.chain_json().unwrap_err();
        assert_eq!(err.status, 409);
        assert_eq!(err.to_string(), "no file open");
    }
}
