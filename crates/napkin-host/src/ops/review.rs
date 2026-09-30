// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! A person's review decisions (Contract 4 §4, §7, §8): the typed operations
//! behind the buttons on a field — mark it good or bad, mark it confidential,
//! reject or verify a finding, pick between two sources, set aside a carried
//! item this document cannot settle, lock the document.
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
use super::edit::{attributed, upstream_read_only, UPSTREAM_KEY};
use super::members::{self, FACTS, FINDINGS, SOURCES};
use super::Outcome;

const CHAIN: &str = "agent/decision-chain.yaml";

/// The polarity values a verdict may carry.
pub const POLARITIES: &[&str] = &["good", "bad"];

/// The action of a verdict that sets a carried item aside (Contract 4 §7.2):
/// it carries no polarity — it says nothing of whether the item is right,
/// only why this document goes ahead without settling it.
pub const SET_ASIDE: &str = "set_aside";

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

pub(super) fn body(raw: &str) -> HostResult<Value> {
    let v: Value = serde_json::from_str(raw)
        .map_err(|e| HostError::bad_request(format!("invalid JSON: {e}")))?;
    if !v.is_object() {
        return Err(HostError::bad_request("the body must be an object"));
    }
    Ok(v)
}

pub(super) fn text(v: &Value, key: &str) -> String {
    v.get(key)
        .and_then(Value::as_str)
        .map(str::trim)
        .unwrap_or_default()
        .to_string()
}

pub(super) fn required(v: &Value, key: &str) -> HostResult<String> {
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

/// `POST /acknowledge`: `{decision, rationale?}` — "Looks right" on a
/// decision — or `{target, rationale}` — a carried item set aside (Contract 4
/// §8.1, item 7). The first of the pair is the decision's id or the item's
/// address; [`acknowledge`] tells them apart by the `#` only an address has.
pub fn parse_acknowledge(raw: &str) -> HostResult<(String, String)> {
    let v = body(raw)?;
    let (decision, target) = (text(&v, "decision"), text(&v, "target"));
    let rationale = text(&v, "rationale");
    match (decision.is_empty(), target.is_empty()) {
        (false, true) if decision.contains('#') => Err(HostError::bad_request(
            "`decision` is a decision's id; a carried item is set aside by its `target`",
        )),
        (false, true) => Ok((decision, rationale)),
        (true, false) if !target.contains('#') => Err(HostError::bad_request(
            "`target` is the carried item's address: <document id>#<path>",
        )),
        (true, false) if rationale.is_empty() => Err(set_aside_needs_a_reason()),
        (true, false) => Ok((target, rationale)),
        (false, false) => Err(HostError::bad_request(
            "give `decision` or `target`, not both",
        )),
        (true, true) => Err(HostError::bad_request(
            "`decision` is required, or `target` to set a carried item aside",
        )),
    }
}

fn set_aside_needs_a_reason() -> HostError {
    HostError::bad_request(
        "setting a carried item aside needs a reason (`rationale`): say why this document can go ahead without settling it",
    )
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
pub(super) fn person(ctx: &Ctx, what: &str) -> HostResult<String> {
    if !ctx.actor.is_human() {
        return Err(HostError::new(
            403,
            format!("only a person can {what}; {} is not one", ctx.actor),
        ));
    }
    Ok(ctx.actor.to_string())
}

pub(super) fn chain_of(doc: &Document) -> HostResult<DecisionChain> {
    let clan = doc.clan();
    Ok(if clan.has_entry(CHAIN) {
        DecisionChain::from_yaml(&clan.read_entry(CHAIN)?)?
    } else {
        DecisionChain::default()
    })
}

/// The lock that holds, if one does: an `approve` nothing has superseded
/// whose targets include this document's id (Contract 4 §7.1). A carried
/// `approve` targets the parent it accepted; it does not lock this document.
/// When more than one does — a document locked again after a part was
/// reopened (§7.5.6) — the newest is the lock.
pub fn lock_of<'c>(chain: &'c DecisionChain, doc_id: &str) -> Option<&'c Decision> {
    lock_index(chain, doc_id).map(|i| &chain.decisions[i])
}

/// Where [`lock_of`]'s decision is in the (newest-first) chain.
pub fn lock_index(chain: &DecisionChain, doc_id: &str) -> Option<usize> {
    chain.decisions.iter().position(|d| {
        d.kind.as_deref() == Some("approve")
            && d.superseded_by.is_none()
            && d.targets.iter().any(|t| t == doc_id)
    })
}

fn locked(d: &Decision) -> HostError {
    HostError::conflict(format!(
        "the document was locked by {} at {}; changes make a new version",
        d.actor.as_deref().unwrap_or(&d.agent),
        d.timestamp
    ))
}

fn not_locked(doc: &Document) -> HostResult<()> {
    match lock_of(&chain_of(doc)?, doc.clan().document_id()) {
        Some(d) => Err(locked(d)),
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

/// The frozen copy of ancestor `id` this document carries, if it carries one.
fn frozen<'v>(data: &'v Value, id: &str) -> Option<&'v Value> {
    data.get(UPSTREAM_KEY)?.get(id).filter(|v| v.is_object())
}

/// A target, checked against the document.
struct Aim {
    /// What the decision records as its target (Contract 4 §8.1, item 1): it
    /// targets what it changes, and where it changes nothing — an ancestor's
    /// frozen copy — the upstream address.
    address: String,
    /// The path part.
    path: String,
}

/// Check that a target names something the document holds — a pin, a
/// finding, a source, a material, a contest, or a data path — and say what
/// the decision about it targets.
///
/// A target is `<id>#<path>`, or a bare path on this document. `<id>` is this
/// document or an ancestor it carries in `data.upstream` (§8.1); any other is
/// refused. On an ancestor, `facts[…]`, `findings[…]` and `sources[…]` are
/// still this document's members — the ancestor's entries were merged into
/// them, and the copy that changes is this one — so the decision targets this
/// document; any other path is looked up in the frozen copy, which nothing
/// here writes, and the decision targets the ancestor's address.
fn target_path(doc: &Document, target: &str) -> HostResult<Aim> {
    let clan = doc.clan();
    let here = clan.document_id();
    let data = data_of(clan)?;
    let (on, path) = match target.split_once('#') {
        Some((d, p)) => (d, p),
        None => (here, target),
    };
    let copy = if on == here {
        None
    } else {
        Some(frozen(&data, on).ok_or_else(|| {
            HostError::bad_request(format!(
                "target {target} is on document {on}, which this document does not carry"
            ))
        })?)
    };
    let in_member = |m, id: &str| -> HostResult<bool> {
        Ok(members::read_list(clan, m)?
            .iter()
            .any(|e| members::entry_id(e) == Some(id)))
    };
    let member = if let Some(id) = keyed(path, "facts") {
        Some(in_member(FACTS, id)?)
    } else if let Some(id) = keyed(path, "findings") {
        Some(in_member(FINDINGS, id)?)
    } else if let Some(id) = keyed(path, "sources") {
        Some(in_member(SOURCES, id)?)
    } else {
        None
    };
    if let Some(found) = member {
        if !found {
            return Err(HostError::not_found(format!(
                "target {path} is not in this document"
            )));
        }
        return Ok(Aim {
            address: format!("{here}#{path}"),
            path: path.to_string(),
        });
    }
    if copy.is_none() && path.split(['.', '[']).next() == Some(UPSTREAM_KEY) {
        return Err(HostError::bad_request(format!(
            "{path} is inside the frozen upstream copy: address it as <document id>#<path>"
        )));
    }
    let own = copy.is_none();
    let data = copy.unwrap_or(&data);
    let found = if let Some(id) = keyed(path, "materials") {
        data.get("materials").and_then(|m| m.get(id)).is_some()
    } else if let Some(id) = keyed(path, "selection.contested") {
        contest_index(data, id).is_some()
    } else {
        path.split('.')
            .try_fold(data, |v, k| v.get(k))
            .is_some()
            // A field the document's schema declares is the document's even
            // while it is empty: a Judge's bad verdict on an empty part must
            // be answerable (Contract 4 §7.2, item 5). Own data only — a
            // frozen copy is what it is.
            || (own && declared(clan, path))
    };
    if !found {
        return Err(HostError::not_found(match copy {
            Some(_) => format!("target {path} is not in the copy of {on} this document carries"),
            None => format!("target {path} is not in this document"),
        }));
    }
    Ok(Aim {
        address: format!("{on}#{path}"),
        path: path.to_string(),
    })
}

/// `path`, in dotted keys, names a property `agent/output-schema.json`
/// declares: each key is in the `properties` of the schema at that depth,
/// following local `$ref`s (`#/definitions/…`, `#/$defs/…`) and the branches
/// of `allOf` / `anyOf` / `oneOf`. `additionalProperties` declares no name,
/// so it does not count. False with no schema.
pub(crate) fn declared(clan: &clan_sdk::ClanFile, path: &str) -> bool {
    let Some(schema) = clan
        .read_entry("agent/output-schema.json")
        .ok()
        .and_then(|b| serde_json::from_slice::<Value>(&b).ok())
    else {
        return false;
    };
    if path.is_empty() || path.contains(['[', ']', '#']) {
        return false;
    }
    fn resolve<'s>(root: &'s Value, node: &'s Value, depth: usize) -> Vec<&'s Value> {
        if depth > 16 {
            return Vec::new();
        }
        let mut out = vec![node];
        if let Some(r) = node.get("$ref").and_then(Value::as_str) {
            if let Some(target) = r.strip_prefix('#').and_then(|p| root.pointer(p)) {
                out.extend(resolve(root, target, depth + 1));
            }
        }
        for key in ["allOf", "anyOf", "oneOf"] {
            for branch in node.get(key).and_then(Value::as_array).into_iter().flatten() {
                out.extend(resolve(root, branch, depth + 1));
            }
        }
        out
    }
    let mut here: Vec<&Value> = vec![&schema];
    for key in path.split('.') {
        if key.is_empty() {
            return false;
        }
        here = here
            .into_iter()
            .flat_map(|n| resolve(&schema, n, 0))
            .filter_map(|n| n.get("properties")?.get(key))
            .collect();
        if here.is_empty() {
            return false;
        }
    }
    true
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

pub(super) fn to_yaml(v: &Value) -> HostResult<serde_yaml::Value> {
    serde_yaml::to_value(v).map_err(|e| HostError::internal(e.to_string()))
}

pub(super) fn to_json(v: &serde_yaml::Value) -> Value {
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
    commit_with(doc, data, m, decision, Vec::new(), delta, now)
}

/// [`commit`], with `then` written after `decision` in the same generation
/// (the last ends up newest). The reply names `decision`.
fn commit_with(
    doc: &Document,
    data: Value,
    m: Members,
    decision: Decision,
    then: Vec<Decision>,
    delta: &str,
    now: &str,
) -> HostResult<Outcome> {
    let id = decision.id.clone().unwrap_or_default();
    let kind = decision.kind.clone().unwrap_or_default();
    let targets = decision.targets.clone();
    let mut decisions = vec![decision];
    decisions.extend(then);
    let bytes = assemble(doc.clan(), data, m, decisions, delta, now, false)?;
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
    let aim = target_path(doc, &input.target)?;
    let clan = doc.clan();
    let now = now();
    let mut m = Members::of(clan)?;

    if let Some(id) = keyed(&aim.path, "findings") {
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
            vec![aim.address.clone()],
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

    // A field the schema declares but the data does not hold yet: marking it
    // good overrides what was said of an empty part, so it says why.
    let data = data_of(clan)?;
    let empty = aim.address.starts_with(&format!("{}#", clan.document_id()))
        && !aim.path.contains('[')
        && aim.path.split('.').try_fold(&data, |v, k| v.get(k)).is_none();
    if empty && input.rationale.is_empty() {
        return Err(HostError::bad_request(
            "the field is empty: marking it good needs a reason, saying why it may stay empty",
        ));
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
        vec![aim.address],
        Vec::new(),
        rationale,
        &now,
    );
    d.polarity = Some(input.polarity.clone());
    d.reason_code = input.reason_code;
    commit(doc, data, m, d, "a verdict", &now)
}

/// A confidentiality mark: whether the target may reach a model, appear in an
/// export, enter the agency's knowledge.
pub fn classify(ctx: &Ctx, doc: &Document, input: Classify) -> HostResult<Outcome> {
    let who = person(ctx, "mark something confidential")?;
    not_locked(doc)?;
    let aim = target_path(doc, &input.target)?;
    let clan = doc.clan();
    let now = now();
    let mut d = decided(
        ctx,
        &who,
        "classify",
        "classify",
        vec![aim.address],
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

/// Where a contest is: this document's `selection.contested`, or the frozen
/// one of an ancestor it carries (`None` or the ancestor's id), with the
/// contest's id and its entry.
fn find_contest(data: &Value, here: &str, contest: &str) -> HostResult<(Option<String>, String, Value)> {
    let missing = || HostError::not_found(format!("contest {contest} is not in this document"));
    let (on, id) = match contest.split_once('#') {
        Some((d, p)) => {
            let id = keyed(p, "selection.contested").ok_or_else(|| {
                HostError::bad_request(format!("{contest} is not a contest's address"))
            })?;
            (Some(d), id)
        }
        None => (None, keyed(contest, "selection.contested").unwrap_or(contest)),
    };
    let entry = |d: &Value| contest_index(d, id).map(|i| d["selection"]["contested"][i].clone());
    let in_copy = |up: &str| -> HostResult<Option<(Option<String>, String, Value)>> {
        let copy = frozen(data, up).ok_or_else(|| {
            HostError::bad_request(format!(
                "contest {contest} is on document {up}, which this document does not carry"
            ))
        })?;
        Ok(entry(copy).map(|c| (Some(up.to_string()), id.to_string(), c)))
    };
    match on {
        Some(d) if d == here => entry(data).map(|c| (None, id.to_string(), c)).ok_or_else(missing),
        Some(up) => in_copy(up)?.ok_or_else(missing),
        None => {
            if let Some(c) = entry(data) {
                return Ok((None, id.to_string(), c));
            }
            // Then each carried copy, in key order.
            let ups: Vec<String> = data
                .get(UPSTREAM_KEY)
                .and_then(Value::as_object)
                .map(|o| o.keys().cloned().collect())
                .unwrap_or_default();
            for up in ups {
                if let Some(found) = in_copy(&up)? {
                    return Ok(found);
                }
            }
            Err(missing())
        }
    }
}

/// Pick one value of an open contest. The chosen value is pinned when it is
/// not already; a pin it replaces stays in the facts member, marked
/// `replaced_by`, so every decision that cited it still resolves.
///
/// A contest carried open in an ancestor's frozen copy (Contract 4 §8.1,
/// item 2) is settled here, in this document: the chosen pin comes from the
/// frozen value and goes into this document's facts, and the decision targets
/// the contest's upstream address. The frozen entry is left as it was — the
/// chain is where a carried contest is settled — so a second resolve of it is
/// refused by the chain, not by the entry.
pub fn resolve(ctx: &Ctx, doc: &Document, input: Resolve) -> HostResult<Outcome> {
    let who = person(ctx, "resolve a contest")?;
    not_locked(doc)?;
    let clan = doc.clan();
    let doc_id = clan.document_id().to_string();
    let now = now();
    let mut data = data_of(clan)?;
    let (upstream, ct, contest) = find_contest(&data, &doc_id, &input.contest)?;
    let on = upstream.as_deref().unwrap_or(&doc_id);
    let contest_address = format!("{on}#selection.contested[{ct}]");
    if contest.get("status").and_then(Value::as_str) != Some("open") {
        return Err(HostError::conflict(match upstream {
            Some(_) => format!("contest {ct} was already resolved upstream, before it was carried here"),
            None => format!("contest {} is already resolved", input.contest),
        }));
    }
    if upstream.is_some() {
        let chain = chain_of(doc)?;
        if let Some(d) = chain.decisions.iter().find(|d| {
            d.kind.as_deref() == Some("resolve")
                && d.superseded_by.is_none()
                && d.targets.contains(&contest_address)
        }) {
            return Err(HostError::conflict(format!(
                "contest {ct} is already resolved in this document ({})",
                d.id.as_deref().unwrap_or("a resolve")
            )));
        }
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
    let mut targets = vec![contest_address];
    let held = m
        .facts
        .iter()
        .any(|e| members::entry_id(e) == Some(input.chosen.as_str()));
    if !held {
        // The chosen value is a layer row the research wrote as contested; the
        // research froze its pin beside the value for exactly this.
        let pin = chosen.get("pin").filter(|p| p.is_object()).ok_or_else(|| {
            HostError::conflict(format!(
                "{} carries no pin to take; rerun the research for it, or set the contest aside with a reason",
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

    if upstream.is_none() {
        let i = contest_index(&data, &ct).expect("found above");
        let entry = &mut data["selection"]["contested"][i];
        entry["status"] = "resolved".into();
        entry["chosen"] = input.chosen.clone().into();
        entry["reason"] = input.rationale.clone().into();
        entry["decided_by"] = d_id.clone().into();
    }

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
    if upstream.is_none() {
        d.fields_changed = vec!["selection.contested".into()];
    }
    commit(
        doc,
        data,
        m,
        d,
        if upstream.is_some() { "a carried contest resolved" } else { "a contest resolved" },
        &now,
    )
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
    source: Option<&str>,
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
    // The person is the pin's source: the document carries that record too,
    // so the pin's evidence reads "verified by <you>", not a missing source.
    if let Some(src) = source.filter(|s| s.starts_with("src_")) {
        if !m.sources.iter().any(|e| members::entry_id(e) == Some(src)) {
            m.sources.push(to_yaml(&serde_json::json!({
                "id": src, "uri": who, "title": "Verified in this document",
                "publisher": who.trim_start_matches("human:"), "tier": "reviewer-verified",
                "retrieved_at": now.get(..10).unwrap_or_default(),
                "licence": pin.get("licence").cloned().unwrap_or_else(|| "open".into()),
            }))?);
        }
    }

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

/// "Looks right": a person has read what an agent flagged, or how sure it
/// was, and accepts the call. Recorded as a good verdict that names the
/// decision (Contract 4 §4), which is what clears it from what needs a person;
/// the decision's own targets are untouched.
///
/// Given an address in place of a decision id, it sets a carried item aside
/// instead ([`set_aside`]).
pub fn acknowledge(ctx: &Ctx, doc: &Document, input: &(String, String)) -> HostResult<Outcome> {
    let (id, rationale) = input;
    if id.contains('#') {
        return set_aside(ctx, doc, id, rationale);
    }
    let who = person(ctx, "accept an agent's call")?;
    not_locked(doc)?;
    let chain = chain_of(doc)?;
    let d = chain
        .decisions
        .iter()
        .find(|d| d.id.as_deref() == Some(id.as_str()))
        .ok_or_else(|| HostError::not_found(format!("decision {id} is not in this document")))?;
    if d.kind.as_deref() == Some("finding") {
        return Err(HostError::bad_request(
            "a finding is not accepted here: verify it, so it is written to the layer as reviewed",
        ));
    }
    let clan = doc.clan();
    let now = now();
    let mut v = decided(
        ctx,
        &who,
        "verdict",
        "looks_right",
        vec![address(clan.document_id(), &format!("decisions[{id}]"))],
        vec![id.clone()],
        if rationale.is_empty() {
            format!("Looks right: {}", clip_line(&d.rationale))
        } else {
            rationale.clone()
        },
        &now,
    );
    v.polarity = Some("good".into());
    commit(doc, data_of(clan)?, Members::of(clan)?, v, "a call accepted", &now)
}

/// Set aside, with a reason, a carried item this document cannot settle
/// (Contract 4 §7.2): a field in an ancestor's frozen copy citing a finding
/// rejected here, an agent branch carried from the parent, or a conflict in
/// a carried merge report — each read-only here and settled, if at all, in
/// the document it came from. One `set_aside` verdict, with no polarity,
/// targets the item's upstream address and cites it (and, on a flagged
/// field, the findings that flag it); the item leaves this document's lock
/// list. Nothing else is written: not the frozen copy, not the parent.
///
/// Only what the lock list offers to set aside (`can_set_aside`) is taken. A
/// carried contest with a value to take, or a carried bad verdict, can be
/// settled here — `/resolve`, or `/verdict good` with a reason — so it is
/// refused (`409`); a carried contest none of whose values is held here or
/// carries a frozen pin cannot, and is offered. So is refused (`409`) an item
/// already set aside; this document's own items are refused (`400`), and an
/// address the list does not hold is `404`.
pub fn set_aside(ctx: &Ctx, doc: &Document, target: &str, rationale: &str) -> HostResult<Outcome> {
    let who = person(ctx, "set a carried item aside")?;
    not_locked(doc)?;
    let rationale = rationale.trim();
    if rationale.is_empty() {
        return Err(set_aside_needs_a_reason());
    }
    let clan = doc.clan();
    let here = clan.document_id();
    let (on, path) = target.split_once('#').ok_or_else(|| {
        HostError::bad_request("`target` is the carried item's address: <document id>#<path>")
    })?;
    if on == here || on.is_empty() {
        return Err(HostError::bad_request(format!(
            "{target} is this document's own: settle it here; only an item carried from upstream is set aside"
        )));
    }
    let data = data_of(clan)?;
    if frozen(&data, on).is_none() {
        return Err(HostError::bad_request(format!(
            "target {target} is on document {on}, which this document does not carry"
        )));
    }
    if path.is_empty() {
        return Err(HostError::bad_request(format!(
            "{target} names the whole document {on}; set aside one item it carried"
        )));
    }

    let view = decisions::decisions(doc)?;
    let listed: Vec<&decisions::Attention> = view
        .attention
        .iter()
        .filter(|a| a.blocks_lock && a.address.as_deref() == Some(target))
        .collect();
    let items: Vec<&decisions::Attention> = listed.iter().copied().filter(|a| a.can_set_aside).collect();
    if items.is_empty() {
        let chain = chain_of(doc)?;
        let earlier = chain.decisions.iter().find(|d| {
            d.kind.as_deref() == Some("verdict")
                && d.action == SET_ASIDE
                && d.superseded_by.is_none()
                && d.targets.iter().any(|t| t == target)
        });
        return Err(match (listed.first(), earlier) {
            (Some(a), _) => HostError::conflict(format!(
                "{target} can be settled here, so it is not set aside: {}",
                match a.code {
                    "open_contest" => "pick a value (`/resolve`)",
                    "bad_verdict" => "override the verdict with a reason (`/verdict good`)",
                    _ => "settle it",
                }
            )),
            (None, Some(d)) => HostError::conflict(format!(
                "{target} is already set aside in this document ({})",
                d.id.as_deref().unwrap_or("a set_aside")
            )),
            (None, None) => HostError::not_found(format!(
                "nothing carried at {target} is on this document's lock list"
            )),
        });
    }

    // Cite the item, and what it rests on: the findings that flag a field,
    // or the decision a merge conflict names.
    let mut cites = vec![target.to_string()];
    for a in &items {
        let extra = a.finding.clone().or_else(|| a.decision.clone());
        if let Some(c) = extra.filter(|c| !cites.contains(c)) {
            cites.push(c);
        }
    }
    let now = now();
    let d = decided(
        ctx,
        &who,
        "verdict",
        SET_ASIDE,
        vec![target.to_string()],
        cites,
        rationale.to_string(),
        &now,
    );
    commit(doc, data, Members::of(clan)?, d, "a carried item set aside", &now)
}

fn clip_line(s: &str) -> String {
    let first = s.split(". Because").next().unwrap_or(s).trim();
    if first.chars().count() > 120 {
        format!("{}…", first.chars().take(117).collect::<String>())
    } else {
        first.to_string()
    }
}

/// `POST /edit`: `{path, value, gate?, rationale, answers?}`.
#[derive(Debug, Clone)]
pub struct Edit {
    pub path: String,
    pub value: Value,
    pub gate: Option<String>,
    /// Why — every edit says.
    pub rationale: String,
    /// The client's part answer this edit answers (Contract 4 §7.5.5): the
    /// one the part was reopened for.
    pub answers: Option<String>,
}

/// `POST /edit`: `{path, value, gate?, rationale, answers?}` — an edit says why.
pub fn parse_edit(raw: &str) -> HostResult<Edit> {
    let v = body(raw)?;
    let value = v.get("value").cloned().ok_or_else(|| HostError::bad_request("`value` is required"))?;
    let rationale = text(&v, "rationale");
    if rationale.is_empty() {
        return Err(HostError::bad_request("say why you changed it (`rationale`)"));
    }
    Ok(Edit {
        path: required(&v, "path")?,
        value,
        gate: Some(text(&v, "gate")).filter(|g| !g.is_empty()),
        rationale,
        answers: Some(text(&v, "answers")).filter(|a| !a.is_empty()),
    })
}

/// An edit of `path` may go ahead: the document is not locked, or `path` is
/// at or inside a part a client's request reopened (Contract 4 §7.5.6, item
/// 2) — `not_locked`'s one exception. `answers`, when given, must be the part
/// answer that part was reopened for.
fn open_for_edit(doc: &Document, path: &str, answers: Option<&str>) -> HostResult<()> {
    let chain = chain_of(doc)?;
    let here = doc.clan().document_id();
    let Some(lock) = lock_of(&chain, here) else {
        return match answers {
            Some(a) => Err(HostError::bad_request(format!(
                "no part is reopened for {a}: `answers` names the client's request a reopened part is edited for"
            ))),
            None => Ok(()),
        };
    };
    let open = super::client_review::reopened(&chain, here);
    let Some(part) = open.iter().find(|r| super::client_review::inside(path, &r.path)) else {
        return Err(locked(lock));
    };
    match answers {
        Some(a) if a != part.answers => Err(HostError::bad_request(format!(
            "{} was reopened for {}, not {a}",
            part.path, part.answers
        ))),
        _ => Ok(()),
    }
}

/// A person edits a value in the document (edit mode). The edit is theirs:
/// one pinned `edit` decision names the path, and a campaign field's envelope
/// becomes `origin: stated`, `by` the person — so no job writes over it
/// (Contract 3 §2.2). Its old provenance (a quote, the pins it was inferred
/// from) no longer describes the value and is dropped; the decision chain
/// keeps the history. A path the host owns (`projection`, or `upstream`, the
/// frozen copy of what the document was spun off from), a member (facts,
/// findings — corrected with `/correct`, verified with `/verify`), or an
/// unchanged value is refused.
///
/// On a locked document only a part reopened for a client's request can be
/// edited (Contract 4 §7.5.6); the edit then records the request it answers
/// (`answers`) and cites it.
pub fn edit(ctx: &Ctx, doc: &Document, input: Edit) -> HostResult<Outcome> {
    let Edit { path, value, gate, rationale, answers } = input;
    let who = person(ctx, "edit the document")?;
    open_for_edit(doc, &path, answers.as_deref())?;
    let segs: Vec<&str> = path.split('.').collect();
    if segs.is_empty() || segs.iter().any(|s| s.is_empty() || s.contains('[') || s.contains('#')) {
        return Err(HostError::bad_request(format!("{path} is not a data path (dotted keys only)")));
    }
    if segs[0] == UPSTREAM_KEY {
        return Err(upstream_read_only(doc.clan(), segs.get(1).copied()));
    }
    if segs[0] == super::members::PROJECTION_KEY {
        return Err(HostError::bad_request("the projection is the host's; edit the value it is built from"));
    }
    if matches!(segs[0], "facts" | "findings" | "sources") {
        return Err(HostError::bad_request("a fact is corrected (/correct) and a finding verified (/verify), not edited"));
    }
    let clan = doc.clan();
    let now = now();
    let mut data = data_of(clan)?;
    let current = segs.iter().try_fold(&data, |v, k| v.get(*k)).cloned();
    let d_id = new_decision_id();

    // A campaign field is an envelope: the value, and how it got there.
    let envelope = segs.len() == 2 && segs[0] == "campaign";
    let next = if envelope {
        let old = current.clone().unwrap_or(Value::Null);
        if old.get("value") == Some(&value) {
            return Err(HostError::conflict(format!("{path} already holds that value")));
        }
        let gate = old
            .get("gate")
            .and_then(Value::as_str)
            .map(String::from)
            .or(gate)
            .ok_or_else(|| HostError::bad_request(format!("{path} is empty: say which gate it belongs to (`gate`)")))?;
        serde_json::json!({ "value": value, "origin": "stated", "gate": gate, "by": who, "decision": d_id })
    } else {
        if current.as_ref() == Some(&value) {
            return Err(HostError::conflict(format!("{path} already holds that value")));
        }
        value
    };
    let mut at = &mut data;
    for k in &segs[..segs.len() - 1] {
        if !at.get(*k).is_some_and(Value::is_object) {
            at[*k] = Value::Object(Default::default());
        }
        at = at.get_mut(*k).expect("just made");
    }
    at[segs[segs.len() - 1]] = next;

    let mut d = decided(
        ctx,
        &who,
        "edit",
        "edit_field",
        vec![address(clan.document_id(), &path)],
        Vec::new(),
        if rationale.is_empty() { "Edited by a person.".to_string() } else { rationale },
        &now,
    );
    d.id = Some(d_id);
    d.pinned = true;
    d.fields_changed = vec![path.clone()];
    if let Some(a) = answers {
        d.cites.push(a.clone());
        d.extra.insert("answers".into(), serde_yaml::Value::String(a));
    }
    let shown = |v: &Value| match v {
        Value::String(s) => s.clone(),
        Value::Array(a) => a.iter().map(|x| x.as_str().map(String::from).unwrap_or_else(|| x.get("name").and_then(Value::as_str).unwrap_or_default().to_string())).collect::<Vec<_>>().join(", "),
        Value::Null => String::new(),
        other => other.get("name").and_then(Value::as_str).map(String::from).unwrap_or_else(|| other.to_string()),
    };
    let before = current.as_ref().map(|c| if envelope { c.get("value").cloned().unwrap_or(Value::Null) } else { c.clone() }).unwrap_or(Value::Null);
    let after = segs.iter().try_fold(&data, |v, k| v.get(*k)).map(|v| if envelope { v.get("value").cloned().unwrap_or(Value::Null) } else { v.clone() }).unwrap_or(Value::Null);
    if !shown(&before).is_empty() {
        d.extra.insert("was".into(), serde_yaml::Value::String(clip(&shown(&before), 300)));
    }
    d.extra.insert("now".into(), serde_yaml::Value::String(clip(&shown(&after), 300)));
    commit(doc, data, Members::of(clan)?, d, "a person's edit", &now)
}

/// What a wording edit changed, as the decision records it: which part of the
/// page, and the words before and after (plain text, clipped).
#[derive(Debug, Clone, Default)]
pub struct TextEdit {
    pub key: String,
    pub html: String,
    pub rationale: String,
    pub part: String,
    pub was: String,
}

fn plain(html: &str) -> String {
    let mut out = String::new();
    let mut tag = false;
    for c in html.chars() {
        match c {
            '<' => tag = true,
            '>' => { tag = false; out.push(' '); }
            _ if !tag => out.push(c),
            _ => {}
        }
    }
    let words = out.split_whitespace().collect::<Vec<_>>().join(" ");
    if words.chars().count() > 300 { format!("{}…", words.chars().take(299).collect::<String>()) } else { words }
}

/// `POST /edit-text`: `{key, html, rationale, part?, was?}`. A rewrite needs
/// its reason; putting the original back does not.
pub fn parse_edit_text_full(raw: &str) -> HostResult<TextEdit> {
    let v = body(raw)?;
    let (key, html) = parse_edit_text(raw)?;
    let rationale = text(&v, "rationale");
    if !html.is_empty() && rationale.is_empty() {
        return Err(HostError::bad_request("say why you changed it (`rationale`)"));
    }
    Ok(TextEdit { key, html, rationale, part: text(&v, "part"), was: text(&v, "was") })
}

/// `POST /edit-text`: `{key, html}` — the key and wording alone.
pub fn parse_edit_text(raw: &str) -> HostResult<(String, String)> {
    let v = body(raw)?;
    let key = required(&v, "key")?;
    if key.len() > 200 || key.chars().any(|c| c.is_whitespace() || c.is_control()) {
        return Err(HostError::bad_request("`key` is a short name without spaces"));
    }
    let html = v.get("html").and_then(Value::as_str).unwrap_or_default().trim().to_string();
    if html.len() > 20_000 {
        return Err(HostError::bad_request("that is too long for one piece of text"));
    }
    Ok((key, html))
}

/// A person rewrites a piece of a view's text (edit mode): the headline, a
/// paragraph, a caption. The wording is kept in `shared/edits.yaml` by key —
/// not in the data, so it works for any app and any document — and one pinned
/// `edit` decision records it. The view shows it in place of its own text,
/// sanitised as any layout is. An empty `html` restores the original.
pub fn edit_text(ctx: &Ctx, doc: &Document, input: TextEdit) -> HostResult<Outcome> {
    let TextEdit { key, html, rationale, part, was } = input;
    let who = person(ctx, "edit the document")?;
    not_locked(doc)?;
    let clan = doc.clan();
    let now = now();
    let mut m = Members::of(clan)?;
    let i = m.edits.iter().position(|e| members::entry_key(e) == Some(key.as_str()));
    let held = i.and_then(|i| m.edits[i].get("html").and_then(|v| v.as_str()).map(String::from));
    if held.as_deref() == Some(html.as_str()) || (held.is_none() && html.is_empty()) {
        return Err(HostError::conflict("the text already reads that way"));
    }
    let d_id = new_decision_id();
    let entry = to_yaml(&serde_json::json!({ "key": key, "html": html, "by": who, "at": now, "decision": d_id }))?;
    match (i, html.is_empty()) {
        (Some(i), true) => { m.edits.remove(i); }
        (Some(i), false) => m.edits[i] = entry,
        (None, _) => m.edits.push(entry),
    }
    let mut d = decided(
        ctx,
        &who,
        "edit",
        if html.is_empty() { "restore_text" } else { "edit_text" },
        vec![address(clan.document_id(), &format!("text[{key}]"))],
        Vec::new(),
        if !rationale.is_empty() {
            rationale
        } else if html.is_empty() {
            "Put the original wording back.".to_string()
        } else {
            "Rewrote the wording.".to_string()
        },
        &now,
    );
    d.id = Some(d_id);
    d.pinned = true;
    // Which part, and the words before and after: what the panel says.
    let was_plain = plain(&held.clone().unwrap_or(was));
    if !part.is_empty() {
        d.extra.insert("part".into(), serde_yaml::Value::String(clip(&part, 40)));
    }
    if !was_plain.is_empty() {
        d.extra.insert("was".into(), serde_yaml::Value::String(was_plain));
    }
    if !html.is_empty() {
        d.extra.insert("now".into(), serde_yaml::Value::String(plain(&html)));
    }
    commit(doc, data_of(clan)?, m, d, "a person's wording", &now)
}

/// `POST /correct`: `{fact, value, source_uri?, rationale}` — asked of the
/// middleware first, as `/verify` is.
pub fn parse_correct(raw: &str) -> HostResult<(String, Value, String, String)> {
    let v = body(raw)?;
    let value = v.get("value").cloned().ok_or_else(|| HostError::bad_request("`value` is required"))?;
    if value.as_str().is_some_and(|s| s.trim().is_empty()) {
        return Err(HostError::bad_request("`value` is empty"));
    }
    Ok((required(&v, "fact")?, value, text(&v, "source_uri"), required(&v, "rationale")?))
}

/// What the middleware is asked before a fact is corrected: the fact, the
/// value, where it comes from, who says so, and the decision id to record.
pub fn correct_request(ctx: &Ctx, doc: &Document, input: &(String, Value, String, String)) -> HostResult<Value> {
    let (fact, value, source_uri, rationale) = input;
    let who = person(ctx, "correct a fact")?;
    not_locked(doc)?;
    let facts = members::read_list(doc.clan(), FACTS)?;
    let f = facts
        .iter()
        .map(to_json)
        .find(|f| f.get("id").and_then(Value::as_str) == Some(fact.as_str()))
        .ok_or_else(|| HostError::not_found(format!("fact {fact} is not in this document")))?;
    if f.get("replaced_by").is_some() {
        return Err(HostError::conflict(format!("fact {fact} has already been replaced")));
    }
    Ok(serde_json::json!({
        "task": "correct_fact",
        "input": { "fact": fact, "value": value, "source_uri": source_uri, "note": rationale,
                   "by": who, "decision_id": new_decision_id() },
    }))
}

/// Correct a fact, given the row the middleware wrote to the layer for it
/// (`pin`) and the person's source record: the pin is added, the old one is
/// kept and marked `replaced_by`, and one pinned `edit` decision records it.
pub fn correct_fact(
    ctx: &Ctx,
    doc: &Document,
    fact: &str,
    rationale: &str,
    decision_id: &str,
    pin: &Value,
    source: Option<&Value>,
) -> HostResult<Outcome> {
    let who = person(ctx, "correct a fact")?;
    not_locked(doc)?;
    let clan = doc.clan();
    let doc_id = clan.document_id().to_string();
    let now = now();
    let mut m = Members::of(clan)?;
    let new_id = pin
        .get("id")
        .and_then(Value::as_str)
        .filter(|s| s.starts_with("f_"))
        .ok_or_else(|| HostError::new(502, "the middleware's corrected fact has no f_ id"))?
        .to_string();
    if pin.get("decision").and_then(Value::as_str) != Some(decision_id) {
        return Err(HostError::new(502, "the middleware's fact names another decision than the correction"));
    }
    let i = m
        .facts
        .iter()
        .position(|e| members::entry_id(e) == Some(fact))
        .ok_or_else(|| HostError::not_found(format!("fact {fact} is not in this document")))?;
    let mut old = to_json(&m.facts[i]);
    if old.get("replaced_by").is_some() {
        return Err(HostError::conflict(format!("fact {fact} has already been replaced")));
    }
    old["replaced_by"] = serde_json::json!({ "fact_id": new_id, "decision": decision_id });
    m.facts[i] = to_yaml(&old)?;
    if !m.facts.iter().any(|e| members::entry_id(e) == Some(new_id.as_str())) {
        m.facts.push(to_yaml(pin)?);
    }
    if let Some(src) = source.filter(|s| s.get("id").and_then(Value::as_str).is_some_and(|i| i.starts_with("src_"))) {
        let sid = src["id"].as_str().unwrap_or_default();
        if !m.sources.iter().any(|e| members::entry_id(e) == Some(sid)) {
            m.sources.push(to_yaml(src)?);
        }
    }
    let mut d = decided(
        ctx,
        &who,
        "edit",
        "correct_fact",
        vec![
            address(&doc_id, &format!("facts[{fact}]")),
            address(&doc_id, &format!("facts[{new_id}]")),
        ],
        vec![fact.to_string(), new_id.clone()],
        rationale.to_string(),
        &now,
    );
    d.id = Some(decision_id.to_string());
    d.pinned = true;
    commit(doc, data_of(clan)?, m, d, "a fact corrected", &now)
}

/// Lock: accept the document as it stands (D7). Refused while anything on the
/// lock list is open; otherwise one `approve` decision records the exact
/// version it accepted. After it, the review operations refuse.
///
/// A locked document with a part reopened for a client's request locks again
/// (Contract 4 §7.5.6, item 3): the whole list is run, a client's unanswered
/// rejection included, and the new `approve` — now the lock — closes every
/// reopened part. The older one is not rewritten. Any of Ellis's suggestions
/// still unconfirmed is closed by it, one dismissal each, written after it
/// (§7.5.3): they were about a version the new lock replaces.
pub fn approve(ctx: &Ctx, doc: &Document, rationale: &str) -> HostResult<Outcome> {
    let who = person(ctx, "lock the document")?;
    let chain = chain_of(doc)?;
    let here = doc.clan().document_id();
    let again = lock_of(&chain, here).is_some();
    if let Some(lock) = lock_of(&chain, here) {
        if super::client_review::reopened(&chain, here).is_empty() {
            return Err(locked(lock));
        }
    }
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
    let closed = if again {
        let lock = d.id.clone().unwrap_or_default();
        super::client_review::closed_by_lock(ctx, &who, &chain, &view.client.suggestions, &lock, &now)?
    } else {
        Vec::new()
    };
    commit_with(doc, data_of(clan)?, Members::of(clan)?, d, closed, "locked", &now)
}

/// Now, in the one shape the campaign schema's `datetime` accepts.
pub(super) fn now() -> String {
    chrono::Utc::now().format("%Y-%m-%dT%H:%M:%SZ").to_string()
}

fn clip(s: &str, n: usize) -> String {
    if s.chars().count() > n { format!("{}…", s.chars().take(n - 1).collect::<String>()) } else { s.to_string() }
}
