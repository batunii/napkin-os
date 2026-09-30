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
    assert_eq!(fact.label, "Awareness prompted · IE");
    assert_eq!(fact.value.as_deref(), Some("47%"));
    assert!(!fact.sources.is_empty(), "a fact names its sources");
    assert!(fact.sources.iter().all(|s| v.cites.contains_key(s)), "and each is resolved");
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
            name: "u_a".into(),
            you: false,
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
fn the_judge_passing_the_redraft_answers_its_own_bad_verdict_but_not_on_a_finding() {
    // The Judge's bad verdict carries reasoning.attention; the badge counted it
    // until a person decided, even after the Judge passed the redraft.
    let chain = |target: &str, later: &str| {
        format!(
            "decisions:
{later}- id: d_bad
  kind: verdict
  agent: draft_brief@1/judge
  actor: process:middleware
  action: judge
  polarity: bad
  targets: ['{DOC}#{target}']
  rationale: Fails why_now.
  reasoning:
    decided: The background fails the rubric (why_now).
    because: [{{ point: no catalyst }}]
    only_option: the rubric decides
    certainty: {{ level: medium, why: a model check decided }}
    would_change_if: the field is rewritten
    attention: Background fails why_now.
  timestamp: 2026-09-24T10:00:00Z
"
        )
    };
    let good = |target: &str, reasoned: bool| {
        let why = if reasoned {
            "  rationale: Passes.
  reasoning:
    decided: The background passes the rubric.
    because: [{ point: the launch is the catalyst }]
    only_option: the rubric decides
    certainty: { level: medium, why: a model check decided }
    would_change_if: the field is rewritten
"
        } else {
            "  rationale: ''
"
        };
        format!(
            "- id: d_good
  kind: verdict
  agent: regenerate_field@1/judge
  actor: process:middleware
  action: judge
  polarity: good
  targets: ['{DOC}#{target}']
{why}  timestamp: 2026-09-24T11:00:00Z
"
        )
    };

    let open = decisions(&doc_with(&[(CHAIN_PATH, &chain("background", ""))])).unwrap();
    assert_eq!(
        codes(&open),
        [("bad_verdict", Some("d_bad")), ("flagged", Some("d_bad"))]
    );

    // A later good verdict with a reason answers both the lock item and the flag.
    let passed = chain("background", &good("background", true));
    let v = decisions(&doc_with(&[(CHAIN_PATH, &passed)])).unwrap();
    assert!(v.attention.is_empty(), "{:?}", v.attention);

    // One without a reason answers neither.
    let bare = chain("background", &good("background", false));
    let v = decisions(&doc_with(&[(CHAIN_PATH, &bare)])).unwrap();
    assert_eq!(
        codes(&v),
        [("bad_verdict", Some("d_bad")), ("flagged", Some("d_bad"))]
    );

    // A good verdict on another field does not answer it.
    let elsewhere = chain("background", &good("audience", true));
    let v = decisions(&doc_with(&[(CHAIN_PATH, &elsewhere)])).unwrap();
    assert_eq!(
        codes(&v),
        [("bad_verdict", Some("d_bad")), ("flagged", Some("d_bad"))]
    );

    // On a finding, only a person answers it (D1): an agent's good verdict
    // leaves the flag standing.
    let finding = chain("findings[fi_a]", &good("findings[fi_a]", true));
    let v = decisions(&doc_with(&[(CHAIN_PATH, &finding)])).unwrap();
    assert_eq!(codes(&v), [("flagged", Some("d_bad"))]);
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


#[test]
fn a_source_the_document_carries_is_named_and_linked() {
    let chain = r#"decisions:
- id: d_01JB0PIN001
  kind: pin
  agent: research_lens@1.0
  action: research_merge
  rationale: pinned
  timestamp: '2026-09-28T10:00:00Z'
  cites: [f_01JB0FACT01]
"#;
    let facts = r#"facts:
- id: f_01JB0FACT01
  entity: category/food.ready_meals_frozen
  key: market.private_label_share
  market: IE
  value: 0.528
  unit: proportion
  sources: [src_shelf01]
"#;
    let sources = r#"sources:
- id: src_shelf01
  uri: https://example.com/cool-sales
  title: Cool sales, hot demand
  publisher: ShelfLife Magazine
  published_at: '2026-03-10'
  tier: secondary
"#;
    let d = doc_with(&[(CHAIN_PATH, chain), (members::FACTS_PATH, facts), (members::SOURCES_PATH, sources)]);
    let v = decisions(&d).unwrap();
    let f = &v.cites["f_01JB0FACT01"];
    assert_eq!(f.label, "Private label share · IE");
    assert_eq!(f.value.as_deref(), Some("52.8%"));
    let s = &v.cites["src_shelf01"];
    assert_eq!(s.kind, "source");
    assert_eq!(s.label, "ShelfLife Magazine — Cool sales, hot demand");
    assert_eq!(s.uri.as_deref(), Some("https://example.com/cool-sales"));
    assert_eq!(s.tier.as_deref(), Some("secondary"));
    assert_eq!(s.detail.as_deref(), Some("1 pinned fact"));
}

#[test]
fn the_viewer_sees_their_own_decisions_as_you() {
    let chain = r#"decisions:
- id: d_01JB0MINE01
  kind: verdict
  agent: human:bc0f
  actor: human:bc0f
  action: reject_finding
  polarity: bad
  rationale: one survey
  timestamp: '2026-09-28T10:00:00Z'
- id: d_01JB0THEIRS
  kind: edit
  agent: human:ana
  actor: human:ana
  action: edit
  rationale: tidy
  timestamp: '2026-09-28T09:00:00Z'
"#;
    let d = doc_with(&[(CHAIN_PATH, chain)]);
    let v = decisions_for(&d, Some("human:bc0f")).unwrap();
    assert!(v.decisions[0].who.you);
    assert_eq!(v.decisions[0].who.name, "You");
    assert!(!v.decisions[1].who.you);
    assert_eq!(v.decisions[1].who.name, "ana");
}

// ── A spun-off document: its ancestors frozen under `upstream` ──────────────

const UP: &str = "3f2a9c1e-7b4d-4e8a-9c6f-0a1b2c3d4e5f";

/// A brief's data carrying one ancestor, frozen, with an open contest and a
/// resolved one, a field citing a finding through its envelope, and the
/// brief's own passages and capture.
fn carried_data() -> String {
    format!(
        "insight: Midweek is a habit
upstream:
  {UP}:
    materials:
      mat_brief01: {{ name: Client brief, kind: client_brief, received_at: '2026-09-20' }}
    campaign:
      in_market: {{ value: Retail only, finding_ids: [fi_rej] }}
    selection:
      contested:
      - id: ct_abv
        key: brand/orchard-hill:product.abv@IE
        status: open
        opened_by: d_con
        values:
        - {{ value: 0.5, unit: percent_abv, fact_id: f_a, from: pinned }}
        - {{ value: 0.4, unit: percent_abv, fact_id: f_b, from: brands_positioning/GB }}
      - id: ct_done
        key: category/cider:market.share@IE
        status: resolved
        values: []
passages:
  psg_3b9f0c2e7a41: {{ citation: IPA Effectiveness 2024, source: IPA, pack: effectiveness, scope: house,
                      licence: open, text: Brands grow by reaching light buyers., uri: 'https://ipa.example/1',
                      retrieved_at: '2026-09-20T10:00:00Z' }}
capture:
  items:
    cap_1a2b3c4d5e6f: {{ key: business_problem, value: Midweek sales fall, status: fact,
                        quote: sales fall midweek, material_id: mat_brief01 }}
"
    )
}

const REJECTED: &str = "findings:
- id: fi_rej
  statement: Frozen is growing fast from a small share
  status: rejected
  rejection: { decision: d_rej, by: 'human:u_a', reason: value not volume }
";

#[test]
fn carried_items_are_on_the_lock_list_and_a_carried_approve_is_history() {
    let chain = format!(
        "decisions:
- id: d_up_lock
  kind: approve
  agent: human
  actor: human:u_a
  action: lock
  targets: ['{UP}']
  rationale: Accepted.
  timestamp: 2026-09-24T12:00:00Z
- id: d_rej
  kind: verdict
  agent: human
  actor: human:u_a
  action: reject_finding
  polarity: bad
  targets: ['{DOC}#findings[fi_rej]']
  rationale: value not volume
  timestamp: 2026-09-24T11:00:00Z
- id: d_con
  kind: contest
  agent: research_lens@1
  action: open_contest
  targets: ['{UP}#selection.contested[ct_abv]']
  rationale: Two values.
  timestamp: 2026-09-24T10:00:00Z
"
    );
    let report = "generated_by: test
conflicts:
- key: campaign.objective
  winner: { value: a, agent: drafter }
  losers: [{ value: b, agent: judge }]
unresolved: 1
";
    let data = carried_data();
    let merge = format!("upstream/{UP}/merge-report.yaml");
    let doc = doc_with(&[
        (CHAIN_PATH, &chain),
        ("shared/data.yaml", &data),
        (members::FINDINGS_PATH, REJECTED),
        (&merge, report),
    ]);
    let v = decisions(&doc).unwrap();
    // The carried contest once, at its upstream address and on the decision
    // that opened it; the one resolved upstream is not listed; the carried
    // merge conflict blocks. The frozen field citing the finding the brief
    // rejected is not the brief's to revise.
    assert_eq!(
        codes(&v),
        [("open_contest", Some("d_con")), ("open_contest", None)]
    );
    let contest = &v.attention[0];
    assert_eq!(
        contest.address.as_deref(),
        Some(format!("{UP}#selection.contested[ct_abv]").as_str())
    );
    assert!(contest.text.contains("carried from upstream"), "{}", contest.text);
    assert_eq!(
        contest.label.as_deref(),
        Some("Contest · brand/orchard-hill:product.abv@IE"),
        "labelled from the frozen copy"
    );
    assert!(v.attention[1].text.contains("settled there, in the parent"));

    // The parent's approve names the parent, whole; it is not a lock here.
    let lock = v
        .decisions
        .iter()
        .find(|b| b.decision.id.as_deref() == Some("d_up_lock"))
        .unwrap();
    assert_eq!(lock.targets[0].kind, "document");
    assert_eq!(lock.targets[0].address, format!("{UP}#"));
    assert!(!lock.targets[0].here);

    // A resolve in this chain settles the carried contest; the frozen entry
    // never changes.
    let resolved = format!(
        "decisions:
- id: d_res
  kind: resolve
  agent: human
  actor: human:u_a
  action: resolve_contest
  targets: ['{UP}#selection.contested[ct_abv]', '{DOC}#facts[f_b]']
  cites: [f_b, d_con]
  rationale: Picked the GB panel.
  timestamp: 2026-09-24T13:00:00Z
{}",
        chain.trim_start_matches("decisions:\n")
    );
    let doc = doc_with(&[
        (CHAIN_PATH, &resolved),
        ("shared/data.yaml", &data),
        (members::FINDINGS_PATH, REJECTED),
        (&merge, report),
    ]);
    assert_eq!(codes(&decisions(&doc).unwrap()), [("open_contest", None)]);
}

#[test]
fn passages_capture_and_frozen_addresses_resolve_as_cites() {
    let chain = format!(
        "decisions:
- id: d_draft
  kind: edit
  agent: draft_brief@1/drafter
  actor: process:middleware
  action: draft
  targets: ['{DOC}#insight']
  cites: [psg_3b9f0c2e7a41, cap_1a2b3c4d5e6f, mat_brief01, f_b, '{UP}#campaign.in_market']
  rationale: Drafted.
  timestamp: 2026-09-24T10:00:00Z
"
    );
    let data = carried_data();
    let v = decisions(&doc_with(&[(CHAIN_PATH, &chain), ("shared/data.yaml", &data)])).unwrap();

    let p = &v.cites["psg_3b9f0c2e7a41"];
    assert_eq!(p.kind, "passage");
    assert_eq!(p.label, "IPA Effectiveness 2024");
    assert_eq!(p.uri.as_deref(), Some("https://ipa.example/1"));
    assert_eq!(p.quote.as_deref(), Some("Brands grow by reaching light buyers."));
    let detail = p.detail.as_deref().unwrap();
    assert!(detail.contains("house knowledge") && detail.contains("retrieved 2026-09-20"), "{detail}");

    let c = &v.cites["cap_1a2b3c4d5e6f"];
    assert_eq!(c.kind, "capture");
    assert_eq!(c.label, "Midweek sales fall");
    assert_eq!(c.quote.as_deref(), Some("sales fall midweek"));
    assert_eq!(
        c.detail.as_deref(),
        Some("Business problem · from your material · in Client brief")
    );

    // The research's material and contest value, from its frozen copy.
    assert_eq!(v.cites["mat_brief01"].kind, "material");
    assert_eq!(v.cites["mat_brief01"].label, "Client brief");
    assert_eq!(v.cites["f_b"].label, "brand/orchard-hill:product.abv@IE = 0.4% ABV");

    let a = &v.cites[&format!("{UP}#campaign.in_market")];
    assert_eq!(a.kind, "address");
    assert_eq!(a.detail.as_deref(), Some("Retail only"));
}

#[test]
fn a_field_whose_writing_decision_cites_a_rejected_finding_is_flagged() {
    let draft = |extra: &str| {
        format!(
            "- id: d_draft
  kind: edit
  agent: draft_brief@1/drafter
  actor: process:middleware
  action: draft
  targets: ['{DOC}#insight']
  cites: [psg_3b9f0c2e7a41]
  rationale: Drafted.
  reasoning:
    decided: Drafted the insight.
    because:
    - point: frozen is growing
      cites: [fi_rej]
    only_option: one insight is drafted
    certainty: {{ level: medium, why: rests on a finding }}
    would_change_if: the finding is rejected
{extra}  timestamp: 2026-09-24T10:00:00Z
"
        )
    };
    let reject = format!(
        "- id: d_rej
  kind: verdict
  agent: human
  actor: human:u_a
  action: reject_finding
  polarity: bad
  targets: ['{DOC}#findings[fi_rej]']
  rationale: value not volume
  timestamp: 2026-09-24T11:00:00Z
"
    );
    let later = |id: &str, body: &str| {
        format!("- id: {id}\n  kind: edit\n{body}  rationale: Later.\n  timestamp: 2026-09-24T12:00:00Z\n")
    };
    let flagged = |newer: &str, extra: &str| {
        let chain = format!("decisions:\n{newer}{reject}{}", draft(extra));
        let data = carried_data();
        let v = decisions(&doc_with(&[
            (CHAIN_PATH, &chain),
            ("shared/data.yaml", &data),
            (members::FINDINGS_PATH, REJECTED),
        ]))
        .unwrap();
        v.attention
            .into_iter()
            .filter(|a| a.code == "flagged_field")
            .map(|a| (a.address.unwrap_or_default(), a.decision))
            .collect::<Vec<_>>()
    };
    let insight = format!("{DOC}#insight");

    // The drafter's decision is how the field stands, and it rests on the
    // finding a person rejected.
    assert_eq!(flagged("", ""), [(insight.clone(), Some("d_rej".to_string()))]);
    // A proposal does not change how the field stands.
    let propose = later(
        "d_prop",
        &format!("  agent: draft_brief@1/drafter\n  action: propose\n  targets: ['{DOC}#insight']\n"),
    );
    assert_eq!(flagged(&propose, "").len(), 1);
    // A redraft that no longer cites it answers it, as does a person's edit
    // of the field (a patch-data names only the key it wrote).
    let redraft = later(
        "d_regen",
        &format!("  agent: regenerate_field@1/drafter\n  action: regenerate\n  targets: ['{DOC}#insight']\n  cites: [psg_3b9f0c2e7a41]\n"),
    );
    assert!(flagged(&redraft, "").is_empty());
    let person = later(
        "d_person",
        "  agent: human\n  actor: human:u_a\n  action: patch-data\n  fields_changed: [insight]\n",
    );
    assert!(flagged(&person, "").is_empty());
    // A superseded draft is not how the field stands.
    assert!(flagged("", "  superseded_by: d_other\n").is_empty());
}
