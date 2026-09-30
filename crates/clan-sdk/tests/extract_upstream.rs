// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! The extract's `upstream` printer (clan-extract.md §1.1, §10.2, §11.3):
//! the example research, spun into a brief, printed as Sai's engine takes
//! it — against a golden file, byte for byte, with `model: false` values
//! replaced wherever they appear, and every row in the shape the engine's
//! validators read.
//!
//! Regenerate the golden after an intended change with
//! `UPDATE_GOLDEN=1 cargo test -p clan-sdk --test extract_upstream`, and
//! read the diff: it is what the engine will be given.

use std::collections::BTreeSet;

use clan_sdk::extract::{
    self, Context, Person, CATEGORY_MAP, DECISION_KINDS, DECISION_ROW_FIELDS, ENGINE_CATEGORIES,
    FACT_ROW_FIELDS, MARKED, MAX_DECISIONS,
};
use clan_sdk::{
    create, make_template, spinoff, AppInfo, ClanBuilder, ClanFile, CreateOptions, Decision,
    DecisionChain, FileEntry, MakeTemplateOptions, SpinoffOptions, SpinoffSpec, MANIFEST_PATH,
};
use serde_json::Value;

const DATA: &str = "shared/data.yaml";
const CHAIN: &str = "agent/decision-chain.yaml";
const FACTS: &str = "shared/facts.yaml";
const FINDINGS: &str = "shared/findings.yaml";
const SOURCES: &str = "shared/sources.yaml";

const RESEARCH_APP: &str = "ie.napkin.campaign-research";
const BRIEF_APP: &str = "ie.napkin.brief-maker";
/// The document id the example's addresses are written against.
const R: &str = "7c1e9a42-5b3d-4f8e-9a6c-2d1f0e8b4a17";

const EXAMPLE: &str = "../../app/templates/campaign-research/example";
const GOLDEN: &str = "tests/fixtures/upstream-example-brief.json";

/// Two of the sources the example's pins cite, as a sources member holds
/// them; the rest are cited by id and read as `{id}` alone.
const SOURCES_YAML: &str = "\
sources:
  - id: src_r0st
    uri: https://example.com/roster
    title: Brand roster
    tier: primary
  - id: src_tr4d
    uri: https://example.com/trade
    title: Trade press
    tier: secondary
";

fn example(path: &str) -> Vec<u8> {
    let dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join(EXAMPLE);
    std::fs::read(dir.join(path)).unwrap()
}

fn open(bytes: Vec<u8>) -> ClanFile {
    ClanFile::from_bytes(bytes).unwrap()
}

fn app(name: &str, app_id: &str, spinoff: Option<SpinoffSpec>) -> AppInfo {
    AppInfo {
        name: name.into(),
        app_id: app_id.into(),
        version: "0.6.0".into(),
        icon: None,
        entry: "human/index.html".into(),
        schema: Some("agent/output-schema.json".into()),
        prompt_templates: vec![],
        data_seed: None,
        spinoff,
    }
}

fn entry(path: &str, role: &str) -> FileEntry {
    FileEntry {
        id: role.into(),
        path: path.into(),
        role: role.into(),
        content_type: "application/yaml".into(),
        priority: None,
        sha256: None,
    }
}

/// `clan` rebuilt with `add` written and registered, and its manifest
/// passed through `edit`.
fn rebuild(
    clan: &ClanFile,
    edit: impl FnOnce(&mut clan_sdk::Manifest),
    add: &[(&str, Vec<u8>, Option<FileEntry>)],
) -> ClanFile {
    let mut manifest = clan.manifest().clone();
    edit(&mut manifest);
    for (_, _, reg) in add {
        if let Some(reg) = reg {
            manifest.files.retain(|f| f.path != reg.path);
            manifest.files.push(reg.clone());
        }
    }
    let mut b = ClanBuilder::new(manifest);
    for (p, v) in clan.read_all_entries().unwrap() {
        if p != MANIFEST_PATH && !add.iter().any(|(a, _, _)| *a == p) {
            b.add_entry(p, v);
        }
    }
    for (p, v, _) in add {
        b.add_entry(*p, v.clone());
    }
    open(b.build().unwrap())
}

/// The example research: its data, chain, pins and findings, and a sources
/// member.
fn research() -> ClanFile {
    let base = open(
        create(CreateOptions {
            title: "Lúnasa 0.0 launch".into(),
            brief: "the example research".into(),
            document_type: None,
            no_render: false,
            schema: None,
        })
        .unwrap(),
    );
    rebuild(
        &base,
        |m| {
            m.document_id = Some(R.into());
            m.app = Some(app("Campaign Research", RESEARCH_APP, None));
        },
        &[
            (DATA, example("shared/data.yaml"), None),
            (CHAIN, example("agent/decision-chain.yaml"), None),
            (
                FACTS,
                example("shared/facts.yaml"),
                Some(entry(FACTS, "pinned-facts")),
            ),
            (
                FINDINGS,
                example("shared/findings.yaml"),
                Some(entry(FINDINGS, "findings")),
            ),
            (
                SOURCES,
                SOURCES_YAML.as_bytes().to_vec(),
                Some(entry(SOURCES, "sources")),
            ),
        ],
    )
}

/// Brief Maker as a template that carries a research spin-off whole.
fn brief_maker() -> ClanFile {
    let scaffold = open(
        create(CreateOptions {
            title: "Brief Maker".into(),
            brief: "the brief template".into(),
            document_type: None,
            no_render: false,
            schema: None,
        })
        .unwrap(),
    );
    let spec = SpinoffSpec {
        accepts: vec![RESEARCH_APP.into()],
        upstream: true,
        pin_source_decisions: true,
        ..Default::default()
    };
    open(
        make_template(
            &scaffold,
            app("Brief Maker", BRIEF_APP, Some(spec)),
            MakeTemplateOptions::default(),
        )
        .unwrap(),
    )
}

fn brief() -> ClanFile {
    open(spinoff(&brief_maker(), &research(), SpinoffOptions::default()).unwrap())
}

/// The dummy account: the example's two people.
fn account() -> Context {
    let mut ctx = Context::default();
    for (id, name) in [("human:u_aoife", "Aoife"), ("human:u_ciaran", "Ciarán")] {
        ctx.people.insert(
            id.into(),
            Person {
                name: name.into(),
                role: Some("planner".into()),
                erased: false,
            },
        );
    }
    ctx
}

/// `clan` with `decisions` written on top of its chain, newest first.
fn with_decisions(clan: &ClanFile, decisions: Vec<Decision>) -> ClanFile {
    let mut chain = DecisionChain::from_yaml(&clan.read_entry(CHAIN).unwrap()).unwrap();
    for d in decisions.into_iter().rev() {
        chain.prepend(d);
    }
    rebuild(clan, |_| {}, &[(CHAIN, chain.to_yaml().unwrap(), None)])
}

fn person(id: &str, kind: &str, action: &str, targets: &[&str], rationale: &str) -> Decision {
    let mut d = Decision::new("human", action, rationale, "2026-09-25T10:00:00Z");
    d.id = Some(id.into());
    d.kind = Some(kind.into());
    d.actor = Some("human:u_aoife".into());
    d.targets = targets.iter().map(|t| t.to_string()).collect();
    d
}

fn not_for_agents(id: &str, target: &str) -> Decision {
    let mut d = person(id, "classify", "classify", &[target], "Not for any model.");
    d.licence = Some(clan_sdk::decision::Licence {
        model: Some(false),
        export: Some(false),
        corpus: Some(false),
        ..Default::default()
    });
    d
}

fn pretty(v: &Value) -> String {
    serde_json::to_string_pretty(v).unwrap() + "\n"
}

#[test]
fn the_example_brief_prints_the_golden_upstream() {
    let b = brief();
    assert_eq!(extract::carried_research(&b).as_deref(), Some(R));
    let up = extract::upstream(&b, &account()).expect("the brief carries research");
    assert_eq!(up.research, R);
    // The example's categories (drinks.*) are not taxonomy codes: no
    // category is sent, and that is the one thing named.
    assert_eq!(up.skipped.len(), 1, "{:?}", up.skipped);
    assert_eq!(up.skipped[0].what, "campaign.categories");
    let got = pretty(&up.payload);

    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join(GOLDEN);
    if std::env::var_os("UPDATE_GOLDEN").is_some() {
        std::fs::write(&path, &got).unwrap();
    }
    let want = std::fs::read_to_string(&path).expect("the golden file; UPDATE_GOLDEN=1 writes it");
    assert_eq!(
        got, want,
        "the upstream printer's output changed; read the diff, then UPDATE_GOLDEN=1"
    );
}

#[test]
fn the_output_is_a_function_of_the_bytes_and_the_account() {
    // Two spin-offs of the same research differ in their ids, stamps and
    // bytes; what the engine is given does not.
    let (a, b) = (brief(), brief());
    assert_ne!(a.manifest().id, b.manifest().id);
    let ctx = account();
    let one = extract::upstream(&a, &ctx).unwrap().payload;
    for _ in 0..3 {
        assert_eq!(extract::upstream(&b, &ctx).unwrap().payload, one);
    }
    assert_eq!(
        serde_json::to_vec(&one).unwrap(),
        serde_json::to_vec(&extract::upstream(&a, &ctx).unwrap().payload).unwrap()
    );

    // Only the names change with the account.
    let other = extract::upstream(&a, &Context::default()).unwrap().payload;
    let who: BTreeSet<&str> = other["decisions"]
        .as_array()
        .unwrap()
        .iter()
        .map(|d| d["who"].as_str().unwrap())
        .collect();
    assert!(
        who.iter()
            .all(|w| !w.contains("u_aoife") && !w.contains("human:")),
        "{who:?}"
    );
    assert!(who.contains("A person on the team"));
    assert_eq!(other["facts"], one["facts"]);
}

#[test]
fn a_document_without_carried_research_prints_nothing() {
    assert!(extract::upstream(&research(), &account()).is_none());
    assert!(extract::upstream(&brief_maker(), &account()).is_none());
}

#[test]
fn the_facets_are_the_engines() {
    let up = extract::upstream(&brief(), &account()).unwrap().payload;
    assert_eq!(up["brand"], "Lúnasa");
    assert_eq!(
        up["competitors"],
        serde_json::json!(["Brightwater 0.0", "Orchard Hill Zero", "Kestrel Press"])
    );
    // drinks.no_low_alcohol is not a taxonomy code, so it maps to none of
    // the engine's categories: left out, and jev chooses one.
    assert!(up.get("category").is_none());
    for c in ENGINE_CATEGORIES {
        assert_ne!(*c, "other");
    }
}

#[test]
fn model_false_values_read_marked_wherever_they_appear() {
    // The IE moderating share (0.31), and the brand's name, marked not for
    // agents in the brief, on the research's addresses.
    let b = with_decisions(
        &brief(),
        vec![
            not_for_agents("d_MARKFACT", &format!("{R}#facts[f_01JA0B6D6G]")),
            not_for_agents("d_MARKBRAND", &format!("{R}#campaign.brand")),
            not_for_agents("d_MARKFIND", &format!("{R}#findings[fi_01JA0F5E]")),
        ],
    );
    let up = extract::upstream(&b, &account()).unwrap().payload;
    let text = serde_json::to_string(&up).unwrap();

    let fact = up["facts"]
        .as_array()
        .unwrap()
        .iter()
        .find(|f| f["id"] == "f_01JA0B6D6G")
        .expect("the row stays; only its value is hidden");
    assert_eq!(fact["value"], MARKED);
    assert_eq!(fact["key"], "consumer.adults_moderating");
    assert!(!text.contains("0.31") && !text.contains("31%"), "{text}");

    // The brand is not a keyword, and its name is replaced in every row.
    assert!(up.get("brand").is_none());
    assert!(!text.contains("Lúnasa"), "{text}");
    assert!(text.contains(MARKED));

    // A hidden finding's statement is not quoted where it was rejected; the
    // person's words stay, and so does the decision.
    let rej = up["decisions"]
        .as_array()
        .unwrap()
        .iter()
        .find(|d| d["id"] == "d_01JA0D07REJ")
        .unwrap();
    assert!(rej["statement"].as_str().unwrap().contains(MARKED));
    assert!(!text.contains("Orchard Hill's March launch"));
    assert!(rej["reason"]
        .as_str()
        .unwrap()
        .starts_with("A launch date is not evidence"));
}

#[test]
fn a_corpus_mark_does_not_hide_anything_here() {
    // The example marks the budget band {model: true, corpus: false}: the
    // agent version shows it (OD1). Nothing in the payload is replaced.
    let up = extract::upstream(&brief(), &account()).unwrap().payload;
    assert!(!serde_json::to_string(&up).unwrap().contains(MARKED));
}

#[test]
fn a_mark_on_the_whole_research_leaves_it_out_and_says_why() {
    let b = with_decisions(&brief(), vec![not_for_agents("d_MARKALL", R)]);
    let up = extract::upstream(&b, &account()).unwrap();
    assert_eq!(up.payload["facts"], serde_json::json!([]));
    assert_eq!(up.payload["decisions"], serde_json::json!([]));
    assert!(up.payload.get("brand").is_none());
    assert_eq!(up.skipped.len(), 1);
    assert_eq!(up.skipped[0].what, R);
}

#[test]
fn a_brief_that_settles_what_it_carried_says_so() {
    // The brief resolves the carried open contest, and dismisses the flag on
    // the in-market window with a reason.
    let mut resolve = person(
        "d_BRIEFRES",
        "resolve",
        "resolve_contest",
        &[&format!("{R}#selection.contested[ct_orchard_hill_abv]")],
        "The label on the shelf reads 0.0%.",
    );
    resolve.cites = vec!["f_01JA0B5C6M".into(), "d_01JA0D05CTO".into()];
    // The host's set-aside (Contract 4 §7.2.1): a `verdict`, action
    // `set_aside`, no polarity. It must never read "marked … as right".
    let dismiss = person(
        "d_BRIEFDIS",
        "verdict",
        "set_aside",
        &[&format!("{R}#campaign.in_market")],
        "The window is the client's call; the brief leaves it open.",
    );
    let branch = person(
        "d_BRIEFBR",
        "verdict",
        "set_aside",
        &[&format!("{R}#agents[br_ellis.2]")],
        "That branch explored a market the brief does not run in.",
    );
    let b = with_decisions(&brief(), vec![dismiss, branch, resolve]);
    let up = extract::upstream(&b, &account()).unwrap().payload;
    let rows = up["decisions"].as_array().unwrap();
    let by_id = |id: &str| rows.iter().find(|d| d["id"] == id);

    let res = by_id("d_BRIEFRES").expect("the brief's resolve is a row");
    assert_eq!(res["kind"], "resolved_contest");
    assert_eq!(res["reason"], "The label on the shelf reads 0.0%.");
    assert!(res["statement"]
        .as_str()
        .unwrap()
        .contains("use 0 percent_abv (fact f_01JA0B5C6M)"));
    assert!(
        rows.iter().all(|d| d["kind"] != "open_contest"),
        "a contest the brief resolved is no longer open"
    );
    let dis = by_id("d_BRIEFDIS").expect("the set-aside is a row");
    assert_eq!(dis["kind"], "edit");
    assert_eq!(
        dis["statement"],
        "set In market aside as something this document cannot settle"
    );
    assert_eq!(
        dis["reason"],
        "The window is the client's call; the brief leaves it open."
    );
    let br = by_id("d_BRIEFBR").expect("a branch's set-aside is a row");
    assert_eq!(
        br["statement"],
        "set the agent branch br_ellis.2 aside as something this document cannot settle"
    );
}

/// The engine's own validators, read from its source when a checkout of
/// `task/jev-human-context` is at hand (`JEV_ENGINE_DIR`, else the sibling
/// worktree); the constants this crate keeps are checked against them.
fn engine_source(file: &str) -> Option<String> {
    let dir = std::env::var_os("JEV_ENGINE_DIR")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|| {
            std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../jev-human/engine")
        });
    std::fs::read_to_string(dir.join(file)).ok()
}

/// The quoted names in the first `{…}` or `(…)` after `prefix` in `src`.
fn python_set(src: &str, prefix: &str) -> Vec<String> {
    let at = src
        .find(prefix)
        .unwrap_or_else(|| panic!("{prefix:?} not in the engine"));
    let rest = &src[at..];
    let end = rest.find(['}', ')']).unwrap();
    rest[..end]
        .split('"')
        .skip(1)
        .step_by(2)
        .map(String::from)
        .collect()
}

#[test]
fn every_row_is_one_the_engine_keeps() {
    let decisions_py = engine_source("research_decisions.py");
    let facts_py = engine_source("research_facts.py");
    if let Some(src) = &decisions_py {
        let kinds: BTreeSet<String> = python_set(src, "KINDS = ").into_iter().collect();
        let ours: BTreeSet<String> = DECISION_KINDS.iter().map(|k| k.to_string()).collect();
        assert_eq!(kinds, ours, "the engine's KINDS moved");
        assert_eq!(
            python_set(src, "ROLES = ")
                .into_iter()
                .collect::<BTreeSet<_>>(),
            ["client", "person"].map(String::from).into()
        );
        let required = python_set(src, "for k in (");
        assert_eq!(required, ["id", "who", "about", "statement"]);
        assert!(src.contains(&format!("MAX_DECISIONS = {MAX_DECISIONS}")));
    } else {
        eprintln!(
            "note: the engine's source is not at hand; checking against the kept constants only"
        );
    }
    if let Some(src) = &facts_py {
        for field in [
            "id",
            "value",
            "status",
            "superseded_by",
            "entity",
            "key",
            "unit",
            "sources",
            "as_of",
            "version",
            "scope",
            "title",
            "uri",
        ] {
            let read = src.contains(&format!("\"{field}\"")) || src.contains(&format!("'{field}'"));
            assert!(read, "research_facts no longer reads {field}");
        }
    }

    let b = with_decisions(
        &brief(),
        vec![not_for_agents(
            "d_MARKFACT",
            &format!("{R}#facts[f_01JA0B6D6G]"),
        )],
    );
    let up = extract::upstream(&b, &account()).unwrap().payload;
    let date = |s: &str| s.len() == 10 && s.as_bytes()[4] == b'-' && s.as_bytes()[7] == b'-';

    let facts = up["facts"].as_array().unwrap();
    assert!(!facts.is_empty());
    let mut ids = BTreeSet::new();
    for f in facts {
        for k in f.as_object().unwrap().keys() {
            assert!(FACT_ROW_FIELDS.contains(&k.as_str()), "fact row field {k}");
        }
        // research_facts.current: an id and a value, a status it reads.
        assert!(f["id"].as_str().is_some_and(|i| !i.is_empty()));
        assert!(f["value"].as_str().is_some_and(|v| !v.is_empty()), "{f}");
        assert!(
            ids.insert(f["id"].as_str().unwrap()),
            "a duplicate id is skipped by the engine"
        );
        assert!(f.get("version").map_or(true, Value::is_u64));
        for s in f
            .get("sources")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
        {
            assert!(s["id"].as_str().is_some_and(|i| i.starts_with("src_")));
        }
    }

    let decisions = up["decisions"].as_array().unwrap();
    assert!(!decisions.is_empty() && decisions.len() <= MAX_DECISIONS);
    let mut ids = BTreeSet::new();
    for d in decisions {
        let o = d.as_object().unwrap();
        for k in o.keys() {
            assert!(
                DECISION_ROW_FIELDS.contains(&k.as_str()),
                "decision row field {k}"
            );
        }
        for k in ["id", "who", "about", "statement"] {
            assert!(
                d[k].as_str().is_some_and(|v| !v.trim().is_empty()),
                "{k} in {d}"
            );
        }
        assert!(DECISION_KINDS.contains(&d["kind"].as_str().unwrap()), "{d}");
        assert!(["person", "client"].contains(&d["role"].as_str().unwrap()));
        assert!(d["reason"].is_null() || d["reason"].is_string());
        assert!(
            d["as_of"].is_null() || d["as_of"].as_str().is_some_and(date),
            "{d}"
        );
        assert_eq!(d["status"], "current");
        assert!(ids.insert(d["id"].as_str().unwrap()));
    }
    let kinds: BTreeSet<&str> = decisions
        .iter()
        .map(|d| d["kind"].as_str().unwrap())
        .collect();
    for k in [
        "rejected_finding",
        "verified_finding",
        "resolved_contest",
        "open_contest",
        "edit",
        "verdict",
    ] {
        assert!(kinds.contains(k), "the example has a {k}");
    }
}

#[test]
fn a_clients_answer_to_the_research_is_in_the_clients_words() {
    let mut answer = person(
        "d_CLIENTA",
        "client_review",
        "client_answer",
        &[R],
        "recorded by human:u_aoife",
    );
    answer
        .extra
        .insert("answer".into(), "accepted_with_changes".into());
    answer.extra.insert(
        "reasons".into(),
        serde_yaml::from_str("[wrong_audience]").unwrap(),
    );
    answer.extra.insert(
        "said".into(),
        "Good work.\nThe audience is  too narrow for GB.".into(),
    );
    answer.extra.insert(
        "client".into(),
        serde_yaml::from_str("{name: Niamh Kelly, email: n@example.com}").unwrap(),
    );
    let mut part = person(
        "d_CLIENTP",
        "client_review",
        "client_answer_part",
        &[&format!("{R}#campaign.audience")],
        "Aoife marked that Niamh rejected Audience.",
    );
    part.extra.insert("answer".into(), "rejected".into());
    part.extra.insert("label".into(), "Audience".into());
    part.extra
        .insert("said".into(), "Too narrow for GB.".into());
    part.extra.insert(
        "client".into(),
        serde_yaml::from_str("{name: Niamh Kelly}").unwrap(),
    );
    let mut suggestion = person(
        "d_SUGGEST",
        "client_review",
        "suggest_part",
        &[&format!("{R}#campaign.objective")],
        "Ellis matched it.",
    );
    suggestion.actor = Some("process:client-parts-match".into());

    let research = with_decisions(&research(), vec![suggestion, part, answer]);
    let b = open(spinoff(&brief_maker(), &research, SpinoffOptions::default()).unwrap());
    let up = extract::upstream(&b, &account()).unwrap().payload;
    let rows: Vec<&Value> = up["decisions"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|d| d["kind"] == "client_review")
        .collect();
    assert_eq!(rows.len(), 2, "Ellis's suggestion is not a row");
    assert_eq!(rows[0]["who"], "Niamh Kelly");
    assert_eq!(rows[0]["role"], "client");
    assert_eq!(
        rows[0]["statement"],
        "accepted the research with changes (the wrong audience)"
    );
    assert_eq!(
        rows[0]["reason"], "Good work.\nThe audience is  too narrow for GB.",
        "verbatim"
    );
    assert_eq!(rows[1]["statement"], "rejected Audience");
    assert_eq!(rows[1]["about"], "Audience");
    assert!(
        !serde_json::to_string(&up)
            .unwrap()
            .contains("n@example.com"),
        "never the email"
    );
}

#[test]
fn over_the_cap_the_changes_go_first_and_are_named() {
    let edits: Vec<Decision> = (0..45)
        .map(|i| {
            person(
                &format!("d_EDIT{i:02}"),
                "edit",
                "patch-data",
                &[&format!("{R}#campaign.deliverables")],
                &format!("Change number {i}, for a reason."),
            )
        })
        .collect();
    let research = with_decisions(&research(), edits);
    let b = open(spinoff(&brief_maker(), &research, SpinoffOptions::default()).unwrap());
    let up = extract::upstream(&b, &account()).unwrap();
    let rows = up.payload["decisions"].as_array().unwrap();
    assert_eq!(rows.len(), MAX_DECISIONS);
    for id in [
        "d_01JA0D07REJ",
        "d_01JA0D09EXC",
        "d_01JA0D09VRB",
        "d_01JA0D06RES",
        "d_01JA0D05CTO",
    ] {
        assert!(rows.iter().any(|d| d["id"] == id), "{id} is never cut");
    }
    let cut: Vec<_> = up
        .skipped
        .iter()
        .filter(|s| s.what != "campaign.categories")
        .collect();
    assert!(cut.iter().all(|s| s.why.contains("cap")));
    assert_eq!(cut.len(), 9 + 45 - MAX_DECISIONS);
}

/// `clan` with its `shared/data.yaml` passed through `edit`.
fn with_data(clan: &ClanFile, edit: impl FnOnce(&mut serde_yaml::Value)) -> ClanFile {
    let mut data: serde_yaml::Value =
        serde_yaml::from_slice(&clan.read_entry(DATA).unwrap()).unwrap();
    edit(&mut data);
    rebuild(
        clan,
        |_| {},
        &[(
            DATA,
            serde_yaml::to_string(&data).unwrap().into_bytes(),
            None,
        )],
    )
}

/// `clan` with the sources member's `src_r0st` titled `title`.
fn with_roster_title(clan: &ClanFile, title: &str) -> ClanFile {
    let yaml = SOURCES_YAML.replace("title: Brand roster", &format!("title: {title}"));
    rebuild(
        clan,
        |_| {},
        &[(SOURCES, yaml.into_bytes(), Some(entry(SOURCES, "sources")))],
    )
}

#[test]
fn a_hidden_value_in_a_source_title_or_a_name_is_replaced_or_left_out() {
    let b = with_roster_title(&brief(), "Lúnasa brand roster");
    let open_up = extract::upstream(&b, &account()).unwrap().payload;
    assert!(
        serde_json::to_string(&open_up)
            .unwrap()
            .contains("Lúnasa brand roster"),
        "unmarked, the title is sent as it is"
    );
    let b = with_decisions(
        &b,
        vec![not_for_agents(
            "d_MARKBRAND",
            &format!("{R}#campaign.brand"),
        )],
    );
    let up = extract::upstream(&b, &account()).unwrap().payload;
    let text = serde_json::to_string(&up).unwrap();
    assert!(!text.contains("Lúnasa"), "{text}");
    let titles: Vec<&str> = up["facts"]
        .as_array()
        .unwrap()
        .iter()
        .flat_map(|f| f["sources"].as_array().into_iter().flatten())
        .filter_map(|s| s["title"].as_str())
        .collect();
    assert!(
        titles.contains(&"[Marked confidential] brand roster"),
        "{titles:?}"
    );
}

#[test]
fn the_documents_own_marks_are_matched_too() {
    // The brief's own client, marked not for agents on the brief's address:
    // every occurrence in the research's rows reads marked.
    let b = with_data(&brief(), |d| {
        d["client"] = "Orchard Hill".into();
    });
    let own = b.document_id().to_string();
    assert_ne!(own, R);
    let before =
        serde_json::to_string(&extract::upstream(&b, &account()).unwrap().payload).unwrap();
    assert!(before.contains("Orchard Hill"), "the example names it");
    let b = with_decisions(
        &b,
        vec![not_for_agents("d_MARKOWN", &format!("{own}#client"))],
    );
    let up = extract::upstream(&b, &account()).unwrap().payload;
    let text = serde_json::to_string(&up).unwrap();
    assert!(!text.contains("Orchard Hill"), "{text}");
    assert!(text.contains(MARKED));
    // A comparator whose name holds the hidden value is left out (a keyword
    // is exact); the others stay.
    assert_eq!(
        up["competitors"],
        serde_json::json!(["Brightwater 0.0", "Kestrel Press"])
    );
    // A mark on another document's data path still governs nothing here.
    let b = with_decisions(
        &brief(),
        vec![not_for_agents(
            "d_MARKELSE",
            "some-other-doc#campaign.brand",
        )],
    );
    assert_eq!(
        extract::upstream(&b, &account()).unwrap().payload["brand"],
        "Lúnasa"
    );
}

#[test]
fn a_research_category_maps_to_the_engines() {
    // Every value is an engine category.
    for (code, cat) in CATEGORY_MAP {
        assert!(ENGINE_CATEGORIES.contains(cat), "{code} -> {cat}");
    }
    if let Some(src) = engine_source("rag/jev_checks.py") {
        let at = src
            .find("CATEGORY_DESC = {")
            .expect("CATEGORY_DESC in the engine");
        let body = &src[at..at + src[at..].find('}').unwrap()];
        let theirs: BTreeSet<String> = body
            .lines()
            .filter_map(|l| {
                l.trim()
                    .strip_prefix('"')?
                    .split_once('"')
                    .map(|(k, _)| k.to_string())
            })
            .filter(|k| k != "other")
            .collect();
        let ours: BTreeSet<String> = ENGINE_CATEGORIES.iter().map(|c| c.to_string()).collect();
        assert_eq!(theirs, ours, "the engine's CATEGORY_DESC moved");
    }
    for (code, want) in [
        ("soft_drinks.carbonates", Some("food_drink")),
        ("alcohol.cider", Some("alcohol")),
        ("alcohol.low_no", Some("alcohol")),
        ("automotive.ev_charging", Some("automotive")),
        ("food.pet_food", Some("fmcg")),
        ("public.betting_gaming", Some("gambling_betting")),
        ("energy.electricity_gas", None),
        ("drinks.cider", None),
        ("alcohol", Some("alcohol")),
        ("nonsense", None),
    ] {
        assert_eq!(extract::engine_category(code), want, "{code}");
    }
    // Every leaf of the planner taxonomy maps, or its vertical has no
    // engine category at all.
    let tax: Value = serde_json::from_slice(
        &std::fs::read(
            std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
                .join("../../docs/contracts/peripherals/taxonomy.json"),
        )
        .unwrap(),
    )
    .unwrap();
    let unmapped_verticals = ["energy", "home_property"];
    for v in tax["verticals"].as_array().unwrap() {
        let vc = v["code"].as_str().unwrap();
        for l in v["leaves"].as_array().unwrap() {
            let code = format!("{vc}.{}", l["code"].as_str().unwrap());
            let got = extract::engine_category(&code);
            assert!(
                got.is_some()
                    || unmapped_verticals.contains(&vc)
                    || code == "public.recruitment_employer",
                "{code} maps to no engine category"
            );
        }
    }

    // Through the printer: the first category that maps is sent.
    let b = with_data(&brief(), |d| {
        let up = &mut d["upstream"][R]["campaign"]["categories"]["value"];
        *up = serde_yaml::from_str("[energy.electricity_gas, alcohol.cider]").unwrap();
    });
    let up = extract::upstream(&b, &account()).unwrap();
    assert_eq!(up.payload["category"], "alcohol");
    assert!(up.skipped.iter().all(|s| s.what != "campaign.categories"));
}
