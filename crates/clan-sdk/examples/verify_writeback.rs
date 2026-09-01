//! End-to-end write-back check: prove an engine draft becomes a provenance-native
//! brief the way the app does it on receiving `draft_brief` fields.
//!
//!   cargo run -p clan-sdk --example verify_writeback -- <app.clan> <draft.json>
//!
//! 1. instantiate a Brief Maker document from the template (fresh data)
//! 2. take the engine's real draft response, drop the app-managed/meta keys the
//!    frontend strips (index.html:467), and apply the rest as ONE attributed
//!    patch — actor "analysis-model", action "draft_brief" — exactly like
//!    do_patch_data (main.rs) via patch_data_with.
//! 3. dump the resulting shared/data.yaml (the filled boxes) and
//!    agent/decision-chain.yaml (the provenance) to prove the loop closed.

use clan_sdk::{
    instantiate, patch_data_with, ClanFile, DecisionEntry, InstantiateOptions, PatchDataOptions,
};
use serde_json::Value;
use std::fs;

// Keys the frontend strips before patchData (index.html:467).
const STRIP: &[&str] = &[
    "rationale",
    "context",
    "reference_assets",
    "locked",
    "locked_fields",
    "brief_style",
    "theme",
    "field_styles",
    "brief_input",
];

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut args = std::env::args().skip(1);
    let app_clan = args.next().expect("usage: <app.clan> <draft.json>");
    let draft_path = args.next().expect("usage: <app.clan> <draft.json>");

    let template = ClanFile::open(&app_clan)?;
    let instance = ClanFile::from_bytes(instantiate(
        &template,
        InstantiateOptions {
            title: "Northwind — Moving People".into(),
            ..Default::default()
        },
    )?)?;
    println!(
        "instantiated Brief Maker doc  (doc_type={:?})",
        instance.manifest().document_type
    );

    let draft: Value = serde_json::from_slice(&fs::read(&draft_path)?)?;
    let rationale = draft
        .get("rationale")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();

    // Build the patch: every returned field except the stripped ones.
    let mut patch = serde_json::Map::new();
    for (k, v) in draft.as_object().unwrap() {
        if !STRIP.contains(&k.as_str()) {
            patch.insert(k.clone(), v.clone());
        }
    }
    let keys: Vec<String> = patch.keys().cloned().collect();
    let patch = Value::Object(patch);

    let decision = DecisionEntry {
        agent_name: "analysis-model".into(),
        action: "draft_brief".into(),
        rationale,
        pinned: false,
        fields_changed: Some(keys.clone()),
    };

    let out = patch_data_with(
        &instance,
        &patch,
        PatchDataOptions {
            append_keys: vec![],
            decision: Some(decision),
        },
        None,
    )?;
    let filled = ClanFile::from_bytes(out)?;

    println!(
        "\npatched {} fields as an attributed decision:\n  {}\n",
        keys.len(),
        keys.join(", ")
    );
    println!("===== shared/data.yaml (the filled brief) =====");
    println!("{}", filled.read_entry_string("shared/data.yaml")?);
    println!("===== agent/decision-chain.yaml (provenance) =====");
    println!("{}", filled.read_entry_string("agent/decision-chain.yaml")?);
    Ok(())
}
