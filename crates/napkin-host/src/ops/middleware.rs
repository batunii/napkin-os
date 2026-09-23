// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! Applying what the middleware computed — the host half of
//! `napkin.middleware/1`.
//!
//! The middleware never writes a document and neither does the app that asked
//! it. It answers with a `change` — a merge patch over `shared/data.yaml`,
//! entries to append to the facts and findings members, and the decisions
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
    compress_chain, pack, AgentOutput, ClanBuilder, ClanFile, CompressionConfig, Decision,
    DecisionChain, PackOptions,
};
use serde_json::Value;

use crate::ctx::{Actor, Ctx};
use crate::document::{Change, Document};
use crate::error::{HostError, HostResult};
use crate::event::HostEvent;

use super::edit::attributed;
use super::members::{self, Member, FACTS, FINDINGS, PROJECTION_KEY};
use super::{json_merge, Outcome};

/// The one API version this host speaks.
pub const API: &str = "napkin.middleware/1";

/// The `request_kind` whose replies are settled here. Every other kind is
/// passed back to the app exactly as it came.
pub const REQUEST_KIND: &str = "middleware";

/// The job id the middleware acts under: every decision it records is
/// `process:middleware`, whichever handler computed it.
pub const JOB: &str = "middleware";

const CHAIN: &str = "agent/decision-chain.yaml";

/// Refuse a reply that is not `napkin.middleware/1`.
///
/// This check is load-bearing, not tidiness. `resolve_proxy` falls back to the
/// generic agent URL when no `middleware` proxy is configured, and that agent
/// answers `request_kind: "middleware"` with something of its own. Handing
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
/// The reply on success is `{ applied: true, base_stale }`; the version it
/// landed at is known only once a store has applied it, so the caller adds it.
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

    // 2. The base version. W2-A4 HOOK: the expected-version check is not
    //    implemented anywhere yet, so a change computed from an older version
    //    is still applied — over whatever the document holds now — and the
    //    reply says so (`base_stale`). When W2-A4 lands, this is where a stale
    //    base becomes a refusal (or a rebase), and `Change::base` below is what
    //    the store compares at apply.
    let base_stale = change
        .get("base_version")
        .and_then(Value::as_str)
        .map(|b| b != doc.version().as_str())
        .unwrap_or(true);

    // 3. What it asks for, validated before anything is built.
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
    let facts_append = entries(change, "facts_append")?;
    let findings_append = entries(change, "findings_append")?;
    let decisions = entries(change, "decisions")?;

    if data_patch.is_none()
        && facts_append.is_empty()
        && findings_append.is_empty()
        && decisions.is_empty()
    {
        return Ok(Outcome::unchanged(serde_json::json!({
            "applied": true, "noop": true, "base_stale": base_stale,
        })));
    }

    // 4. The members, with their appends. Both are written — and registered —
    //    on every apply, so the projection always has two hashes to name.
    let facts_doc = members::read_doc(clan, FACTS)?;
    let mut facts = members::list_of(&facts_doc, FACTS)?;
    append(&mut facts, &facts_append, FACTS)?;
    let findings_doc = members::read_doc(clan, FINDINGS)?;
    let mut findings = members::list_of(&findings_doc, FINDINGS)?;
    append(&mut findings, &findings_append, FINDINGS)?;
    check_findings(&findings_append, &facts)?;

    let facts_bytes = members::write_doc(facts_doc, FACTS, facts.clone())?;
    let findings_bytes = members::write_doc(findings_doc, FINDINGS, findings.clone())?;

    // 5. The data: the patch merged over what is there, then the projection
    //    rebuilt from the member bytes just written. `pack` validates the
    //    whole result against the document's schema.
    let now = now();
    let mut data = data_json(clan)?;
    if let Some(p) = data_patch {
        json_merge(&mut data, p);
    }
    if let Some(obj) = data.as_object_mut() {
        obj.insert(
            PROJECTION_KEY.into(),
            members::projection(&facts, &facts_bytes, &findings, &findings_bytes, &now),
        );
    }

    let handler = ctx.handler.clone().unwrap_or_else(|| JOB.to_string());
    let packed = pack(
        clan,
        AgentOutput {
            mode: "data-update".into(),
            structured: data,
            design: None,
            human: None,
            decision: None,
        },
        PackOptions {
            delta: Some(format!("middleware change from {handler}")),
            ..Default::default()
        },
        None,
    )?;

    // 6. One archive: the packed generation, plus the two members registered
    //    with their roles and the decisions prepended to its chain.
    let packed = ClanFile::from_bytes(packed)?;
    let mut manifest = packed.manifest().clone();
    members::register(&mut manifest, FACTS);
    members::register(&mut manifest, FINDINGS);

    let mut chain = if packed.has_entry(CHAIN) {
        DecisionChain::from_yaml(&packed.read_entry(CHAIN)?)?
    } else {
        DecisionChain::default()
    };
    for d in &decisions {
        chain.prepend(decision(ctx, reply, d, &now)?);
    }
    compress_chain(&mut chain, &CompressionConfig::default(), None);

    let mut builder = ClanBuilder::new(manifest);
    for (path, bytes) in packed.read_all_entries()? {
        if path == clan_sdk::MANIFEST_PATH {
            continue;
        }
        builder.add_entry(path, bytes);
    }
    builder.add_entry(FACTS.path, facts_bytes);
    builder.add_entry(FINDINGS.path, findings_bytes);
    builder.add_entry(CHAIN, chain.to_yaml()?);
    let bytes = builder.build()?;

    let keys: Vec<String> = data_patch
        .and_then(Value::as_object)
        .map(|o| o.keys().cloned().collect())
        .unwrap_or_default();
    let notice = serde_json::json!({
        "ok": true,
        "keys": keys,
        "source": REQUEST_KIND,
        "handler": ctx.handler,
        "facts_appended": facts_append.len(),
        "findings_appended": findings_append.len(),
        "decisions_appended": decisions.len(),
    });
    let change: Change = doc
        .change(bytes)?
        .with_event(HostEvent::DataChanged(notice));
    Ok(Outcome::changed(
        serde_json::json!({ "applied": true, "base_stale": base_stale }),
        change,
    ))
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

/// Append `new` to a member's entries. Every entry needs an id — the
/// projection and every address key on it — and an id already present is
/// refused rather than duplicated or overwritten: a pin is frozen, and the
/// merge that decides "same identity, one pin" is the middleware's to do.
fn append(items: &mut Vec<serde_yaml::Value>, new: &[Value], m: Member) -> HostResult<()> {
    for entry in new {
        let id = entry
            .get("id")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .ok_or_else(|| HostError::bad_request(format!("an entry for {} has no id", m.path)))?;
        if items.iter().any(|e| members::entry_id(e) == Some(id)) {
            return Err(HostError::conflict(format!(
                "{} already holds {id}",
                m.path
            )));
        }
        let y = serde_yaml::to_value(entry)
            .map_err(|e| HostError::bad_request(format!("{id} is not representable: {e}")))?;
        items.push(y);
    }
    Ok(())
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

/// One middleware decision as the SDK's `Decision` holds it.
///
/// Identity, kind, targets and cites are the decision's own fields, taken as
/// the middleware sent them. Attribution is the context's: `actor` is always
/// `process:middleware`, `handler` and `backend` are the reply's (a decision
/// that names its own fills in only what the reply left out), `scope` is the
/// one the shell resolved. `agent` keeps the middleware's claim — the body's
/// `agent`, else the reply's handler — and `claimed_agent` records it too when
/// it is not the actor. The rationale is the middleware's, verbatim.
/// `fields_changed` stays empty: a middleware decision says what it is about
/// in `targets`, and the host does not know which of the patched keys each
/// one accounts for.
fn decision(ctx: &Ctx, reply: &Value, d: &Value, now: &str) -> HostResult<Decision> {
    let s = |k: &str| d.get(k).and_then(Value::as_str);
    let id = s("id")
        .filter(|s| !s.is_empty())
        .ok_or_else(|| HostError::bad_request("a decision has no id"))?;
    let kind = s("kind").unwrap_or("edit");
    let strings = |k: &str| -> Vec<String> {
        d.get(k)
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .filter_map(|x| x.as_str().map(String::from))
                    .collect()
            })
            .unwrap_or_default()
    };

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
    let mut out = attributed(&ctx, &agent, kind);
    out.id = Some(id.to_string());
    out.agent = agent;
    out.action = s("action").unwrap_or(kind).to_string();
    out.rationale = s("rationale").unwrap_or_default().to_string();
    out.timestamp = now.to_string();
    out.targets = strings("targets");
    out.cites = strings("cites");
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
