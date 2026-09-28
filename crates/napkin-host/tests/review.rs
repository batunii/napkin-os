// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! A person's review decisions: each records one decision as the person, with
//! what it changes in the same generation — or refuses and writes nothing.

#![recursion_limit = "256"]

use std::sync::Arc;

use clan_sdk::{ClanFile, DecisionChain};
use napkin_host::ops::review::{self, Classify, Resolve, Verdict};
use napkin_host::{handle, Actor, Ctx, DocId, FsStore, HostRequest, NoConfig, Session};
use serde_json::{json, Value};

struct Fixture {
    _dir: tempfile::TempDir,
    session: Session,
    id: DocId,
    doc: String,
}

fn reasoning(decided: &str, cites: &[&str]) -> Value {
    json!({ "decided": decided,
            "because": [{ "point": "the cited material says so", "cites": cites }],
            "rejected": [{ "option": "leave it open", "why": "the material settles it" }],
            "certainty": { "level": "high", "why": "a verbatim quote" },
            "would_change_if": "the client says otherwise" })
}

fn pin(id: &str, value: f64, layer: &str) -> Value {
    json!({
        "id": id, "entity": "category/drinks.cider", "key": "market.share_frozen", "value": value,
        "unit": "proportion", "as_of": "2026-06-30", "retrieved_at": "2026-09-12",
        "sources": ["src_4f2a"], "confidence": "high", "licence": "open",
        "status": "active", "version": 2, "supersedes": null,
        "origin": format!("fact://category/drinks.cider/market.share_frozen@{}", 2),
        "decision": "d_01JA0D02PIN", "pinned_at": "2026-09-21T09:00:04Z", "pin_reason": "research",
        "layer": layer, "market": "IE", "method": "report",
    })
}

/// A document with two pins, a proposed finding citing them, and an open
/// contest between the first pin and a newer value the research froze a pin
/// for — seeded the way it arrives: as a middleware change.
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
    let clan = session.clan_context_for_agent();
    let doc = clan["id"].as_str().unwrap().to_string();

    let mut other = pin("f_01JA0B3P5R", 0.44, "category");
    other["key"] = json!("market.value_growth");
    other["origin"] = json!("fact://category/drinks.cider/market.value_growth@2");
    let mut newer = pin("f_01JA0B9Z9Z", 0.19, "category");
    newer["version"] = json!(3);
    newer["decision"] = json!("d_01JA0D03CON");
    let reply = json!({
        "api": "napkin.middleware/1", "task": "research_lens", "handler": "research_lens@1.0",
        "job": { "id": "job_1", "state": "done" }, "result": {},
        "change": {
            "doc": doc, "base_version": clan["version"],
            "read": { "selection.contested": null },
            "data_patch": { "selection": { "contested": [ {
                "id": "ct_share", "key": "category/drinks.cider:market.share_frozen@IE", "status": "open",
                "opened_by": "d_01JA0D03CON",
                "values": [
                    { "value": 0.23, "fact_id": "f_01JA0B3P4Q", "from": "pinned", "sources": ["src_4f2a"] },
                    { "value": 0.19, "fact_id": "f_01JA0B9Z9Z", "from": "market_structure/IE",
                      "sources": ["src_77aa"], "pin": newer } ] } ] } },
            "facts_append": [ pin("f_01JA0B3P4Q", 0.23, "category"), other ],
            "findings_append": [ {
                "id": "fi_01JA0F2B", "statement": "Frozen is growing fast from a small share",
                "cites": ["f_01JA0B3P4Q", "f_01JA0B3P5R"], "method": "synthesis",
                "status": "proposed", "derived_by": "synthesise_findings@1.0",
                "confidence": "medium", "derived_at": "2026-09-23T10:00:01Z", "decision": "d_01JA0D06SYN" } ],
            "decisions": [
                { "id": "d_01JA0D02PIN", "kind": "pin", "agent": "research_lens@1.0", "action": "research_merge",
                  "rationale": "pinned", "targets": [format!("{doc}#facts[f_01JA0B3P4Q]")],
                  "cites": ["src_4f2a"], "reasoning": reasoning("Pinned.", &["src_4f2a"]) },
                { "id": "d_01JA0D03CON", "kind": "contest", "agent": "research_lens@1.0", "action": "open_contest",
                  "rationale": "two values", "targets": [format!("{doc}#selection.contested[ct_share]")],
                  "cites": ["src_77aa"], "reasoning": reasoning("Opened a contest.", &["src_77aa"]) },
                { "id": "d_01JA0D06SYN", "kind": "finding", "agent": "research_lens@1.0",
                  "action": "proposed a finding", "rationale": "two pins agree",
                  "targets": [format!("{doc}#findings[fi_01JA0F2B]")],
                  "cites": ["f_01JA0B3P4Q", "f_01JA0B3P5R"],
                  "reasoning": reasoning("Proposed a finding.", &["f_01JA0B3P4Q"]) } ]
        },
        "trace": {}
    });
    let env = json!({ "ok": true, "status": 200, "endpoint": "http://m", "data": reply, "error": null });
    let (out, _) = session.settle_middleware(session.ctx(), env);
    assert_eq!(out["data"]["change"]["applied"], true, "{out}");
    Fixture {
        _dir: dir,
        session,
        id,
        doc,
    }
}

fn on_disk(f: &Fixture) -> ClanFile {
    ClanFile::open(f.id.as_str()).unwrap()
}

fn yaml(f: &Fixture, path: &str) -> Value {
    let y: serde_yaml::Value =
        serde_yaml::from_slice(&on_disk(f).read_entry(path).unwrap()).unwrap();
    serde_json::to_value(y).unwrap()
}

fn chain(f: &Fixture) -> DecisionChain {
    DecisionChain::from_yaml(&on_disk(f).read_entry("agent/decision-chain.yaml").unwrap()).unwrap()
}

fn finding(f: &Fixture) -> Value {
    yaml(f, "shared/findings.yaml")["findings"][0].clone()
}

fn verdict(target: &str, polarity: &str, rationale: &str) -> Verdict {
    Verdict::parse(&json!({ "target": target, "polarity": polarity, "rationale": rationale }).to_string())
        .unwrap()
}

/// Run `op` as the session's person; the error status when it refuses.
fn run(
    f: &Fixture,
    op: impl FnOnce(&Ctx, &napkin_host::Document) -> napkin_host::HostResult<napkin_host::Outcome>,
) -> Result<Value, u16> {
    let before = std::fs::read(f.id.as_str()).unwrap();
    match f.session.perform(f.session.ctx(), op) {
        Ok(done) => Ok(done.reply),
        Err(e) => {
            assert_eq!(std::fs::read(f.id.as_str()).unwrap(), before, "a refusal writes nothing");
            Err(e.status)
        }
    }
}

#[test]
fn a_bad_verdict_on_a_finding_rejects_it_with_the_reason() {
    let f = fixture();
    let reply = run(&f, |c, d| {
        review::verdict(c, d, verdict("findings[fi_01JA0F2B]", "bad", "The growth figure is value, not volume."))
    })
    .unwrap();

    let fi = finding(&f);
    assert_eq!(fi["status"], "rejected");
    assert_eq!(fi["rejection"]["reason"], "The growth figure is value, not volume.");
    assert_eq!(fi["rejection"]["by"], f.session.ctx().actor.as_str());
    assert_eq!(fi["rejection"]["decision"], reply["decision"]);
    let d = &chain(&f).decisions[0];
    assert_eq!(d.id.as_deref(), reply["decision"].as_str());
    assert_eq!(d.kind.as_deref(), Some("verdict"));
    assert_eq!(d.polarity.as_deref(), Some("bad"));
    assert_eq!(d.actor.as_deref(), Some(f.session.ctx().actor.as_str()));
    assert_eq!(d.targets, vec![format!("{}#findings[fi_01JA0F2B]", f.doc)]);
    // The projection follows the member.
    let data = yaml(&f, "shared/data.yaml");
    assert_eq!(data["projection"]["findings"]["fi_01JA0F2B"]["status"], "rejected");
    let report = clan_sdk::validate(&on_disk(&f));
    assert!(report.is_valid(), "{}", report.display());

    // Once decided, it is decided.
    assert_eq!(
        run(&f, |c, d| review::verdict(c, d, verdict("findings[fi_01JA0F2B]", "bad", "again"))),
        Err(409)
    );
}

#[test]
fn a_finding_is_verified_not_marked_good_and_a_bad_mark_needs_a_reason() {
    let f = fixture();
    assert_eq!(
        run(&f, |c, d| review::verdict(c, d, verdict("findings[fi_01JA0F2B]", "good", ""))),
        Err(400)
    );
    let e = Verdict::parse(&json!({ "target": "facts[f_01JA0B3P4Q]", "polarity": "bad" }).to_string())
        .unwrap_err();
    assert_eq!(e.status, 400);
    // A good mark on a pin is recorded, with nothing else changed.
    let before = yaml(&f, "shared/facts.yaml");
    run(&f, |c, d| review::verdict(c, d, verdict("facts[f_01JA0B3P4Q]", "good", ""))).unwrap();
    let d = &chain(&f).decisions[0];
    assert_eq!(d.polarity.as_deref(), Some("good"));
    assert_eq!(d.targets, vec![format!("{}#facts[f_01JA0B3P4Q]", f.doc)]);
    assert_eq!(yaml(&f, "shared/facts.yaml"), before);
    // Something the document does not hold is not a target.
    assert_eq!(
        run(&f, |c, d| review::verdict(c, d, verdict("facts[f_01NOPE0001]", "good", ""))),
        Err(404)
    );
}

#[test]
fn a_confidential_mark_records_where_it_may_travel() {
    let f = fixture();
    let input = Classify::parse(
        &json!({ "target": format!("{}#facts[f_01JA0B3P4Q]", f.doc), "model": true,
                 "export": false, "corpus": false, "rationale": "Pricing is under NDA until launch" })
            .to_string(),
    )
    .unwrap();
    run(&f, |c, d| review::classify(c, d, input)).unwrap();
    let d = &chain(&f).decisions[0];
    assert_eq!(d.kind.as_deref(), Some("classify"));
    let l = d.licence.as_ref().unwrap();
    assert_eq!((l.model, l.export, l.corpus), (Some(true), Some(false), Some(false)));
    assert_eq!(d.rationale, "Pricing is under NDA until launch");
    // Without a reason, or all three flags, it is not a mark.
    assert!(Classify::parse(r#"{"target":"facts[f_01JA0B3P4Q]","model":true,"export":false,"corpus":false}"#).is_err());
    assert!(Classify::parse(r#"{"target":"facts[f_01JA0B3P4Q]","model":true,"rationale":"x"}"#).is_err());
}

#[test]
fn resolving_a_contest_pins_the_pick_and_marks_the_pin_it_replaces() {
    let f = fixture();
    let input = Resolve::parse(
        r#"{"contest":"ct_share","chosen":"f_01JA0B9Z9Z","rationale":"Retail-only is the market we enter"}"#,
    )
    .unwrap();
    let reply = run(&f, |c, d| review::resolve(c, d, input)).unwrap();

    let data = yaml(&f, "shared/data.yaml");
    let ct = &data["selection"]["contested"][0];
    assert_eq!(ct["status"], "resolved");
    assert_eq!(ct["chosen"], "f_01JA0B9Z9Z");
    assert_eq!(ct["reason"], "Retail-only is the market we enter");
    assert_eq!(ct["decided_by"], reply["decision"]);

    let facts = yaml(&f, "shared/facts.yaml")["facts"].clone();
    let by = |id: &str| facts.as_array().unwrap().iter().find(|p| p["id"] == id).cloned().unwrap();
    assert_eq!(by("f_01JA0B9Z9Z")["value"], 0.19, "the pick is pinned, as frozen");
    assert_eq!(by("f_01JA0B3P4Q")["replaced_by"]["fact_id"], "f_01JA0B9Z9Z");
    assert_eq!(by("f_01JA0B3P4Q")["value"], 0.23, "the replaced pin keeps its value");
    assert!(by("f_01JA0B3P5R").get("replaced_by").is_none(), "another identity is untouched");

    let d = &chain(&f).decisions[0];
    assert_eq!(d.kind.as_deref(), Some("resolve"));
    assert!(d.cites.contains(&"f_01JA0B9Z9Z".to_string()));
    assert!(d.cites.contains(&"d_01JA0D03CON".to_string()));
    let report = clan_sdk::validate(&on_disk(&f));
    assert!(report.is_valid(), "{}", report.display());

    // It is resolved now; and a value that is not one of its own is refused.
    let again = Resolve::parse(r#"{"contest":"ct_share","chosen":"f_01JA0B3P4Q","rationale":"x"}"#).unwrap();
    assert_eq!(run(&f, |c, d| review::resolve(c, d, again)), Err(409));
}

#[test]
fn keeping_the_pinned_value_pins_nothing_new() {
    let f = fixture();
    let before = yaml(&f, "shared/facts.yaml");
    let input =
        Resolve::parse(r#"{"contest":"ct_share","chosen":"f_01JA0B3P4Q","rationale":"The panel is retail value"}"#)
            .unwrap();
    run(&f, |c, d| review::resolve(c, d, input)).unwrap();
    assert_eq!(yaml(&f, "shared/facts.yaml"), before);
    let wrong = Resolve::parse(r#"{"contest":"ct_nope","chosen":"f_01JA0B3P4Q","rationale":"x"}"#).unwrap();
    assert_eq!(run(&f, |c, d| review::resolve(c, d, wrong)), Err(404));
}

fn synthesis_pin(decision: &str) -> Value {
    let mut p = pin("f_01JB5YN7HS", 0.0, "category");
    p["key"] = json!("synthesis.fi_01ja0f2b");
    p["value"] = json!("Frozen is growing fast from a small share");
    p["unit"] = json!("text");
    p["method"] = json!("synthesis");
    p["sources"] = json!(["src_human01", "f_01JA0B3P4Q", "f_01JA0B3P5R"]);
    p["decision"] = json!(decision);
    p
}

#[test]
fn verifying_pins_what_the_layer_wrote_under_the_same_decision() {
    let f = fixture();
    let ask = f
        .session
        .read(|d| review::verify_request(f.session.ctx(), d, "fi_01JA0F2B"))
        .unwrap();
    assert_eq!(ask["task"], "verify_finding");
    assert_eq!(ask["input"]["by"], f.session.ctx().actor.as_str());
    let did = ask["input"]["decision_id"].as_str().unwrap().to_string();

    // A pin the middleware wrote under some other decision is not this one.
    let stray = synthesis_pin("d_01JBOTHER01");
    assert_eq!(
        run(&f, |c, d| review::verify_finding(c, d, "fi_01JA0F2B", "", &did, &stray)),
        Err(502)
    );

    let p = synthesis_pin(&did);
    run(&f, |c, d| review::verify_finding(c, d, "fi_01JA0F2B", "Checked both tables", &did, &p)).unwrap();
    let fi = finding(&f);
    assert_eq!(fi["status"], "verified");
    assert_eq!(fi["verification"]["fact_id"], "f_01JB5YN7HS");
    assert_eq!(fi["verification"]["decision"], did);
    let d = &chain(&f).decisions[0];
    assert_eq!(d.id.as_deref(), Some(did.as_str()));
    assert_eq!(d.kind.as_deref(), Some("verify"));
    assert_eq!(d.rationale, "Checked both tables");
    assert!(yaml(&f, "shared/facts.yaml")["facts"]
        .as_array()
        .unwrap()
        .iter()
        .any(|x| x["id"] == "f_01JB5YN7HS"));
    // A verified finding is not asked about again.
    assert_eq!(
        f.session
            .read(|d| review::verify_request(f.session.ctx(), d, "fi_01JA0F2B"))
            .unwrap_err()
            .status,
        409
    );
}

#[test]
fn only_a_person_decides() {
    let f = fixture();
    let bot = Ctx::new(Actor::process("job_7").unwrap());
    let before = std::fs::read(f.id.as_str()).unwrap();
    let e = f
        .session
        .perform(&bot, |c, d| review::verdict(c, d, verdict("facts[f_01JA0B3P4Q]", "good", "")))
        .unwrap_err();
    assert_eq!(e.status, 403);
    assert_eq!(std::fs::read(f.id.as_str()).unwrap(), before);
}

#[test]
fn lock_waits_for_everything_open_then_holds() {
    let f = fixture();
    // An unverified finding and an open contest stand in the way.
    let e = f
        .session
        .perform(f.session.ctx(), |c, d| review::approve(c, d, ""))
        .unwrap_err();
    assert_eq!(e.status, 409);
    assert!(e.message.contains("2 things still need a person"), "{}", e.message);

    run(&f, |c, d| review::verdict(c, d, verdict("findings[fi_01JA0F2B]", "bad", "Wrong base"))).unwrap();
    let input =
        Resolve::parse(r#"{"contest":"ct_share","chosen":"f_01JA0B3P4Q","rationale":"Retail value"}"#).unwrap();
    run(&f, |c, d| review::resolve(c, d, input)).unwrap();

    let version = f.session.current_version().unwrap();
    run(&f, |c, d| review::approve(c, d, "")).unwrap();
    let d = &chain(&f).decisions[0];
    assert_eq!(d.kind.as_deref(), Some("approve"));
    assert_eq!(d.version.as_deref(), Some(version.as_str()), "it names what it accepted");

    // Locked: nothing more is decided on this version.
    assert_eq!(
        run(&f, |c, d| review::verdict(c, d, verdict("facts[f_01JA0B3P4Q]", "good", ""))),
        Err(409)
    );
    assert_eq!(run(&f, |c, d| review::approve(c, d, "")), Err(409));
}

#[test]
fn the_routes_carry_the_decisions() {
    let f = fixture();
    let post = |path: &str, body: Value| {
        handle(&f.session, &NoConfig, HostRequest::new(path, "", body.to_string().into_bytes()))
    };
    let r = post("/verdict", json!({ "target": "findings[fi_01JA0F2B]", "polarity": "bad", "rationale": "No" }));
    assert_eq!(r.status, 200, "{}", String::from_utf8_lossy(&r.body));
    assert_eq!(finding(&f)["status"], "rejected");
    let r = post("/classify", json!({ "target": "facts[f_01JA0B3P4Q]", "model": false, "export": false,
                                      "corpus": false, "rationale": "Client data" }));
    assert_eq!(r.status, 200);
    let r = post("/resolve", json!({ "contest": "ct_share", "chosen": "f_01JA0B9Z9Z", "rationale": "Newer" }));
    assert_eq!(r.status, 200);
    let r = post("/approve", json!({}));
    assert_eq!(r.status, 200, "{}", String::from_utf8_lossy(&r.body));
    let r = post("/verdict", json!({ "target": "facts[f_01JA0B3P4Q]", "polarity": "sideways" }));
    assert_eq!(r.status, 400);
}
