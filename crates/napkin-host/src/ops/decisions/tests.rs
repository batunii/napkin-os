// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

use super::*;
use crate::store::DocId;
use clan_sdk::{ClanBuilder, ClanFile, CreateOptions};

const DOC: &str = "7c1e9a42-5b3d-4f8e-9a6c-2d1f0e8b4a17";
const EXAMPLE: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../../app/templates/campaign-research/example"
);

/// A document with `entries` written over a fresh one, under the id the
/// example's addresses use.
fn doc_with(entries: &[(&str, &str)]) -> Document {
    let bytes = clan_sdk::create(CreateOptions {
        title: "Decisions".into(),
        brief: "test".into(),
        document_type: None,
        no_render: true,
        schema: None,
    })
    .unwrap();
    let clan = ClanFile::from_bytes(bytes).unwrap();
    let mut manifest = clan.manifest().clone();
    manifest.document_id = Some(DOC.into());
    let mut b = ClanBuilder::new(manifest);
    for (path, bytes) in clan.read_all_entries().unwrap() {
        if path != clan_sdk::MANIFEST_PATH {
            b.add_entry(path, bytes);
        }
    }
    for (path, body) in entries {
        b.add_entry(*path, body.as_bytes().to_vec());
    }
    Document::from_bytes(DocId::new("t.clan"), b.build().unwrap()).unwrap()
}

/// The campaign-research example, as `npm run apps` packs it.
fn example() -> Document {
    let read = |p: &str| std::fs::read_to_string(format!("{EXAMPLE}/{p}")).unwrap();
    let (chain, data, facts, findings) = (
        read("agent/decision-chain.yaml"),
        read("shared/data.yaml"),
        read("shared/facts.yaml"),
        read("shared/findings.yaml"),
    );
    doc_with(&[
        (CHAIN_PATH, &chain),
        ("shared/data.yaml", &data),
        (members::FACTS_PATH, &facts),
        (members::FINDINGS_PATH, &findings),
    ])
}

fn codes(v: &DecisionsView) -> Vec<(&'static str, Option<&str>)> {
    v.attention
        .iter()
        .map(|a| (a.code, a.decision.as_deref()))
        .collect()
}

#[test]
fn the_example_lists_what_blocks_its_lock() {
    let v = decisions(&example()).unwrap();
    assert!(v.problem.is_none());
    assert_eq!(v.document_id, DOC);

    // Newest first.
    let stamps: Vec<i64> = v
        .decisions
        .iter()
        .map(|b| stamp(&b.decision.timestamp))
        .collect();
    assert!(stamps.windows(2).all(|w| w[0] >= w[1]), "{stamps:?}");

    let c = codes(&v);
    // The open ABV contest, on the decision that opened it; the resolved
    // share contest is not listed.
    assert!(
        c.contains(&("open_contest", Some("d_01JA0D05CTO"))),
        "{c:?}"
    );
    assert_eq!(c.iter().filter(|x| x.0 == "open_contest").count(), 1);
    // Three proposed findings, all from the synthesise decision.
    let unverified = ("unverified_finding", Some("d_01JA0D06SYN"));
    assert_eq!(c.iter().filter(|x| **x == unverified).count(), 3);
    // campaign.in_market still cites the rejected fi_01JA0F5E.
    let flagged: Vec<_> = v
        .attention
        .iter()
        .filter(|a| a.code == "flagged_field")
        .collect();
    assert_eq!(flagged.len(), 1, "{flagged:?}");
    assert_eq!(
        flagged[0].address.as_deref(),
        Some(format!("{DOC}#campaign.in_market").as_str())
    );
    assert_eq!(flagged[0].decision.as_deref(), Some("d_01JA0D07REJ"));
    // The bad verdict on the objective is unanswered; rejecting a finding is
    // not a verdict to answer.
    assert!(c.contains(&("bad_verdict", Some("d_01JA0D09VRB"))), "{c:?}");
    assert!(!c.contains(&("bad_verdict", Some("d_01JA0D07REJ"))));
    // The contest's reasoning asks for a person.
    assert!(c.contains(&("flagged", Some("d_01JA0D05CTO"))));
    // Synthesis was unsure, and a person verifying one of its findings does
    // not settle the rest; selection was sure.
    assert!(c.contains(&("low_certainty", Some("d_01JA0D06SYN"))));
    assert!(!c.iter().any(|x| x.1 == Some("d_01JA0D03SEL")));

    assert!(!v.lock.can_lock);
    assert_eq!(
        v.lock.blockers,
        v.attention.iter().filter(|a| a.blocks_lock).count()
    );
    // Lock blockers come first.
    let first_free = v.attention.iter().position(|a| !a.blocks_lock).unwrap();
    assert!(v.attention[first_free..].iter().all(|a| !a.blocks_lock));

    // The block carries its reasons.
    let cto = v
        .decisions
        .iter()
        .find(|b| b.decision.id.as_deref() == Some("d_01JA0D05CTO"))
        .unwrap();
    let reasons: Vec<_> = cto.attention.iter().map(|r| r.code).collect();
    assert_eq!(reasons, ["open_contest", "flagged"]);
    assert_eq!(cto.who.kind, "agent");
    assert_eq!(cto.who.name, "Start campaign");
    assert_eq!(cto.targets[0].kind, "contest");
    assert_eq!(
        cto.targets[0].label,
        "Contest · brand/orchard-hill:product.abv"
    );
}

#[test]
fn cites_resolve_to_what_they_name() {
    let v = decisions(&example()).unwrap();
    let fact = &v.cites["f_01JA0B2K7M"];
    assert_eq!(fact.kind, "fact");
    assert_eq!(fact.label, "brand/lunasa · awareness.prompted · IE");
    let detail = fact.detail.as_deref().unwrap();
    assert!(detail.starts_with("47% · as of 2026-06-30"), "{detail}");

    let created = v
        .decisions
        .iter()
        .find(|b| b.decision.id.as_deref() == Some("d_01JA0D01CRT"))
        .unwrap();
    let labels: Vec<_> = created.targets.iter().map(|t| t.label.as_str()).collect();
    assert!(labels.contains(&"Material · Planner prompt"), "{labels:?}");
    assert!(labels.contains(&"Campaign › Ask source"), "{labels:?}");

    let finding = &v.cites["fi_01JA0F1A"];
    assert_eq!(finding.kind, "finding");
    assert!(finding.detail.as_deref().unwrap().contains("verified"));

    assert_eq!(v.cites["mat_prompt01"].kind, "material");
    assert_eq!(v.cites["mat_prompt01"].label, "Planner prompt");

    // Held in the open contest, not pinned.
    let held = &v.cites["f_01JA0B5C7N"];
    assert_eq!(held.kind, "fact");
    assert_eq!(held.label, "brand/orchard-hill:product.abv = 0.5% ABV");
    assert!(held
        .detail
        .as_deref()
        .unwrap()
        .contains("brands_positioning/GB"));

    let source = &v.cites["src_tr4d"];
    assert_eq!(source.kind, "source");
    assert!(source
        .detail
        .as_deref()
        .unwrap()
        .contains("knowledge layer"));

    let address = &v.cites[&format!("{DOC}#selection.contested[ct_orchard_hill_abv]")];
    assert_eq!(address.kind, "address");
    assert_eq!(address.label, "Contest · brand/orchard-hill:product.abv");
}

const CHAIN: &str = "decisions:
- id: d_3
  kind: edit
  agent: human
  actor: human:u_a
  action: patch-data
  targets: ['7c1e9a42-5b3d-4f8e-9a6c-2d1f0e8b4a17#campaign.name']
  rationale: Renamed.
  timestamp: 2026-09-24T12:00:00Z
- id: d_2
  kind: edit
  agent: extract_ask@1
  actor: process:middleware
  handler: extract_ask@1
  action: extract_ask
  targets: ['7c1e9a42-5b3d-4f8e-9a6c-2d1f0e8b4a17#campaign.budget_band']
  rationale: Read the band.
  reasoning:
    decided: Read the band as 50k to 250k.
    because:
    - point: the email gives a range
      cites: [mat_email]
    only_option: one range is stated
    certainty: { level: low, why: the range straddles two bands }
    would_change_if: the client names a figure
  timestamp: 2026-09-24T11:00:00Z
- id: d_1
  kind: edit
  agent: extract_ask@1
  actor: process:middleware
  handler: extract_ask@1
  action: extract_ask
  targets: ['7c1e9a42-5b3d-4f8e-9a6c-2d1f0e8b4a17#campaign.name']
  rationale: Named it.
  reasoning:
    decided: Named it Spring.
    because:
    - point: the email says spring
    only_option: one name is given
    certainty: { level: low, why: the email names it only in passing }
    would_change_if: the client names it
    attention: the name is a guess
  timestamp: 2026-09-24T10:00:00Z
- agent: human
  action: create
  rationale: Opened.
  timestamp: 2026-09-24T09:00:00Z
";

#[test]
fn a_person_on_one_of_several_targets_does_not_clear_it_but_one_on_the_decision_does() {
    let chain = |later: &str| {
        format!(
            "decisions:
{later}- id: d_x
  kind: finding
  agent: synthesise_findings@1
  action: synthesise
  targets: ['{DOC}#findings[fi_a]', '{DOC}#findings[fi_b]']
  rationale: Two findings.
  reasoning:
    decided: Proposed two findings.
    because: [{{ point: the pins agree }}]
    only_option: nothing else was asked
    certainty: {{ level: low, why: fi_b rests on one pin }}
    would_change_if: a second source
  timestamp: 2026-09-24T10:00:00Z
"
        )
    };
    let one = format!(
        "- id: d_v
  kind: verify
  agent: human
  actor: human:u_a
  action: verify
  targets: ['{DOC}#findings[fi_a]']
  rationale: Checked.
  timestamp: 2026-09-24T11:00:00Z
"
    );
    let v = decisions(&doc_with(&[(CHAIN_PATH, &chain(&one))])).unwrap();
    assert_eq!(codes(&v), [("low_certainty", Some("d_x"))]);

    let on_it = format!(
        "- id: d_g
  kind: verdict
  agent: human
  actor: human:u_a
  polarity: good
  action: verdict
  targets: ['{DOC}#decisions[d_x]']
  rationale: Both hold.
  timestamp: 2026-09-24T11:00:00Z
"
    );
    let v = decisions(&doc_with(&[(CHAIN_PATH, &chain(&on_it))])).unwrap();
    assert!(v.attention.is_empty(), "{:?}", v.attention);
}

#[test]
fn low_certainty_and_asked_for_attention_clear_once_a_person_decides() {
    let v = decisions(&doc_with(&[(CHAIN_PATH, CHAIN)])).unwrap();
    // d_1 was answered by the person's rename (d_3); d_2 was not.
    assert_eq!(codes(&v), [("low_certainty", Some("d_2"))]);
    assert_eq!(
        v.attention[0].text,
        "Low certainty: the range straddles two bands"
    );
    assert!(!v.attention[0].blocks_lock);
    assert!(v.lock.can_lock);

    let order: Vec<_> = v
        .decisions
        .iter()
        .map(|b| b.decision.id.as_deref().unwrap_or("-"))
        .collect();
    assert_eq!(order, ["d_3", "d_2", "d_1", "-"]);
    assert_eq!(
        v.decisions[0].who,
        Who {
            kind: "person",
            id: "u_a".into(),
            name: "u_a".into()
        }
    );
    assert_eq!(v.decisions[1].who.name, "Extract ask");
    assert_eq!(v.decisions[1].targets[0].label, "Campaign › Budget band");
    // A legacy entry with no actor still reads as a person.
    assert_eq!(v.decisions[3].who.kind, "person");
    assert_eq!(v.cites["mat_email"].kind, "unknown");
}

#[test]
fn a_contest_is_open_until_a_resolve_names_it_and_merges_and_branches_block() {
    let chain = "decisions:
- id: d_c
  kind: contest
  agent: host
  action: contested a stale write
  targets: ['7c1e9a42-5b3d-4f8e-9a6c-2d1f0e8b4a17#campaign.name']
  rationale: Held.
  status: open
  timestamp: 2026-09-24T10:00:00Z
- id: d_u
  kind: contest
  agent: host
  action: contest
  targets: ['upstream-doc#campaign.problem']
  rationale: Carried.
  timestamp: 2026-09-24T09:00:00Z
";
    let report = "generated_by: test
conflicts:
- key: campaign.objective
  winner: { value: a, agent: drafter }
  losers: [{ value: b, agent: judge }]
unresolved: 1
";
    let doc = doc_with(&[
        (CHAIN_PATH, chain),
        (MERGE_REPORT_PATH, report),
        ("agents/u.drafter.t/data.yaml", "{}"),
    ]);
    let v = decisions(&doc).unwrap();
    assert_eq!(
        codes(&v),
        [
            ("open_contest", Some("d_c")),
            ("open_contest", Some("d_u")),
            ("open_contest", None),
            ("unmerged_branch", None),
        ]
    );
    assert!(v.attention[1].text.contains("carried from upstream"));
    assert!(v.attention[2].text.contains("between drafter and judge"));
    assert_eq!(v.lock.blockers, 4);

    // A later resolve settles it.
    let resolved = format!(
        "decisions:
- id: d_r
  kind: resolve
  agent: human
  actor: human:u_a
  action: resolve
  targets: ['{DOC}#campaign.name']
  rationale: Kept ours.
  timestamp: 2026-09-24T11:00:00Z
{}",
        chain.trim_start_matches("decisions:\n")
    );
    let v = decisions(&doc_with(&[(CHAIN_PATH, &resolved)])).unwrap();
    assert_eq!(codes(&v), [("open_contest", Some("d_u"))]);
}

#[test]
fn a_bad_verdict_is_answered_by_a_later_edit_or_a_reasoned_good_one() {
    let chain = |later: &str| {
        format!(
            "decisions:
{later}- id: d_v
  kind: verdict
  agent: human
  actor: human:u_c
  action: verdict
  polarity: bad
  targets: ['{DOC}#campaign.objective']
  rationale: Two objectives.
  timestamp: 2026-09-24T10:00:00Z
"
        )
    };
    let open = decisions(&doc_with(&[(CHAIN_PATH, &chain(""))])).unwrap();
    assert_eq!(codes(&open), [("bad_verdict", Some("d_v"))]);
    assert!(open.attention[0]
        .text
        .starts_with("Campaign › Objective was marked bad by u_c"));

    let edit = format!(
        "- id: d_e
  kind: edit
  agent: human
  action: patch-data
  targets: ['{DOC}#campaign.objective']
  rationale: One objective.
  timestamp: 2026-09-24T11:00:00Z
"
    );
    let v = decisions(&doc_with(&[(CHAIN_PATH, &chain(&edit))])).unwrap();
    assert!(v.attention.is_empty(), "{:?}", v.attention);

    // A patch-data names only the top-level key it wrote; the view says which
    // field in the rationale.
    let patched = |field: &str| {
        format!(
            "- id: d_p
  kind: edit
  agent: human
  actor: human:u_a
  action: confirm
  fields_changed: [campaign]
  rationale: 'd_p · edit · {DOC}#campaign.{field} · confirmed from extracted'
  timestamp: 2026-09-24T11:00:00Z
"
        )
    };
    let v = decisions(&doc_with(&[(CHAIN_PATH, &chain(&patched("objective")))])).unwrap();
    assert!(v.attention.is_empty(), "{:?}", v.attention);
    let v = decisions(&doc_with(&[(CHAIN_PATH, &chain(&patched("objective_2")))])).unwrap();
    assert_eq!(codes(&v), [("bad_verdict", Some("d_v"))]);

    let unreasoned = format!(
        "- id: d_g
  kind: verdict
  agent: human
  polarity: good
  action: verdict
  targets: ['{DOC}#campaign.objective']
  rationale: ''
  timestamp: 2026-09-24T11:00:00Z
"
    );
    let v = decisions(&doc_with(&[(CHAIN_PATH, &chain(&unreasoned))])).unwrap();
    assert_eq!(codes(&v), [("bad_verdict", Some("d_v"))]);
}

#[test]
fn no_chain_is_an_empty_view_and_a_broken_one_says_so() {
    let v = decisions(&doc_with(&[])).unwrap();
    assert!(v.problem.is_none());
    assert!(v.lock.can_lock);

    let v = decisions(&doc_with(&[(CHAIN_PATH, "decisions: [oops")])).unwrap();
    assert!(v.problem.as_deref().unwrap().contains("does not read"));
    assert!(v.decisions.is_empty());
}

#[test]
fn paths_split_into_names_and_keys() {
    assert_eq!(
        segments("a.b[k].c[x][y]"),
        [
            Seg::Name("a"),
            Seg::Name("b"),
            Seg::Key("k"),
            Seg::Name("c"),
            Seg::Key("x"),
            Seg::Key("y"),
        ]
    );
    assert!(segments("").is_empty());
    assert_eq!(humanise("in_market"), "In market");
}
