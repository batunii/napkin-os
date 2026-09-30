// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! A client's answer to a locked document (Contract 4 §7.5, §8.2): recorded
//! by staff on the locked version, each part's state by its own value hash, a
//! rejection blocking the next lock until the part is edited, and "Make this
//! change" reopening one part.

#![recursion_limit = "256"]

use std::sync::Arc;

use clan_sdk::{ClanFile, DecisionChain};
use napkin_host::ops::client_review::{self as cr, ClientReview, Ellis};
use napkin_host::ops::decisions::{decisions, DecisionsView};
use napkin_host::ops::review;
use napkin_host::{handle, Actor, Ctx, DocId, FsStore, HostRequest, NoConfig, Session};
use serde_json::{json, Value};

const SMP: &str = "single_minded_proposition";
const SAID: &str = "Honestly this isn't the brief we talked about. The summer line doesn't feel like us.";

struct Fixture {
    _dir: tempfile::TempDir,
    session: Session,
    id: DocId,
    doc: String,
}

/// A brief with three parts, locked: what a client is sent.
fn locked() -> Fixture {
    let f = open();
    f.post_ok("/approve", json!({}));
    f
}

fn open() -> Fixture {
    let dir = tempfile::tempdir().unwrap();
    let id = DocId::from(dir.path().join("brief.clan"));
    let bytes = clan_sdk::create(clan_sdk::CreateOptions {
        title: "Brief".into(),
        brief: "a brief".into(),
        document_type: None,
        no_render: false,
        schema: None,
    })
    .unwrap();
    std::fs::write(id.as_str(), bytes).unwrap();
    let ctx = Ctx::new(Actor::human("aoife").unwrap());
    let session = Session::with_ctx(Arc::new(FsStore::new(dir.path().to_path_buf())), ctx);
    session.open(id.clone()).unwrap();
    let doc = session.clan_context_for_agent()["id"].as_str().unwrap().to_string();
    let f = Fixture { _dir: dir, session, id, doc };
    f.post_ok(
        "/patch-data",
        json!({ "agent": "human", "patch": {
            SMP: "Summer tastes better without the hangover.",
            "audience": { "primary": "Adults 25-40", "commercial": "Grocery shoppers" },
            "tone": "Warm, dry, a little wry",
        } }),
    );
    f
}

impl Fixture {
    fn post(&self, path: &str, body: Value) -> (u16, Value) {
        let r = handle(&self.session, &NoConfig, HostRequest::new(path, "", body.to_string().into_bytes()));
        (r.status, serde_json::from_slice(&r.body).unwrap_or(Value::Null))
    }

    fn post_ok(&self, path: &str, body: Value) -> Value {
        let (status, v) = self.post(path, body);
        assert_eq!(status, 200, "{path}: {v}");
        v
    }

    /// The status a refused request answers with, having written nothing.
    fn refused(&self, path: &str, body: Value) -> u16 {
        let before = std::fs::read(self.id.as_str()).unwrap();
        let (status, v) = self.post(path, body);
        assert_ne!(status, 200, "{path} should be refused: {v}");
        assert_eq!(std::fs::read(self.id.as_str()).unwrap(), before, "a refusal writes nothing");
        status
    }

    fn view(&self) -> DecisionsView {
        self.session.read(decisions).unwrap()
    }

    fn json_view(&self) -> Value {
        serde_json::to_value(self.view()).unwrap()
    }

    fn chain(&self) -> DecisionChain {
        let clan = ClanFile::open(self.id.as_str()).unwrap();
        DecisionChain::from_yaml(&clan.read_entry("agent/decision-chain.yaml").unwrap()).unwrap()
    }

    fn part(&self, path: &str) -> Value {
        let v = self.json_view();
        v["client"]["parts"]
            .as_array()
            .unwrap()
            .iter()
            .find(|p| p["address"] == format!("{}#{path}", self.doc))
            .cloned()
            .unwrap_or(Value::Null)
    }

    fn codes(&self) -> Vec<(String, bool)> {
        self.view()
            .attention
            .iter()
            .filter(|a| a.code.starts_with("client_"))
            .map(|a| (a.code.to_string(), a.blocks_lock))
            .collect()
    }

    /// Stamp decision `id` with `ts` on disk, as a wrong clock would have
    /// written it, and open the document again. Its place in the chain is
    /// left alone.
    fn restamp(&self, id: &str, ts: &str) {
        let clan = ClanFile::open(self.id.as_str()).unwrap();
        let mut chain = self.chain();
        chain.decisions.iter_mut().find(|d| d.id.as_deref() == Some(id)).expect("in the chain").timestamp = ts.into();
        let mut b = clan_sdk::ClanBuilder::new(clan.manifest().clone());
        for (path, bytes) in clan.read_all_entries().unwrap() {
            match path.as_str() {
                clan_sdk::MANIFEST_PATH => {}
                "agent/decision-chain.yaml" => b.add_entry(path, chain.to_yaml().unwrap()),
                _ => b.add_entry(path, bytes),
            }
        }
        std::fs::write(self.id.as_str(), b.build().unwrap()).unwrap();
        self.session.open(self.id.clone()).unwrap();
    }

    fn head(&self) -> String {
        self.chain().decisions[0].id.clone().unwrap()
    }

    fn edit(&self, path: &str, value: &str, answers: Option<&str>) -> (u16, Value) {
        let mut body = json!({ "path": path, "value": value, "rationale": "Jane Murphy asked for it" });
        if let Some(a) = answers {
            body["answers"] = json!(a);
        }
        self.post("/edit", body)
    }
}

fn parts() -> Value {
    json!([
        { "address": SMP, "label": "Single-minded proposition" },
        { "address": "audience", "label": "Audience" },
        { "address": "tone", "label": "Tone" },
    ])
}

fn review_body(answer: &str, marked: Value) -> Value {
    json!({
        "answer": answer,
        "client": { "name": "Jane Murphy", "email": "Jane@Acme.ie" },
        "channel": "pasted_email",
        "said": SAID,
        "parts": parts(),
        "marked": marked,
    })
}

#[test]
fn accepted_marks_every_part_accepted_on_the_locked_version() {
    let f = locked();
    let lock = f.chain().decisions[0].clone();
    let r = f.post_ok(
        "/client-review",
        json!({ "answer": "accepted", "client": { "name": "Jane Murphy" }, "channel": "none", "parts": parts() }),
    );
    assert_eq!(r["suggestions"]["status"], "none", "nothing to ask of an acceptance");

    let d = &f.chain().decisions[0];
    assert_eq!(d.kind.as_deref(), Some("client_review"));
    assert_eq!(d.action, "client_answer");
    assert_eq!(d.targets, vec![f.doc.clone()]);
    assert_eq!(d.cites, vec![lock.id.clone().unwrap()], "it cites the approve it answers");
    assert!(d.polarity.is_none(), "never a verdict");
    let seen = serde_json::to_value(&d.extra["seen"]).unwrap();
    assert_eq!(seen["version"], json!(lock.version), "tied to the locked version");
    assert_eq!(seen["parts"].as_array().unwrap().len(), 3);
    assert_eq!(serde_json::to_value(&d.extra["evidence"]).unwrap(), json!({ "strength": "weaker" }));

    let v = f.json_view();
    let ps = v["client"]["parts"].as_array().unwrap();
    assert_eq!(ps.len(), 3);
    for p in ps {
        assert_eq!(p["state"], "accepted");
        assert_eq!(p["found_by"], "document");
        assert_eq!(p["stale"], false);
        assert_eq!(p["decision"], r["decision"]);
    }
    assert_eq!(v["client"]["answer"]["current"], true);
    assert_eq!(v["client"]["answer"]["recorded_by"]["id"], "aoife");
    assert_eq!(v["client"]["available"], true);
    assert_eq!(v["lock"]["locked"], true);
    assert!(f.codes().is_empty(), "{:?}", f.codes());
    let report = clan_sdk::validate(&ClanFile::open(f.id.as_str()).unwrap());
    assert!(report.is_valid(), "{}", report.display());
}

#[test]
fn a_client_review_needs_a_locked_document() {
    let f = open();
    assert_eq!(f.refused("/client-review", review_body("rejected", json!([]))), 409);
    assert!(!f.json_view()["client"]["available"].as_bool().unwrap());
}

#[test]
fn the_recorder_is_the_person_in_ctx_and_a_body_cannot_name_one() {
    let f = locked();
    for key in ["actor", "recorded_by"] {
        let mut body = review_body("accepted", json!([]));
        body[key] = json!("client:jane");
        assert_eq!(f.refused("/client-review", body), 400, "{key}");
    }
    let r = f.post_ok("/client-review", review_body("rejected", json!([{ "address": "tone", "answer": "rejected" }])));
    for d in &f.chain().decisions[..2] {
        assert_eq!(d.actor.as_deref(), Some("human:aoife"), "the recorder, from Ctx");
    }
    let client = serde_json::to_value(&f.chain().decisions[1].extra["client"]).unwrap();
    assert_eq!(client, json!({ "name": "Jane Murphy", "email": "jane@acme.ie" }), "the client is data");
    let mut confirm = json!({ "review": r["decision"], "address": "audience", "answer": "rejected" });
    confirm["recorded_by"] = json!("human:jane");
    assert_eq!(f.refused("/client-review/confirm", confirm), 400);

    // A process records no client's answer.
    let job = Ctx::new(Actor::process("job_1").unwrap());
    let input = ClientReview::parse(&review_body("accepted", json!([])).to_string()).unwrap();
    let e = f
        .session
        .perform(&job, |c, d| cr::record(c, d, input, Ellis::Unavailable(String::new())))
        .unwrap_err();
    assert_eq!(e.status, 403);
}

#[test]
fn the_clients_words_survive_byte_for_byte() {
    let f = locked();
    let said = "  Honestly?\r\n\tThis isn't   the brief — “we talked”.\n\n- no: 3,000 € #not a comment\nnull\n  ";
    let mut body = review_body("accepted_with_changes", json!([{ "address": "tone", "answer": "accepted_with_changes" }]));
    body["said"] = json!(said);
    let r = f.post_ok("/client-review", body);
    // Enough decisions after it that compression reaches it.
    for _ in 0..6 {
        f.post_ok("/client-review", json!({ "answer": "accepted", "client": { "name": "Jane Murphy" },
                                            "channel": "none", "parts": parts() }));
    }
    let chain = f.chain();
    let d = chain.decisions.iter().find(|d| d.id.as_deref() == r["decision"].as_str()).unwrap();
    assert_eq!(d.extra["said"].as_str(), Some(said));
    let again = DecisionChain::from_yaml(&chain.to_yaml().unwrap()).unwrap();
    let d = again.decisions.iter().find(|d| d.id.as_deref() == r["decision"].as_str()).unwrap();
    assert_eq!(d.extra["said"].as_str(), Some(said));
    let v = f.json_view();
    let answer = v["client"]["answers"].as_array().unwrap().iter().find(|a| a["decision"] == r["decision"]).unwrap();
    assert_eq!(answer["said"], said);

    // All-whitespace is no words, and a channel that needs words refuses it.
    let mut body = review_body("rejected", json!([]));
    body["said"] = json!(" \n\t ");
    assert_eq!(f.refused("/client-review", body), 400);
}

#[test]
fn accepted_with_changes_never_blocks_and_make_this_change_answers_it() {
    let f = locked();
    let r = f.post_ok("/client-review", review_body("accepted_with_changes", json!([{ "address": "audience", "answer": "accepted_with_changes" }])));
    let answer = r["parts"][0].as_str().unwrap().to_string();
    assert_eq!(f.codes(), vec![("client_change_asked".into(), false)]);
    assert_eq!(f.part("audience")["state"], "accepted_with_changes");

    let u = f.post_ok("/client-review/reopen", json!({ "answer": answer }));
    assert_eq!(u["address"], format!("{}#audience", f.doc));
    assert_eq!(u["reason"], format!("Jane Murphy asked: {SAID}"), "the words, as the reason");
    assert_eq!(f.json_view()["lock"]["reopened"][0]["answers"], answer);
    assert_eq!(f.json_view()["client"]["available"], false);
    // Reopened, nothing edited: it still does not block.
    assert!(f.view().lock.can_lock);
    let (s, _) = f.edit("audience.primary", "Adults 30-45", Some(&answer));
    assert_eq!(s, 200);
    let edit = &f.chain().decisions[0];
    assert_eq!(edit.extra["answers"].as_str(), Some(answer.as_str()));
    assert!(edit.cites.contains(&answer), "the edit cites the request it answers");
    assert_eq!(f.part("audience")["answered"], true);
    assert!(f.codes().is_empty(), "{:?}", f.codes());

    f.post_ok("/approve", json!({}));
    let v = f.json_view();
    assert_eq!(v["lock"]["reopened"], json!([]), "the new lock closes the part");
    assert_eq!(v["client"]["answer"]["current"], false, "it answered the older lock's version");
    assert_eq!(f.edit("audience.primary", "Adults", None).0, 409, "locked again");
}

#[test]
fn a_rejected_part_blocks_locking_again_until_it_is_edited_with_a_reason() {
    let f = locked();
    let r = f.post_ok("/client-review", review_body("rejected", json!([{ "address": SMP, "answer": "rejected" }])));
    let answer = r["parts"][0].as_str().unwrap().to_string();
    assert_eq!(f.codes(), vec![("client_rejected".into(), true)]);
    assert!(!f.view().lock.can_lock);
    // Locked with nothing reopened: nothing to lock again.
    assert_eq!(f.refused("/approve", json!({})), 409);
    // Only the rejected part can be reopened, and only that part edited.
    assert_eq!(f.refused("/client-review/reopen", json!({ "answer": r["decision"] })), 409);
    assert_eq!(f.edit(SMP, "Summer, lighter.", None).0, 409, "locked");
    f.post_ok("/client-review/reopen", json!({ "answer": answer }));
    assert_eq!(f.refused("/client-review/reopen", json!({ "answer": answer })), 409, "reopened already");
    assert_eq!(f.refused("/client-review", review_body("accepted", json!([]))), 409, "a part is reopened");
    assert_eq!(f.edit("tone", "Bright", None).0, 409, "another part stays locked");
    assert_eq!(f.edit(SMP, "Summer, lighter.", Some("d_other")).0, 400, "answers names the request");
    assert_eq!(f.refused("/approve", json!({})), 409, "the rejection is not answered yet");

    assert_eq!(f.edit(SMP, "Summer, lighter.", Some(&answer)).0, 200);
    let p = f.part(SMP);
    assert_eq!((p["answered"].as_bool(), p["stale"].as_bool()), (Some(true), Some(true)));
    assert!(f.codes().is_empty(), "{:?}", f.codes());
    assert_eq!(f.refused("/client-review/reopen", json!({ "answer": answer })), 409, "answered");
    f.post_ok("/approve", json!({}));
    let locks = f.chain().decisions.iter().filter(|d| d.kind.as_deref() == Some("approve")).count();
    assert_eq!(locks, 2, "the older lock is not rewritten");
}

#[test]
fn a_rejection_with_parts_unknown_blocks_until_a_part_is_confirmed_and_edited() {
    let f = locked();
    let input = ClientReview::parse(&review_body("rejected", json!([])).to_string()).unwrap();
    let ask = f.session.read(|d| cr::request(f.session.ctx(), d, &input)).unwrap().expect("Ellis is asked");
    assert_eq!(ask["task"], "find_client_parts");
    assert_eq!(ask["input"]["proof"], SAID);
    assert_eq!(ask["input"]["parts"][0]["value"], "Summer tastes better without the hangover.");
    assert_eq!(ask["input"]["parts"][1]["value"], r#"{"commercial":"Grocery shoppers","primary":"Adults 25-40"}"#);
    let smp = format!("{}#{SMP}", f.doc);
    let reply = json!({
        "api": "napkin.middleware/1", "task": "find_client_parts", "handler": "find_client_parts@1.0",
        "job": { "id": "job_x", "state": "done" }, "change": null,
        "result": { "summary": "two parts", "dropped": 0, "suggestions": [
            { "address": smp, "answer": "rejected", "quote": "The summer  line doesn't\nfeel like us." },
            { "address": "tone", "answer": "rejected", "quote": "It is too loud." },
            { "address": "budget", "answer": "rejected", "quote": "The summer line" },
            { "address": SMP, "answer": "accepted", "quote": "The summer line" },
            { "address": "audience", "answer": "sideways", "quote": "Honestly" } ] },
        "trace": { "backend": "mock-backend" }
    });
    let done = f
        .session
        .perform(f.session.ctx(), |c, d| cr::record(c, d, input, Ellis::Answered(reply)))
        .unwrap();
    let r = done.reply;
    assert_eq!(r["suggestions"]["status"], "found");
    assert_eq!(r["suggestions"]["dropped"], 4, "a quote not in the words, an unknown part, a second one on a part, a bad answer");
    let s = &f.chain().decisions[0];
    assert_eq!(s.action, "suggest_part");
    assert_eq!(s.actor.as_deref(), Some("process:middleware"));
    assert_eq!(s.handler.as_deref(), Some("find_client_parts@1.0"));
    assert_eq!(s.backend.as_deref(), Some("mock-backend"));
    assert_eq!(s.targets, vec![smp.clone()]);

    // Derived by the agent: it counts for nothing yet.
    assert_eq!(
        f.codes(),
        vec![("client_rejected_parts_unknown".into(), true), ("client_part_suggested".into(), false)]
    );
    assert_eq!(f.json_view()["client"]["parts"], json!([]));
    let suggestion = r["suggestions"]["decisions"][0].clone();
    let c = f.post_ok("/client-review/confirm", json!({ "suggestion": suggestion, "confirm": true }));
    assert_eq!(f.refused("/client-review/confirm", json!({ "suggestion": suggestion, "confirm": false })), 409);
    let p = f.part(SMP);
    assert_eq!(p["found_by"], "agent");
    assert_eq!(p["quote"], "The summer  line doesn't\nfeel like us.");
    assert_eq!(f.json_view()["client"]["answer"]["parts_known"], true);
    assert_eq!(f.codes(), vec![("client_rejected".into(), true)], "the part is known; now it wants an edit");

    let u = f.post_ok("/client-review/reopen", json!({ "answer": c["decision"] }));
    assert_eq!(u["reason"], "Jane Murphy asked: The summer line doesn't feel like us.");
    assert_eq!(f.refused("/approve", json!({})), 409);
    assert_eq!(f.edit(SMP, "Summer, but ours.", Some(c["decision"].as_str().unwrap())).0, 200);
    f.post_ok("/approve", json!({}));
}

#[test]
fn without_ellis_the_answer_is_recorded_and_a_person_marks_the_parts() {
    let f = locked();
    let r = f.post_ok("/client-review", review_body("rejected", json!([])));
    assert_eq!(r["suggestions"]["status"], "unavailable");
    assert!(r["suggestions"]["reason"].as_str().is_some_and(|s| !s.is_empty()));
    assert_eq!(f.codes(), vec![("client_rejected_parts_unknown".into(), true)]);

    let review_id = r["decision"].clone();
    assert_eq!(f.refused("/client-review/confirm", json!({ "review": review_id, "address": "budget", "answer": "rejected" })), 400);
    f.post_ok("/client-review/confirm", json!({ "review": review_id, "address": "tone", "answer": "accepted_with_changes" }));
    assert_eq!(f.refused("/client-review/confirm", json!({ "review": review_id, "address": "tone", "answer": "rejected" })), 409);
    // Known, and only a change asked: nothing blocks.
    assert_eq!(f.codes(), vec![("client_change_asked".into(), false)]);
    assert_eq!(f.part("tone")["found_by"], "person");

    // An acceptance marks every part already.
    let a = f.post_ok("/client-review", json!({ "answer": "accepted", "client": { "name": "Jane Murphy" }, "channel": "none", "parts": parts() }));
    assert_eq!(f.refused("/client-review/confirm", json!({ "review": a["decision"], "address": "tone", "answer": "rejected" })), 409);
    assert_eq!(f.part("tone")["state"], "accepted", "the newer acceptance is the part's current answer");
}

#[test]
fn a_part_goes_stale_only_when_its_own_value_changes() {
    let f = locked();
    let r = f.post_ok(
        "/client-review",
        review_body("rejected", json!([{ "address": SMP, "answer": "rejected" }, { "address": "audience", "answer": "accepted_with_changes" }])),
    );
    let smp = r["parts"][0].as_str().unwrap().to_string();
    f.post_ok("/client-review/reopen", json!({ "answer": smp }));
    assert_eq!(f.edit(SMP, "Summer, lighter.", None).0, 200);
    assert_eq!(f.part(SMP)["stale"], true);
    assert_eq!(f.part("audience")["stale"], false, "another part changing does not stale it");
    assert_eq!(f.part("tone"), Value::Null, "tone has no answer");
}

#[test]
fn parts_are_this_documents_dotted_paths_and_the_evidence_is_stated() {
    let f = locked();
    let with_parts = |p: Value| {
        let mut b = review_body("rejected", json!([]));
        b["parts"] = p;
        b
    };
    for p in [
        json!([{ "address": "sections[s_2]", "label": "Section" }]),
        json!([{ "address": "d_other#audience", "label": "Audience" }]),
        json!([{ "address": "upstream.x.audience", "label": "Audience" }]),
        json!([{ "address": "audience", "label": "A" }, { "address": "audience.commercial", "label": "B" }]),
        json!([{ "address": "audience", "label": "A" }, { "address": "audience", "label": "B" }]),
        json!([{ "address": "audience", "label": "" }]),
    ] {
        assert_eq!(f.refused("/client-review", with_parts(p.clone())), 400, "{p}");
    }
    assert_eq!(f.refused("/client-review", review_body("rejected", json!([{ "address": "budget", "answer": "rejected" }]))), 400);
    assert_eq!(f.refused("/client-review", review_body("accepted", json!([{ "address": "tone", "answer": "rejected" }]))), 400);
    let mut reasons = review_body("accepted_with_changes", json!([]));
    reasons["reasons"] = json!(["tone"]);
    assert_eq!(f.refused("/client-review", reasons), 400, "reasons are for a rejection");

    // An attached email is strong evidence; the file must be in the document.
    let mut body = json!({ "answer": "rejected", "client": { "name": "Jane Murphy" }, "reasons": ["off_brief", "tone"],
                           "channel": "file", "asset": "human/assets/re-brief.eml", "parts": parts() });
    assert_eq!(f.refused("/client-review", body.clone()), 404);
    let eml = b"From: jane@acme.ie\r\nSubject: Re: brief\r\n\r\nNot us.\r\n".to_vec();
    f.session.upload_asset("re-brief.eml", None, eml.clone()).unwrap();
    let r = f.post_ok("/client-review", body.clone());
    assert_eq!(r["suggestions"]["status"], "none", "no words to read: the host reads no .eml yet");
    let a = &f.json_view()["client"]["answer"];
    assert_eq!(a["evidence"]["strength"], "strong");
    assert_eq!(a["evidence"]["sha256"], clan_sdk::hash::sha256_prefixed(&eml));
    assert_eq!(a["reasons"], json!(["off_brief", "tone"]));
    assert_eq!(f.chain().decisions[0].rationale, "Jane Murphy rejected the document (off brief, tone), from an attached file; recorded by aoife.");
    body["asset"] = json!("human/assets/notes.txt");
    assert_eq!(f.refused("/client-review", body), 400, "an email or a PDF");
}

#[test]
fn a_value_marked_model_false_is_not_sent_to_ellis() {
    let f = open();
    f.post_ok("/classify", json!({ "target": "tone", "model": false, "export": true, "corpus": false,
                                  "rationale": "The client's own words" }));
    f.post_ok("/approve", json!({}));
    let input = ClientReview::parse(&review_body("rejected", json!([])).to_string()).unwrap();
    let ask = f.session.read(|d| cr::request(f.session.ctx(), d, &input)).unwrap().unwrap();
    assert_eq!(ask["input"]["parts"][2]["address"], format!("{}#tone", f.doc));
    assert_eq!(ask["input"]["parts"][2]["value"], Value::Null);
    assert_eq!(ask["input"]["parts"][2]["label"], "Tone", "the part is still named");
}

#[test]
fn a_part_holding_a_value_marked_model_false_is_not_sent_either() {
    let f = open();
    f.post_ok("/classify", json!({ "target": "audience.commercial", "model": false, "export": true, "corpus": false,
                                  "rationale": "The client's own numbers" }));
    f.post_ok("/approve", json!({}));
    let input = ClientReview::parse(&review_body("rejected", json!([])).to_string()).unwrap();
    let ask = f.session.read(|d| cr::request(f.session.ctx(), d, &input)).unwrap().unwrap();
    assert_eq!(ask["input"]["parts"][1]["address"], format!("{}#audience", f.doc));
    assert_eq!(ask["input"]["parts"][1]["value"], Value::Null, "the part's value carries the marked path");
    assert!(!ask.to_string().contains("Grocery shoppers"));
    assert_eq!(ask["input"]["parts"][2]["value"], "Warm, dry, a little wry", "other parts are sent");
}

#[test]
fn a_suggestion_is_dismissed_by_a_person_and_the_edit_mode_reason_falls_back() {
    let f = locked();
    let mut body = review_body("rejected", json!([]));
    body["said"] = json!("Audience is wrong.");
    let input = ClientReview::parse(&body.to_string()).unwrap();
    let reply = json!({ "api": "napkin.middleware/1", "job": { "state": "done" }, "change": null,
        "result": { "suggestions": [ { "address": "audience", "answer": "rejected", "quote": "Audience is wrong." } ] } });
    let done = f.session.perform(f.session.ctx(), |c, d| cr::record(c, d, input, Ellis::Answered(reply))).unwrap();
    let s = done.reply["suggestions"]["decisions"][0].clone();
    f.post_ok("/client-review/confirm", json!({ "suggestion": s, "confirm": false, "rationale": "She means the tone." }));
    assert_eq!(f.chain().decisions[0].action, "dismiss_part");
    assert_eq!(f.json_view()["client"]["suggestions"], json!([]));
    assert_eq!(f.codes(), vec![("client_rejected_parts_unknown".into(), true)], "a dismissal is not an answer");

    // Marked by hand with no quote: the reason is the client's words.
    let r = f.json_view()["client"]["answer"]["decision"].clone();
    let p = f.post_ok("/client-review/confirm", json!({ "review": r, "address": "tone", "answer": "rejected" }));
    let u = f.post_ok("/client-review/reopen", json!({ "answer": p["decision"] }));
    assert_eq!(u["reason"], "Jane Murphy asked: Audience is wrong.");
}

#[test]
fn a_part_marked_by_hand_settles_ellis_suggestion_on_it() {
    let f = locked();
    let mut body = review_body("rejected", json!([]));
    body["said"] = json!("Audience is wrong.");
    let input = ClientReview::parse(&body.to_string()).unwrap();
    let reply = json!({ "api": "napkin.middleware/1", "job": { "state": "done" }, "change": null,
        "result": { "suggestions": [ { "address": "audience", "answer": "rejected", "quote": "Audience is wrong." } ] } });
    let done = f.session.perform(f.session.ctx(), |c, d| cr::record(c, d, input, Ellis::Answered(reply))).unwrap();
    let s = done.reply["suggestions"]["decisions"][0].clone();
    f.post_ok("/client-review/confirm", json!({ "review": done.reply["decision"], "address": "audience",
                                                "answer": "accepted_with_changes" }));
    // Neither can be recorded now, so it is not left asking for either.
    assert_eq!(f.refused("/client-review/confirm", json!({ "suggestion": s, "confirm": true })), 409);
    assert_eq!(f.refused("/client-review/confirm", json!({ "suggestion": s, "confirm": false })), 409);
    assert_eq!(f.json_view()["client"]["suggestions"], json!([]));
    assert_eq!(f.codes(), vec![("client_change_asked".into(), false)]);
}

#[test]
fn an_ellis_reply_that_is_not_an_answer_leaves_the_review_without_suggestions() {
    let f = locked();
    let input = ClientReview::parse(&review_body("rejected", json!([])).to_string()).unwrap();
    let reply = json!({ "api": "napkin.middleware/1", "job": { "state": "done" },
                        "change": { "data_patch": { "tone": "x" } }, "result": { "suggestions": [] } });
    let done = f.session.perform(f.session.ctx(), |c, d| cr::record(c, d, input, Ellis::Answered(reply))).unwrap();
    assert_eq!(done.reply["suggestions"]["status"], "unavailable");
    assert_eq!(f.chain().decisions[0].action, "client_answer", "the answer is recorded; nothing else");
    assert_eq!(serde_json::to_value(&f.chain().decisions[0].extra["said"]).unwrap(), json!(SAID));
}

#[test]
fn the_ops_refuse_what_the_routes_refuse() {
    let f = locked();
    let e = f
        .session
        .perform(f.session.ctx(), |c, d| review::approve(c, d, ""))
        .unwrap_err();
    assert_eq!(e.status, 409, "locked, nothing reopened");
}

// ── which came first: the chain's order, never the clocks ───────────────────

const PAST: &str = "2001-01-01T00:00:00Z";
const FUTURE: &str = "2099-12-31T23:59:59Z";

#[test]
fn an_edit_stamped_before_the_answer_but_written_after_it_answers_it() {
    let f = locked();
    let r = f.post_ok("/client-review", review_body("rejected", json!([{ "address": SMP, "answer": "rejected" }])));
    let answer = r["parts"][0].as_str().unwrap().to_string();
    f.post_ok("/client-review/reopen", json!({ "answer": answer }));
    assert_eq!(f.edit(SMP, "Summer, lighter.", Some(&answer)).0, 200);
    f.restamp(&f.head(), PAST);
    assert_eq!(f.part(SMP)["answered"], true, "written after the answer, whatever its clock said");
    assert!(f.codes().is_empty(), "{:?}", f.codes());
    f.post_ok("/approve", json!({}));
}

#[test]
fn a_part_answer_stamped_in_the_future_does_not_hide_a_later_edit() {
    let f = locked();
    let r = f.post_ok("/client-review", review_body("rejected", json!([{ "address": SMP, "answer": "rejected" }])));
    let answer = r["parts"][0].as_str().unwrap().to_string();
    f.restamp(&answer, FUTURE);
    f.post_ok("/client-review/reopen", json!({ "answer": answer }));
    assert_eq!(f.edit(SMP, "Summer, lighter.", Some(&answer)).0, 200);
    let p = f.part(SMP);
    assert_eq!((p["answered"].as_bool(), p["stale"].as_bool()), (Some(true), Some(true)));
    assert_eq!(f.refused("/client-review/reopen", json!({ "answer": answer })), 409, "answered");

    // Nor does a newer answer on the part lose to it for being stamped earlier.
    let review = r["decision"].as_str().unwrap().to_string();
    f.post_ok("/approve", json!({}));
    let again = f.post_ok("/client-review", json!({ "answer": "accepted", "client": { "name": "Jane Murphy" },
                                                    "channel": "none", "parts": parts() }));
    f.restamp(again["decision"].as_str().unwrap(), PAST);
    let p = f.part(SMP);
    assert_eq!(p["state"], "accepted", "the answer written last is current");
    assert_ne!(p["review"], review);
}

#[test]
fn an_unlock_stamped_before_the_lock_but_written_after_it_reopens_the_part() {
    let f = locked();
    let r = f.post_ok("/client-review", review_body("rejected", json!([{ "address": SMP, "answer": "rejected" }])));
    let answer = r["parts"][0].as_str().unwrap().to_string();
    let u = f.post_ok("/client-review/reopen", json!({ "answer": answer }));
    f.restamp(u["decision"].as_str().unwrap(), PAST);
    let v = f.json_view();
    assert_eq!(v["lock"]["reopened"][0]["decision"], u["decision"], "after the lock, by the chain");
    assert_eq!(f.part(SMP)["reopened"], true);
    assert_eq!(f.edit(SMP, "Summer, lighter.", Some(&answer)).0, 200, "the part is open for its edit");
    f.post_ok("/approve", json!({}));
}

#[test]
fn the_newest_approve_is_the_lock_whatever_its_stamp() {
    let f = locked();
    let first = f.head();
    let r = f.post_ok("/client-review", review_body("accepted_with_changes", json!([{ "address": "tone", "answer": "accepted_with_changes" }])));
    f.post_ok("/client-review/reopen", json!({ "answer": r["parts"][0] }));
    let second = f.post_ok("/approve", json!({}))["decision"].as_str().unwrap().to_string();
    f.restamp(&second, PAST);
    f.restamp(&first, FUTURE);
    let v = f.json_view();
    assert_eq!(v["lock"]["reopened"], json!([]), "the second lock closed the part");
    assert_eq!(v["client"]["available"], true);
    assert_eq!(review::lock_of(&f.chain(), &f.doc).and_then(|d| d.id.clone()), Some(second));
    // The history is shown in the order it was written, not by the clocks.
    let shown: Vec<_> = f.view().decisions.iter().map(|b| b.decision.id.clone()).collect();
    let written: Vec<_> = f.chain().decisions.iter().map(|d| d.id.clone()).collect();
    assert_eq!(shown, written);
}

// ── locking again closes Ellis's open suggestions ───────────────────────────

#[test]
fn locking_again_closes_ellis_suggestions_nobody_confirmed() {
    let f = locked();
    let mut body = review_body("rejected", json!([]));
    body["said"] = json!("Audience is wrong. The tone is off.");
    let input = ClientReview::parse(&body.to_string()).unwrap();
    let reply = json!({ "api": "napkin.middleware/1", "job": { "state": "done" }, "change": null,
        "result": { "suggestions": [
            { "address": "audience", "answer": "rejected", "quote": "Audience is wrong." },
            { "address": "tone", "answer": "rejected", "quote": "The tone is off." } ] } });
    let done = f.session.perform(f.session.ctx(), |c, d| cr::record(c, d, input, Ellis::Answered(reply))).unwrap();
    let ids = done.reply["suggestions"]["decisions"].as_array().unwrap().clone();
    let c = f.post_ok("/client-review/confirm", json!({ "suggestion": ids[0], "confirm": true }));
    let (open, part) = (ids[1].as_str().unwrap().to_string(), c["decision"].as_str().unwrap().to_string());
    f.post_ok("/client-review/reopen", json!({ "answer": part }));
    assert_eq!(f.edit("audience", "Adults 30-45", Some(&part)).0, 200);
    assert_eq!(f.codes(), vec![("client_part_suggested".into(), false)], "still asking");

    let lock = f.post_ok("/approve", json!({}))["decision"].as_str().unwrap().to_string();
    let chain = f.chain();
    let d = &chain.decisions[0];
    assert_eq!((d.kind.as_deref(), d.action.as_str()), (Some("client_review"), "dismiss_part"));
    assert_eq!(chain.decisions[1].id.as_deref(), Some(lock.as_str()), "written after the lock");
    assert_eq!(d.actor.as_deref(), Some("human:aoife"), "the person locking");
    assert_eq!(d.extra["suggestion"].as_str(), Some(open.as_str()));
    assert_eq!(d.extra["closed_by"].as_str(), Some(lock.as_str()));
    assert_eq!(d.cites, vec![open.clone(), lock.clone()]);
    assert_eq!(d.targets, vec![format!("{}#tone", f.doc)]);
    assert!(d.rationale.starts_with("Closed by the new lock"), "{}", d.rationale);
    assert_eq!(f.json_view()["client"]["suggestions"], json!([]));
    assert!(f.codes().is_empty(), "{:?}", f.codes());
    assert_eq!(f.refused("/client-review/confirm", json!({ "suggestion": open, "confirm": true })), 409);
    let report = clan_sdk::validate(&ClanFile::open(f.id.as_str()).unwrap());
    assert!(report.is_valid(), "{}", report.display());
}
