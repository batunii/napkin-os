// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! Operations that change a document.
//!
//! Each is `(Ctx, Document@version, input) → Outcome`: it reads the snapshot,
//! runs the SDK over it and returns the [`Change`]s that would make the result
//! true. None of them can write — they are handed a snapshot, not a store — so
//! whether a change lands, and whether it is still based on the current
//! version, is decided by whoever applies it.

use clan_sdk::{
    apply_patch_and_repack, decision::DecisionScope, fork as sdk_fork, patch_asset_with,
    patch_context, patch_data_with, ClanBuilder, ClanFile, Decision, DecisionChain,
    DecisionEntry, PatchDataOptions, Reasoning,
};
use serde_json::Value;

use crate::ctx::Ctx;
use crate::document::{Change, Document};
use crate::error::{HostError, HostResult};
use crate::event::HostEvent;
use crate::html::strip_scripts;
use crate::log::log;
use crate::store::DocStore;

use super::members::{self, PROJECTION_KEY};
use super::{extract_text, json_merge, sanitize_asset_name, Outcome};

/// The block in `shared/data.yaml` that holds each ancestor's data, frozen, by
/// its document id (Contract 4 §5.2). The spin-off writes it; nothing else does.
pub const UPSTREAM_KEY: &str = "upstream";

/// The refusal for a write that names `upstream`, or anything under it
/// (Contract 4 §8.1, item 4). `named` is the ancestor the write named, when it
/// named one; otherwise the document's own ancestors are named.
pub fn upstream_read_only(clan: &clan_sdk::ClanFile, named: Option<&str>) -> HostError {
    let held: Vec<String> = super::assemble::data_of(clan)
        .ok()
        .and_then(|d| d.get(UPSTREAM_KEY)?.as_object().map(|o| o.keys().cloned().collect()))
        .unwrap_or_default();
    let of = match named {
        Some(id) => id.to_string(),
        None if held.is_empty() => "its parent documents".to_string(),
        None => held.join(", "),
    };
    HostError::bad_request(format!(
        "upstream is the frozen copy of {of}; it is read-only"
    ))
}

/// The ancestor a patch writes under `upstream`, when it names one.
fn upstream_named(patch: &Value) -> Option<&str> {
    patch
        .get(UPSTREAM_KEY)?
        .as_object()?
        .keys()
        .next()
        .map(String::as_str)
}

/// The decision an attributed write records.
///
/// `agent` keeps what the caller *claimed* — `human`, `analysis-model` — because
/// that is what apps use to tell an AI draft from a person's edit, and what the
/// SDK and every existing chain reader match on. Who actually asked is the
/// typed part ([`attributed`]): `actor`, `handler`, `backend` and `scope` from
/// `ctx`, and `claimed_agent` when the claim is not the actor. The rationale is
/// the caller's, untouched.
pub fn attribute(
    ctx: &Ctx,
    claimed: &str,
    action: &str,
    rationale: &str,
    pinned: bool,
    fields_changed: Option<Vec<String>>,
) -> DecisionEntry {
    DecisionEntry {
        agent_name: claimed.to_string(),
        action: action.to_string(),
        rationale: rationale.to_string(),
        pinned,
        fields_changed,
        typed: Some(attributed(ctx, claimed, "edit")),
    }
}

/// The attribution every decision the layer records carries, as fields:
/// `kind`, `actor`, `handler`, `backend`, `scope` (when the shell resolved
/// one), and `claimed_agent` when what the body claimed differs from the
/// actor. Everything else is left for the caller to fill.
pub fn attributed(ctx: &Ctx, claimed: &str, kind: &str) -> Decision {
    let scope = (!ctx.scope.is_empty()).then(|| DecisionScope {
        org: ctx.scope.org.clone(),
        brand: ctx.scope.brand.clone(),
        ..Default::default()
    });
    Decision {
        kind: Some(kind.to_string()),
        actor: Some(ctx.actor.to_string()),
        claimed_agent: (!claimed.is_empty() && claimed != ctx.actor.as_str())
            .then(|| claimed.to_string()),
        handler: ctx.handler.clone(),
        backend: ctx.backend.clone(),
        scope,
        ..Default::default()
    }
}

/// Replace `human/index.html` with the rendered view, scripts stripped.
pub fn snapshot(_ctx: &Ctx, doc: &Document, rendered_html: &str) -> HostResult<Outcome> {
    let clean = strip_scripts(rendered_html);
    log(&format!("snapshot: stripped len={}", clean.len()));
    let clan = doc.clan();
    let mut builder = ClanBuilder::new(clan.manifest().clone());
    for (path, bytes) in clan.read_all_entries()? {
        if path == "manifest.yaml" || path == "human/index.html" {
            continue;
        }
        builder.add_entry(path, bytes);
    }
    builder.add_entry("human/index.html", clean.into_bytes());
    Ok(Outcome::changed(Value::Null, doc.change(builder.build()?)?))
}

/// A legacy HTML-fragment patch. The reply is the payload of the
/// informational `clan-patch-saved` event; the change carries that event too.
///
/// No-op guard (F4): a patch with this id that already holds identical content
/// is not rewritten. Backstops the client-side skip-if-unchanged so a blur
/// with no edit never churns the file.
pub fn save_patch(_ctx: &Ctx, doc: &Document, id: &str, content: &str) -> HostResult<Outcome> {
    log(&format!(
        "save_patch: id={id:?} content={:?}…",
        &content[..content.len().min(80)]
    ));
    let reply = serde_json::json!({ "id": id, "content": content });
    if let Ok(bytes) = doc.clan().read_entry("human/patches.yaml") {
        if let Ok(existing) = clan_sdk::Patches::from_yaml(&bytes) {
            if existing
                .patches
                .iter()
                .any(|p| p.id == id && p.content == content)
            {
                log(&format!("save_patch: no-op (id={id:?} unchanged), skipped"));
                return Ok(Outcome::unchanged(reply));
            }
        }
    }
    let bytes = apply_patch_and_repack(doc.clan(), id.to_string(), content.to_string())?;
    let change = doc
        .change(bytes)?
        .with_event(HostEvent::PatchSaved(reply.clone()));
    Ok(Outcome::changed(reply, change))
}

/// A `POST /patch-data` body, validated before any document is looked at.
pub struct PatchData {
    patch: Value,
    keys: Vec<String>,
    append_keys: Vec<String>,
    /// `(agent, action, rationale, pinned)` when the body names an agent.
    claim: Option<(String, String, String, bool)>,
    /// The body's structured `reasoning`, when it gives one (optional: a
    /// person's edit usually has none). Recorded only with a claim.
    reasoning: Option<Reasoning>,
}

impl PatchData {
    pub fn parse(body: &str) -> HostResult<Self> {
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
        let str_of = |k: &str, default: &str| {
            json.get(k)
                .and_then(|v| v.as_str())
                .unwrap_or(default)
                .to_string()
        };
        let claim = json.get("agent").and_then(|v| v.as_str()).map(|agent| {
            (
                agent.to_string(),
                str_of("action", "edit"),
                str_of("rationale", ""),
                json.get("pinned")
                    .and_then(|v| v.as_bool())
                    .unwrap_or(false),
            )
        });
        let reasoning = match json.get("reasoning") {
            None | Some(Value::Null) => None,
            Some(r) => {
                let r: Reasoning = serde_json::from_value(r.clone())
                    .map_err(|e| HostError::bad_request(format!("reasoning is malformed: {e}")))?;
                let problems = r.problems();
                if !problems.is_empty() {
                    return Err(HostError::bad_request(problems.join("; ")));
                }
                Some(r)
            }
        };
        Ok(Self {
            patch,
            keys,
            append_keys,
            claim,
            reasoning,
        })
    }
}

/// `POST /patch-data` — structured write to `shared/data.yaml`, recorded in the
/// decision chain when the body names an agent (the provenance-native human/AI
/// co-author write path).
pub fn patch_data(ctx: &Ctx, doc: &Document, input: PatchData) -> HostResult<Outcome> {
    // A document with facts or findings members has a host-owned projection
    // of them in its data (Contract 3 §5). No agent or human patch writes it —
    // it is rebuilt from the members, never edited.
    if members::carries_members(doc.clan()) && input.keys.iter().any(|k| k == PROJECTION_KEY) {
        return Err(HostError::bad_request(
            "`projection` is written by the host from the facts and findings members; patch those instead",
        ));
    }
    // A spun-off document holds its ancestors' data frozen under `upstream`
    // (Contract 4 §5.2): what it was spun off from, never edited in the child.
    if input.keys.iter().any(|k| k == UPSTREAM_KEY) {
        return Err(upstream_read_only(doc.clan(), upstream_named(&input.patch)));
    }
    // No-op guard: if applying the patch changes nothing, skip entirely — no
    // rewrite, no decision-chain entry. Stops redundant "edits" (e.g. opening
    // a field and saving without changing it) from polluting the provenance.
    if input.append_keys.is_empty() {
        let existing: Value = doc
            .clan()
            .read_entry("shared/data.yaml")
            .ok()
            .and_then(|b| serde_yaml::from_slice::<serde_yaml::Value>(&b).ok())
            .and_then(|y| serde_json::to_value(y).ok())
            .unwrap_or(Value::Object(Default::default()));
        let mut merged = existing.clone();
        json_merge(&mut merged, &input.patch);
        if merged == existing {
            return Ok(Outcome::unchanged(
                serde_json::json!({ "ok": true, "noop": true, "keys": [] }),
            ));
        }
    }

    // Attribution over exactly the patched keys (F15). The name is the app's
    // claim; who actually asked comes from `ctx`.
    let reasoning = input.reasoning;
    let decision = input.claim.map(|(agent, action, rationale, pinned)| {
        let rationale = match (&reasoning, rationale.trim().is_empty()) {
            (Some(r), true) => r.summary(),
            _ => rationale,
        };
        let mut entry = attribute(
            ctx,
            &agent,
            &action,
            &rationale,
            pinned,
            Some(input.keys.clone()),
        );
        if let Some(typed) = entry.typed.as_mut() {
            typed.reasoning = reasoning;
        }
        entry
    });
    // A person changing only how the document looks (fields the app's schema
    // marks `x-clan-appearance`) asks for no reason and is not a decision
    // about the work: a run of such changes is one rolling entry.
    if let Some(entry) = decision.as_ref().filter(|e| is_human_claim(ctx, &e.agent_name)) {
        let looks = appearance_keys(doc);
        if input.append_keys.is_empty() && input.keys.iter().all(|k| looks.contains(k)) {
            let bytes = roll_look(ctx, doc.clan(), &input.patch, &input.keys, entry)?;
            let reply = serde_json::json!({ "ok": true, "keys": input.keys });
            let change = doc
                .change(bytes)?
                .with_event(HostEvent::DataChanged(reply.clone()));
            return Ok(Outcome::changed(reply, change));
        }
    }
    let opts = PatchDataOptions {
        append_keys: input.append_keys,
        decision,
    };
    let bytes = patch_data_with(doc.clan(), &input.patch, opts, None)?;
    let reply = serde_json::json!({ "ok": true, "keys": input.keys });
    let change = doc
        .change(bytes)?
        .with_event(HostEvent::DataChanged(reply.clone()));
    Ok(Outcome::changed(reply, change))
}

/// The action of the rolling entry a run of look changes is recorded as.
pub const LOOK_ACTION: &str = "look";

const CHAIN_PATH: &str = "agent/decision-chain.yaml";

/// A write made by a person, as the body claims it and the context confirms.
fn is_human_claim(ctx: &Ctx, claimed: &str) -> bool {
    ctx.actor.is_human() || claimed == "human" || claimed.starts_with("human:")
}

/// The top-level data keys an app's schema marks `"x-clan-appearance": true`:
/// how the document looks, not what it says. Read from the document's own
/// schema and from the installed app's, so a document made before the app
/// marked them rolls too.
pub fn appearance_keys(doc: &Document) -> std::collections::BTreeSet<String> {
    let mut out = clan_appearance_keys(doc.clan());
    if let Some(view) = doc.library_view() {
        out.extend(clan_appearance_keys(view.template()));
    }
    out
}

/// [`appearance_keys`], from one archive's own schema.
pub fn clan_appearance_keys(clan: &ClanFile) -> std::collections::BTreeSet<String> {
    let mut out = std::collections::BTreeSet::new();
    let mut read = |clan: &ClanFile| {
        let path = clan
            .manifest()
            .app
            .as_ref()
            .and_then(|a| a.schema.clone())
            .unwrap_or_else(|| "agent/output-schema.json".into());
        let Ok(bytes) = clan.read_entry(&path) else { return };
        let Ok(schema) = serde_json::from_slice::<Value>(&bytes) else { return };
        if let Some(props) = schema.get("properties").and_then(Value::as_object) {
            for (k, v) in props {
                if v.get("x-clan-appearance").and_then(Value::as_bool) == Some(true) {
                    out.insert(k.clone());
                }
            }
        }
    };
    read(clan);
    out
}

/// Write a look change, and record it in the rolling entry: the newest
/// decision when it is already this person's look entry, else a new one.
/// The entry keeps each key's latest words (`looks`), and its rationale is
/// those words together.
fn roll_look(
    ctx: &Ctx,
    clan: &ClanFile,
    patch: &Value,
    keys: &[String],
    entry: &DecisionEntry,
) -> HostResult<Vec<u8>> {
    let chain = clan
        .read_entry(CHAIN_PATH)
        .ok()
        .and_then(|b| DecisionChain::from_yaml(&b).ok())
        .unwrap_or_default();
    let actor = ctx.actor.to_string();
    let rolling = chain.decisions.first().is_some_and(|d| {
        d.action == LOOK_ACTION && d.actor.as_deref() == Some(actor.as_str())
    });
    let said = |k: &str| {
        let r = entry.rationale.trim();
        if r.is_empty() { format!("changed {}", k.replace('_', " ")) } else { r.to_string() }
    };

    if !rolling {
        let mut first = attribute(ctx, &entry.agent_name, LOOK_ACTION, "", false, Some(keys.to_vec()));
        first.rationale = look_rationale(&keys.iter().map(|k| (k.clone(), said(k))).collect::<Vec<_>>());
        let bytes = patch_data_with(
            clan,
            patch,
            PatchDataOptions { append_keys: vec![], decision: Some(first) },
            None,
        )?;
        // Keep each key's words beside the entry, for the next change to roll into.
        return set_looks(bytes, |d| {
            for k in keys {
                d.push((k.clone(), said(k)));
            }
        });
    }

    let bytes = patch_data_with(
        clan,
        patch,
        PatchDataOptions { append_keys: vec![], decision: None },
        None,
    )?;
    set_looks(bytes, |d| {
        for k in keys {
            // A key changed again keeps its place and takes its latest words.
            match d.iter_mut().find(|(have, _)| have == k) {
                Some(slot) => slot.1 = said(k),
                None => d.push((k.clone(), said(k))),
            }
        }
    })
}

/// A look entry's rationale: "Changed the look: Studio brief; Harbour palette".
fn look_rationale(looks: &[(String, String)]) -> String {
    let parts: Vec<&str> = looks.iter().map(|(_, said)| said.as_str()).collect();
    format!("Changed the look: {}", parts.join("; "))
}

/// Update the newest decision — a look entry — in `bytes`: its `looks`, its
/// rationale and fields, and its time, which is the latest change's.
fn set_looks(
    bytes: Vec<u8>,
    edit: impl FnOnce(&mut Vec<(String, String)>),
) -> HostResult<Vec<u8>> {
    let clan = ClanFile::from_bytes(bytes)?;
    let mut chain = DecisionChain::from_yaml(&clan.read_entry(CHAIN_PATH)?)?;
    let Some(d) = chain.decisions.first_mut() else {
        return Ok(clan.raw_bytes().to_vec());
    };
    // `looks` is a mapping in the order the keys were first changed.
    let mut looks: Vec<(String, String)> = d
        .extra
        .get("looks")
        .and_then(|v| v.as_mapping())
        .map(|m| {
            m.iter()
                .filter_map(|(k, v)| Some((k.as_str()?.to_string(), v.as_str()?.to_string())))
                .collect()
        })
        .unwrap_or_default();
    edit(&mut looks);
    d.rationale = look_rationale(&looks);
    d.fields_changed = looks.iter().map(|(k, _)| k.clone()).collect();
    d.timestamp = chrono::Utc::now().to_rfc3339();
    let map: serde_yaml::Mapping = looks
        .into_iter()
        .map(|(k, v)| (serde_yaml::Value::String(k), serde_yaml::Value::String(v)))
        .collect();
    d.extra.insert("looks".into(), serde_yaml::Value::Mapping(map));
    let mut b = ClanBuilder::new(clan.manifest().clone());
    for (path, data) in clan.read_all_entries()? {
        if path != "manifest.yaml" && path != CHAIN_PATH {
            b.add_entry(path, data);
        }
    }
    b.add_entry(CHAIN_PATH, chain.to_yaml()?);
    Ok(b.build()?)
}

/// The agents a `POST /fork` body names, validated before any document is
/// looked at.
pub fn parse_fork(body: &str) -> HostResult<Vec<String>> {
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
    Ok(agents)
}

/// `POST /fork` — one new branch document per agent, next to the parent. The
/// parent itself does not change.
///
/// Reads the store to refuse a branch that already exists, and asks the
/// library where branches go; it writes nothing. Every branch is checked
/// before any is proposed, so a refusal leaves no half-made fork behind.
pub fn fork(
    _ctx: &Ctx,
    doc: &Document,
    store: &dyn DocStore,
    agents: &[String],
) -> HostResult<Outcome> {
    let mut changes = Vec::new();
    let mut written = Vec::new();
    for (agent_id, bytes) in sdk_fork(doc.clan(), agents)? {
        let branch = store.fork_branch(doc.id(), &agent_id);
        if store.exists(&branch) {
            return Err(HostError::conflict(format!(
                "refusing to overwrite {branch}"
            )));
        }
        written.push(serde_json::json!({ "agent": agent_id, "path": branch.to_string() }));
        changes.push(Change::create(branch, bytes));
    }
    Ok(Outcome {
        reply: serde_json::json!({ "ok": true, "branches": written }),
        changes,
    })
}

/// The validated name an upload will be stored under.
pub fn parse_asset_name(name: &str) -> HostResult<String> {
    sanitize_asset_name(name).ok_or_else(|| HostError::bad_request("invalid asset name"))
}

/// `POST /upload-asset?name=&agent=` — store a binary asset inside the archive,
/// with its extracted text cached beside it.
pub fn upload_asset(
    ctx: &Ctx,
    doc: &Document,
    name: &str,
    claimed: Option<&str>,
    body: Vec<u8>,
) -> HostResult<Outcome> {
    let decision = claimed.map(|agent| {
        attribute(
            ctx,
            agent,
            "upload-asset",
            &format!("added asset {name}"),
            false,
            None,
        )
    });
    // Extract text BEFORE the bytes are moved into the repack.
    let extracted = extract_text(name, &body);
    let mut bytes = patch_asset_with(doc.clan(), name, body, decision)?;
    // Cache the extracted text as a sidecar INSIDE the .clan so it travels
    // with the document and is never re-extracted. No decision entry (it's a
    // cache, not an authored decision), and it stays out of the data layer.
    // Folded into the same change as the asset: one write, not two.
    let mut extracted_chars = 0usize;
    if let Some(text) = extracted {
        extracted_chars = text.chars().count();
        let sidecar = format!("human/assets/.extracted/{name}.txt");
        let with_asset = clan_sdk::ClanFile::from_bytes(bytes.clone())?;
        if let Ok(nb) = patch_asset_with(&with_asset, &sidecar, text.into_bytes(), None) {
            bytes = nb;
        }
    }
    let reply = serde_json::json!({
        "ok": true,
        "internal_path": format!("human/assets/{name}"),
        "extracted_chars": extracted_chars
    });
    Ok(Outcome::changed(reply, doc.change(bytes)?))
}

/// Retitle the document (e.g. to the AI-set brief name). Title is not covered
/// by the app signature, so trust is preserved.
pub fn set_title(_ctx: &Ctx, doc: &Document, title: &str) -> HostResult<Outcome> {
    let clan = doc.clan();
    let mut manifest = clan.manifest().clone();
    manifest.title = title.to_string();
    let mut builder = ClanBuilder::new(manifest);
    for (p, b) in clan.read_all_entries()? {
        if p == "manifest.yaml" {
            continue;
        }
        builder.add_entry(p, b);
    }
    let change = doc
        .change(builder.build()?)?
        .with_event(HostEvent::TitleChanged(title.to_string()));
    Ok(Outcome::changed(
        serde_json::json!({ "ok": true, "title": title }),
        change,
    ))
}

/// Replace or append `agent/context.md` — the running brief context
/// downstream agents read. Used by the first generate (full context) and
/// human notes.
pub fn set_context(
    _ctx: &Ctx,
    doc: &Document,
    markdown: &str,
    append: bool,
) -> HostResult<Outcome> {
    let bytes = patch_context(doc.clan(), markdown, append)?;
    Ok(Outcome::changed(
        serde_json::json!({ "ok": true }),
        doc.change(bytes)?,
    ))
}
