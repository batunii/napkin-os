// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! One generation of a document that carries the members: the data, the
//! facts, findings and sources members, and the decisions that justify the
//! change, written as one archive.
//!
//! Everything that changes a member goes through here — a middleware change
//! and a person's review decision alike — so the projection is always rebuilt
//! from the member bytes actually written, the members are always registered
//! under their roles, and `pack` always validates the data against the
//! document's schema.

use clan_sdk::{
    compress_chain, pack, AgentOutput, ClanBuilder, ClanFile, CompressionConfig, Decision,
    DecisionChain, PackOptions,
};
use serde_json::Value;

use crate::error::HostResult;

use super::members::{self, EDITS, FACTS, FINDINGS, PROJECTION_KEY, SOURCES};

const CHAIN: &str = "agent/decision-chain.yaml";

/// The three list members as they are to be written.
pub struct Members {
    pub facts: Vec<serde_yaml::Value>,
    pub findings: Vec<serde_yaml::Value>,
    pub sources: Vec<serde_yaml::Value>,
    /// A person's wording edits (`shared/edits.yaml`); written only once there is one.
    pub edits: Vec<serde_yaml::Value>,
}

impl Members {
    /// What `clan` holds now.
    pub fn of(clan: &ClanFile) -> HostResult<Self> {
        Ok(Self {
            facts: members::read_list(clan, FACTS)?,
            findings: members::read_list(clan, FINDINGS)?,
            sources: members::read_list(clan, SOURCES)?,
            edits: members::read_list(clan, EDITS)?,
        })
    }
}

/// The document's `shared/data.yaml` as JSON, without the host-owned
/// projection (which [`assemble`] rebuilds).
pub fn data_of(clan: &ClanFile) -> HostResult<Value> {
    let mut data = match clan.read_entry("shared/data.yaml") {
        Ok(bytes) => {
            let y: serde_yaml::Value = serde_yaml::from_slice(&bytes)
                .map_err(|e| crate::error::HostError::internal(format!("shared/data.yaml: {e}")))?;
            serde_json::to_value(y).unwrap_or(Value::Null)
        }
        Err(_) => Value::Null,
    };
    if !data.is_object() {
        data = Value::Object(Default::default());
    }
    if let Some(o) = data.as_object_mut() {
        o.remove(PROJECTION_KEY);
    }
    Ok(data)
}

/// Build the next archive from `clan`: `data` (its projection rebuilt here),
/// the three members, and `decisions` prepended to the chain in the order
/// given (so the last one given ends up newest).
///
/// The sources member is written once there is something in it: a document
/// that never cited a source does not grow an empty file. The projection
/// carries sources only when the document's schema has room for them
/// ([`members::projects_sources`]).
///
/// A document that carries no members and is given none (a brief, marked
/// good) is written without them, and without a projection its schema may
/// not allow — unless `always_members`, which the middleware path keeps.
pub fn assemble(
    clan: &ClanFile,
    mut data: Value,
    m: Members,
    decisions: Vec<Decision>,
    delta: &str,
    now: &str,
    always_members: bool,
) -> HostResult<Vec<u8>> {
    let with_members = always_members
        || members::carries_members(clan)
        || !m.facts.is_empty()
        || !m.findings.is_empty()
        || !m.sources.is_empty();
    let facts_bytes = members::write_doc(members::read_doc(clan, FACTS)?, FACTS, m.facts.clone())?;
    let findings_bytes =
        members::write_doc(members::read_doc(clan, FINDINGS)?, FINDINGS, m.findings.clone())?;
    let has_sources = !m.sources.is_empty() || clan.has_entry(SOURCES.path);
    let sources_bytes =
        members::write_doc(members::read_doc(clan, SOURCES)?, SOURCES, m.sources.clone())?;
    let has_edits = !m.edits.is_empty() || clan.has_entry(EDITS.path);
    let edits_bytes = members::write_doc(members::read_doc(clan, EDITS)?, EDITS, m.edits.clone())?;
    let projected_sources = members::projects_sources(clan)
        .then_some((m.sources.as_slice(), sources_bytes.as_slice()));

    if let Some(obj) = data.as_object_mut().filter(|_| with_members) {
        obj.insert(
            PROJECTION_KEY.into(),
            members::projection(
                &m.facts,
                &facts_bytes,
                &m.findings,
                &findings_bytes,
                projected_sources,
                now,
            ),
        );
    }

    let packed = pack(
        clan,
        AgentOutput {
            mode: "data-update".into(),
            structured: data,
            design: None,
            human: None,
            decision: None,
        },
        PackOptions {
            delta: Some(delta.to_string()),
            ..Default::default()
        },
        None,
    )?;

    // One archive: the packed generation, the members registered with their
    // roles, and the decisions prepended to its chain.
    let packed = ClanFile::from_bytes(packed)?;
    let mut manifest = packed.manifest().clone();
    if with_members {
        members::register(&mut manifest, FACTS);
        members::register(&mut manifest, FINDINGS);
    }
    if with_members && has_sources {
        members::register(&mut manifest, SOURCES);
    }
    if has_edits {
        members::register(&mut manifest, EDITS);
    }

    let mut chain = if packed.has_entry(CHAIN) {
        DecisionChain::from_yaml(&packed.read_entry(CHAIN)?)?
    } else {
        DecisionChain::default()
    };
    for d in decisions {
        chain.prepend(d);
    }
    compress_chain(&mut chain, &CompressionConfig::default(), None);

    let mut builder = ClanBuilder::new(manifest);
    for (path, bytes) in packed.read_all_entries()? {
        if path == clan_sdk::MANIFEST_PATH {
            continue;
        }
        builder.add_entry(path, bytes);
    }
    if with_members {
        builder.add_entry(FACTS.path, facts_bytes);
        builder.add_entry(FINDINGS.path, findings_bytes);
    }
    if with_members && has_sources {
        builder.add_entry(SOURCES.path, sources_bytes);
    }
    if has_edits {
        builder.add_entry(EDITS.path, edits_bytes);
    }
    builder.add_entry(CHAIN, chain.to_yaml()?);
    Ok(builder.build()?)
}
