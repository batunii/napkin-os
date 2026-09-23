// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! Typed decisions survive every write path, and merges refuse to fold
//! disagreeing verdicts.

use clan_sdk::{
    create, fork, merge, pack, patch_data_with, patch_decision, patch_state, validate, AgentOutput,
    ClanBuilder, ClanFile, CreateOptions, Decision, DecisionChain, DecisionEntry, Manifest,
    MergeOptions, MergeReport, PackOptions, PatchDataOptions, MANIFEST_PATH, MERGE_REPORT_PATH,
};
use serde_json::json;

const CHAIN: &str = "agent/decision-chain.yaml";
const RESEARCH_CHAIN: &str = include_str!("fixtures/research-decision-chain.yaml");

fn doc() -> ClanFile {
    ClanFile::from_bytes(
        create(CreateOptions {
            title: "Identity".into(),
            brief: "a brief".into(),
            document_type: None,
            no_render: false,
            schema: None,
        })
        .unwrap(),
    )
    .unwrap()
}

fn open(bytes: Vec<u8>) -> ClanFile {
    ClanFile::from_bytes(bytes).unwrap()
}

/// Rebuild `clan` with `path` replaced and the manifest edited in place — no
/// new revision.
fn rebuilt(
    clan: &ClanFile,
    path: &str,
    bytes: &[u8],
    edit: impl FnOnce(&mut Manifest),
) -> ClanFile {
    let mut manifest = clan.manifest().clone();
    edit(&mut manifest);
    let mut b = ClanBuilder::new(manifest);
    for (p, v) in clan.read_all_entries().unwrap() {
        if p != MANIFEST_PATH && p != path {
            b.add_entry(p, v);
        }
    }
    b.add_entry(path, bytes.to_vec());
    open(b.build().unwrap())
}

fn chain_of(clan: &ClanFile) -> DecisionChain {
    DecisionChain::from_yaml(&clan.read_entry(CHAIN).unwrap()).unwrap()
}

fn entries_as_values(yaml: &[u8]) -> Vec<serde_yaml::Value> {
    let v: serde_yaml::Value = serde_yaml::from_slice(yaml).unwrap();
    v["decisions"].as_sequence().unwrap().clone()
}

fn strict_ok(clan: &ClanFile) {
    let report = validate(clan);
    assert!(report.is_content_valid(), "{}", report.display());
}

// ── typed decisions ──────────────────────────────────────────────────────

#[test]
fn research_chain_keeps_every_field_through_patch_data() {
    let base = doc();
    let clan = rebuilt(&base, CHAIN, RESEARCH_CHAIN.as_bytes(), |_| {});
    strict_ok(&clan);

    let mut typed = Decision {
        kind: Some("edit".into()),
        actor: Some("human:u_aoife".into()),
        targets: vec!["7c1e9a42-5b3d-4f8e-9a6c-2d1f0e8b4a17#campaign.name".into()],
        ..Default::default()
    };
    typed
        .extra
        .insert("future_field".into(), serde_yaml::Value::from(true));
    let next = open(
        patch_data_with(
            &clan,
            &json!({"campaign": {"name": "Midweek"}}),
            PatchDataOptions {
                append_keys: vec![],
                decision: Some(DecisionEntry {
                    agent_name: "human".into(),
                    action: "patch-data".into(),
                    rationale: "Named it.".into(),
                    pinned: false,
                    fields_changed: Some(vec!["campaign".into()]),
                    typed: Some(typed),
                }),
            },
            None,
        )
        .unwrap(),
    );
    strict_ok(&next);

    let before = entries_as_values(RESEARCH_CHAIN.as_bytes());
    let after = entries_as_values(&next.read_entry(CHAIN).unwrap());
    assert_eq!(after.len(), before.len() + 1);
    // Every original entry — typed fields, unknown fields (flags, source,
    // wrote_fact, material_read, abstained) and all — is exactly as it was.
    assert_eq!(&after[1..], &before[..]);

    let newest = &chain_of(&next).decisions[0];
    assert!(newest.id.as_deref().unwrap().starts_with("d_"));
    assert_eq!(newest.kind.as_deref(), Some("edit"));
    assert_eq!(newest.actor.as_deref(), Some("human:u_aoife"));
    assert_eq!(newest.fields_changed, vec!["campaign".to_string()]);
    assert_eq!(newest.extra["future_field"], serde_yaml::Value::from(true));
}

#[test]
fn every_new_decision_gets_a_stable_id() {
    let clan = doc();
    let next = open(
        patch_decision(
            &clan,
            DecisionEntry {
                agent_name: "a".into(),
                action: "b".into(),
                rationale: "c".into(),
                ..Default::default()
            },
            None,
        )
        .unwrap(),
    );
    let id = chain_of(&next).decisions[0].id.clone().unwrap();
    // Editing the rationale later does not move the id.
    let again = open(patch_state(&next, &json!({"x": 1})).unwrap());
    assert_eq!(
        chain_of(&again).decisions[0].id.as_deref(),
        Some(id.as_str())
    );
}

#[test]
fn agent_json_decisions_carry_typed_fields_but_not_attribution() {
    let out = AgentOutput::from_json(
        &json!({
            "mode": "data-update",
            "structured": {"a": 1},
            "decision": {
                "agent": "extractor", "action": "extract", "rationale": "r",
                "kind": "edit", "cites": ["mat_email01"],
                "actor": "human:forged", "handler": "forged@1.0"
            }
        })
        .to_string(),
    )
    .unwrap();
    let next = open(pack(&doc(), out, PackOptions::default(), None).unwrap());
    let d = &chain_of(&next).decisions[0];
    assert_eq!(d.kind.as_deref(), Some("edit"));
    assert_eq!(d.cites, vec!["mat_email01".to_string()]);
    assert_eq!(
        d.actor, None,
        "the actor comes from the context, not the body"
    );
    assert_eq!(d.handler, None);
    assert!(d.id.is_some());
}

fn verdict_entry(agent: &str, judged: &str, polarity: &str, code: &str) -> DecisionEntry {
    DecisionEntry {
        agent_name: agent.into(),
        action: "verdict".into(),
        rationale: format!("{agent} says {polarity}"),
        typed: Some(Decision {
            kind: Some("verdict".into()),
            polarity: Some(polarity.into()),
            reason_code: Some(code.into()),
            targets: vec![format!("doc#decisions[{judged}]")],
            ..Default::default()
        }),
        ..Default::default()
    }
}

/// A root with one decision to judge, forked into two branches that each
/// record a verdict on it.
fn judged_merge(a: (&str, &str), b: (&str, &str)) -> (ClanFile, String) {
    let root = open(
        patch_decision(
            &doc(),
            DecisionEntry {
                agent_name: "extractor".into(),
                action: "extract".into(),
                rationale: "Read the email.".into(),
                ..Default::default()
            },
            None,
        )
        .unwrap(),
    );
    let judged = chain_of(&root).decisions[0].id.clone().unwrap();
    let branches = fork(&root, &["alpha".into(), "beta".into()]).unwrap();
    let mut it = branches.into_iter();
    let alpha = open(it.next().unwrap().1);
    let beta = open(it.next().unwrap().1);
    let alpha =
        open(patch_decision(&alpha, verdict_entry("alpha", &judged, a.0, a.1), None).unwrap());
    let beta = open(patch_decision(&beta, verdict_entry("beta", &judged, b.0, b.1), None).unwrap());
    let outcome = merge(&[alpha, beta], MergeOptions::default()).unwrap();
    (open(outcome.bytes), judged)
}

#[test]
fn disagreeing_verdicts_on_one_decision_are_a_conflict_not_a_fold() {
    let (merged, judged) = judged_merge(("good", "client_words"), ("bad", "other"));
    let report = MergeReport::from_yaml(&merged.read_entry(MERGE_REPORT_PATH).unwrap()).unwrap();
    assert_eq!(report.unresolved, 1);
    let c = &report.conflicts[0];
    assert_eq!(c.decision.as_deref(), Some(judged.as_str()));
    assert_eq!(c.key, format!("decisions[{judged}]"));
    assert_eq!(c.losers.len(), 1);
    // Both verdicts stay in the chain; nothing was picked.
    let chain = chain_of(&merged);
    let polarities: Vec<_> = chain
        .decisions
        .iter()
        .filter_map(|d| d.polarity.as_deref())
        .collect();
    assert!(polarities.contains(&"good") && polarities.contains(&"bad"));
    strict_ok(&merged);

    // A resolve decision targeting the judged decision settles it.
    let settled = open(
        patch_decision(
            &merged,
            DecisionEntry {
                agent_name: "human".into(),
                action: "resolve".into(),
                rationale: "The planner's reading stands.".into(),
                typed: Some(Decision {
                    kind: Some("resolve".into()),
                    targets: vec![judged.clone()],
                    ..Default::default()
                }),
                ..Default::default()
            },
            None,
        )
        .unwrap(),
    );
    let report = MergeReport::from_yaml(&settled.read_entry(MERGE_REPORT_PATH).unwrap()).unwrap();
    assert_eq!(report.unresolved, 0);
    assert!(report.conflicts.is_empty());
    strict_ok(&settled);
}

#[test]
fn agreeing_verdicts_fold() {
    let (merged, _) = judged_merge(("good", "client_words"), ("good", "client_words"));
    let report = MergeReport::from_yaml(&merged.read_entry(MERGE_REPORT_PATH).unwrap()).unwrap();
    assert_eq!(report.unresolved, 0);
}
