// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! The host half of `napkin.middleware/1`: a canned middleware reply goes in
//! the way the proxy would hand it over, and the open document comes out
//! changed — once, by the host, as `process:middleware` — or untouched with a
//! reason. No network: the proxy call is the only part not exercised here.

#![recursion_limit = "256"]

use std::sync::Arc;

use clan_sdk::{ClanFile, DecisionChain};
use napkin_host::ops::members::{
    FACTS_PATH, FACTS_ROLE, FINDINGS_PATH, FINDINGS_ROLE, SOURCES_PATH, SOURCES_ROLE,
};
use napkin_host::ops::middleware;
use napkin_host::{Ctx, DocId, Document, FsStore, HostEvent, Session};
use serde_json::{json, Value};

struct Fixture {
    _dir: tempfile::TempDir,
    session: Session,
    id: DocId,
}

/// The Research Tool's pipeline declaration, as its documents carry it: the
/// app, not the host, says which edits must carry reasoning (R1).
const CAMPAIGN_PIPELINE: &str =
    include_str!("../../../app/templates/campaign-research/app/pipeline.yaml");
/// Brief Maker's.
const BRIEF_PIPELINE: &str = include_str!("../../../app/templates/brief-maker/app/pipeline.yaml");

fn fixture() -> Fixture {
    fixture_with(Some(CAMPAIGN_PIPELINE))
}

/// A fresh document carrying `pipeline` as its `app/pipeline.yaml` (none when
/// `None`).
fn fixture_with(pipeline: Option<&str>) -> Fixture {
    let dir = tempfile::tempdir().unwrap();
    let id = DocId::from(dir.path().join("campaign.clan"));
    let mut bytes = clan_sdk::create(clan_sdk::CreateOptions {
        title: "Campaign".into(),
        brief: "a campaign".into(),
        document_type: None,
        no_render: false,
        schema: None,
    })
    .unwrap();
    if let Some(p) = pipeline {
        let clan = ClanFile::from_bytes(bytes).unwrap();
        let mut b = clan_sdk::ClanBuilder::new(clan.manifest().clone());
        for (path, entry) in clan.read_all_entries().unwrap() {
            if path != clan_sdk::MANIFEST_PATH {
                b.add_entry(path, entry);
            }
        }
        b.add_entry("app/pipeline.yaml", p.as_bytes().to_vec());
        bytes = b.build().unwrap();
    }
    std::fs::write(id.as_str(), bytes).unwrap();
    let session = Session::new(Arc::new(FsStore::new(dir.path().to_path_buf())));
    session.open(id.clone()).unwrap();
    Fixture {
        _dir: dir,
        session,
        id,
    }
}

fn on_disk(f: &Fixture) -> ClanFile {
    ClanFile::open(f.id.as_str()).unwrap()
}

fn chain(clan: &ClanFile) -> DecisionChain {
    DecisionChain::from_yaml(&clan.read_entry("agent/decision-chain.yaml").unwrap()).unwrap()
}

fn yaml(clan: &ClanFile, path: &str) -> Value {
    let y: serde_yaml::Value = serde_yaml::from_slice(&clan.read_entry(path).unwrap()).unwrap();
    serde_json::to_value(y).unwrap()
}

/// Reasoning in the shape `napkin.middleware/1` requires (§3).
fn reasoning(decided: &str, cites: &[&str]) -> Value {
    json!({ "decided": decided,
            "because": [{ "point": "the cited material says so", "cites": cites }],
            "rejected": [{ "option": "leave it open", "why": "the material settles it" }],
            "certainty": { "level": "high", "why": "a verbatim quote" },
            "would_change_if": "the client says otherwise" })
}

fn fact(id: &str, value: f64, stale: bool) -> Value {
    let mut f = json!({
        "id": id, "entity": "brand/lunasa", "key": "awareness.prompted", "value": value,
        "unit": "proportion", "as_of": "2026-06-30", "retrieved_at": "2026-09-12",
        "sources": ["src_4f2a"], "confidence": "high", "licence": "client-confidential",
        "status": "active", "version": 2, "supersedes": null,
        "origin": "fact://brand/lunasa/awareness.prompted@2", "decision": "d_01JA0D02EXT",
        "pinned_at": "2026-09-21T09:00:04Z", "pin_reason": "cited in the problem",
        "layer": "brand", "market": "IE", "method": "measurement",
    });
    if stale {
        f["stale"] = json!({ "detected_at": "2026-09-22T10:00:00Z", "current_version": 3,
                             "current_fact_id": "f_01JA0B9Z9Z", "current_value": 0.52 });
    }
    f
}

/// What the middleware answers `extract_ask` with, computed for `clan` as the
/// host sent it: its `id` and `version`, and what it read of the one field it
/// patches.
fn reply_for(clan: &Value) -> Value {
    json!({
        "api": "napkin.middleware/1",
        "task": "extract_ask",
        "handler": "extract_ask@1.0.0",
        "job": { "id": "job_1", "state": "done", "progress": { "done": 1, "total": 1 },
                 "started_at": "2026-09-23T10:00:00Z", "finished_at": "2026-09-23T10:00:02Z", "error": null },
        "result": { "summary": "extracted the problem" },
        "change": {
            "doc": clan["id"],
            "base_version": clan["version"],
            "read": { "campaign.problem": clan["data"]["campaign"]["problem"] },
            "data_patch": { "campaign": { "problem": {
                "value": "people who love Lúnasa are drinking less of it midweek",
                "origin": "extracted", "gate": "brief",
                "source": { "material_id": "mat_email01", "locator": "¶2" },
                "decision": "d_01JA0D02EXT" } } },
            "facts_append": [ fact("f_01JA0B3P4Q", 0.61, false), fact("f_01JA0B3P5R", 0.44, true) ],
            "findings_append": [ {
                "id": "fi_01JA0F2B", "statement": "Lúnasa arrives with permission",
                "cites": ["f_01JA0B3P4Q", "f_01JA0B3P5R"], "method": "synthesis",
                "status": "proposed", "derived_by": "synthesise_findings@1.0",
                "confidence": "low", "derived_at": "2026-09-23T10:00:01Z", "decision": "d_01JA0D06SYN" } ],
            "decisions": [
                { "id": "d_01JA0D02EXT", "kind": "edit", "agent": "extract_ask@1.0.0",
                  "action": "extracted the ask", "rationale": "read from the client's email",
                  "targets": [format!("{}#campaign.problem", clan["id"].as_str().unwrap())],
                  "cites": ["mat_email01"], "handler": "extract_ask@1.0.0", "backend": "ignored",
                  "reasoning": reasoning("Filled the problem from the email.", &["mat_email01"]) },
                { "id": "d_01JA0D06SYN", "kind": "finding", "agent": "extract_ask@1.0.0",
                  "action": "proposed a finding", "rationale": "two pins agree",
                  "targets": [format!("{}#findings[fi_01JA0F2B]", clan["id"].as_str().unwrap())],
                  "cites": ["f_01JA0B3P4Q", "f_01JA0B3P5R"],
                  "reasoning": reasoning("Proposed a finding.", &["f_01JA0B3P4Q", "f_01JA0B3P5R"]) }
            ]
        },
        "trace": { "scope": { "org": "dev", "brand": "dev" }, "backend": "mock-backend",
                   "model": "none", "hits": [], "usage": { "input_tokens": 0, "output_tokens": 0 } }
    })
}

/// The proxy's envelope around a 200 from the middleware endpoint.
fn envelope(data: Value) -> Value {
    json!({ "ok": true, "status": 200, "endpoint": "http://middleware.test/v1/tasks",
            "data": data, "error": null })
}

fn settle(f: &Fixture, data: Value) -> (Value, Vec<HostEvent>) {
    f.session.settle_middleware(f.session.ctx(), envelope(data))
}

#[test]
fn the_agent_context_names_the_document_and_carries_the_members() {
    let f = fixture();
    let clan = f.session.clan_context_for_agent();
    let open = on_disk(&f);
    assert_eq!(clan["id"], open.document_id());
    assert_eq!(clan["revision"], open.manifest().id.as_str());
    assert_eq!(
        clan["version"],
        f.session.current_version().unwrap().as_str()
    );
    assert_eq!(clan["facts"], json!([]));
    assert_eq!(clan["findings"], json!([]));
    assert_eq!(clan["pipeline"]["pipeline"], "napkin-campaign-research");
    // What the existing agent reads is still there.
    for k in [
        "schema",
        "data",
        "decision_chain",
        "context",
        "lineage",
        "app",
    ] {
        assert!(clan.get(k).is_some(), "context lost {k}");
    }

    settle(&f, reply_for(&clan));
    let clan = f.session.clan_context_for_agent();
    assert_eq!(clan["facts"].as_array().unwrap().len(), 2);
    assert_eq!(clan["findings"][0]["id"], "fi_01JA0F2B");
    // The identity survives the write; the revision does not.
    let after = on_disk(&f);
    assert_eq!(clan["id"], after.document_id());
    assert_eq!(clan["id"], open.document_id());
    assert_eq!(clan["revision"], after.manifest().id.as_str());
    assert_ne!(clan["revision"], open.manifest().id.as_str());
}

/// `window.__CLAN__` as the view is handed it.
fn view_context(f: &Fixture) -> Value {
    let html = f.session.human_html().unwrap();
    let start = html.find("window.__CLAN__ = ").unwrap() + "window.__CLAN__ = ".len();
    let end = start + html[start..].find(";</script>").unwrap();
    serde_json::from_str(&html[start..end]).unwrap()
}

#[test]
fn the_view_is_told_the_document_id_as_well_as_the_revision() {
    let f = fixture();
    let before = view_context(&f)["manifest"].clone();
    let clan = f.session.clan_context_for_agent();
    settle(&f, reply_for(&clan));
    let after = view_context(&f)["manifest"].clone();

    // A view addresses what it writes by `document_id`; reopened after a
    // write, it must not be handed the revision in its place.
    assert_eq!(after["document_id"], on_disk(&f).document_id());
    assert_eq!(after["document_id"], before["document_id"]);
    assert_ne!(after["id"], before["id"]);
}

#[test]
fn an_uploaded_attachment_reaches_a_middleware_task_as_text() {
    let f = fixture();
    let email = "The problem, in a sentence: people are drinking less midweek.";
    f.session
        .upload_asset("client-email.txt", Some("human"), email.as_bytes().to_vec())
        .unwrap();

    // The app sends `{task, input: {attachments}}`; §1 has the host fill in
    // `text`, and an attachment without it grounds nothing.
    let mut payload = json!({ "task": "extract_ask", "input": {
        "prompt": "pull the ask out of the email",
        "attachments": [{ "name": "client-email.txt", "sha256": "sha256:00" }] } });
    f.session.attach_extracted_text(&mut payload);
    assert_eq!(payload["input"]["attachments"][0]["text"], email);

    // A picture goes as its bytes, for the middleware to transcribe (§10.1);
    // one of another type, or text, does not.
    let png = b"\x89PNG\r\n\x1a\nnot really a png".to_vec();
    f.session
        .upload_asset("mood.png", Some("human"), png.clone())
        .unwrap();
    f.session
        .upload_asset("vector.svg", Some("human"), b"<svg/>".to_vec())
        .unwrap();
    let mut payload = json!({ "task": "draft_brief", "input": { "attachments": [
        { "name": "mood.png", "sha256": "sha256:01" },
        { "name": "vector.svg", "sha256": "sha256:02" },
        { "name": "client-email.txt", "sha256": "sha256:00" }] } });
    f.session.attach_extracted_text(&mut payload);
    let atts = &payload["input"]["attachments"];
    use base64::Engine as _;
    assert_eq!(atts[0]["image"]["media_type"], "image/png");
    assert_eq!(
        atts[0]["image"]["data"],
        base64::engine::general_purpose::STANDARD.encode(&png)
    );
    assert!(atts[1].get("image").is_none(), "svg is not a picture type");
    assert!(atts[2].get("image").is_none(), "text goes as text");

    // The agent's shape is unchanged: top-level attachments, `extracted_text`.
    let mut agent = json!({ "attachments": [{ "name": "client-email.txt" }] });
    f.session.attach_extracted_text(&mut agent);
    assert_eq!(agent["attachments"][0]["extracted_text"], email);
    assert!(agent["attachments"][0].get("text").is_none());
}

#[test]
fn a_change_lands_once_as_the_middleware_with_members_and_projection() {
    let f = fixture();
    let before = on_disk(&f);
    let chain_before = chain(&before).decisions.len();
    let clan = f.session.clan_context_for_agent();

    let (out, events) = settle(&f, reply_for(&clan));
    let settled = &out["data"]["change"];
    assert_eq!(settled["applied"], true, "{out}");
    assert_eq!(settled["base_stale"], false);
    let after = on_disk(&f);
    assert_eq!(
        settled["version"],
        clan_sdk::hash::sha256_prefixed(after.raw_bytes()),
        "the reply names the version the store is now at"
    );
    // The rest of the envelope is passed through for the app to display.
    assert_eq!(out["ok"], true);
    assert_eq!(out["data"]["result"]["summary"], "extracted the problem");
    // And the document as it now stands rides beside it, for the view to swap
    // into window.__CLAN__.data.
    assert_eq!(out["clan"]["version"], settled["version"]);
    assert_eq!(out["clan"]["id"], on_disk(&f).document_id());
    assert_eq!(out["clan"]["revision"], on_disk(&f).manifest().id.as_str());
    assert_eq!(
        out["clan"]["data"],
        yaml(&on_disk(&f), "shared/data.yaml"),
        "exactly what is on disk"
    );

    // One generation: its parent is exactly what was on disk before.
    assert_eq!(
        after.manifest().lineage.as_ref().unwrap().parent_sha256,
        Some(before.sha256())
    );

    // Data patched.
    let data = yaml(&after, "shared/data.yaml");
    assert_eq!(
        data["campaign"]["problem"]["value"],
        "people who love Lúnasa are drinking less of it midweek"
    );

    // Members present, registered under their own roles, and the archive
    // still validates (every registered file there, every hash right).
    let m = after.manifest();
    assert_eq!(m.file_by_path(FACTS_PATH).unwrap().role, FACTS_ROLE);
    assert_eq!(m.file_by_path(FINDINGS_PATH).unwrap().role, FINDINGS_ROLE);
    let report = clan_sdk::validate(&after);
    assert!(report.is_valid(), "{}", report.display());
    let facts = yaml(&after, FACTS_PATH);
    assert_eq!(facts["facts"][0]["id"], "f_01JA0B3P4Q");
    assert_eq!(facts["facts"][1]["stale"]["current_version"], 3);
    let findings = yaml(&after, FINDINGS_PATH);
    assert_eq!(findings["findings"][0]["status"], "proposed");

    // Decisions appended, newest first, as process:middleware with the
    // reply's handler and backend; identity, kind, targets and cites are the
    // decision's own fields, and the rationale is the middleware's verbatim.
    let c = chain(&after);
    assert_eq!(c.decisions.len(), chain_before + 2);
    let (newest, first) = (&c.decisions[0], &c.decisions[1]);
    let doc_id = clan["id"].as_str().unwrap();
    assert_eq!(first.id.as_deref(), Some("d_01JA0D02EXT"));
    assert_eq!(first.kind.as_deref(), Some("edit"));
    assert_eq!(first.agent, "extract_ask@1.0.0");
    assert_eq!(first.claimed_agent.as_deref(), Some("extract_ask@1.0.0"));
    assert_eq!(first.actor.as_deref(), Some("process:middleware"));
    assert_eq!(first.handler.as_deref(), Some("extract_ask@1.0.0"));
    assert_eq!(
        first.backend.as_deref(),
        Some("mock-backend"),
        "the reply's backend, not the decision's"
    );
    assert!(first.scope.is_none(), "the local shell resolves no scope");
    assert_eq!(first.action, "extracted the ask");
    assert_eq!(first.rationale, "read from the client's email");
    let why = first.reasoning.as_ref().expect("the reasoning is typed");
    assert_eq!(why.decided, "Filled the problem from the email.");
    assert_eq!(why.because[0].cites, vec!["mat_email01".to_string()]);
    assert_eq!(why.certainty.level, "high");
    assert_eq!(first.targets, vec![format!("{doc_id}#campaign.problem")]);
    assert_eq!(first.cites, vec!["mat_email01".to_string()]);
    assert!(
        first.fields_changed.is_empty(),
        "targets are not changed keys"
    );
    assert!(
        first.trace_ref.is_none(),
        "identity is the id, not a trace-ref"
    );
    assert_eq!(newest.id.as_deref(), Some("d_01JA0D06SYN"));
    assert_eq!(newest.kind.as_deref(), Some("finding"));
    assert_eq!(
        newest.cites,
        vec!["f_01JA0B3P4Q".to_string(), "f_01JA0B3P5R".to_string()]
    );
    assert_eq!(
        newest.targets,
        vec![format!("{doc_id}#findings[fi_01JA0F2B]")]
    );

    // Projection rebuilt from the members, hashed over their exact bytes.
    let p = &data["projection"];
    assert_eq!(
        p["built_from"]["facts_sha256"],
        clan_sdk::hash::sha256_prefixed(&after.read_entry(FACTS_PATH).unwrap())
    );
    assert_eq!(
        p["built_from"]["findings_sha256"],
        clan_sdk::hash::sha256_prefixed(&after.read_entry(FINDINGS_PATH).unwrap())
    );
    let built_at = p["built_from"]["built_at"].as_str().unwrap();
    assert!(
        built_at.ends_with('Z') && built_at.len() == 20,
        "{built_at}"
    );
    let pin = &p["pins"]["f_01JA0B3P4Q"];
    assert_eq!(pin["value"], 0.61);
    assert_eq!(pin["market"], "IE");
    assert_eq!(pin["stale"], false);
    assert!(pin.get("current_version").is_none());
    assert!(pin.get("origin").is_none(), "only the view's keys");
    assert_eq!(pin["retrieved_at"], "2026-09-12");
    assert_eq!(pin["sources"], json!(["src_4f2a"]));
    assert_eq!(pin["version"], 2);
    assert_eq!(pin["status"], "active");
    assert_eq!(pin["pin_reason"], "cited in the problem");
    assert!(pin.get("current_value").is_none());
    let stale = &p["pins"]["f_01JA0B3P5R"];
    assert_eq!(stale["stale"], true);
    assert_eq!(stale["current_version"], 3);
    assert_eq!(stale["current_value"], 0.52);
    assert_eq!(p["findings"]["fi_01JA0F2B"]["status"], "proposed");
    assert_eq!(
        p["findings"]["fi_01JA0F2B"]["cites"],
        json!(["f_01JA0B3P4Q", "f_01JA0B3P5R"])
    );

    // The view is told the data changed.
    assert_eq!(events.len(), 1);
    assert_eq!(events[0].name(), "clan-data-changed");
    assert_eq!(events[0].payload()["source"], "middleware");
    assert_eq!(events[0].payload()["keys"], json!(["campaign"]));
}

#[test]
fn the_operation_proposes_exactly_one_change() {
    let f = fixture();
    let clan = f.session.clan_context_for_agent();
    let doc = Document::from_bytes(f.id.clone(), std::fs::read(f.id.as_str()).unwrap()).unwrap();
    let outcome = middleware::apply(&Ctx::local(), &doc, &reply_for(&clan)).unwrap();
    assert_eq!(outcome.changes.len(), 1);
    let change = &outcome.changes[0];
    assert_eq!(change.expected_version(), Some(doc.version()));
    assert_eq!(
        change.decisions.len(),
        2,
        "the decisions it appends, listed"
    );
    // Nothing was written: an operation only proposes.
    assert_eq!(std::fs::read(f.id.as_str()).unwrap(), doc.bytes());
}

#[test]
fn a_change_for_another_document_is_refused() {
    let f = fixture();
    let before = std::fs::read(f.id.as_str()).unwrap();
    let mut clan = f.session.clan_context_for_agent();
    clan["id"] = json!("00000000-0000-0000-0000-000000000000");

    let (out, events) = settle(&f, reply_for(&clan));
    assert_eq!(out["data"]["change"]["applied"], false);
    let reason = out["data"]["change"]["reason"].as_str().unwrap();
    assert!(reason.contains("00000000-0000"), "{reason}");
    assert!(events.is_empty());
    assert!(
        out.get("clan").is_none(),
        "nothing landed, nothing to refresh"
    );
    assert_eq!(
        std::fs::read(f.id.as_str()).unwrap(),
        before,
        "nothing written"
    );
}

#[test]
fn a_reply_that_is_not_the_middleware_api_is_an_error() {
    let f = fixture();
    let before = std::fs::read(f.id.as_str()).unwrap();
    let clan = f.session.clan_context_for_agent();

    // The generic agent answering in the middleware's place, with no api.
    let mut foreign = reply_for(&clan);
    foreign.as_object_mut().unwrap().remove("api");
    let (out, events) = settle(&f, foreign);
    assert_eq!(out["ok"], false);
    assert_eq!(out["data"], Value::Null, "its body is not passed on");
    assert!(out["error"]
        .as_str()
        .unwrap()
        .contains("napkin.middleware/1"));
    assert!(events.is_empty());

    // Another version of the API is not this one either.
    let mut other = reply_for(&clan);
    other["api"] = json!("napkin.middleware/2");
    let (out, _) = settle(&f, other);
    assert_eq!(out["ok"], false);

    assert_eq!(
        std::fs::read(f.id.as_str()).unwrap(),
        before,
        "nothing written"
    );
}

#[test]
fn a_reply_without_a_change_passes_through() {
    let f = fixture();
    let before = std::fs::read(f.id.as_str()).unwrap();
    let clan = f.session.clan_context_for_agent();
    let mut queued = reply_for(&clan);
    queued["job"]["state"] = json!("running");
    queued["change"] = Value::Null;
    let (out, events) = settle(&f, queued.clone());
    assert_eq!(out, envelope(queued));
    assert!(events.is_empty());
    assert_eq!(std::fs::read(f.id.as_str()).unwrap(), before);
}

const HUMAN_PROBLEM: &str = r#"{"patch":{"campaign":{"problem":{"value":"midweek is not the problem","origin":"stated","gate":"brief","by":"human:local","decision":"d_01JA0D09HUM"}}},"agent":"human","action":"set problem"}"#;
const HUMAN_OBJECTIVE: &str = r#"{"patch":{"campaign":{"objective":{"value":"win midweek","origin":"stated","gate":"brief","by":"human:local","decision":"d_01JA0D09HUM"}}},"agent":"human","action":"set objective"}"#;

// N2: a person edited a different field while the job ran. Nothing conflicts:
// the job's field applies, and so does the person's.
#[test]
fn a_stale_base_applies_a_field_nobody_touched() {
    let f = fixture();
    let clan = f.session.clan_context_for_agent();
    f.session.patch_data(HUMAN_OBJECTIVE).unwrap();

    let (out, _) = settle(&f, reply_for(&clan));
    let settled = &out["data"]["change"];
    assert_eq!(settled["applied"], true, "{out}");
    assert_eq!(settled["base_stale"], true);
    assert_eq!(settled["applied_fields"], json!(["campaign.problem"]));
    assert_eq!(settled["contested_fields"], json!([]));

    let after = on_disk(&f);
    let data = yaml(&after, "shared/data.yaml");
    assert_eq!(
        data["campaign"]["problem"]["value"],
        "people who love Lúnasa are drinking less of it midweek"
    );
    assert_eq!(data["campaign"]["objective"]["value"], "win midweek");
    let c = chain(&after);
    assert!(c
        .decisions
        .iter()
        .all(|d| d.kind.as_deref() != Some("contest")));
    assert!(c.ids().contains("d_01JA0D02EXT"));
}

// N2: a person edited the same field. The job's write does not land over
// theirs; it becomes a contest holding both values, and the rest applies.
#[test]
fn a_stale_base_contests_a_field_a_person_changed() {
    let f = fixture();
    let clan = f.session.clan_context_for_agent();
    f.session.patch_data(HUMAN_PROBLEM).unwrap();

    let (out, events) = settle(&f, reply_for(&clan));
    let settled = &out["data"]["change"];
    assert_eq!(settled["applied"], true, "{out}");
    assert_eq!(settled["base_stale"], true);
    assert_eq!(settled["applied_fields"], json!([]));
    assert_eq!(settled["contested_fields"], json!(["campaign.problem"]));
    assert_eq!(settled["contests"].as_array().unwrap().len(), 1);
    assert_eq!(events.len(), 1);

    let after = on_disk(&f);
    let data = yaml(&after, "shared/data.yaml");
    assert_eq!(
        data["campaign"]["problem"]["value"], "midweek is not the problem",
        "the person's value stands"
    );
    // The appends applied regardless.
    assert_eq!(
        yaml(&after, FACTS_PATH)["facts"].as_array().unwrap().len(),
        2
    );
    assert_eq!(
        yaml(&after, FINDINGS_PATH)["findings"][0]["id"],
        "fi_01JA0F2B"
    );

    let c = chain(&after);
    let contest = &c.decisions[0];
    assert_eq!(contest.kind.as_deref(), Some("contest"));
    assert_eq!(
        contest.id.as_deref(),
        settled["contests"][0].as_str(),
        "the reply names the contest it opened"
    );
    assert_eq!(contest.actor.as_deref(), Some("process:middleware"));
    assert_eq!(contest.handler.as_deref(), Some("extract_ask@1.0.0"));
    let doc_id = clan["id"].as_str().unwrap();
    assert_eq!(contest.targets, vec![format!("{doc_id}#campaign.problem")]);
    assert_eq!(contest.cites, vec!["d_01JA0D02EXT".to_string()]);
    // The host decided this one, so it says why in the same shape.
    let why = contest
        .reasoning
        .as_ref()
        .expect("the host reasons its contest");
    assert!(why.problems().is_empty(), "{:?}", why.problems());
    assert!(why.because[0].cites.contains(&"d_01JA0D02EXT".to_string()));
    assert_eq!(why.rejected.len(), 2);
    assert!(why.attention.is_some());
    assert!(clan_sdk::validate(&after).is_content_valid());
    assert_eq!(contest.extra["status"], serde_yaml::Value::from("open"));
    let values = serde_json::to_value(&contest.extra["values"]).unwrap();
    assert_eq!(values[0]["from"], "document");
    assert_eq!(values[0]["value"]["value"], "midweek is not the problem");
    assert_eq!(values[1]["from"], "extract_ask@1.0.0");
    assert_eq!(
        values[1]["value"]["value"],
        "people who love Lúnasa are drinking less of it midweek"
    );
    assert_eq!(values[1]["base_version"], clan["version"]);
    // The job's edit decision describes a write that did not happen: it waits
    // inside the contest, not in the chain. The finding's decision applied.
    let ids = c.ids();
    assert!(!ids.contains("d_01JA0D02EXT"));
    assert!(ids.contains("d_01JA0D06SYN"));
    let withheld = serde_json::to_value(&contest.extra["withheld"]).unwrap();
    assert_eq!(withheld[0]["id"], "d_01JA0D02EXT");
    assert!(clan_sdk::validate(&after).is_valid());

    // Delivered again (a re-poll of the done job): nothing new.
    let before = std::fs::read(f.id.as_str()).unwrap();
    let (out, _) = settle(&f, reply_for(&clan));
    assert_eq!(out["data"]["change"]["applied"], false);
    assert_eq!(out["data"]["change"]["reason"], "already applied");
    assert_eq!(std::fs::read(f.id.as_str()).unwrap(), before);
}

#[test]
fn a_stale_base_without_a_read_set_is_refused() {
    let f = fixture();
    let clan = f.session.clan_context_for_agent();
    f.session.patch_data(HUMAN_OBJECTIVE).unwrap();
    let before = std::fs::read(f.id.as_str()).unwrap();

    let mut r = reply_for(&clan);
    r["change"].as_object_mut().unwrap().remove("read");
    let (out, events) = settle(&f, r);
    assert_eq!(
        out["data"]["change"],
        json!({ "applied": false, "reason": "stale base and no read-set; rerun" })
    );
    assert!(events.is_empty());
    assert_eq!(std::fs::read(f.id.as_str()).unwrap(), before);

    // A read-set that does not cover what the patch writes cannot be judged
    // either.
    let mut r = reply_for(&clan);
    r["change"]["read"] = json!({ "campaign.objective": null });
    let (out, _) = settle(&f, r);
    assert_eq!(out["data"]["change"]["applied"], false);
    assert!(out["data"]["change"]["reason"]
        .as_str()
        .unwrap()
        .contains("does not cover campaign.problem"));
    assert_eq!(std::fs::read(f.id.as_str()).unwrap(), before);

    // On a current base the read-set is not needed.
    let clan = f.session.clan_context_for_agent();
    let mut r = reply_for(&clan);
    r["change"].as_object_mut().unwrap().remove("read");
    let (out, _) = settle(&f, r);
    assert_eq!(out["data"]["change"]["applied"], true, "{out}");
    assert_eq!(out["data"]["change"]["base_stale"], false);
}

// The middleware's own error body reaches the app — type and message — while
// the generic agent's stays hidden.
#[test]
fn a_middleware_error_is_passed_through_to_the_app() {
    let f = fixture();
    let env = napkin_host::proxy::reply_envelope(
        "middleware",
        "http://middleware.test/v1/tasks",
        400,
        r#"{"error":{"type":"invalid_input","message":"research needs markets"}}"#.into(),
    );
    let (out, events) = f.session.settle_middleware(f.session.ctx(), env);
    assert_eq!(out["ok"], false);
    assert_eq!(out["status"], 400);
    assert_eq!(out["error"]["type"], "invalid_input");
    assert_eq!(out["error"]["message"], "research needs markets");
    assert!(events.is_empty());
}

#[test]
fn the_middleware_cannot_write_the_projection_or_verify_a_finding() {
    let f = fixture();
    let before = std::fs::read(f.id.as_str()).unwrap();
    let clan = f.session.clan_context_for_agent();

    let mut r = reply_for(&clan);
    r["change"]["data_patch"]["projection"] = json!({ "pins": {} });
    let (out, _) = settle(&f, r);
    assert_eq!(out["data"]["change"]["applied"], false);
    assert!(out["data"]["change"]["reason"]
        .as_str()
        .unwrap()
        .contains("projection"));

    let mut r = reply_for(&clan);
    r["change"]["findings_append"][0]["status"] = json!("verified");
    let (out, _) = settle(&f, r);
    assert_eq!(out["data"]["change"]["applied"], false);
    assert!(out["data"]["change"]["reason"]
        .as_str()
        .unwrap()
        .contains("not `proposed`"));

    let mut r = reply_for(&clan);
    r["change"]["findings_append"][0]["cites"] = json!(["f_NOTPINNED1"]);
    let (out, _) = settle(&f, r);
    assert_eq!(out["data"]["change"]["applied"], false);
    assert!(out["data"]["change"]["reason"]
        .as_str()
        .unwrap()
        .contains("f_NOTPINNED1, which is not in"));

    assert_eq!(
        std::fs::read(f.id.as_str()).unwrap(),
        before,
        "nothing written"
    );
}

#[test]
fn the_middleware_cannot_write_the_frozen_upstream_copy() {
    let f = fixture();
    let before = std::fs::read(f.id.as_str()).unwrap();
    let clan = f.session.clan_context_for_agent();
    let parent = "3f2a9c1e-7b4d-4e8a-9c6f-0a1b2c3d4e5f";
    for upstream in [json!({ parent: { "campaign": { "problem": "rewritten" } } }), json!(null)] {
        let mut r = reply_for(&clan);
        r["change"]["data_patch"]["upstream"] = upstream;
        let (out, events) = settle(&f, r);
        assert_eq!(out["data"]["change"]["applied"], false, "{out}");
        let reason = out["data"]["change"]["reason"].as_str().unwrap();
        assert!(reason.contains("upstream is the frozen copy") && reason.contains("read-only"), "{reason}");
        assert!(events.is_empty());
    }
    assert_eq!(std::fs::read(f.id.as_str()).unwrap(), before, "nothing written");
}

#[test]
fn a_change_applied_twice_is_refused_the_second_time() {
    let f = fixture();
    let clan = f.session.clan_context_for_agent();
    let (out, _) = settle(&f, reply_for(&clan));
    assert_eq!(out["data"]["change"]["applied"], true);
    let before = std::fs::read(f.id.as_str()).unwrap();
    let chain_len = chain(&on_disk(&f)).decisions.len();

    // The same reply again, as a re-poll of a done job would bring it: its
    // base is now stale, its read-set still matches for nothing it changed.
    let (out, events) = settle(&f, reply_for(&clan));
    assert_eq!(
        out["data"]["change"],
        json!({ "applied": false, "reason": "already applied" })
    );
    assert!(events.is_empty());
    assert_eq!(std::fs::read(f.id.as_str()).unwrap(), before);

    // And computed afresh against the new version: still nothing new.
    let clan = f.session.clan_context_for_agent();
    let (out, _) = settle(&f, reply_for(&clan));
    assert_eq!(out["data"]["change"]["reason"], "already applied");
    assert_eq!(chain(&on_disk(&f)).decisions.len(), chain_len);
}

#[test]
fn decisions_already_in_the_chain_are_not_appended_again() {
    let f = fixture();
    let clan = f.session.clan_context_for_agent();
    settle(&f, reply_for(&clan));
    let clan = f.session.clan_context_for_agent();
    let mut r = reply_for(&clan);
    r["change"]["decisions"].as_array_mut().unwrap().push(
        json!({ "id": "d_01JA0D07NEW", "kind": "edit", "action": "noted",
                "reasoning": reasoning("Noted it.", &["mat_email01"]),
                      "targets": [format!("{}#campaign.problem", clan["id"].as_str().unwrap())] }),
    );
    let (out, _) = settle(&f, r);
    assert_eq!(out["data"]["change"]["applied"], true, "{out}");
    let c = chain(&on_disk(&f));
    for id in ["d_01JA0D02EXT", "d_01JA0D06SYN", "d_01JA0D07NEW"] {
        assert_eq!(
            c.decisions
                .iter()
                .filter(|d| d.id.as_deref() == Some(id))
                .count(),
            1,
            "{id} once"
        );
    }
}

#[test]
fn a_pin_id_with_different_content_is_refused() {
    let f = fixture();
    let clan = f.session.clan_context_for_agent();
    settle(&f, reply_for(&clan));
    let clan = f.session.clan_context_for_agent();
    let mut r = reply_for(&clan);
    r["change"]["facts_append"][0]["value"] = json!(0.99);
    let (out, _) = settle(&f, r);
    assert_eq!(out["data"]["change"]["applied"], false);
    assert!(out["data"]["change"]["reason"]
        .as_str()
        .unwrap()
        .contains("already holds f_01JA0B3P4Q with different content"));
}

// D2: a data-update pack replaces shared/data.yaml whole. The members live
// outside it, so an ordinary patch-data afterwards keeps them — bytes and
// registration — and leaves the projection matching them.
#[test]
fn an_ordinary_patch_data_afterwards_keeps_the_members() {
    let f = fixture();
    let clan = f.session.clan_context_for_agent();
    settle(&f, reply_for(&clan));
    let applied = on_disk(&f);
    let facts = applied.read_entry(FACTS_PATH).unwrap();
    let findings = applied.read_entry(FINDINGS_PATH).unwrap();

    f.session
        .patch_data(r#"{"patch":{"campaign":{"objective":{"value":"win midweek","origin":"stated","gate":"brief","by":"human:local","decision":"d_01JA0D09HUM"}}},"agent":"human","action":"set objective"}"#)
        .unwrap();

    let after = on_disk(&f);
    assert_eq!(after.read_entry(FACTS_PATH).unwrap(), facts);
    assert_eq!(after.read_entry(FINDINGS_PATH).unwrap(), findings);
    assert_eq!(
        after.manifest().file_by_path(FACTS_PATH).unwrap().role,
        FACTS_ROLE
    );
    assert_eq!(
        after.manifest().file_by_path(FINDINGS_PATH).unwrap().role,
        FINDINGS_ROLE
    );
    let data = yaml(&after, "shared/data.yaml");
    assert_eq!(data["campaign"]["objective"]["value"], "win midweek");
    assert_eq!(
        data["campaign"]["problem"]["origin"], "extracted",
        "the middleware's field survives the merge"
    );
    assert_eq!(
        data["projection"]["built_from"]["facts_sha256"],
        clan_sdk::hash::sha256_prefixed(&facts)
    );
    assert!(clan_sdk::validate(&after).is_valid());

    // A title change (a builder rewrite, not a pack) keeps them too.
    f.session.set_title("Renamed").unwrap();
    assert_eq!(on_disk(&f).read_entry(FACTS_PATH).unwrap(), facts);

    // And no patch writes the projection on a document that has members.
    let err = f
        .session
        .patch_data(r#"{"patch":{"projection":{"pins":{}}},"agent":"human"}"#)
        .unwrap_err();
    assert_eq!(err.status, 400);
}

// ── Chat intake: one long job that stages its changes ───────────────────────

/// A `start_campaign` poll: the job in `state` at `stage`, carrying `change`.
fn campaign_poll(state: &str, stage: &str, change: Value) -> Value {
    json!({
        "api": "napkin.middleware/1",
        "task": "start_campaign",
        "handler": "start_campaign@1.0",
        "job": { "id": "job_intake1", "state": state, "stage": stage,
                 "progress": { "done": 1, "total": 6 },
                 "started_at": "2026-09-24T10:00:00Z", "finished_at": null, "error": null },
        "result": { "summary": format!("at {stage}"),
                    "messages": [{ "id": "msg_a1", "text": "Reading the brief", "stage": stage }] },
        "change": change,
        "trace": { "scope": { "org": "dev", "brand": "dev" }, "backend": "mock-backend",
                   "model": null, "hits": [], "usage": { "input_tokens": 0, "output_tokens": 0 } }
    })
}

fn field(value: &str, decision: &str) -> Value {
    json!({ "value": value, "origin": "extracted", "gate": "brief",
            "source": { "material_id": "mat_email01", "locator": "¶1" }, "decision": decision })
}

fn edit(clan: &Value, id: &str, path: &str) -> Value {
    json!({ "id": id, "kind": "edit", "agent": "start_campaign@1.0", "action": "staged",
            "rationale": "from the brief", "reasoning": reasoning("Staged it.", &["mat_email01"]),
            "targets": [format!("{}#{path}", clan["id"].as_str().unwrap())], "cites": ["mat_email01"] })
}

/// What `identify` finished: the brand, one pin, one decision — computed for
/// `clan` as the job read it when it started.
fn identify_change(clan: &Value) -> Value {
    json!({
        "doc": clan["id"], "base_version": clan["version"],
        "read": { "campaign.brand": null },
        "data_patch": { "campaign": { "brand": field("Lúnasa", "d_01JB0IDENT1") } },
        "facts_append": [ fact("f_01JB0PIN001", 0.61, false) ],
        "decisions": [ edit(clan, "d_01JB0IDENT1", "campaign.brand") ],
    })
}

/// The same job one stage later: everything `identify` delivered, repeated,
/// plus what `select` added. Same base — the job read the document once.
fn select_change(clan: &Value) -> Value {
    let mut c = identify_change(clan);
    c["read"]["campaign.objective"] = Value::Null;
    c["data_patch"]["campaign"]["objective"] = field("win midweek", "d_01JB0SELCT2");
    c["facts_append"]
        .as_array_mut()
        .unwrap()
        .push(fact("f_01JB0PIN002", 0.44, false));
    c["decisions"]
        .as_array_mut()
        .unwrap()
        .push(edit(clan, "d_01JB0SELCT2", "campaign.objective"));
    c
}

#[test]
fn a_running_reply_applies_its_staged_change() {
    for state in ["queued", "running", "needs_input"] {
        let f = fixture();
        let clan = f.session.clan_context_for_agent();
        let poll = campaign_poll(state, "identify", identify_change(&clan));

        let (out, events) = settle(&f, poll.clone());
        let settled = &out["data"]["change"];
        assert_eq!(settled["applied"], true, "{state}: {out}");
        assert_eq!(settled["applied_fields"], json!(["campaign.brand"]));
        assert_eq!(events.len(), 1, "{state}");
        // The job is the app's to read, as the middleware sent it.
        assert_eq!(out["data"]["job"], poll["job"], "{state}");
        assert_eq!(out["data"]["result"], poll["result"]);
        assert_eq!(out["clan"]["version"], settled["version"]);

        let after = on_disk(&f);
        let data = yaml(&after, "shared/data.yaml");
        assert_eq!(data["campaign"]["brand"]["value"], "Lúnasa");
        assert_eq!(yaml(&after, FACTS_PATH)["facts"][0]["id"], "f_01JB0PIN001");
        assert!(chain(&after).ids().contains("d_01JB0IDENT1"));
    }
}

#[test]
fn a_later_poll_adds_only_what_is_new() {
    let f = fixture();
    let clan = f.session.clan_context_for_agent();
    let (out, _) = settle(
        &f,
        campaign_poll("running", "identify", identify_change(&clan)),
    );
    assert_eq!(out["data"]["change"]["applied"], true, "{out}");
    let chain_len = chain(&on_disk(&f)).decisions.len();

    // The next poll repeats identify's part over the base the job started
    // from — stale now, by the job's own first write.
    let (out, events) = settle(&f, campaign_poll("running", "select", select_change(&clan)));
    let settled = &out["data"]["change"];
    assert_eq!(settled["applied"], true, "{out}");
    assert_eq!(settled["base_stale"], true);
    assert_eq!(
        settled["applied_fields"],
        json!(["campaign.objective"]),
        "the repeated field is not applied again"
    );
    assert_eq!(settled["contested_fields"], json!([]));
    assert_eq!(events.len(), 1);

    let after = on_disk(&f);
    let data = yaml(&after, "shared/data.yaml");
    assert_eq!(data["campaign"]["brand"]["value"], "Lúnasa");
    assert_eq!(data["campaign"]["objective"]["value"], "win midweek");
    let facts = yaml(&after, FACTS_PATH)["facts"].clone();
    let ids: Vec<&str> = facts
        .as_array()
        .unwrap()
        .iter()
        .map(|f| f["id"].as_str().unwrap())
        .collect();
    assert_eq!(ids, ["f_01JB0PIN001", "f_01JB0PIN002"], "each pin once");
    let c = chain(&after);
    assert_eq!(c.decisions.len(), chain_len + 1, "only the new decision");
    for id in ["d_01JB0IDENT1", "d_01JB0SELCT2"] {
        assert_eq!(
            c.decisions
                .iter()
                .filter(|d| d.id.as_deref() == Some(id))
                .count(),
            1,
            "{id} once"
        );
    }
    assert!(clan_sdk::validate(&after).is_valid());

    // The done poll repeats it all: nothing new.
    let before = std::fs::read(f.id.as_str()).unwrap();
    let (out, events) = settle(&f, campaign_poll("done", "report", select_change(&clan)));
    assert_eq!(
        out["data"]["change"],
        json!({ "applied": false, "reason": "already applied" })
    );
    assert!(events.is_empty());
    assert_eq!(std::fs::read(f.id.as_str()).unwrap(), before);
}

// A person confirms what an earlier stage proposed; the next poll repeats that
// stage. The repeat was judged when it arrived — it does not come back as a
// contest against the person's confirmation.
#[test]
fn a_repeated_stage_does_not_contest_what_a_person_did_since() {
    let f = fixture();
    let clan = f.session.clan_context_for_agent();
    settle(
        &f,
        campaign_poll("needs_input", "identify", identify_change(&clan)),
    );
    f.session
        .patch_data(r#"{"patch":{"campaign":{"brand":{"value":"Lúnasa","origin":"confirmed","gate":"brief","by":"human:local","decision":"d_01JB0HUMAN1"}}},"agent":"human","action":"confirmed the brand"}"#)
        .unwrap();

    let (out, _) = settle(&f, campaign_poll("running", "select", select_change(&clan)));
    let settled = &out["data"]["change"];
    assert_eq!(settled["applied"], true, "{out}");
    assert_eq!(settled["contested_fields"], json!([]));
    assert_eq!(settled["applied_fields"], json!(["campaign.objective"]));

    let after = on_disk(&f);
    let data = yaml(&after, "shared/data.yaml");
    assert_eq!(
        data["campaign"]["brand"]["origin"], "confirmed",
        "the person's stands"
    );
    assert_eq!(data["campaign"]["objective"]["value"], "win midweek");
    assert!(chain(&after)
        .decisions
        .iter()
        .all(|d| d.kind.as_deref() != Some("contest")));
}

#[test]
fn needs_input_reaches_the_app_untouched() {
    let question = json!({
        "id": "q_brand", "text": "Which of these is the client?",
        "options": [ { "id": "o1", "label": "Lúnasa", "value": "brand/lunasa" },
                     { "id": "o2", "label": "Samhain", "value": "brand/samhain" },
                     { "id": "o3", "label": "None of these", "value": null } ],
        "allow_text": true, "address": "campaign.brand",
    });

    // Without a change: the envelope is the middleware's, byte for byte.
    let f = fixture();
    let before = std::fs::read(f.id.as_str()).unwrap();
    let mut asking = campaign_poll("needs_input", "identify", Value::Null);
    asking["job"]["question"] = question.clone();
    let (out, events) = settle(&f, asking.clone());
    assert_eq!(out, envelope(asking));
    assert!(events.is_empty());
    assert_eq!(std::fs::read(f.id.as_str()).unwrap(), before);

    // With one: the change lands and the question still reaches the app.
    let clan = f.session.clan_context_for_agent();
    let mut asking = campaign_poll("needs_input", "identify", identify_change(&clan));
    asking["job"]["question"] = question.clone();
    let (out, _) = settle(&f, asking.clone());
    assert_eq!(out["data"]["change"]["applied"], true, "{out}");
    assert_eq!(out["data"]["job"]["state"], "needs_input");
    assert_eq!(out["data"]["job"]["stage"], "identify");
    assert_eq!(out["data"]["job"]["question"], question);
    assert_eq!(out["data"]["job"], asking["job"]);
}

// `intake.messages` is a map two parties add to. A person's message and the
// middleware's land side by side: different keys, no stale-base contest.
#[test]
fn messages_added_by_both_parties_do_not_contest() {
    let f = fixture();
    let clan = f.session.clan_context_for_agent();
    // The person writes while the job runs, so the job's base goes stale.
    f.session
        .patch_data(r#"{"patch":{"intake":{"messages":{"msg_u2":{"role":"user","text":"also Portugal","at":"2026-09-24T10:00:05Z"}}}},"agent":"human","action":"sent a message"}"#)
        .unwrap();

    let doc_id = clan["id"].as_str().unwrap();
    let change = json!({
        "doc": doc_id, "base_version": clan["version"],
        "read": { "intake.messages.msg_a1": null },
        "data_patch": { "intake": { "messages": { "msg_a1": {
            "role": "agent", "text": "Reading the brief", "at": "2026-09-24T10:00:04Z",
            "job_id": "job_intake1", "stage": "extract" } } } },
    });
    let (out, _) = settle(&f, campaign_poll("running", "extract", change.clone()));
    let settled = &out["data"]["change"];
    assert_eq!(settled["applied"], true, "{out}");
    assert_eq!(settled["base_stale"], true);
    assert_eq!(settled["applied_fields"], json!(["intake.messages.msg_a1"]));
    assert_eq!(settled["contested_fields"], json!([]));

    let data = yaml(&on_disk(&f), "shared/data.yaml");
    let messages = data["intake"]["messages"].as_object().unwrap();
    assert_eq!(messages.len(), 2, "{messages:?}");
    assert_eq!(messages["msg_u2"]["role"], "user");
    assert_eq!(messages["msg_a1"]["role"], "agent");
    assert_eq!(
        out["clan"]["data"]["intake"]["messages"]["msg_u2"]["text"],
        "also Portugal"
    );

    // Another person's message, then the same agent message again on a later
    // poll: still nothing to contest, nothing new to write.
    f.session
        .patch_data(r#"{"patch":{"intake":{"messages":{"msg_u3":{"role":"user","text":"and Spain","at":"2026-09-24T10:00:09Z"}}}},"agent":"human","action":"sent a message"}"#)
        .unwrap();
    let (out, _) = settle(&f, campaign_poll("running", "identify", change));
    assert_eq!(
        out["data"]["change"],
        json!({ "applied": false, "reason": "already applied" })
    );
    let data = yaml(&on_disk(&f), "shared/data.yaml");
    assert_eq!(data["intake"]["messages"].as_object().unwrap().len(), 3);
}

// ── Addresses into maps: the key in brackets ────────────────────────────────

/// One staged entry of a map, `<top>.<key>` in the patch, delivered with a
/// decision whose only target is `target` (the bracketed address, as the
/// middleware writes it). Delivered, touched by a person, then delivered again
/// by a later poll over the same base: whether the repeat is recognised as
/// already delivered. Returns the second settle's `change` reply and the open
/// contests afterwards.
fn repeat_after_a_person_touched(
    patch: Value,
    read_key: &str,
    target: &str,
    person: &str,
) -> (Value, usize) {
    let f = fixture();
    let clan = f.session.clan_context_for_agent();
    let doc_id = clan["id"].as_str().unwrap().to_string();
    let change = json!({
        "doc": doc_id, "base_version": clan["version"],
        "read": { read_key: null },
        "data_patch": patch,
        "decisions": [{ "id": "d_01JB0STAGE1", "kind": "edit", "agent": "start_campaign@1.0",
                        "action": "staged", "rationale": "the stage's entry",
                        "reasoning": reasoning("Staged the entry.", &["mat_email01"]),
                        "targets": [target.replace("<doc>", &doc_id)] }],
    });
    let (out, _) = settle(&f, campaign_poll("running", "extract", change.clone()));
    assert_eq!(out["data"]["change"]["applied"], true, "{out}");

    // A person adds a message of their own and changes the delivered entry.
    f.session
        .patch_data(r#"{"patch":{"intake":{"messages":{"msg_u3":{"role":"user","text":"and Spain","at":"2026-09-24T10:00:09Z"}}}},"agent":"human","action":"sent a message"}"#)
        .unwrap();
    f.session.patch_data(person).unwrap();

    let (out, _) = settle(&f, campaign_poll("running", "identify", change));
    let contests = chain(&on_disk(&f))
        .decisions
        .iter()
        .filter(|d| d.kind.as_deref() == Some("contest"))
        .count();
    (out["data"]["change"].clone(), contests)
}

fn staged_message() -> Value {
    json!({ "intake": { "messages": { "msg_a1": {
        "role": "agent", "text": "Reading the brief", "at": "2026-09-24T10:00:04Z",
        "job_id": "job_intake1", "stage": "extract" } } } })
}

const PERSON_MARKS_MESSAGE: &str = r#"{"patch":{"intake":{"messages":{"msg_a1":{"pinned":true}}}},"agent":"human","action":"pinned a message"}"#;

#[test]
fn a_bracketed_message_target_delivered_twice_is_skipped_the_second_time() {
    let (reply, contests) = repeat_after_a_person_touched(
        staged_message(),
        "intake.messages.msg_a1",
        "<doc>#intake.messages[msg_a1]",
        PERSON_MARKS_MESSAGE,
    );
    assert_eq!(
        reply,
        json!({ "applied": false, "reason": "already applied" }),
        "recognised as delivered, not contested"
    );
    assert_eq!(contests, 0);
}

#[test]
fn a_bracketed_material_target_is_matched() {
    let (reply, contests) = repeat_after_a_person_touched(
        json!({ "materials": { "mat_email01": {
            "kind": "email", "label": "The client's email", "added_at": "2026-09-24T10:00:01Z" } } }),
        "materials.mat_email01",
        "<doc>#materials[mat_email01]",
        r#"{"patch":{"materials":{"mat_email01":{"label":"Client email (renamed)"}}},"agent":"human","action":"renamed a material"}"#,
    );
    assert_eq!(
        reply,
        json!({ "applied": false, "reason": "already applied" })
    );
    assert_eq!(contests, 0);
}

// Neither a target on another document nor a malformed address speaks for the
// field, so the repeat is judged as usual — and the person's change contests
// it. (The host does not guess what such a target meant.)
#[test]
fn a_target_elsewhere_or_malformed_does_not_count_as_delivered() {
    for target in [
        "d_01OTHERDOC#intake.messages[msg_a1]",
        "d_01OTHERDOC#intake.messages.msg_a1",
        "intake.messages[msg_a1]",
        "<doc>#intake.messages[msg_a1",
        "<doc>#intake.messages[msg.a1]",
        "<doc>#intake.messages[]",
        "<doc>#intake.messages[msg_a1]x",
        "<doc>#[msg_a1]",
        "<doc>#intake..messages.msg_a1",
    ] {
        let (reply, contests) = repeat_after_a_person_touched(
            staged_message(),
            "intake.messages.msg_a1",
            target,
            PERSON_MARKS_MESSAGE,
        );
        assert_eq!(reply["applied"], true, "{target}: {reply}");
        assert_eq!(
            reply["contested_fields"],
            json!(["intake.messages.msg_a1"]),
            "{target}"
        );
        assert_eq!(contests, 1, "{target}");
    }
}

// The dotted form of an address still matches, as before.
#[test]
fn a_dotted_target_on_this_document_still_counts() {
    let (reply, contests) = repeat_after_a_person_touched(
        staged_message(),
        "intake.messages.msg_a1",
        "<doc>#intake.messages.msg_a1",
        PERSON_MARKS_MESSAGE,
    );
    assert_eq!(reply["reason"], "already applied", "{reply}");
    assert_eq!(contests, 0);
}

// ── reasoning (middleware-api.md §3) ─────────────────────────────────────

fn refused_reason(f: &Fixture, r: Value) -> String {
    let before = std::fs::read(f.id.as_str()).unwrap();
    let (out, events) = settle(f, r);
    assert_eq!(out["data"]["change"]["applied"], false, "{out}");
    assert!(events.is_empty());
    assert_eq!(
        std::fs::read(f.id.as_str()).unwrap(),
        before,
        "nothing written"
    );
    out["data"]["change"]["reason"]
        .as_str()
        .unwrap()
        .to_string()
}

#[test]
fn a_required_decision_without_reasoning_refuses_the_change() {
    let f = fixture();
    let clan = f.session.clan_context_for_agent();

    // An edit that writes a campaign field.
    let mut r = reply_for(&clan);
    r["change"]["decisions"][0]
        .as_object_mut()
        .unwrap()
        .remove("reasoning");
    let why = refused_reason(&f, r);
    assert!(
        why.contains("d_01JA0D02EXT (edit) carries no reasoning"),
        "{why}"
    );

    // A finding.
    let mut r = reply_for(&clan);
    r["change"]["decisions"][1]["reasoning"] = Value::Null;
    let why = refused_reason(&f, r);
    assert!(
        why.contains("d_01JA0D06SYN (finding) carries no reasoning"),
        "{why}"
    );

    // Malformed: no evidence, an unknown certainty, a figure with no cite.
    let mut r = reply_for(&clan);
    r["change"]["decisions"][0]["reasoning"]["because"] = json!([]);
    assert!(refused_reason(&f, r).contains("because has no point"));
    let mut r = reply_for(&clan);
    r["change"]["decisions"][0]["reasoning"]["certainty"]["level"] = json!("0.9");
    assert!(refused_reason(&f, r).contains("is not one of: high, medium, low"));
    let mut r = reply_for(&clan);
    r["change"]["decisions"][0]["reasoning"]["because"] =
        json!([{ "point": "41% say so", "cites": [] }]);
    assert!(refused_reason(&f, r).contains("states a figure and cites nothing"));
    let mut r = reply_for(&clan);
    r["change"]["decisions"][0]["reasoning"] = json!("because");
    assert!(refused_reason(&f, r).contains("reasoning is malformed"));
}

#[test]
fn a_chat_message_needs_no_reasoning_and_an_empty_rationale_takes_the_summary() {
    let f = fixture();
    let clan = f.session.clan_context_for_agent();
    let doc_id = clan["id"].as_str().unwrap().to_string();
    let mut r = reply_for(&clan);
    // The extract decision sends reasoning and no rationale.
    r["change"]["decisions"][0]["rationale"] = json!("");
    // A narration: an edit that only posts a chat message.
    r["change"]["data_patch"]["intake"] = json!({ "messages": { "msg_n1": {
        "role": "agent", "text": "Read the email.", "at": "2026-09-24T10:00:00Z" } } });
    r["change"]["read"]["intake.messages.msg_n1"] = Value::Null;
    r["change"]["decisions"].as_array_mut().unwrap().push(json!({
        "id": "d_01JA0D08NAR", "kind": "edit", "action": "narrate", "rationale": "the stage's message",
        "targets": [format!("{doc_id}#intake.messages[msg_n1]")] }));
    let (out, _) = settle(&f, r);
    assert_eq!(out["data"]["change"]["applied"], true, "{out}");
    let c = chain(&on_disk(&f));
    let by = |id: &str| {
        c.decisions
            .iter()
            .find(|d| d.id.as_deref() == Some(id))
            .unwrap()
    };
    assert!(by("d_01JA0D08NAR").reasoning.is_none());
    assert_eq!(
        by("d_01JA0D02EXT").rationale,
        "Filled the problem from the email. Because: the cited material says so"
    );
}

#[test]
fn patch_data_records_a_persons_reasoning_when_given() {
    let f = fixture();
    let body = json!({
        "patch": { "campaign": { "objective": { "value": "win midweek", "origin": "stated",
                   "gate": "brief", "by": "human:local", "decision": "d_01JA0D09HUM" } } },
        "agent": "human", "action": "set objective",
        "reasoning": { "decided": "Set the objective to midweek.",
                       "because": [{ "point": "the client said so on the call" }],
                       "rejected": [], "only_option": "the client named one objective",
                       "certainty": { "level": "high", "why": "the client's own words" },
                       "would_change_if": "the client revises the ask" } });
    f.session.patch_data(&body.to_string()).unwrap();
    let c = chain(&on_disk(&f));
    let d = &c.decisions[0];
    assert_eq!(d.action, "set objective");
    assert_eq!(
        d.rationale,
        "Set the objective to midweek. Because: the client said so on the call"
    );
    assert_eq!(
        d.reasoning.as_ref().unwrap().only_option.as_deref(),
        Some("the client named one objective")
    );

    // A malformed shape is refused before anything is written.
    let mut bad = body.clone();
    bad["reasoning"]["because"] = json!([]);
    bad["patch"]["campaign"]["objective"]["value"] = json!("win weekends");
    let err = f.session.patch_data(&bad.to_string()).unwrap_err();
    assert!(
        err.message.contains("because has no point"),
        "{}",
        err.message
    );
}

// ── every field a decision sends (middleware-api.md §10.7, host work) ────

/// A Brief Maker judge stage: a bad verdict on the insight, carrying every
/// field a middleware decision can, plus fields the host owns said otherwise.
fn verdict_reply(clan: &Value) -> Value {
    let doc = clan["id"].as_str().unwrap();
    json!({
        "api": "napkin.middleware/1", "task": "draft_brief", "handler": "draft_brief@1.0",
        "job": { "id": "job_b1", "state": "running", "stage": "judge",
                 "progress": { "done": 2, "total": 3 }, "question": null,
                 "started_at": "2026-09-24T09:00:00Z", "finished_at": null, "error": null },
        "result": { "summary": "judging" },
        "change": {
            "doc": doc, "base_version": clan["version"],
            "read": { "open_questions": Value::Null },
            "data_patch": { "open_questions": ["[high] Agree the insight — it failed twice"] },
            "decisions": [
                { "id": "d_01JB0V01VER", "kind": "verdict", "agent": "draft_brief@1.0/judge",
                  "action": "judge", "polarity": "bad", "reason_code": "cliche",
                  "taxonomy_version": "reason-codes/1", "reviewer_role": "judge",
                  "licence": { "model": true, "export": false },
                  "targets": [format!("{doc}#insight")], "cites": ["d_01JB0D01DRF", "psg_3b9f0c2e7a41d5c8e210"],
                  "claimed_agent": "golden-critic",
                  "actor": "human:mallory", "scope": { "org": "elsewhere" },
                  "timestamp": "2026-09-24T09:00:07Z",
                  "abstained": ["budget_and_scope"], "material_read": ["mat_9f2c0a1b3d4e5f60"],
                  "unread": ["mat_0000000000000000"], "proposed_value": "People buy cider for the ritual.",
                  "checks": [{ "check": "ownable", "status": "fail" }],
                  "reasoning": { "decided": "Failed the insight.",
                                 "because": [{ "point": "it restates the category's cliché", "cites": ["psg_3b9f0c2e7a41d5c8e210"] }],
                                 "only_option": "a failed model check decides the verdict",
                                 "certainty": { "level": "medium", "why": "a model check decided" },
                                 "would_change_if": "a redraft passes the ownable check",
                                 "attention": "The insight failed twice; agree it with the client." } },
                { "id": "d_01JB0Q01QST", "kind": "edit", "agent": "draft_brief@1.0/judge",
                  "action": "questions", "targets": [format!("{doc}#open_questions")],
                  "cites": ["d_01JB0V01VER"],
                  "reasoning": reasoning("Composed the open questions.", &["d_01JB0V01VER"]) }
            ]
        },
        "trace": { "scope": { "org": "dev", "brand": "dev" }, "backend": "mock-backend",
                   "model": null, "hits": [], "usage": { "input_tokens": 0, "output_tokens": 0 } }
    })
}

#[test]
fn every_field_a_middleware_decision_sends_is_kept() {
    let f = fixture_with(Some(BRIEF_PIPELINE));
    let clan = f.session.clan_context_for_agent();
    let (out, _) = settle(&f, verdict_reply(&clan));
    assert_eq!(out["data"]["change"]["applied"], true, "{out}");

    let c = chain(&on_disk(&f));
    let d = c
        .decisions
        .iter()
        .find(|d| d.id.as_deref() == Some("d_01JB0V01VER"))
        .unwrap();
    // The verdict's own fields.
    assert_eq!(d.kind.as_deref(), Some("verdict"));
    assert_eq!(d.polarity.as_deref(), Some("bad"));
    assert_eq!(d.reason_code.as_deref(), Some("cliche"));
    assert_eq!(d.taxonomy_version.as_deref(), Some("reason-codes/1"));
    assert_eq!(d.reviewer_role.as_deref(), Some("judge"));
    let licence = d.licence.as_ref().unwrap();
    assert_eq!((licence.model, licence.export), (Some(true), Some(false)));
    assert_eq!(d.agent, "draft_brief@1.0/judge");
    assert_eq!(d.claimed_agent.as_deref(), Some("golden-critic"));
    assert_eq!(d.cites, ["d_01JB0D01DRF", "psg_3b9f0c2e7a41d5c8e210"]);
    // The flatten tail, verbatim.
    let y = |s: &str| serde_yaml::from_str::<serde_yaml::Value>(s).unwrap();
    assert_eq!(d.extra["abstained"], y("[budget_and_scope]"));
    assert_eq!(d.extra["material_read"], y("[mat_9f2c0a1b3d4e5f60]"));
    assert_eq!(d.extra["unread"], y("[mat_0000000000000000]"));
    assert_eq!(
        d.extra["proposed_value"],
        y("People buy cider for the ritual.")
    );
    assert_eq!(d.extra["checks"], y("[{check: ownable, status: fail}]"));
    // Attribution is the host's; what the body said instead is kept as a claim.
    assert_eq!(d.actor.as_deref(), Some("process:middleware"));
    assert_eq!(d.handler.as_deref(), Some("draft_brief@1.0"));
    assert_eq!(d.backend.as_deref(), Some("mock-backend"));
    assert_eq!(d.extra["claimed_actor"], y("human:mallory"));
    assert_eq!(d.extra["claimed_scope"], y("{org: elsewhere}"));
    assert_eq!(d.extra["claimed_timestamp"], y("'2026-09-24T09:00:07Z'"));
    assert_ne!(d.timestamp, "2026-09-24T09:00:07Z");
    // A bad verdict needs a rationale; the reasoning's summary is it.
    assert!(
        d.rationale.starts_with("Failed the insight."),
        "{}",
        d.rationale
    );
    assert!(d.fields_changed.is_empty());
}

#[test]
fn a_malformed_decision_field_refuses_the_change() {
    let f = fixture_with(Some(BRIEF_PIPELINE));
    let clan = f.session.clan_context_for_agent();
    let mut r = verdict_reply(&clan);
    r["change"]["decisions"][0]["polarity"] = json!(3);
    let why = refused_reason(&f, r);
    assert!(why.contains("decision d_01JB0V01VER is malformed"), "{why}");
}

// ── R1, declared by the app ──────────────────────────────────────────────

/// An edit to `field` with no reasoning, and nothing else.
fn bare_edit(clan: &Value, field: &str, action: &str) -> Value {
    let doc = clan["id"].as_str().unwrap();
    let mut patch = json!({});
    patch[field] = json!("a value");
    let mut read = json!({});
    read[field] = Value::Null;
    json!({
        "api": "napkin.middleware/1", "task": "draft_brief", "handler": "draft_brief@1.0",
        "job": { "id": "job_b2", "state": "done", "progress": { "done": 3, "total": 3 },
                 "started_at": "2026-09-24T09:00:00Z", "finished_at": "2026-09-24T09:01:00Z", "error": null },
        "result": { "summary": "done" },
        "change": {
            "doc": doc, "base_version": clan["version"],
            "read": read, "data_patch": patch,
            "decisions": [{ "id": format!("d_01JB0E{}{}", field.len(), action.len()), "kind": "edit",
                            "action": action, "rationale": "wrote it",
                            "targets": [format!("{doc}#{field}")] }]
        },
        "trace": { "backend": "mock-backend", "usage": { "input_tokens": 0, "output_tokens": 0 } }
    })
}

#[test]
fn brief_maker_declares_its_fields_need_reasoning() {
    let f = fixture_with(Some(BRIEF_PIPELINE));
    let clan = f.session.clan_context_for_agent();
    let why = refused_reason(&f, bare_edit(&clan, "insight", "draft"));
    assert!(why.contains("(edit) carries no reasoning"), "{why}");
    assert!(why.contains("insight"), "names the declared paths: {why}");
    // A path the app did not declare needs none.
    let (out, _) = settle(&f, bare_edit(&clan, "brief_note", "capture"));
    assert_eq!(out["data"]["change"]["applied"], true, "{out}");
}

#[test]
fn the_research_tool_declares_campaign_selection_and_report() {
    // Its behaviour before the rule moved out of the host: unchanged.
    let f = fixture();
    let clan = f.session.clan_context_for_agent();
    for field in ["campaign", "selection", "report"] {
        let why = refused_reason(&f, bare_edit(&clan, field, "extract"));
        assert!(why.contains("carries no reasoning"), "{field}: {why}");
    }
    // A Brief Maker field is not the Research Tool's to require.
    let (out, _) = settle(&f, bare_edit(&clan, "insight", "draft"));
    assert_eq!(out["data"]["change"]["applied"], true, "{out}");
}

#[test]
fn with_no_declaration_the_floor_still_holds() {
    let f = fixture_with(None);
    let clan = f.session.clan_context_for_agent();
    // An edit is the app's to require; with no app, it needs none.
    let (out, _) = settle(&f, bare_edit(&clan, "insight", "draft"));
    assert_eq!(out["data"]["change"]["applied"], true, "{out}");
    // A proposal always does.
    let clan = f.session.clan_context_for_agent();
    let why = refused_reason(&f, bare_edit(&clan, "audience", "propose"));
    assert!(why.contains("carries no reasoning"), "{why}");
    // So does a verdict.
    let mut r = verdict_reply(&clan);
    r["change"]["decisions"][0]
        .as_object_mut()
        .unwrap()
        .remove("reasoning");
    let why = refused_reason(&f, r);
    assert!(
        why.contains("d_01JB0V01VER (verdict) carries no reasoning"),
        "{why}"
    );
}

#[test]
fn a_declaration_the_host_cannot_read_refuses_the_change() {
    for bad in [
        "reasoning: [campaign]\n",
        "reasoning:\n  edits: campaign\n",
        "reasoning:\n  kinds: [ponder]\n",
    ] {
        let f = fixture_with(Some(bad));
        let clan = f.session.clan_context_for_agent();
        let why = refused_reason(&f, reply_for(&clan));
        assert!(why.contains("app/pipeline.yaml `reasoning"), "{bad}: {why}");
    }
}

// ── sources: the evidence travels with the document ────────────────────────

/// A schema with room for sources in the projection, the way the Research
/// Tool's is after this change: nothing else about it is checked.
const SCHEMA_WITH_SOURCES: &str =
    r#"{"type":"object","properties":{"projection":{"type":"object","properties":{"sources":{"type":"object"}}}}}"#;

/// A fresh document whose data schema is `schema`.
fn fixture_with_schema(schema: &str) -> Fixture {
    let f = fixture();
    let clan = on_disk(&f);
    let mut b = clan_sdk::ClanBuilder::new(clan.manifest().clone());
    for (path, entry) in clan.read_all_entries().unwrap() {
        if path != clan_sdk::MANIFEST_PATH && path != "agent/output-schema.json" {
            b.add_entry(path, entry);
        }
    }
    b.add_entry("agent/output-schema.json", schema.as_bytes().to_vec());
    std::fs::write(f.id.as_str(), b.build().unwrap()).unwrap();
    f.session.open(f.id.clone()).unwrap();
    f
}

fn source(id: &str, title: &str) -> Value {
    json!({ "id": id, "uri": "https://example.com/panel-2026", "title": title,
            "publisher": "Example Panel", "published_at": "2026-06-30",
            "retrieved_at": "2026-09-12", "tier": "syndicated", "domain": "example.com",
            "licence": "open" })
}

/// `reply_for`, with the first pin quoting its source and the source's record.
fn reply_with_sources(clan: &Value, title: &str) -> Value {
    let mut r = reply_for(clan);
    r["change"]["facts_append"][0]["quotes"] =
        json!({ "src_4f2a": "Prompted awareness reached 61% by June." });
    r["change"]["sources_append"] = json!([source("src_4f2a", title)]);
    r
}

#[test]
fn sources_land_in_their_own_member_and_the_projection_carries_them() {
    let f = fixture_with_schema(SCHEMA_WITH_SOURCES);
    let clan = f.session.clan_context_for_agent();
    let (out, _) = settle(&f, reply_with_sources(&clan, "Panel, June 2026"));
    assert_eq!(out["data"]["change"]["applied"], true, "{out}");

    let after = on_disk(&f);
    assert_eq!(
        after.manifest().file_by_path(SOURCES_PATH).unwrap().role,
        SOURCES_ROLE
    );
    let report = clan_sdk::validate(&after);
    assert!(report.is_valid(), "{}", report.display());
    assert_eq!(yaml(&after, SOURCES_PATH)["sources"][0]["id"], "src_4f2a");

    // What a view binds to: the source by id, and the quote on the pin.
    let p = &yaml(&after, "shared/data.yaml")["projection"];
    assert_eq!(p["sources"]["src_4f2a"]["uri"], "https://example.com/panel-2026");
    assert_eq!(p["sources"]["src_4f2a"]["publisher"], "Example Panel");
    assert!(p["sources"]["src_4f2a"].get("id").is_none());
    assert_eq!(
        p["pins"]["f_01JA0B3P4Q"]["quotes"]["src_4f2a"],
        "Prompted awareness reached 61% by June."
    );
    assert_eq!(
        p["built_from"]["sources_sha256"],
        clan_sdk::hash::sha256_prefixed(&after.read_entry(SOURCES_PATH).unwrap())
    );
}

#[test]
fn a_source_delivered_again_keeps_its_first_record() {
    let f = fixture_with_schema(SCHEMA_WITH_SOURCES);
    let clan = f.session.clan_context_for_agent();
    settle(&f, reply_with_sources(&clan, "Panel, June 2026"));

    // A second lens cites the same page under another title, with a new pin
    // of its own: the change lands, and the source is not a conflict.
    let clan = f.session.clan_context_for_agent();
    let mut r = reply_with_sources(&clan, "Example Panel — H1 2026 report");
    r["change"]["data_patch"] = json!({});
    r["change"]["read"] = json!({});
    r["change"]["facts_append"] = json!([fact("f_01JA0B3P6S", 0.3, false)]);
    r["change"]["findings_append"] = json!([]);
    r["change"]["decisions"] = json!([]);
    let (out, _) = settle(&f, r);
    assert_eq!(out["data"]["change"]["applied"], true, "{out}");

    let sources = yaml(&on_disk(&f), SOURCES_PATH)["sources"].clone();
    assert_eq!(sources.as_array().unwrap().len(), 1);
    assert_eq!(sources[0]["title"], "Panel, June 2026");
}

#[test]
fn a_document_made_before_sources_keeps_its_projection_shape() {
    // The generic schema has no `projection.sources`: the member is still
    // kept, but the projection is built as it always was.
    let f = fixture();
    let clan = f.session.clan_context_for_agent();
    let (out, _) = settle(&f, reply_with_sources(&clan, "Panel, June 2026"));
    assert_eq!(out["data"]["change"]["applied"], true, "{out}");
    let after = on_disk(&f);
    assert_eq!(yaml(&after, SOURCES_PATH)["sources"][0]["id"], "src_4f2a");
    let p = &yaml(&after, "shared/data.yaml")["projection"];
    assert!(p.get("sources").is_none(), "{p}");
    assert!(p["built_from"].get("sources_sha256").is_none());
    assert!(p["pins"]["f_01JA0B3P4Q"].get("quotes").is_none());
}

#[test]
fn a_change_without_sources_writes_no_sources_member() {
    let f = fixture_with_schema(SCHEMA_WITH_SOURCES);
    let clan = f.session.clan_context_for_agent();
    settle(&f, reply_for(&clan));
    let after = on_disk(&f);
    assert!(!after.has_entry(SOURCES_PATH));
    assert!(after.manifest().file_by_path(SOURCES_PATH).is_none());
    // The projection still says there are none, in the shape the schema has.
    let p = &yaml(&after, "shared/data.yaml")["projection"];
    assert_eq!(p["sources"], json!({}));
}

#[test]
fn a_source_without_a_uri_or_a_src_id_is_refused() {
    let f = fixture_with_schema(SCHEMA_WITH_SOURCES);
    let clan = f.session.clan_context_for_agent();
    let mut r = reply_with_sources(&clan, "Panel");
    r["change"]["sources_append"][0]["uri"] = json!(" ");
    assert!(refused_reason(&f, r).contains("src_4f2a has no uri"));
    let mut r = reply_with_sources(&clan, "Panel");
    r["change"]["sources_append"][0]["id"] = json!("human:ana");
    assert!(refused_reason(&f, r).contains("no `src_` id"));
}
