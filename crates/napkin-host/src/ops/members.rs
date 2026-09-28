// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! The facts, findings and sources members, and the projection the host keeps
//! of them.
//!
//! `shared/facts.yaml` (role `pinned-facts`) and `shared/findings.yaml` (role
//! `findings`) are members of their own, registered in the manifest like any
//! other file, rather than blocks inside `shared/data.yaml`. That is decision
//! D2: a data-update pack replaces `shared/data.yaml` whole, so anything kept
//! inside it is one careless write away from being deleted — every pin, with
//! no error. A registered member is carried verbatim by every SDK write that
//! does not name it (`pack` copies each parent entry and keeps each parent
//! `files[]` entry), so nothing folds it away.
//!
//! `shared/sources.yaml` (role `sources`) holds what each cited source is —
//! its address, title, publisher, dates, tier and licence — so a citation in
//! the document leads somewhere without a call to the knowledge layer. Like a
//! pin it is a frozen copy: the first delivery of an id is the one kept.
//!
//! What a view binds to lives in `shared/data.yaml` only, so the host keeps a
//! read-only scalar copy there — `projection` — and rebuilds it whenever either
//! member changes. The members are the authority: `projection.built_from`
//! records their hashes, and a view whose hashes do not match says so rather
//! than render the copy as current (Contract 3 §5).

use clan_sdk::{ClanFile, FileEntry, Manifest};
use serde_json::{Map, Value};

use crate::error::{HostError, HostResult};

pub const FACTS_PATH: &str = "shared/facts.yaml";
pub const FINDINGS_PATH: &str = "shared/findings.yaml";
pub const FACTS_ROLE: &str = "pinned-facts";
pub const FINDINGS_ROLE: &str = "findings";
pub const SOURCES_PATH: &str = "shared/sources.yaml";
pub const SOURCES_ROLE: &str = "sources";

/// The block in `shared/data.yaml` the host owns. No patch writes it.
pub const PROJECTION_KEY: &str = "projection";

/// One of the two list members: where it lives, how it is registered, and the
/// top-level key its list sits under.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Member {
    pub path: &'static str,
    pub role: &'static str,
    pub key: &'static str,
}

pub const FACTS: Member = Member {
    path: FACTS_PATH,
    role: FACTS_ROLE,
    key: "facts",
};

pub const FINDINGS: Member = Member {
    path: FINDINGS_PATH,
    role: FINDINGS_ROLE,
    key: "findings",
};

pub const SOURCES: Member = Member {
    path: SOURCES_PATH,
    role: SOURCES_ROLE,
    key: "sources",
};

/// The fields of a source record the projection copies, in order.
const SOURCE_FIELDS: &[&str] = &[
    "uri",
    "title",
    "publisher",
    "published_at",
    "retrieved_at",
    "tier",
    "domain",
    "licence",
];

/// Whether the document's data schema has room for sources in its projection
/// (`projection.sources`, and `quotes` on each pin). A document made before
/// sources existed has a schema that forbids them; its projection is built as
/// it always was, so it keeps validating.
pub fn projects_sources(clan: &ClanFile) -> bool {
    let Ok(bytes) = clan.read_entry("agent/output-schema.json") else {
        return false;
    };
    serde_json::from_slice::<Value>(&bytes)
        .ok()
        .and_then(|s| {
            s.pointer("/properties/projection/properties/sources")
                .map(|_| ())
        })
        .is_some()
}

/// Whether the document has either member — in its archive or its registry.
/// A document that does is one whose `projection` the host owns.
pub fn carries_members(clan: &ClanFile) -> bool {
    [FACTS, FINDINGS, SOURCES]
        .iter()
        .any(|m| clan.has_entry(m.path) || clan.manifest().file_by_path(m.path).is_some())
}

/// A member's list for an agent to read: empty when the document has none, or
/// when what it has does not parse (the agent gets nothing rather than a
/// guess; the apply path, which must not lose entries, refuses instead).
pub fn list_for_agent(clan: &ClanFile, m: Member) -> Value {
    read_list(clan, m)
        .ok()
        .map(|items| {
            Value::Array(
                items
                    .iter()
                    .filter_map(|y| serde_json::to_value(y).ok())
                    .collect(),
            )
        })
        .unwrap_or_else(|| Value::Array(Vec::new()))
}

/// The member as a YAML document, or an empty mapping when it is absent.
/// Keys other than the list are kept, so a member that grows a header block
/// does not lose it on the next append.
pub fn read_doc(clan: &ClanFile, m: Member) -> HostResult<serde_yaml::Mapping> {
    if !clan.has_entry(m.path) {
        return Ok(serde_yaml::Mapping::new());
    }
    let bytes = clan.read_entry(m.path)?;
    if bytes.iter().all(u8::is_ascii_whitespace) {
        return Ok(serde_yaml::Mapping::new());
    }
    match serde_yaml::from_slice::<serde_yaml::Value>(&bytes) {
        Ok(serde_yaml::Value::Mapping(map)) => Ok(map),
        Ok(serde_yaml::Value::Null) => Ok(serde_yaml::Mapping::new()),
        Ok(_) => Err(HostError::internal(format!(
            "{} is not a mapping with a `{}` list",
            m.path, m.key
        ))),
        Err(e) => Err(HostError::internal(format!(
            "{} does not parse: {e}",
            m.path
        ))),
    }
}

/// The member's entries, in order.
pub fn read_list(clan: &ClanFile, m: Member) -> HostResult<Vec<serde_yaml::Value>> {
    list_of(&read_doc(clan, m)?, m)
}

pub fn list_of(doc: &serde_yaml::Mapping, m: Member) -> HostResult<Vec<serde_yaml::Value>> {
    match doc.get(m.key) {
        None | Some(serde_yaml::Value::Null) => Ok(Vec::new()),
        Some(serde_yaml::Value::Sequence(items)) => Ok(items.clone()),
        Some(_) => Err(HostError::internal(format!(
            "`{}` in {} is not a list",
            m.key, m.path
        ))),
    }
}

/// Serialise a member with `items` as its list, keeping its other keys.
pub fn write_doc(
    mut doc: serde_yaml::Mapping,
    m: Member,
    items: Vec<serde_yaml::Value>,
) -> HostResult<Vec<u8>> {
    doc.insert(m.key.into(), serde_yaml::Value::Sequence(items));
    serde_yaml::to_string(&doc)
        .map(String::into_bytes)
        .map_err(|e| HostError::internal(format!("could not write {}: {e}", m.path)))
}

/// The `id` of one member entry, when it has a string one.
pub fn entry_id(entry: &serde_yaml::Value) -> Option<&str> {
    entry.get("id").and_then(|v| v.as_str())
}

/// Register the member in `manifest.files` with its own role, unless its path
/// is already registered (whoever registered it first — packaging — wins).
/// `ClanBuilder::build` fills in the entry's sha256 from the bytes written.
pub fn register(manifest: &mut Manifest, m: Member) {
    if manifest.file_by_path(m.path).is_some() {
        return;
    }
    manifest.files.push(FileEntry {
        id: m.role.to_string(),
        path: m.path.to_string(),
        role: m.role.to_string(),
        content_type: "application/yaml".into(),
        priority: None,
        sha256: None,
    });
}

/// The projection of the two members, as Contract 3 §5 lays it out: pins by
/// fact id, findings by id, and the sha256 of each member's bytes.
///
/// A pin carries what a view shows beside the value — where and when it was
/// learned (`retrieved_at`, `sources`), the layer row it froze (`version`,
/// `status`) and why it was pinned (`pin_reason`) — plus the stale flag and,
/// when stale, the version and value the layer is at now (`current_version`,
/// `current_value`), so the view can offer the current one without a second
/// read.
///
/// Only keys the entry actually has are copied — a pin missing its `layer` is
/// left without one for the schema to reject, never given an invented value.
///
/// With `sources` (the document's schema has room for them, see
/// [`projects_sources`]) each pin also carries its `quotes` — the verbatim
/// passage each source gave for it — and `replaced_by` when a person picked
/// another value for its identity, and the projection gains `sources`, each
/// cited source by id, with the member's hash in `built_from`.
pub fn projection(
    facts: &[serde_yaml::Value],
    facts_bytes: &[u8],
    findings: &[serde_yaml::Value],
    findings_bytes: &[u8],
    sources: Option<(&[serde_yaml::Value], &[u8])>,
    built_at: &str,
) -> Value {
    let mut pins = Map::new();
    for f in facts {
        let Some(id) = entry_id(f) else { continue };
        let f = serde_json::to_value(f).unwrap_or(Value::Null);
        let mut pin = Map::new();
        for k in [
            "layer",
            "entity",
            "key",
            "market",
            "value",
            "unit",
            "as_of",
            "confidence",
            "licence",
            "method",
            "retrieved_at",
            "sources",
            "version",
            "status",
            "pin_reason",
        ] {
            if let Some(v) = f.get(k).filter(|v| !v.is_null()) {
                pin.insert(k.into(), v.clone());
            }
        }
        if sources.is_some() {
            for k in ["quotes", "replaced_by"] {
                if let Some(v) = f.get(k).filter(|v| v.is_object()) {
                    pin.insert(k.into(), v.clone());
                }
            }
        }
        let stale = f.get("stale").filter(|v| !v.is_null());
        pin.insert("stale".into(), Value::Bool(stale.is_some()));
        for k in ["current_version", "current_value"] {
            if let Some(v) = stale.and_then(|s| s.get(k)).filter(|v| !v.is_null()) {
                pin.insert(k.into(), v.clone());
            }
        }
        pins.insert(id.to_string(), Value::Object(pin));
    }

    let mut found = Map::new();
    for f in findings {
        let Some(id) = entry_id(f) else { continue };
        let f = serde_json::to_value(f).unwrap_or(Value::Null);
        let mut entry = Map::new();
        for k in ["statement", "status", "confidence", "cites"] {
            if let Some(v) = f.get(k).filter(|v| !v.is_null()) {
                entry.insert(k.into(), v.clone());
            }
        }
        if let Some(fid) = f
            .get("verification")
            .and_then(|v| v.get("fact_id"))
            .filter(|v| !v.is_null())
        {
            entry.insert("fact_id".into(), fid.clone());
        }
        found.insert(id.to_string(), Value::Object(entry));
    }

    let mut out = serde_json::json!({
        "built_from": {
            "facts_sha256": clan_sdk::hash::sha256_prefixed(facts_bytes),
            "findings_sha256": clan_sdk::hash::sha256_prefixed(findings_bytes),
            "built_at": built_at,
        },
        "pins": Value::Object(pins),
        "findings": Value::Object(found),
    });
    if let Some((sources, sources_bytes)) = sources {
        let mut by_id = Map::new();
        for s in sources {
            let Some(id) = entry_id(s) else { continue };
            let s = serde_json::to_value(s).unwrap_or(Value::Null);
            let mut src = Map::new();
            for k in SOURCE_FIELDS {
                if let Some(v) = s.get(*k).filter(|v| !v.is_null()) {
                    src.insert((*k).into(), v.clone());
                }
            }
            by_id.insert(id.to_string(), Value::Object(src));
        }
        out["built_from"]["sources_sha256"] =
            Value::String(clan_sdk::hash::sha256_prefixed(sources_bytes));
        out["sources"] = Value::Object(by_id);
    }
    out
}
