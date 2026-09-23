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
                             "current_fact_id": "f_01JA0B9Z9Z" });
    }
    f
}

/// What the middleware answers `extract_ask` with, computed for `clan` as the
/// host sent it: its `id` and `version`.
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
    assert_eq!(clan["id"], open.manifest().id.as_str());
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
    assert_eq!(clan["id"], on_disk(&f).manifest().id.as_str());
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
    // reply's handler and backend; identity and targets kept verbatim.
    let c = chain(&after);
    assert_eq!(c.decisions.len(), chain_before + 2);
    let (newest, first) = (&c.decisions[0], &c.decisions[1]);
    assert_eq!(first.agent, "extract_ask@1.0.0");
    assert_eq!(first.action, "extracted the ask");
    assert!(
        first.rationale.starts_with(
            "[actor process:middleware handler extract_ask@1.0.0 backend mock-backend] \
             [decision d_01JA0D02EXT kind edit cites mat_email01] read from the client's email"
        ),
        "{}",
        first.rationale
    );
    let tr = first.trace_ref.as_ref().unwrap();
    assert_eq!(tr.store, "napkin.middleware/1");
    assert_eq!(tr.entry, "d_01JA0D02EXT");
    assert!(tr.content_hash.starts_with("sha256:"));
    assert_eq!(
        first.fields_changed,
        vec![format!("{}#campaign.problem", clan["id"].as_str().unwrap())]
    );
    assert_eq!(newest.trace_ref.as_ref().unwrap().entry, "d_01JA0D06SYN");
    assert!(newest
        .rationale
        .contains("kind finding cites f_01JA0B3P4Q,f_01JA0B3P5R"));

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
    assert!(pin.get("pin_reason").is_none(), "only the scalar view keys");
    assert_eq!(p["pins"]["f_01JA0B3P5R"]["stale"], true);
    assert_eq!(p["pins"]["f_01JA0B3P5R"]["current_version"], 3);
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

#[test]
fn a_stale_base_is_applied_and_said_so() {
    let f = fixture();
    let mut clan = f.session.clan_context_for_agent();
    clan["version"] = json!("sha256:0000");
    let (out, _) = settle(&f, reply_for(&clan));
    assert_eq!(out["data"]["change"]["applied"], true);
    assert_eq!(out["data"]["change"]["base_stale"], true);
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
fn a_repeated_change_does_not_pin_twice() {
    let f = fixture();
    let clan = f.session.clan_context_for_agent();
    settle(&f, reply_for(&clan));
    let clan = f.session.clan_context_for_agent();
    let (out, _) = settle(&f, reply_for(&clan));
    assert_eq!(out["data"]["change"]["applied"], false);
    assert!(out["data"]["change"]["reason"]
        .as_str()
        .unwrap()
        .contains("already holds"));
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
