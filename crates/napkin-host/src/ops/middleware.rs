// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! Applying what the middleware computed — the host half of
//! `napkin.middleware/1`.
//!
//! The middleware never writes a document and neither does the app that asked
//! it. It answers with a `change` — a merge patch over `shared/data.yaml`,
//! entries to append to the facts, findings and sources members, and the decisions
//! that justify them — and the host applies that as ONE [`Change`] through the
//! single write funnel, as the actor `process:middleware`. What the app gets
//! back is informational: the envelope, with `change` replaced by whether it
//! landed and at which version.
//!
//! [`apply`] is a plain operation — `(Ctx, Document@version, reply) →
//! Outcome` — so the desktop runs it after its proxy call and the server runs
//! the same function in-process (P2), and a test can feed it a canned reply
//! without any HTTP.

use clan_sdk::{
    Certainty, ClanFile, Decision, DecisionChain, ReasonPoint, Reasoning, Rejected,
};
use serde_json::Value;

use crate::ctx::{Actor, Ctx};
use crate::document::{Change, Document};
use crate::error::{HostError, HostResult};
use crate::event::HostEvent;

use super::edit::attributed;
use super::assemble::{assemble, Members};
use super::members::{self, Member, FACTS, FINDINGS, PROJECTION_KEY, SOURCES};
use super::{json_merge, Outcome};

/// The transport verb that reads a job without starting work: the one
/// middleware task the web product does not charge to the agent quota.
pub const JOB_STATUS: &str = "job_status";

/// The one API version this host speaks.
pub const API: &str = "napkin.middleware/1";

/// The `request_kind` whose replies are settled here. Every other kind is
/// passed back to the app exactly as it came.
pub const REQUEST_KIND: &str = "middleware";

/// The job id the middleware acts under: every decision it records is
/// `process:middleware`, whichever handler computed it.
pub const JOB: &str = "middleware";

const CHAIN: &str = "agent/decision-chain.yaml";

/// The error type a middleware request gets when this host has no middleware:
/// no `proxies.middleware` in `workspace.yaml`, or a build with no network.
pub const NO_MIDDLEWARE: &str = "no_middleware";

/// The envelope a middleware request gets when none is configured. The error
/// has the `{type, message}` shape of a middleware error (§4), so an app reads
/// it the same way.
pub fn no_middleware() -> Value {
    serde_json::json!({
        "ok": false, "status": 0, "endpoint": null, "data": null,
        "error": { "type": NO_MIDDLEWARE,
                   "message": "No middleware is configured for this workspace (proxies.middleware in workspace.yaml)." },
    })
}

/// Refuse a reply that is not `napkin.middleware/1`.
///
/// This check is load-bearing, not tidiness. A middleware request is never
/// sent to the agent URL when no `middleware` proxy is configured (it is
/// answered [`no_middleware`] instead), but a `proxies.middleware` endpoint
/// pointed at the wrong service still answers with something of its own. Handing
/// that to the app as if the middleware had spoken — or worse, applying a
/// `change` out of it — is exactly the silent fall-through M4 forbids. So an
/// answer that does not name this API is an error the app sees, whatever else
/// it contains.
pub fn check_api(data: &Value) -> HostResult<()> {
    match data.get("api").and_then(Value::as_str) {
        Some(API) => Ok(()),
        Some(other) => Err(HostError::new(
            502,
            format!("middleware replied with api {other:?}; this host speaks {API}"),
        )),
        None => Err(HostError::new(
            502,
            format!(
                "the endpoint for request_kind \"middleware\" did not answer as {API} \
                 (no `api` field) — is a middleware proxy configured?"
            ),
        )),
    }
}

/// The context a middleware change is applied under: the actor is always
/// `process:middleware`, the handler and backend are the reply's, and the
/// scope is the one the shell resolved for whoever asked — never the reply's.
pub fn ctx_for(outer: &Ctx, reply: &Value) -> HostResult<Ctx> {
    let mut ctx = Ctx::new(Actor::process(JOB)?).with_scope(outer.scope.clone());
    if let Some(h) = reply.get("handler").and_then(Value::as_str) {
        ctx = ctx.with_handler(h);
    }
    if let Some(b) = reply
        .get("trace")
        .and_then(|t| t.get("backend"))
        .and_then(Value::as_str)
    {
        ctx = ctx.with_backend(b);
    }
    Ok(ctx)
}

/// What the app is told when a change is not applied.
pub fn refused(reason: impl Into<String>) -> Value {
    serde_json::json!({ "applied": false, "reason": reason.into() })
}

/// Apply the `change` a middleware reply carries to `doc`.
///
/// `reply` is the middleware's response body (the proxy envelope's `data`).
/// A refusal — the change was computed for another document, it would write
/// the host-owned projection, an entry is malformed, the schema rejects the
/// result — is not an error: it is an unchanged outcome whose reply says
/// `applied: false` and why, because the app asked for work and must be told
/// what became of it.
///
/// The reply on success is `{ applied: true, base_stale, applied_fields,
/// contested_fields, contests }`; the version it landed at is known only once
/// a store has applied it, so the caller adds it. A stale base is judged field
/// by field against the change's read-set (`split_patch`); a change of
/// which nothing is new is refused as `already applied`.
///
/// The job's state is not consulted: a `queued`, `running` or `needs_input`
/// reply may carry the change for the stages finished so far, and it applies
/// exactly as a `done` one. A later poll that repeats part of it adds only
/// what is new (see [`delivered_before`]). The envelope — the job's `state`, `stage` and `question` reach the app untouched.
pub fn apply(outer: &Ctx, doc: &Document, reply: &Value) -> HostResult<Outcome> {
    check_api(reply)?;
    let change = match reply.get("change") {
        Some(c @ Value::Object(_)) => c,
        None | Some(Value::Null) => {
            return Ok(Outcome::unchanged(refused("the reply carries no change")))
        }
        Some(_) => return Ok(Outcome::unchanged(refused("`change` is not an object"))),
    };
    let ctx = ctx_for(outer, reply)?;
    match plan(&ctx, doc, reply, change) {
        Ok(outcome) => Ok(outcome),
        Err(e) => Ok(Outcome::unchanged(refused(e.message))),
    }
}

fn plan(ctx: &Ctx, doc: &Document, reply: &Value, change: &Value) -> HostResult<Outcome> {
    let clan = doc.clan();

    // 1. The change must be for the document that is open, by its identity
    //    (`document_id`, stable across revisions) — not another one. Which
    //    revision it read is `base_version`'s to say.
    let open_id = clan.document_id();
    match change.get("doc").and_then(Value::as_str) {
        Some(d) if d == open_id => {}
        Some(d) => {
            return Err(HostError::conflict(format!(
                "change was computed for document {d}, but {open_id} is open"
            )))
        }
        None => return Err(HostError::bad_request("change names no `doc`")),
    }

    // 2. What it asks for, validated before anything is built.
    let data_patch = match change.get("data_patch") {
        None | Some(Value::Null) => None,
        Some(p @ Value::Object(o)) => {
            if o.contains_key(PROJECTION_KEY) {
                return Err(HostError::bad_request(
                    "data_patch writes `projection`, which only the host writes",
                ));
            }
            Some(p)
        }
        Some(_) => return Err(HostError::bad_request("data_patch is not an object")),
    };
    let read = match change.get("read") {
        None | Some(Value::Null) => None,
        Some(Value::Object(r)) => Some(r),
        Some(_) => return Err(HostError::bad_request("`read` is not an object")),
    };
    let facts_append = entries(change, "facts_append")?;
    let findings_append = entries(change, "findings_append")?;
    let sources_append = entries(change, "sources_append")?;
    let decisions = entries(change, "decisions")?;
    let rule = ReasoningRule::of(clan)?;
    for d in &decisions {
        check_reasoning(d, open_id, &rule)?;
        // Every decision must also read whole into the chain's shape, before
        // anything is built.
        decision(ctx, reply, d, "")?;
    }

    if data_patch.is_none()
        && facts_append.is_empty()
        && findings_append.is_empty()
        && sources_append.is_empty()
        && decisions.is_empty()
    {
        return Ok(Outcome::unchanged(serde_json::json!({
            "applied": true, "noop": true, "base_stale": base_stale(change, doc),
        })));
    }

    // 3. The base version. A change computed from the version the host holds
    //    applies whole. One computed from an older version is judged field by
    //    field against what the job read (N2): a field nobody touched since
    //    applies, a field someone changed becomes a contest, and the appends
    //    apply regardless. Without a read-set there is nothing to judge by.
    //
    //    W2-A4 HOOK: this is the host's per-field rule for a base that is
    //    already stale when the reply arrives. The store-level expected-version
    //    check — the document moving between this snapshot and the write — is
    //    still W2-A4's; `Change::base` below is what the store compares.
    let stale = base_stale(change, doc);
    let current = data_json(clan)?;
    let leaves = data_patch.map(patch_leaves).unwrap_or_default();
    if stale && read.is_none() && !leaves.is_empty() {
        return Err(HostError::conflict("stale base and no read-set; rerun"));
    }
    let chain_before = if clan.has_entry(CHAIN) {
        DecisionChain::from_yaml(&clan.read_entry(CHAIN)?)?
    } else {
        DecisionChain::default()
    };
    let known = known_decision_ids(&chain_before);
    let delivered = |field: &str| delivered_before(&decisions, open_id, &known, field);
    let split = split_patch(&leaves, read, &current, stale, &delivered)?;

    // 4. What is already in the document is not added again: a pin or finding
    //    with the same id and content, a decision with the same id, a contest
    //    already open over the same value.
    let handler = ctx.handler.clone().unwrap_or_else(|| JOB.to_string());
    let base = change
        .get("base_version")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let contests: Vec<&Contest> = split
        .contested
        .iter()
        .filter(|c| !already_contested(&chain_before, open_id, c))
        .collect();

    let facts_doc = members::read_doc(clan, FACTS)?;
    let mut facts = members::list_of(&facts_doc, FACTS)?;
    let new_facts = append(&mut facts, &facts_append, FACTS)?;
    let findings_doc = members::read_doc(clan, FINDINGS)?;
    let mut findings = members::list_of(&findings_doc, FINDINGS)?;
    let new_findings = append(&mut findings, &findings_append, FINDINGS)?;
    check_findings(&new_findings, &facts)?;
    let sources_doc = members::read_doc(clan, SOURCES)?;
    let mut sources = members::list_of(&sources_doc, SOURCES)?;
    let new_sources = append_sources(&mut sources, &sources_append)?;

    // A job decision about nothing but contested fields describes a write
    // that did not happen; it is kept inside the contest instead.
    let contested_paths: Vec<&str> = split.contested.iter().map(|c| c.path.as_str()).collect();
    let (withheld, recorded): (Vec<&Value>, Vec<&Value>) = decisions
        .iter()
        .filter(|d| {
            d.get("id")
                .and_then(Value::as_str)
                .map_or(true, |id| !known.contains(id))
        })
        .partition(|d| only_about(d, open_id, &contested_paths));

    let mut data = current.clone();
    json_merge(&mut data, &split.patch);
    if new_facts.is_empty()
        && new_findings.is_empty()
        && new_sources.is_empty()
        && recorded.is_empty()
        && contests.is_empty()
        && data == current
    {
        return Err(HostError::conflict("already applied"));
    }

    // 5. The decisions, in the order they are prepended: the job's own, then
    //    a contest for each field someone changed since the job read it.
    let now = now();
    let mut prepend = Vec::new();
    for d in &recorded {
        prepend.push(decision(ctx, reply, d, &now)?);
    }
    let mut contest_ids = Vec::new();
    for c in &contests {
        let held: Vec<&Value> = withheld
            .iter()
            .copied()
            .filter(|d| touches(d, open_id, &c.path))
            .collect();
        let d = contest_decision(ctx, open_id, &handler, base, doc, c, &held, &now)?;
        contest_ids.push(d.id.clone().unwrap_or_default());
        prepend.push(d);
    }

    // 6. One archive: the applicable part of the patch merged over what is
    //    there, the members, the projection rebuilt from them, the decisions.
    let bytes = assemble(
        clan,
        data,
        Members {
            facts,
            findings,
            sources,
        },
        prepend,
        &format!("middleware change from {handler}"),
        &now,
        true,
    )?;

    let keys: Vec<String> = split
        .patch
        .as_object()
        .map(|o| o.keys().cloned().collect())
        .unwrap_or_default();
    let notice = serde_json::json!({
        "ok": true,
        "keys": keys,
        "source": REQUEST_KIND,
        "handler": ctx.handler,
        "facts_appended": new_facts.len(),
        "findings_appended": new_findings.len(),
        "sources_appended": new_sources.len(),
        "decisions_appended": recorded.len() + contests.len(),
    });
    let change: Change = doc
        .change(bytes)?
        .with_event(HostEvent::DataChanged(notice));
    let contested_fields: Vec<&str> = split.contested.iter().map(|c| c.path.as_str()).collect();
    Ok(Outcome::changed(
        serde_json::json!({
            "applied": true,
            "base_stale": stale,
            "applied_fields": split.applied,
            "contested_fields": contested_fields,
            "contests": contest_ids,
        }),
        change,
    ))
}

/// True unless the change names the version the host holds. A change that
/// names none is treated as stale: nothing says what it read.
fn base_stale(change: &Value, doc: &Document) -> bool {
    change
        .get("base_version")
        .and_then(Value::as_str)
        .map(|b| b != doc.version().as_str())
        .unwrap_or(true)
}

/// A field the job read and someone changed before its change arrived.
struct Contest {
    /// The read-set path, dotted (`campaign.problem`).
    path: String,
    /// What the document holds there now.
    current: Value,
    /// What the job read there.
    read: Value,
    /// What the job would have written: its patch merged over what it read.
    proposed: Value,
}

/// The data patch sorted into what applies and what is contested.
struct Split {
    /// A merge patch of only the leaves that apply.
    patch: Value,
    /// The field paths that applied, as the read-set names them (or, without
    /// one, the patch's fields to two levels).
    applied: Vec<String>,
    contested: Vec<Contest>,
}

/// A path a merge patch sets, and the value it sets there.
type Leaf = (Vec<String>, Value);

/// Every path a merge patch sets: its leaves (a non-object, `null` included,
/// or an empty object), each with the value it sets.
fn patch_leaves(patch: &Value) -> Vec<Leaf> {
    fn walk(v: &Value, at: &mut Vec<String>, out: &mut Vec<Leaf>) {
        match v {
            Value::Object(o) if !o.is_empty() => {
                for (k, child) in o {
                    at.push(k.clone());
                    walk(child, at, out);
                    at.pop();
                }
            }
            _ if !at.is_empty() => out.push((at.clone(), v.clone())),
            _ => {}
        }
    }
    let mut out = Vec::new();
    walk(patch, &mut Vec::new(), &mut out);
    out
}

fn get_path<'a>(v: &'a Value, path: &[&str]) -> Option<&'a Value> {
    path.iter().try_fold(v, |v, k| v.get(*k))
}

fn set_path(v: &mut Value, path: &[String], leaf: Value) {
    let mut at = v;
    for k in &path[..path.len() - 1] {
        if !at.is_object() {
            *at = Value::Object(Default::default());
        }
        at = at
            .as_object_mut()
            .unwrap()
            .entry(k.clone())
            .or_insert_with(|| Value::Object(Default::default()));
    }
    if !at.is_object() {
        *at = Value::Object(Default::default());
    }
    at.as_object_mut()
        .unwrap()
        .insert(path[path.len() - 1].clone(), leaf);
}

/// The read-set key a patched path is judged by: the longest one at or above
/// it.
fn covering<'a>(read: &'a serde_json::Map<String, Value>, leaf: &[String]) -> Option<&'a str> {
    let dotted = leaf.join(".");
    read.keys()
        .filter(|k| under(&dotted, k))
        .max_by_key(|k| k.len())
        .map(String::as_str)
}

/// True when this change already reached the document for `field`: some of
/// its decisions are about that field, and every one of them is already in
/// the chain (recorded, or held in a contest).
///
/// A long job's polls repeat what earlier stages delivered (`start_campaign`
/// answers each poll with the stages finished so far). The repeated part was
/// judged when it first arrived — applied or contested — and a person may have
/// acted on it since, confirming the field or resolving the contest. Judging
/// it again against the stale base would contest the person's value with the
/// job's own earlier write, so it is skipped: neither applied again nor
/// contested. A field no decision speaks for is judged as usual (a repeat of
/// it is a no-op there because the document already holds what it writes).
fn delivered_before(
    decisions: &[Value],
    doc_id: &str,
    known: &std::collections::BTreeSet<String>,
    field: &str,
) -> bool {
    let mut about = decisions
        .iter()
        .filter(|d| touches(d, doc_id, field))
        .peekable();
    about.peek().is_some()
        && about.all(|d| {
            d.get("id")
                .and_then(Value::as_str)
                .is_some_and(|id| known.contains(id))
        })
}

/// Sort the patch's leaves by the stale-base rule. A field this change
/// already delivered (`delivered`) is skipped whole. On a current base every
/// other leaf applies. On a stale one each leaf is judged by the read-set key that
/// covers it, all of a key's leaves together: if the document still holds
/// there what the job read — or already holds what the job would write — they
/// apply; if not, that key becomes one contest and none of its leaves is
/// written. A leaf no key covers cannot be judged, and refuses the change.
fn split_patch(
    leaves: &[Leaf],
    read: Option<&serde_json::Map<String, Value>>,
    current: &Value,
    stale: bool,
    delivered: &dyn Fn(&str) -> bool,
) -> HostResult<Split> {
    let mut patch = Value::Object(Default::default());
    let mut applied: Vec<String> = Vec::new();
    let mut contested: Vec<Contest> = Vec::new();

    // Leaves grouped by the field they are judged as, in patch order.
    let mut groups: Vec<(String, Vec<&Leaf>)> = Vec::new();
    for leaf in leaves {
        let key = match read.and_then(|r| covering(r, &leaf.0)) {
            Some(k) => k.to_string(),
            None if stale => {
                return Err(HostError::conflict(format!(
                    "stale base and the read-set does not cover {}; rerun",
                    leaf.0.join(".")
                )))
            }
            None => leaf.0.iter().take(2).cloned().collect::<Vec<_>>().join("."),
        };
        match groups.iter_mut().find(|(k, _)| *k == key) {
            Some((_, g)) => g.push(leaf),
            None => groups.push((key, vec![leaf])),
        }
    }

    for (key, group) in groups {
        if delivered(&key) {
            continue;
        }
        let apply_group = |patch: &mut Value| {
            for (path, value) in &group {
                set_path(patch, path, value.clone());
            }
        };
        if !stale {
            apply_group(&mut patch);
            applied.push(key);
            continue;
        }
        let segs: Vec<&str> = key.split('.').collect();
        let now = get_path(current, &segs).cloned().unwrap_or(Value::Null);
        let was = read
            .and_then(|r| r.get(&key))
            .cloned()
            .unwrap_or(Value::Null);
        // What the job would leave there: its leaves merged over what it read.
        let mut sub = Value::Object(Default::default());
        let mut whole = None;
        for (path, value) in &group {
            let rest = &path[segs.len()..];
            if rest.is_empty() {
                whole = Some(value.clone());
            } else {
                set_path(&mut sub, rest, value.clone());
            }
        }
        let mut proposed = was.clone();
        if let Some(w) = whole {
            json_merge(&mut proposed, &w);
        }
        json_merge(&mut proposed, &sub);

        if now == was || now == proposed {
            apply_group(&mut patch);
            applied.push(key);
        } else {
            contested.push(Contest {
                path: key,
                current: now,
                read: was,
                proposed,
            });
        }
    }
    Ok(Split {
        patch,
        applied,
        contested,
    })
}

/// Every decision id the chain holds, including the job decisions a contest
/// withheld — those were received, and a second delivery is not new.
fn known_decision_ids(chain: &DecisionChain) -> std::collections::BTreeSet<String> {
    let mut ids: std::collections::BTreeSet<String> =
        chain.ids().into_iter().map(String::from).collect();
    for d in &chain.decisions {
        if let Some(serde_yaml::Value::Sequence(held)) = d.extra.get("withheld") {
            ids.extend(
                held.iter()
                    .filter_map(|h| h.get("id").and_then(|v| v.as_str()))
                    .map(String::from),
            );
        }
    }
    ids
}

/// A contest over the same field and the same proposed value is already open.
fn already_contested(chain: &DecisionChain, doc_id: &str, c: &Contest) -> bool {
    let target = format!("{doc_id}#{}", c.path);
    let Ok(proposed) = serde_yaml::to_value(&c.proposed) else {
        return false;
    };
    chain.decisions.iter().any(|d| {
        d.kind.as_deref() == Some("contest")
            && d.targets.contains(&target)
            && matches!(d.extra.get("values"), Some(serde_yaml::Value::Sequence(vs))
                if vs.iter().any(|v| v.get("value") == Some(&proposed)))
    })
}

/// The paths a job decision targets on the open document, in the dotted form
/// read-set keys and patch paths use (see [`normalise_target`]). A target that
/// is on another document, or that does not parse, names nothing here.
fn target_paths(d: &Value, doc_id: &str) -> Vec<String> {
    d.get("targets")
        .and_then(Value::as_array)
        .map(|a| {
            a.iter()
                .filter_map(Value::as_str)
                .filter_map(|t| normalise_target(t, doc_id))
                .collect()
        })
        .unwrap_or_default()
}

/// An address `<doc-id>#<entity-keyed path>` (Contract 3 §2.4) as the dotted
/// path it names on `doc_id`: `<doc-id>#intake.messages[msg_X]` is
/// `intake.messages.msg_X`, `<doc-id>#report` is `report`.
///
/// `None` — matching nothing — for an address on another document, one with no
/// `#`, and one whose path is malformed: an empty segment, an unclosed or stray
/// bracket, a bracket not following a segment, or a key that is empty or holds
/// `.`, `[`, `]` or `#`. Map keys in this schema are id-like, so anything else
/// is not guessed at.
fn normalise_target(target: &str, doc_id: &str) -> Option<String> {
    let (doc, path) = target.split_once('#')?;
    if doc != doc_id || path.is_empty() {
        return None;
    }
    let bad = |c: char| matches!(c, '.' | '[' | ']' | '#');
    let mut out: Vec<&str> = Vec::new();
    let mut rest = path;
    // At the start of a segment: a name up to the next `.` or `[`.
    loop {
        let end = rest.find(['.', '[']).unwrap_or(rest.len());
        let name = &rest[..end];
        if name.is_empty() || name.contains(bad) {
            return None;
        }
        out.push(name);
        rest = &rest[end..];
        // Any number of `[key]` after it.
        while let Some(after) = rest.strip_prefix('[') {
            let close = after.find(']')?;
            let key = &after[..close];
            if key.is_empty() || key.contains(bad) {
                return None;
            }
            out.push(key);
            rest = &after[close + 1..];
        }
        match rest.strip_prefix('.') {
            Some(next) => rest = next,
            None if rest.is_empty() => return Some(out.join(".")),
            None => return None,
        }
    }
}

/// True when dotted `path` is `field` or lies under it.
fn under(path: &str, field: &str) -> bool {
    path == field || path.starts_with(&format!("{field}."))
}

/// True when every target of a job decision is at or under one of `paths`.
fn only_about(d: &Value, doc_id: &str, paths: &[&str]) -> bool {
    let targets = target_paths(d, doc_id);
    !targets.is_empty()
        && targets
            .iter()
            .all(|t| paths.iter().any(|field| under(t, field)))
}

/// True when some target of a job decision is at or under `field`.
fn touches(d: &Value, doc_id: &str, field: &str) -> bool {
    target_paths(d, doc_id).iter().any(|t| under(t, field))
}

/// The `contest` decision a stale write over a changed field opens: both
/// values, nothing picked (Contract 4 §4, N2). The document keeps what it
/// holds; the job's value waits in the contest with the decisions that
/// justified it.
#[allow(clippy::too_many_arguments)]
fn contest_decision(
    ctx: &Ctx,
    doc_id: &str,
    handler: &str,
    base: &str,
    doc: &Document,
    c: &Contest,
    held: &[&Value],
    now: &str,
) -> HostResult<Decision> {
    let yaml = |v: &Value| {
        serde_yaml::to_value(v).map_err(|e| HostError::internal(format!("contest value: {e}")))
    };
    let mut d = attributed(ctx, handler, "contest");
    d.id = Some(clan_sdk::decision::new_decision_id());
    d.agent = handler.to_string();
    d.action = "contested a stale write".into();
    d.rationale = format!(
        "{handler} read {} at {base}, but the document changed it before the job's \
         change arrived; the job's value was not written over it.",
        c.path
    );
    d.timestamp = now.to_string();
    d.targets = vec![format!("{doc_id}#{}", c.path)];
    d.cites = held
        .iter()
        .filter_map(|h| h.get("id").and_then(Value::as_str).map(String::from))
        .collect();
    d.reasoning = Some(contest_reasoning(doc_id, handler, base, doc, c, &d.cites));
    d.extra.insert("status".into(), "open".into());
    let values = serde_json::json!([
        { "value": c.current, "from": "document", "version": doc.version().as_str() },
        { "value": c.proposed, "from": handler, "base_version": base, "read": c.read },
    ]);
    d.extra.insert("values".into(), yaml(&values)?);
    if !held.is_empty() {
        let held: Vec<Value> = held.iter().map(|v| (*v).clone()).collect();
        d.extra
            .insert("withheld".into(), yaml(&Value::Array(held))?);
    }
    Ok(d)
}

/// The host's own reasoning for a contest it opens: it is the one deciding
/// here, so it says why in the same shape it asks of the middleware.
fn contest_reasoning(
    doc_id: &str,
    handler: &str,
    base: &str,
    doc: &Document,
    c: &Contest,
    held: &[String],
) -> Reasoning {
    let field = format!("{doc_id}#{}", c.path);
    let mut job_cites = vec![field.clone()];
    job_cites.extend(held.iter().cloned());
    Reasoning {
        decided: format!(
            "Held {handler}'s value for {} in a contest instead of writing it.",
            c.path
        ),
        because: vec![
            ReasonPoint {
                point: format!(
                    "{handler} computed its value from version {base}, where the field held what it read."
                ),
                cites: job_cites,
                ..Default::default()
            },
            ReasonPoint {
                point: format!(
                    "The document changed the field before the change arrived; it now holds another value at version {}.",
                    doc.version().as_str()
                ),
                cites: vec![field],
                ..Default::default()
            },
        ],
        rejected: vec![
            Rejected {
                option: "write the job's value".into(),
                why: "it would silently overwrite a change made after the job read the field".into(),
                ..Default::default()
            },
            Rejected {
                option: "drop the job's value".into(),
                why: "the job's evidence would be lost; the contest keeps it with its decisions".into(),
                ..Default::default()
            },
        ],
        certainty: Certainty {
            level: "high".into(),
            why: "the versions and both values are recorded as the host saw them".into(),
            ..Default::default()
        },
        would_change_if: "a person resolves the contest, or the job reruns on the current version"
            .into(),
        attention: Some("a stale write met a newer value; a person should pick".into()),
        ..Default::default()
    }
}

/// Decision kinds whose reasoning is required whatever the app declares
/// (Contract 4 §3, R1): each is a judgement someone will ask "why" of. An app
/// can add to this floor, never lower it.
pub const REASONED_KINDS: &[&str] = &["pin", "contest", "finding", "verdict"];

/// An `edit` with this action is a proposal (middleware-api.md §10.5): the
/// value a person is asked to take. It is required to say why, always.
pub const PROPOSE_ACTION: &str = "propose";

/// Which middleware decisions must carry `reasoning` (R1) — declared by the
/// app, not known to the OS layer.
///
/// The app declares it in its `app/pipeline.yaml`, which travels in every
/// document made from it:
///
/// ```yaml
/// reasoning:
///   kinds: [pin, contest, finding]          # added to the floor
///   edits: [campaign, selection, report]    # an edit targeting a path at or under one of these
/// ```
///
/// With no declaration the floor applies: every `pin`, `contest`, `finding`
/// and `verdict`, and every `edit` whose action is `propose`. `edits` are
/// dotted data paths (`campaign`, `objectives.commercial`), matched against a
/// decision's `targets` on the open document after bracket keys are
/// normalised (`materials[mat_1]` is `materials.mat_1`).
#[derive(Debug, Clone, Default, PartialEq)]
pub struct ReasoningRule {
    /// Kinds the app requires on top of [`REASONED_KINDS`].
    pub kinds: Vec<String>,
    /// Data paths an `edit` must say why it wrote.
    pub edits: Vec<String>,
}

impl ReasoningRule {
    /// The rule `pipeline` (the parsed `app/pipeline.yaml`, `Null` when the
    /// document has none) declares. A `reasoning` block that is not the shape
    /// above is an error — a declaration the host cannot read is not quietly
    /// treated as none (M4).
    pub fn from_pipeline(pipeline: &Value) -> HostResult<Self> {
        let block = match pipeline.get("reasoning") {
            None | Some(Value::Null) => return Ok(Self::default()),
            Some(b @ Value::Object(_)) => b,
            Some(_) => {
                return Err(HostError::bad_request(
                    "app/pipeline.yaml `reasoning` is not a map of `kinds` and `edits`",
                ))
            }
        };
        let list = |key: &str| -> HostResult<Vec<String>> {
            match block.get(key) {
                None | Some(Value::Null) => Ok(Vec::new()),
                Some(Value::Array(items)) => items
                    .iter()
                    .map(|i| match i.as_str().map(str::trim) {
                        Some(s) if !s.is_empty() => Ok(s.to_string()),
                        _ => Err(HostError::bad_request(format!(
                            "app/pipeline.yaml `reasoning.{key}` holds something that is not a name"
                        ))),
                    })
                    .collect(),
                Some(_) => Err(HostError::bad_request(format!(
                    "app/pipeline.yaml `reasoning.{key}` is not a list"
                ))),
            }
        };
        let kinds = list("kinds")?;
        if let Some(k) = kinds
            .iter()
            .find(|k| !clan_sdk::decision::DECISION_KINDS.contains(&k.as_str()))
        {
            return Err(HostError::bad_request(format!(
                "app/pipeline.yaml `reasoning.kinds` names {k:?}, which is not a decision kind"
            )));
        }
        Ok(Self {
            kinds,
            edits: list("edits")?,
        })
    }

    /// The rule the open document's app declares.
    pub fn of(clan: &ClanFile) -> HostResult<Self> {
        let pipeline = match clan.read_entry("app/pipeline.yaml") {
            Ok(bytes) => {
                let y: serde_yaml::Value = serde_yaml::from_slice(&bytes).map_err(|e| {
                    HostError::bad_request(format!("app/pipeline.yaml does not parse: {e}"))
                })?;
                serde_json::to_value(y).map_err(|e| HostError::internal(e.to_string()))?
            }
            Err(_) => Value::Null,
        };
        Self::from_pipeline(&pipeline)
    }

    /// True when a middleware decision must carry `reasoning`.
    pub fn requires(&self, d: &Value, doc_id: &str) -> bool {
        let kind = d.get("kind").and_then(Value::as_str).unwrap_or("edit");
        if REASONED_KINDS.contains(&kind) || self.kinds.iter().any(|k| k == kind) {
            return true;
        }
        kind == "edit"
            && (d.get("action").and_then(Value::as_str) == Some(PROPOSE_ACTION)
                || target_paths(d, doc_id)
                    .iter()
                    .any(|t| self.edits.iter().any(|p| under(t, p))))
    }

    /// What the refusal says is required, in words.
    fn describe(&self) -> String {
        let mut kinds: Vec<&str> = REASONED_KINDS.to_vec();
        kinds.extend(
            self.kinds
                .iter()
                .map(String::as_str)
                .filter(|k| !REASONED_KINDS.contains(k)),
        );
        let mut s = format!("{} decisions, and proposals", kinds.join(", "));
        if !self.edits.is_empty() {
            s.push_str(&format!(
                ", and edits that write {} (as this document's app declares)",
                self.edits.join(", ")
            ));
        }
        s
    }
}

/// Refuse a decision whose reasoning is malformed, or absent where the app's
/// rule requires it.
///
/// Refusal rather than accept-and-flag: reasoning is what the decider knew
/// when it decided, so it cannot be supplied later without being invented,
/// and a chain that fills with unexplained agent writes is exactly what the
/// viewer's decision blocks exist to prevent. The middleware is ours and the
/// refusal names the decision and the gap, so the fix is at the source and a
/// rerun lands it — the same rule as a finding citing a pin the document does
/// not hold.
fn check_reasoning(d: &Value, doc_id: &str, rule: &ReasoningRule) -> HostResult<()> {
    let id = d.get("id").and_then(Value::as_str).unwrap_or("?");
    let kind = d.get("kind").and_then(Value::as_str).unwrap_or("edit");
    match d.get("reasoning") {
        None | Some(Value::Null) if rule.requires(d, doc_id) => {
            Err(HostError::bad_request(format!(
                "decision {id} ({kind}) carries no reasoning; napkin.middleware/1 requires it on {}",
                rule.describe()
            )))
        }
        None | Some(Value::Null) => Ok(()),
        Some(r) => {
            let r: Reasoning = serde_json::from_value(r.clone()).map_err(|e| {
                HostError::bad_request(format!("decision {id}: reasoning is malformed ({e})"))
            })?;
            let problems = r.problems();
            if problems.is_empty() {
                Ok(())
            } else {
                Err(HostError::bad_request(format!(
                    "decision {id} ({kind}): {}",
                    problems.join("; ")
                )))
            }
        }
    }
}

/// A list the change may carry; absent and `null` are both empty.
fn entries(change: &Value, key: &str) -> HostResult<Vec<Value>> {
    match change.get(key) {
        None | Some(Value::Null) => Ok(Vec::new()),
        Some(Value::Array(items)) => {
            if items.iter().any(|i| !i.is_object()) {
                return Err(HostError::bad_request(format!(
                    "every entry of {key} must be an object"
                )));
            }
            Ok(items.clone())
        }
        Some(_) => Err(HostError::bad_request(format!("{key} is not a list"))),
    }
}

/// Append `new` to a member's entries and return the ones that were new.
/// Every entry needs an id — the projection and every address key on it. An
/// entry already present with the same content is skipped (a second delivery
/// of the same change); one present with different content is refused rather
/// than overwritten: a pin is frozen, and the merge that decides "same
/// identity, one pin" is the middleware's to do.
fn append(items: &mut Vec<serde_yaml::Value>, new: &[Value], m: Member) -> HostResult<Vec<Value>> {
    let mut added = Vec::new();
    for entry in new {
        let id = entry
            .get("id")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .ok_or_else(|| HostError::bad_request(format!("an entry for {} has no id", m.path)))?;
        let y = serde_yaml::to_value(entry)
            .map_err(|e| HostError::bad_request(format!("{id} is not representable: {e}")))?;
        if let Some(held) = items.iter().find(|e| members::entry_id(e) == Some(id)) {
            if *held == y {
                continue;
            }
            return Err(HostError::conflict(format!(
                "{} already holds {id} with different content",
                m.path
            )));
        }
        items.push(y);
        added.push(entry.clone());
    }
    Ok(added)
}

/// Append source records, returning the ones that were new.
///
/// A source is who said something, not a claim: the same id delivered again
/// with a different title or retrieval date (two lenses citing one page) is
/// the same source, so the record first delivered is kept and the later one
/// dropped — never a conflict. Each record needs a `src_` id and the `uri`
/// it was read from; a verification by a person is cited as `human:<id>` and
/// has no record here.
fn append_sources(items: &mut Vec<serde_yaml::Value>, new: &[Value]) -> HostResult<Vec<Value>> {
    let mut added = Vec::new();
    for entry in new {
        let id = entry
            .get("id")
            .and_then(Value::as_str)
            .filter(|s| s.starts_with("src_") && s.len() > 4)
            .ok_or_else(|| {
                HostError::bad_request(format!(
                    "a source for {} has no `src_` id",
                    SOURCES.path
                ))
            })?;
        if entry
            .get("uri")
            .and_then(Value::as_str)
            .map_or(true, |u| u.trim().is_empty())
        {
            return Err(HostError::bad_request(format!("source {id} has no uri")));
        }
        if items.iter().any(|e| members::entry_id(e) == Some(id))
            || added
                .iter()
                .any(|e: &Value| e.get("id").and_then(Value::as_str) == Some(id))
        {
            continue;
        }
        let y = serde_yaml::to_value(entry)
            .map_err(|e| HostError::bad_request(format!("{id} is not representable: {e}")))?;
        items.push(y);
        added.push(entry.clone());
    }
    Ok(added)
}

/// What the host holds the middleware to about findings (Contract 3 §6, D1
/// amended): they arrive `proposed` — only a human verifies — and every pin a
/// finding cites is in the facts member.
fn check_findings(new: &[Value], facts: &[serde_yaml::Value]) -> HostResult<()> {
    for f in new {
        let id = f.get("id").and_then(Value::as_str).unwrap_or("?");
        if f.get("status").and_then(Value::as_str) != Some("proposed") {
            return Err(HostError::bad_request(format!(
                "finding {id} is not `proposed`; only a human verifies or rejects a finding"
            )));
        }
        for cite in f
            .get("cites")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
        {
            let cite = cite.as_str().unwrap_or_default();
            if !facts.iter().any(|e| members::entry_id(e) == Some(cite)) {
                return Err(HostError::bad_request(format!(
                    "finding {id} cites {cite}, which is not in {}",
                    FACTS.path
                )));
            }
        }
    }
    Ok(())
}

/// The decision fields the host owns: whatever the middleware put there, the
/// recorded value is the host's (Contract 4 §3 — copied from `Ctx`, never from
/// the body). A decision that said something else keeps what it said as
/// `claimed_<field>`, so nothing it sent is lost.
const HOST_OWNED: &[&str] = &["actor", "scope", "handler", "backend", "timestamp"];

/// One middleware decision as the SDK's `Decision` holds it.
///
/// **Nothing the middleware sent is dropped.** The body is read whole into the
/// SDK's struct: identity, kind, targets, cites, the verdict fields
/// (`polarity`, `reason_code`, `taxonomy_version`, `reviewer_role`), `licence`,
/// `fields_changed`, `superseded_by`, and every key the struct does not name —
/// the flatten tail (`abstained`, `material_read`, `unread`,
/// `proposed_value`, …) — survive as sent.
///
/// Attribution is the context's: `actor` is always `process:middleware`,
/// `handler` and `backend` are the reply's (a decision that names its own
/// fills in only what the reply left out), `scope` is the one the shell
/// resolved, `timestamp` is when the host recorded it. Where the body said
/// otherwise for one of those, its value is kept as `claimed_<field>`
/// ([`HOST_OWNED`]). `agent` keeps the middleware's claim — the body's
/// `agent`, else the reply's handler — and `claimed_agent` is the body's own
/// when it sent one, else the agent when that is not the actor. The rationale
/// is the middleware's, verbatim, or the reasoning's one-line summary when it
/// sent none.
fn decision(ctx: &Ctx, reply: &Value, d: &Value, now: &str) -> HostResult<Decision> {
    let s = |k: &str| d.get(k).and_then(Value::as_str);
    let id = s("id")
        .filter(|s| !s.is_empty())
        .ok_or_else(|| HostError::bad_request("a decision has no id"))?;
    let kind = s("kind").unwrap_or("edit").to_string();

    let mut ctx = ctx.clone();
    if ctx.handler.is_none() {
        ctx.handler = s("handler").map(String::from);
    }
    if ctx.backend.is_none() {
        ctx.backend = s("backend").map(String::from);
    }
    let agent = s("agent")
        .or_else(|| reply.get("handler").and_then(Value::as_str))
        .unwrap_or(JOB)
        .to_string();
    let attribution = attributed(&ctx, &agent, &kind);

    // The body, with the host-owned fields set aside and the fields the SDK
    // requires filled, read into the struct in one go.
    let mut body = d.as_object().cloned().unwrap_or_default();
    let mut claims = Vec::new();
    for key in HOST_OWNED {
        if let Some(v) = body.remove(*key) {
            claims.push((*key, v));
        }
    }
    body.insert("kind".into(), Value::String(kind.clone()));
    body.insert("agent".into(), Value::String(agent.clone()));
    body.insert(
        "action".into(),
        Value::String(s("action").unwrap_or(&kind).to_string()),
    );
    body.insert("timestamp".into(), Value::String(now.to_string()));
    let rationale = s("rationale").filter(|r| !r.trim().is_empty());
    body.insert(
        "rationale".into(),
        Value::String(rationale.unwrap_or_default().to_string()),
    );
    if body.get("reasoning") == Some(&Value::Null) {
        body.remove("reasoning");
    }
    let mut out: Decision = serde_json::from_value(Value::Object(body))
        .map_err(|e| HostError::bad_request(format!("decision {id} is malformed ({e})")))?;

    out.actor = attribution.actor;
    out.handler = attribution.handler;
    out.backend = attribution.backend;
    out.scope = attribution.scope;
    if out.claimed_agent.as_deref().map_or(true, str::is_empty) {
        out.claimed_agent = attribution.claimed_agent;
    }
    if rationale.is_none() {
        if let Some(r) = &out.reasoning {
            out.rationale = r.summary();
        }
    }
    let recorded = serde_json::to_value(&out).unwrap_or(Value::Null);
    for (key, said) in claims {
        if said.is_null() || recorded.get(key) == Some(&said) {
            continue;
        }
        let said = serde_yaml::to_value(&said)
            .map_err(|e| HostError::bad_request(format!("decision {id}: {key}: {e}")))?;
        out.extra.insert(format!("claimed_{key}"), said);
    }
    Ok(out)
}

fn data_json(clan: &ClanFile) -> HostResult<Value> {
    let bytes = clan.read_entry("shared/data.yaml")?;
    let y: serde_yaml::Value = serde_yaml::from_slice(&bytes)
        .map_err(|e| HostError::internal(format!("shared/data.yaml does not parse: {e}")))?;
    let v = serde_json::to_value(y).map_err(|e| HostError::internal(e.to_string()))?;
    Ok(if v.is_null() {
        Value::Object(Default::default())
    } else {
        v
    })
}

/// Now, in the one shape the campaign schema's `datetime` accepts.
fn now() -> String {
    chrono::Utc::now().format("%Y-%m-%dT%H:%M:%SZ").to_string()
}
