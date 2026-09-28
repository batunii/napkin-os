// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! A person's review decisions (Contract 4 §4, §7, §8): the typed operations
//! behind the buttons on a field — mark it good or bad, mark it confidential,
//! reject or verify a finding, pick between two sources, lock the document.
//!
//! Each is `(Ctx, Document@version, input) → Outcome`, like every other
//! operation: it records one decision, attributed to the person in `Ctx`,
//! and whatever the decision changes in the document — a finding's status, a
//! contest's choice, a pin — in the same generation.
//!
//! What stays out: the knowledge layer. Verifying a finding writes it to the
//! layer as a synthesis fact, which only the middleware can do; the route
//! asks the middleware first ([`verify_request`]) and hands what it wrote to
//! [`verify_finding`], which pins it. A classify mark is recorded here; taking
//! it to the layer's licence (C4) is the middleware's, and not built yet.
//!
//! Only a person decides these. A process acting for one uses the middleware.

use clan_sdk::decision::{new_decision_id, Licence};
use clan_sdk::{Decision, DecisionChain};
use serde_json::Value;

use crate::ctx::Ctx;
use crate::document::Document;
use crate::error::{HostError, HostResult};
use crate::event::HostEvent;

use super::assemble::{assemble, data_of, Members};
use super::decisions;
use super::edit::attributed;
use super::members::{self, FACTS, FINDINGS};
use super::Outcome;

const CHAIN: &str = "agent/decision-chain.yaml";

/// The polarity values a verdict may carry.
pub const POLARITIES: &[&str] = &["good", "bad"];

// ── inputs ──────────────────────────────────────────────────────────────────

/// `POST /verdict`: a field is good or bad. On a finding, bad is its
/// rejection (Contract 3 §6.3); good is not accepted there — a finding is
/// verified with `/verify`, which writes it to the layer.
#[derive(Debug, Clone)]
pub struct Verdict {
    pub target: String,
    pub polarity: String,
    pub reason_code: Option<String>,
    pub rationale: String,
}

/// `POST /classify`: where the target may travel (C2).
#[derive(Debug, Clone)]
pub struct Classify {
    pub target: String,
    pub model: bool,
    pub export: bool,
    pub corpus: bool,
    pub rationale: String,
}

/// `POST /resolve`: pick one value of an open contest.
#[derive(Debug, Clone)]
pub struct Resolve {
    pub contest: String,
    pub chosen: String,
    pub rationale: String,
}

fn body(raw: &str) -> HostResult<Value> {
    let v: Value = serde_json::from_str(raw)
        .map_err(|e| HostError::bad_request(format!("invalid JSON: {e}")))?;
    if !v.is_object() {
        return Err(HostError::bad_request("the body must be an object"));
    }
    Ok(v)
}

fn text(v: &Value, key: &str) -> String {
    v.get(key)
        .and_then(Value::as_str)
        .map(str::trim)
        .unwrap_or_default()
        .to_string()
}

fn required(v: &Value, key: &str) -> HostResult<String> {
    let t = text(v, key);
    if t.is_empty() {
        return Err(HostError::bad_request(format!("`{key}` is required")));
    }
    Ok(t)
}

fn flag(v: &Value, key: &str) -> HostResult<bool> {
    v.get(key)
        .and_then(Value::as_bool)
        .ok_or_else(|| HostError::bad_request(format!("`{key}` must be true or false")))
}

impl Verdict {
    pub fn parse(raw: &str) -> HostResult<Self> {
        let v = body(raw)?;
        let polarity = required(&v, "polarity")?;
        if !POLARITIES.contains(&polarity.as_str()) {
            return Err(HostError::bad_request("`polarity` is good or bad"));
        }
        let rationale = text(&v, "rationale");
        if polarity == "bad" && rationale.is_empty() {
            return Err(HostError::bad_request(
                "a bad verdict needs a rationale: say what is wrong",
            ));
        }
        Ok(Self {
            target: required(&v, "target")?,
            polarity,
            reason_code: Some(text(&v, "reason_code")).filter(|s| !s.is_empty()),
            rationale,
        })
    }
}

impl Classify {
    pub fn parse(raw: &str) -> HostResult<Self> {
        let v = body(raw)?;
        Ok(Self {
            target: required(&v, "target")?,
            model: flag(&v, "model")?,
            export: flag(&v, "export")?,
            corpus: flag(&v, "corpus")?,
            rationale: required(&v, "rationale")?,
        })
    }
}

impl Resolve {
    pub fn parse(raw: &str) -> HostResult<Self> {
        let v = body(raw)?;
        Ok(Self {
            contest: required(&v, "contest")?,
            chosen: required(&v, "chosen")?,
            rationale: required(&v, "rationale")?,
        })
    }
}

/// `POST /verify`: `{finding, rationale?}`.
pub fn parse_verify(raw: &str) -> HostResult<(String, String)> {
    let v = body(raw)?;
    Ok((required(&v, "finding")?, text(&v, "rationale")))
}

/// `POST /approve`: `{rationale?}`.
pub fn parse_approve(raw: &str) -> HostResult<String> {
    let v = if raw.trim().is_empty() {
        Value::Object(Default::default())
    } else {
        body(raw)?
    };
    Ok(text(&v, "rationale"))
}

// ── shared checks ───────────────────────────────────────────────────────────

/// The actor, when it is a person.
fn person(ctx: &Ctx, what: &str) -> HostResult<String> {
    if !ctx.actor.is_human() {
        return Err(HostError::new(
            403,
            format!("only a person can {what}; {} is not one", ctx.actor),
        ));
    }
    Ok(ctx.actor.to_string())
}

fn chain_of(doc: &Document) -> HostResult<DecisionChain> {
    let clan = doc.clan();
    Ok(if clan.has_entry(CHAIN) {
        DecisionChain::from_yaml(&clan.read_entry(CHAIN)?)?
    } else {
        DecisionChain::default()
    })
}

/// The lock that holds, if one does: an `approve` nothing has superseded.
fn lock_of(chain: &DecisionChain) -> Option<&Decision> {
    chain
        .decisions
        .iter()
        .find(|d| d.kind.as_deref() == Some("approve") && d.superseded_by.is_none())
}

fn not_locked(doc: &Document) -> HostResult<()> {
    match lock_of(&chain_of(doc)?) {
        Some(d) => Err(HostError::conflict(format!(
            "the document was locked by {} at {}; changes make a new version",
            d.actor.as_deref().unwrap_or(&d.agent),
            d.timestamp
        ))),
        None => Ok(()),
    }
}

/// `path` as a full address on this document, whether it came with the
/// document id or without.
fn address(doc_id: &str, path: &str) -> String {
    match path.split_once('#') {
        Some((_, p)) => format!("{doc_id}#{p}"),
        None => format!("{doc_id}#{path}"),
    }
}

/// The `[id]` of a `name[id]` path.
fn keyed<'a>(path: &'a str, name: &str) -> Option<&'a str> {
    path.strip_prefix(name)?
        .strip_prefix('[')?
        .strip_suffix(']')
}

/// The path part of a target, and a check that it names something the
/// document holds: a pin, a finding, a material, a contest, or a data path.
fn target_path(doc: &Document, target: &str) -> HostResult<String> {
    let clan = doc.clan();
    let path = match target.split_once('#') {
        Some((d, p)) if d == clan.document_id() => p,
        Some((d, _)) => {
            return Err(HostError::bad_request(format!(
                "target {target} is on document {d}, not this one"
            )))
        }
        None => target,
    }
    .to_string();
    let data = data_of(clan)?;
    let found = if let Some(id) = keyed(&path, "facts") {
        members::read_list(clan, FACTS)?
            .iter()
            .any(|e| members::entry_id(e) == Some(id))
    } else if let Some(id) = keyed(&path, "findings") {
        members::read_list(clan, FINDINGS)?
            .iter()
            .any(|e| members::entry_id(e) == Some(id))
    } else if let Some(id) = keyed(&path, "materials") {
        data.get("materials").and_then(|m| m.get(id)).is_some()
    } else if let Some(id) = keyed(&path, "selection.contested") {
        contest_index(&data, id).is_some()
    } else {
        path.split('.')
            .try_fold(&data, |v, k| v.get(k))
            .is_some()
    };
    if !found {
        return Err(HostError::not_found(format!(
            "target {path} is not in this document"
        )));
    }
    Ok(path)
}

fn contest_index(data: &Value, id: &str) -> Option<usize> {
    data.pointer("/selection/contested")?
        .as_array()?
        .iter()
        .position(|c| c.get("id").and_then(Value::as_str) == Some(id))
}

/// A decision by `who`, of `kind`, about `targets`.
#[allow(clippy::too_many_arguments)]
fn decided(
    ctx: &Ctx,
    who: &str,
    kind: &str,
    action: &str,
    targets: Vec<String>,
    cites: Vec<String>,
    rationale: String,
    now: &str,
) -> Decision {
    let mut d = attributed(ctx, "", kind);
    d.id = Some(new_decision_id());
    d.agent = who.to_string();
    d.action = action.to_string();
    d.targets = targets;
    d.cites = cites;
    d.rationale = rationale;
    d.timestamp = now.to_string();
    d
}

fn to_yaml(v: &Value) -> HostResult<serde_yaml::Value> {
    serde_yaml::to_value(v).map_err(|e| HostError::internal(e.to_string()))
}

fn to_json(v: &serde_yaml::Value) -> Value {
    serde_json::to_value(v).unwrap_or(Value::Null)
}

fn finding_index(findings: &[serde_yaml::Value], id: &str) -> HostResult<usize> {
    findings
        .iter()
        .position(|e| members::entry_id(e) == Some(id))
        .ok_or_else(|| HostError::not_found(format!("finding {id} is not in this document")))
}

fn still_proposed(f: &Value, id: &str) -> HostResult<()> {
    match f.get("status").and_then(Value::as_str) {
        Some("proposed") => Ok(()),
        Some(other) => Err(HostError::conflict(format!("finding {id} is already {other}"))),
        None => Err(HostError::internal(format!("finding {id} has no status"))),
    }
}

/// Write one generation with `decision` and whatever it changed, and say so.
fn commit(
    doc: &Document,
    data: Value,
    m: Members,
    decision: Decision,
    delta: &str,
    now: &str,
) -> HostResult<Outcome> {
    let id = decision.id.clone().unwrap_or_default();
    let kind = decision.kind.clone().unwrap_or_default();
    let targets = decision.targets.clone();
    let bytes = assemble(doc.clan(), data, m, vec![decision], delta, now, false)?;
    let notice = serde_json::json!({ "ok": true, "source": "review", "kind": kind, "decision": id });
    let change = doc
        .change(bytes)?
        .with_event(HostEvent::DataChanged(notice));
    Ok(Outcome::changed(
        serde_json::json!({ "ok": true, "decision": id, "kind": kind, "targets": targets }),
        change,
    ))
}

// ── the operations ──────────────────────────────────────────────────────────

/// A good or bad mark on a field; a bad one on a finding rejects it.
pub fn verdict(ctx: &Ctx, doc: &Document, input: Verdict) -> HostResult<Outcome> {
    let who = person(ctx, "mark a field good or bad")?;
    not_locked(doc)?;
    let path = target_path(doc, &input.target)?;
    let clan = doc.clan();
    let doc_id = clan.document_id().to_string();
    let now = now();
    let mut m = Members::of(clan)?;

    if let Some(id) = keyed(&path, "findings") {
        if input.polarity == "good" {
            return Err(HostError::bad_request(
                "a finding is not marked good: verify it, so it is written to the layer as reviewed",
            ));
        }
        // Rejection (Contract 3 §6.3): the finding stays, struck, with why.
        let i = finding_index(&m.findings, id)?;
        let mut f = to_json(&m.findings[i]);
        still_proposed(&f, id)?;
        let mut d = decided(
            ctx,
            &who,
            "verdict",
            "reject_finding",
            vec![address(&doc_id, &path)],
            vec![id.to_string()],
            input.rationale.clone(),
            &now,
        );
        d.polarity = Some("bad".into());
        d.reason_code = input.reason_code.clone();
        f["status"] = "rejected".into();
        f["rejection"] = serde_json::json!({
            "decision": d.id, "by": who, "at": now, "reason": input.rationale,
        });
        m.findings[i] = to_yaml(&f)?;
        return commit(doc, data_of(clan)?, m, d, "a finding rejected", &now);
    }

    let rationale = if input.rationale.is_empty() {
        "Marked good.".to_string()
    } else {
        input.rationale.clone()
    };
    let mut d = decided(
        ctx,
        &who,
        "verdict",
        &format!("mark_{}", input.polarity),
        vec![address(&doc_id, &path)],
        Vec::new(),
        rationale,
        &now,
    );
    d.polarity = Some(input.polarity.clone());
    d.reason_code = input.reason_code;
    commit(doc, data_of(clan)?, m, d, "a verdict", &now)
}

/// A confidentiality mark: whether the target may reach a model, appear in an
/// export, enter the agency's knowledge.
pub fn classify(ctx: &Ctx, doc: &Document, input: Classify) -> HostResult<Outcome> {
    let who = person(ctx, "mark something confidential")?;
    not_locked(doc)?;
    let path = target_path(doc, &input.target)?;
    let clan = doc.clan();
    let now = now();
    let mut d = decided(
        ctx,
        &who,
        "classify",
        "classify",
        vec![address(clan.document_id(), &path)],
        Vec::new(),
        input.rationale,
        &now,
    );
    d.licence = Some(Licence {
        model: Some(input.model),
        export: Some(input.export),
        corpus: Some(input.corpus),
        ..Default::default()
    });
    commit(doc, data_of(clan)?, Members::of(clan)?, d, "a classify mark", &now)
}

/// Pick one value of an open contest. The chosen value is pinned when it is
/// not already; a pin it replaces stays in the facts member, marked
/// `replaced_by`, so every decision that cited it still resolves.
pub fn resolve(ctx: &Ctx, doc: &Document, input: Resolve) -> HostResult<Outcome> {
    let who = person(ctx, "resolve a contest")?;
    not_locked(doc)?;
    let clan = doc.clan();
    let doc_id = clan.document_id().to_string();
    let now = now();
    let mut data = data_of(clan)?;
    let i = contest_index(&data, &input.contest).ok_or_else(|| {
        HostError::not_found(format!("contest {} is not in this document", input.contest))
    })?;
    let contest = data["selection"]["contested"][i].clone();
    if contest.get("status").and_then(Value::as_str) != Some("open") {
        return Err(HostError::conflict(format!(
            "contest {} is already resolved",
            input.contest
        )));
    }
    let values = contest
        .get("values")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    let chosen = values
        .iter()
        .find(|v| v.get("fact_id").and_then(Value::as_str) == Some(input.chosen.as_str()))
        .ok_or_else(|| {
            HostError::bad_request(format!(
                "{} is not one of contest {}'s values",
                input.chosen, input.contest
            ))
        })?;

    let mut m = Members::of(clan)?;
    let d_id = new_decision_id();
    let mut targets = vec![address(&doc_id, &format!("selection.contested[{}]", input.contest))];
    let held = m
        .facts
        .iter()
        .any(|e| members::entry_id(e) == Some(input.chosen.as_str()));
    if !held {
        // The chosen value is a layer row the research wrote as contested; the
        // research froze its pin beside the value for exactly this.
        let pin = chosen.get("pin").filter(|p| p.is_object()).ok_or_else(|| {
            HostError::conflict(format!(
                "{} carries no pin to take; rerun the research for it",
                input.chosen
            ))
        })?;
        if pin.get("id").and_then(Value::as_str) != Some(input.chosen.as_str()) {
            return Err(HostError::bad_request(format!(
                "the pin carried for {} names another fact",
                input.chosen
            )));
        }
        // A pin of the same identity the person did not choose is replaced.
        let ident = |f: &Value| {
            (
                f.get("entity").cloned(),
                f.get("key").cloned(),
                f.get("market").cloned(),
            )
        };
        let id_new = ident(pin);
        for e in m.facts.iter_mut() {
            let f = to_json(e);
            if ident(&f) == id_new && f.get("replaced_by").is_none() {
                let mut f = f;
                f["replaced_by"] = serde_json::json!({ "fact_id": input.chosen, "decision": d_id });
                *e = to_yaml(&f)?;
                if let Some(old) = f.get("id").and_then(Value::as_str) {
                    targets.push(address(&doc_id, &format!("facts[{old}]")));
                }
            }
        }
        m.facts.push(to_yaml(pin)?);
        targets.push(address(&doc_id, &format!("facts[{}]", input.chosen)));
    }

    let entry = &mut data["selection"]["contested"][i];
    entry["status"] = "resolved".into();
    entry["chosen"] = input.chosen.clone().into();
    entry["reason"] = input.rationale.clone().into();
    entry["decided_by"] = d_id.clone().into();

    let mut cites = vec![input.chosen.clone()];
    if let Some(o) = contest.get("opened_by").and_then(Value::as_str) {
        cites.push(o.to_string());
    }
    let mut d = decided(
        ctx,
        &who,
        "resolve",
        "resolve_contest",
        targets,
        cites,
        input.rationale,
        &now,
    );
    d.id = Some(d_id);
    d.fields_changed = vec!["selection.contested".into()];
    commit(doc, data, m, d, "a contest resolved", &now)
}

/// What the middleware is asked before a finding is verified: the finding,
/// who verifies it, and the id the decision will carry, so the layer fact and
/// the document's decision name the same one. Refuses anything the host would
/// refuse afterwards, so the layer is not written for nothing.
pub fn verify_request(ctx: &Ctx, doc: &Document, finding: &str) -> HostResult<Value> {
    let who = person(ctx, "verify a finding")?;
    not_locked(doc)?;
    let findings = members::read_list(doc.clan(), FINDINGS)?;
    let f = to_json(&findings[finding_index(&findings, finding)?]);
    still_proposed(&f, finding)?;
    Ok(serde_json::json!({
        "task": "verify_finding",
        "input": { "finding": finding, "by": who, "decision_id": new_decision_id() },
    }))
}

/// Verify a finding, given the synthesis fact the middleware wrote to the
/// layer for it (`pin`, as the facts member holds pins): the finding becomes
/// `verified` with its `verification`, the fact is pinned, and one `verify`
/// decision — the id the layer was given — records it.
pub fn verify_finding(
    ctx: &Ctx,
    doc: &Document,
    finding: &str,
    rationale: &str,
    decision_id: &str,
    pin: &Value,
) -> HostResult<Outcome> {
    let who = person(ctx, "verify a finding")?;
    not_locked(doc)?;
    let clan = doc.clan();
    let doc_id = clan.document_id().to_string();
    let now = now();
    let mut m = Members::of(clan)?;
    let i = finding_index(&m.findings, finding)?;
    let mut f = to_json(&m.findings[i]);
    still_proposed(&f, finding)?;

    let fact_id = pin
        .get("id")
        .and_then(Value::as_str)
        .filter(|s| s.starts_with("f_"))
        .ok_or_else(|| HostError::new(502, "the middleware's synthesis fact has no f_ id"))?
        .to_string();
    if pin.get("method").and_then(Value::as_str) != Some("synthesis") {
        return Err(HostError::new(502, "the middleware's fact is not a synthesis"));
    }
    if pin.get("decision").and_then(Value::as_str) != Some(decision_id) {
        return Err(HostError::new(
            502,
            "the middleware's fact names another decision than the verification",
        ));
    }
    if m.facts.iter().any(|e| members::entry_id(e) == Some(fact_id.as_str())) {
        return Err(HostError::conflict(format!("{fact_id} is already pinned")));
    }
    m.facts.push(to_yaml(pin)?);

    f["status"] = "verified".into();
    f["verification"] = serde_json::json!({
        "decision": decision_id, "by": who, "at": now, "fact_id": fact_id,
    });
    let cites: Vec<String> = f
        .get("cites")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|c| c.as_str().map(String::from))
        .collect();
    m.findings[i] = to_yaml(&f)?;

    let mut d = decided(
        ctx,
        &who,
        "verify",
        "verify_finding",
        vec![
            address(&doc_id, &format!("findings[{finding}]")),
            address(&doc_id, &format!("facts[{fact_id}]")),
        ],
        cites,
        if rationale.is_empty() {
            "Verified by a person.".to_string()
        } else {
            rationale.to_string()
        },
        &now,
    );
    d.id = Some(decision_id.to_string());
    commit(doc, data_of(clan)?, m, d, "a finding verified", &now)
}

/// Lock: accept the document as it stands (D7). Refused while anything on the
/// lock list is open; otherwise one `approve` decision records the exact
/// version it accepted. After it, the review operations refuse.
pub fn approve(ctx: &Ctx, doc: &Document, rationale: &str) -> HostResult<Outcome> {
    let who = person(ctx, "lock the document")?;
    not_locked(doc)?;
    let view = decisions::decisions(doc)?;
    if !view.lock.can_lock {
        let open: Vec<String> = view
            .attention
            .iter()
            .filter(|a| a.blocks_lock)
            .map(|a| a.text.clone())
            .collect();
        return Err(HostError::conflict(format!(
            "{} thing{} still need{} a person before lock: {}",
            open.len(),
            if open.len() == 1 { "" } else { "s" },
            if open.len() == 1 { "s" } else { "" },
            open.join(" ")
        )));
    }
    let clan = doc.clan();
    let now = now();
    let mut d = decided(
        ctx,
        &who,
        "approve",
        "lock",
        vec![clan.document_id().to_string()],
        Vec::new(),
        if rationale.is_empty() {
            "Accepted and locked.".to_string()
        } else {
            rationale.to_string()
        },
        &now,
    );
    d.version = Some(doc.version().as_str().to_string());
    commit(doc, data_of(clan)?, Members::of(clan)?, d, "locked", &now)
}

/// Now, in the one shape the campaign schema's `datetime` accepts.
fn now() -> String {
    chrono::Utc::now().format("%Y-%m-%dT%H:%M:%SZ").to_string()
}
