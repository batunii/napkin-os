// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! The host half of `napkin.middleware/1`: a canned middleware reply goes in
//! the way the proxy would hand it over, and the open document comes out
//! changed — once, by the host, as `process:middleware` — or untouched with a
//! reason. No network: the proxy call is the only part not exercised here.

use std::sync::Arc;

use clan_sdk::{ClanFile, DecisionChain};
use napkin_host::ops::members::{FACTS_PATH, FACTS_ROLE, FINDINGS_PATH, FINDINGS_ROLE};
use napkin_host::ops::middleware;
use napkin_host::{Ctx, DocId, Document, FsStore, HostEvent, Session};
use serde_json::{json, Value};

struct Fixture {
    _dir: tempfile::TempDir,
    session: Session,
    id: DocId,
}

fn fixture() -> Fixture {
    let dir = tempfile::tempdir().unwrap();
    let id = DocId::from(dir.path().join("campaign.clan"));
    let bytes = clan_sdk::create(clan_sdk::CreateOptions {
        title: "Campaign".into(),
        brief: "a campaign".into(),
        document_type: None,
        no_render: false,
        schema: None,
    })
    .unwrap();
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
                  "cites": ["mat_email01"], "handler": "extract_ask@1.0.0", "backend": "ignored" },
                { "id": "d_01JA0D06SYN", "kind": "finding", "agent": "extract_ask@1.0.0",
                  "action": "proposed a finding", "rationale": "two pins agree",
                  "targets": [format!("{}#findings[fi_01JA0F2B]", clan["id"].as_str().unwrap())],
                  "cites": ["f_01JA0B3P4Q", "f_01JA0B3P5R"] }
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
    assert_eq!(clan["pipeline"], Value::Null);
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
