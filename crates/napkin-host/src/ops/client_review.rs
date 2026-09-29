// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! A client's answer to a locked document (Contract 4 §7.5, §8.2): recorded
//! by the agency person it reached, tied to the version the lock accepted,
//! with the client's words kept verbatim and the evidence stated for what it
//! is.
//!
//! Four records, all `kind: client_review`, told apart by `action`: the
//! document answer ([`CLIENT_ANSWER`]), a part answer ([`CLIENT_ANSWER_PART`]),
//! Ellis's suggestion of a part the words were about ([`SUGGEST_PART`]), and a
//! person dismissing one ([`DISMISS_PART`]). "Make this change" writes an
//! `unlock` that reopens one part ([`REOPEN_PART`]) and nothing else, so the
//! part can be edited while every other part stays exactly as the client saw
//! it; the next `approve` closes it.
//!
//! What stays out: who the client is. The recorder is the person in `Ctx`,
//! like every decision's actor; the client is data a person typed, and the
//! record says what evidence it rests on, never more. Nothing here filters by
//! a confidentiality mark: clients see everything (§7.5, item 6).
//!
//! What each part's answer is now — its state, whether it went stale, whether
//! it was answered, whether it is reopened — is derived by
//! [`decisions`](super::decisions), from the chain and the data, never stored.

use std::collections::BTreeSet;

use clan_sdk::decision::new_decision_id;
use clan_sdk::hash::sha256_prefixed;
use clan_sdk::{Certainty, Decision, DecisionChain, ReasonPoint, Reasoning};
use serde_json::{json, Value};

use crate::ctx::{Actor, Ctx};
use crate::document::Document;
use crate::error::{HostError, HostResult};
use crate::event::HostEvent;

use super::assemble::{assemble, data_of, Members};
use super::decisions;
use super::edit::{attributed, UPSTREAM_KEY};
use super::members::PROJECTION_KEY;
use super::middleware;
use super::review::{body, chain_of, lock_index, now, person, required, text, to_json, to_yaml};
use super::{sanitize_asset_name, Outcome, MAX_EXTRACT_CHARS};

/// Every client review record's kind.
pub const KIND: &str = "client_review";
/// The kind that reopens one part of a locked document.
pub const UNLOCK: &str = "unlock";

pub const CLIENT_ANSWER: &str = "client_answer";
pub const CLIENT_ANSWER_PART: &str = "client_answer_part";
pub const SUGGEST_PART: &str = "suggest_part";
pub const DISMISS_PART: &str = "dismiss_part";
pub const REOPEN_PART: &str = "reopen_part";

/// What a client may answer, of the document or of one part.
pub const ANSWERS: &[&str] = &["accepted", "accepted_with_changes", "rejected"];
/// Why a client rejected the document, when the recorder says.
pub const REASONS: &[&str] = &["off_brief", "wrong_audience", "tone", "facts_wrong", "budget", "other"];
/// How the answer reached the agency.
pub const CHANNELS: &[&str] = &["pasted_email", "file", "call", "none"];

/// The middleware task that finds which parts the words were about
/// (`napkin.middleware/1` §11), and the handler recorded when its reply does
/// not name one this host accepts.
pub const TASK: &str = "find_client_parts";
pub const HANDLER: &str = "find_client_parts@1.0";

const MAX_PARTS: usize = 100;
const MAX_LABEL: usize = 80;
const MAX_NAME: usize = 120;
const MAX_SAID: usize = 20_000;
const MAX_QUOTE: usize = 500;
const MAX_VALUE: usize = 2_000;
const MAX_REOPEN_REASON: usize = 300;

// ── inputs ──────────────────────────────────────────────────────────────────

/// Who the client is, as the recorder typed it. Data, not identity.
#[derive(Debug, Clone, PartialEq)]
pub struct Client {
    pub name: String,
    pub email: Option<String>,
}

/// `POST /client-review`: the document-level answer, the app's parts, and any
/// the recorder marked.
#[derive(Debug, Clone)]
pub struct ClientReview {
    pub answer: String,
    pub client: Client,
    pub reasons: Vec<String>,
    pub channel: String,
    /// The client's words exactly as sent — never trimmed inside, rewritten or
    /// compressed. `None` when absent or all whitespace.
    pub said: Option<String>,
    pub asset: Option<String>,
    /// `(address as sent, label)`, the app's whole list.
    pub parts: Vec<(String, String)>,
    /// `(address as sent, answer)`.
    pub marked: Vec<(String, String)>,
}

/// `POST /client-review/confirm`: settle one of Ellis's suggestions, or mark a
/// part by hand after the review was recorded.
#[derive(Debug, Clone)]
pub enum Confirm {
    Suggestion {
        id: String,
        confirm: bool,
        answer: Option<String>,
        rationale: String,
    },
    Mark {
        review: String,
        address: String,
        answer: String,
        rationale: String,
    },
}

/// What Ellis (the middleware's `find_client_parts`) said, when the host
/// asked it.
#[derive(Debug, Clone)]
pub enum Ellis {
    /// It was not asked, or could not answer: why, in words the reply
    /// carries. Ignored when there was nothing to ask.
    Unavailable(String),
    /// The `napkin.middleware/1` response body it answered with.
    Answered(Value),
}

fn bad(msg: impl Into<String>) -> HostError {
    HostError::bad_request(msg)
}

/// The recorder is whoever is signed in (§7.5, item 5): a body that tries to
/// say otherwise is refused, not ignored.
fn no_recorder(v: &Value) -> HostResult<()> {
    for key in ["actor", "recorded_by"] {
        if v.get(key).is_some() {
            return Err(bad(format!(
                "`{key}` is not taken from the body: the recorder is whoever is signed in, and the client is `client`"
            )));
        }
    }
    Ok(())
}

fn answer_of(v: &Value, key: &str) -> HostResult<String> {
    let a = required(v, key)?;
    if !ANSWERS.contains(&a.as_str()) {
        return Err(bad(format!("`{key}` is accepted, accepted_with_changes or rejected")));
    }
    Ok(a)
}

fn client_of(v: &Value) -> HostResult<Client> {
    let c = v
        .get("client")
        .filter(|c| c.is_object())
        .ok_or_else(|| bad("`client` is required: {name, email?}, the person who answered"))?;
    let name = text(c, "name");
    if name.is_empty() {
        return Err(bad("`client.name` is required"));
    }
    if name.chars().count() > MAX_NAME {
        return Err(bad(format!("`client.name` is at most {MAX_NAME} characters")));
    }
    let email = match c.get("email") {
        None | Some(Value::Null) => None,
        Some(Value::String(s)) if s.trim().is_empty() => None,
        Some(Value::String(s)) => {
            let e = s.trim().to_lowercase();
            let ok = matches!(e.split_once('@'), Some((l, r)) if !l.is_empty() && !r.is_empty() && !r.contains('@'))
                && !e.chars().any(char::is_whitespace);
            if !ok {
                return Err(bad(format!("`client.email` {s:?} is not an email address")));
            }
            Some(e)
        }
        Some(_) => return Err(bad("`client.email` is text")),
    };
    Ok(Client { name, email })
}

fn list<'v>(v: &'v Value, key: &str) -> HostResult<&'v [Value]> {
    match v.get(key) {
        None | Some(Value::Null) => Ok(&[]),
        Some(Value::Array(a)) => Ok(a),
        Some(_) => Err(bad(format!("`{key}` is a list"))),
    }
}

impl ClientReview {
    pub fn parse(raw: &str) -> HostResult<Self> {
        let v = body(raw)?;
        no_recorder(&v)?;
        if v.get("evidence").is_some() {
            return Err(bad("`evidence` is the host's: send the attached file as `asset`"));
        }
        let answer = answer_of(&v, "answer")?;
        let client = client_of(&v)?;

        let mut reasons: Vec<String> = Vec::new();
        for r in list(&v, "reasons")? {
            let r = r.as_str().ok_or_else(|| bad("`reasons` is a list of reason codes"))?;
            if !REASONS.contains(&r) {
                return Err(bad(format!("reason {r:?} is not one of: {}", REASONS.join(", "))));
            }
            if reasons.iter().any(|x| x == r) {
                return Err(bad(format!("reason {r} is given twice")));
            }
            reasons.push(r.to_string());
        }
        if !reasons.is_empty() && answer != "rejected" {
            return Err(bad("reasons are for a rejection"));
        }

        // Verbatim: the words are stored as sent, and only a body of nothing
        // but whitespace counts as no words.
        let said = match v.get("said") {
            None | Some(Value::Null) => None,
            Some(Value::String(s)) if s.trim().is_empty() => None,
            Some(Value::String(s)) if s.chars().count() > MAX_SAID => {
                return Err(bad(format!("`said` is at most {MAX_SAID} characters")))
            }
            Some(Value::String(s)) => Some(s.clone()),
            Some(_) => return Err(bad("`said` is the client's words, as text")),
        };
        let asset = Some(text(&v, "asset")).filter(|a| !a.is_empty());
        let channel = required(&v, "channel")?;
        if !CHANNELS.contains(&channel.as_str()) {
            return Err(bad(format!("`channel` is one of: {}", CHANNELS.join(", "))));
        }
        match channel.as_str() {
            "pasted_email" | "call" if said.is_none() => {
                return Err(bad(format!("`{channel}` needs the client's words in `said`")))
            }
            "file" if asset.is_none() => {
                return Err(bad("`file` needs the attached file in `asset`, stored first with /upload-asset"))
            }
            "none" if said.is_some() || asset.is_some() => {
                return Err(bad("`none` takes no words and no file: say which channel they came by"))
            }
            _ => {}
        }
        if asset.is_some() && channel != "file" {
            return Err(bad("an attached file is channel `file`"));
        }

        let raw_parts = v
            .get("parts")
            .and_then(Value::as_array)
            .ok_or_else(|| bad("`parts` is the app's whole list of parts, every time (it may be empty)"))?;
        if raw_parts.len() > MAX_PARTS {
            return Err(bad(format!("at most {MAX_PARTS} parts")));
        }
        let mut parts = Vec::new();
        for p in raw_parts {
            let address = required(p, "address")?;
            let label = text(p, "label");
            if label.is_empty() {
                return Err(bad(format!("part {address} needs a `label`")));
            }
            if label.chars().count() > MAX_LABEL {
                return Err(bad(format!("part {address}'s label is at most {MAX_LABEL} characters")));
            }
            parts.push((address, label));
        }
        let mut marked = Vec::new();
        for m in list(&v, "marked")? {
            marked.push((required(m, "address")?, answer_of(m, "answer")?));
        }
        if answer == "accepted" && !marked.is_empty() {
            return Err(bad("an accepted document marks every part accepted already: mark no parts"));
        }
        Ok(Self { answer, client, reasons, channel, said, asset, parts, marked })
    }
}

/// `POST /client-review/confirm`.
pub fn parse_confirm(raw: &str) -> HostResult<Confirm> {
    let v = body(raw)?;
    no_recorder(&v)?;
    let rationale = text(&v, "rationale");
    let suggestion = text(&v, "suggestion");
    if !suggestion.is_empty() {
        let confirm = v
            .get("confirm")
            .and_then(Value::as_bool)
            .ok_or_else(|| bad("`confirm` must be true or false"))?;
        let answer = match v.get("answer") {
            None | Some(Value::Null) => None,
            Some(_) => Some(answer_of(&v, "answer")?),
        };
        return Ok(Confirm::Suggestion { id: suggestion, confirm, answer, rationale });
    }
    if text(&v, "review").is_empty() {
        return Err(bad("`suggestion`, or `review` and `address`, is required"));
    }
    Ok(Confirm::Mark {
        review: required(&v, "review")?,
        address: required(&v, "address")?,
        answer: answer_of(&v, "answer")?,
        rationale,
    })
}

/// `POST /client-review/reopen`: `{answer}`, the part answer to act on.
pub fn parse_reopen(raw: &str) -> HostResult<String> {
    let v = body(raw)?;
    no_recorder(&v)?;
    required(&v, "answer")
}

// ── parts and hashes (§7.5.1) ───────────────────────────────────────────────

/// A part's address checked against §7.5.1, as `(full address, path)`: a
/// data path on this document in dotted keys, the paths `/edit` can write.
fn part_address(doc_id: &str, raw: &str) -> HostResult<(String, String)> {
    let raw = raw.trim();
    let path = match raw.split_once('#') {
        Some((d, p)) if d == doc_id => p,
        Some((d, _)) => {
            return Err(bad(format!(
                "part {raw} is on document {d}: a part is this document's own (a carried part is its parent's work, and its frozen copy is read-only)"
            )))
        }
        None => raw,
    };
    let segs: Vec<&str> = path.split('.').collect();
    if path.is_empty() || segs.iter().any(|s| s.is_empty() || s.contains(['[', ']', '#'])) {
        return Err(bad(format!(
            "part {raw} is not a data path in dotted keys: /edit could not write it, so a rejection of it could never be answered"
        )));
    }
    if matches!(segs[0], UPSTREAM_KEY | PROJECTION_KEY | "facts" | "findings" | "sources") {
        return Err(bad(format!(
            "part {raw} is not the document's own content: {} is the host's or a member",
            segs[0]
        )));
    }
    Ok((format!("{doc_id}#{path}"), path.to_string()))
}

/// `path` is `part`, or inside it.
pub(crate) fn inside(path: &str, part: &str) -> bool {
    path == part || path.strip_prefix(part).is_some_and(|r| r.starts_with('.'))
}

/// The value at a dotted `path`, as the client read it: a field envelope
/// (an object with `value` and `origin`, Contract 3 §2) is its `value` — its
/// provenance is not what the client read. `null` when the data holds none.
pub(crate) fn part_value(data: &Value, path: &str) -> Value {
    let v = path
        .split('.')
        .try_fold(data, |v, k| v.get(k))
        .cloned()
        .unwrap_or(Value::Null);
    match v {
        Value::Object(ref m) if m.contains_key("value") && m.contains_key("origin") => m["value"].clone(),
        other => other,
    }
}

/// A part's hash (§7.5.1): `sha256_prefixed` of the canonical JSON of its
/// value — object keys sorted by code point, no insignificant whitespace,
/// strings as `serde_json` writes them.
pub(crate) fn part_hash(data: &Value, path: &str) -> String {
    let mut out = String::new();
    canonical(&part_value(data, path), &mut out);
    sha256_prefixed(out.as_bytes())
}

fn canonical(v: &Value, out: &mut String) {
    match v {
        Value::Object(m) => {
            // `String`'s order is its UTF-8 bytes', which is code point order.
            let mut keys: Vec<&String> = m.keys().collect();
            keys.sort();
            out.push('{');
            for (i, k) in keys.into_iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                out.push_str(&Value::String(k.clone()).to_string());
                out.push(':');
                canonical(&m[k], out);
            }
            out.push('}');
        }
        Value::Array(items) => {
            out.push('[');
            for (i, x) in items.iter().enumerate() {
                if i > 0 {
                    out.push(',');
                }
                canonical(x, out);
            }
            out.push(']');
        }
        other => out.push_str(&other.to_string()),
    }
}

/// The value as Ellis is sent it (`napkin.middleware/1` §11, item 2): plain
/// text, clipped; `None` when the part is empty.
fn value_text(v: &Value) -> Option<String> {
    let s = match v {
        Value::Null => return None,
        Value::String(s) => s.clone(),
        Value::Array(a) if a.is_empty() => return None,
        Value::Object(m) if m.is_empty() => return None,
        other => other.to_string(),
    };
    (!s.trim().is_empty()).then(|| s.chars().take(MAX_VALUE).collect())
}

/// A `classify` mark says the address may not reach a model: the newest one
/// on it, or on a path it is inside, has `model: false`. Every model call
/// withholds such a value (`napkin.middleware/1` §10.13, item 6); it is not a
/// filter on what the client sees.
fn withheld(chain: &DecisionChain, address: &str) -> bool {
    chain
        .decisions
        .iter()
        .filter(|d| d.kind.as_deref() == Some("classify") && d.superseded_by.is_none())
        .find(|d| d.targets.iter().any(|t| inside(address, t)))
        .and_then(|d| d.licence.as_ref()?.model)
        .is_some_and(|model| !model)
}

// ── chain reads ─────────────────────────────────────────────────────────────

/// One part reopened by an `unlock` newer than the lock (§7.5.6, item 2).
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct Reopened {
    pub address: String,
    pub path: String,
    /// The `unlock`.
    pub decision: String,
    /// The part answer it was reopened for.
    pub answers: String,
}

/// True when decision `a` came after decision `b`: a later second, or the same
/// second and earlier in the (newest-first) chain.
pub(crate) fn after(chain: &DecisionChain, a: usize, b: usize) -> bool {
    let at = |i: usize| decisions::stamp(&chain.decisions[i].timestamp);
    (at(a), std::cmp::Reverse(a)) > (at(b), std::cmp::Reverse(b))
}

/// Every part of this document an `unlock`, not superseded, has reopened since
/// the newest lock. Empty when the document is not locked: the next `approve`
/// closes them all.
pub(crate) fn reopened(chain: &DecisionChain, doc_id: &str) -> Vec<Reopened> {
    let Some(lock) = lock_index(chain, doc_id) else {
        return Vec::new();
    };
    chain
        .decisions
        .iter()
        .enumerate()
        .filter(|(i, d)| {
            d.kind.as_deref() == Some(UNLOCK)
                && d.action == REOPEN_PART
                && d.superseded_by.is_none()
                && after(chain, *i, lock)
        })
        .filter_map(|(_, d)| {
            let (on, path) = d.targets.first()?.split_once('#')?;
            (on == doc_id && !path.is_empty()).then(|| Reopened {
                address: format!("{doc_id}#{path}"),
                path: path.to_string(),
                decision: d.id.clone().unwrap_or_default(),
                answers: extra_str(d, "answers").unwrap_or_default().to_string(),
            })
        })
        .collect()
}

pub(crate) fn extra_str<'d>(d: &'d Decision, key: &str) -> Option<&'d str> {
    d.extra.get(key).and_then(|v| v.as_str())
}

pub(crate) fn extra_json(d: &Decision, key: &str) -> Value {
    d.extra.get(key).map(to_json).unwrap_or(Value::Null)
}

fn is(d: &Decision, action: &str) -> bool {
    d.kind.as_deref() == Some(KIND) && d.action == action
}

fn find<'c>(chain: &'c DecisionChain, id: &str) -> HostResult<&'c Decision> {
    chain
        .decisions
        .iter()
        .find(|d| d.id.as_deref() == Some(id))
        .ok_or_else(|| HostError::not_found(format!("decision {id} is not in this document")))
}

/// The part answer already recorded for `review` on `address`, if any.
fn answered_part<'c>(chain: &'c DecisionChain, review: &str, address: &str) -> Option<&'c Decision> {
    chain.decisions.iter().find(|d| {
        is(d, CLIENT_ANSWER_PART)
            && extra_str(d, "review") == Some(review)
            && d.targets.first().map(String::as_str) == Some(address)
    })
}

fn client_name(d: &Decision) -> String {
    d.extra
        .get("client")
        .and_then(|c| c.get("name"))
        .and_then(|n| n.as_str())
        .unwrap_or("The client")
        .to_string()
}

// ── words ───────────────────────────────────────────────────────────────────

fn reason_words(r: &str) -> String {
    r.replace('_', " ")
}

/// `<client> <verb> <part>`: what a part answer says.
fn part_verb(answer: &str) -> &'static str {
    match answer {
        "accepted" => "accepted",
        "accepted_with_changes" => "asked for a change to",
        _ => "rejected",
    }
}

fn answer_words(answer: &str) -> &'static str {
    match answer {
        "accepted" => "accepted",
        "accepted_with_changes" => "accepted with changes",
        _ => "rejected",
    }
}

fn person_name(who: &str) -> &str {
    who.strip_prefix("human:").unwrap_or(who)
}

fn one_line(s: &str) -> String {
    s.split_whitespace().collect::<Vec<_>>().join(" ")
}

fn clip(s: &str, n: usize) -> String {
    if s.chars().count() > n {
        format!("{}…", s.chars().take(n - 1).collect::<String>())
    } else {
        s.to_string()
    }
}

// ── the document answer ─────────────────────────────────────────────────────

/// One part as the client saw it.
struct Seen {
    address: String,
    path: String,
    label: String,
    hash: String,
}

/// A review checked against the document, before anything is asked or
/// written.
struct Prepared {
    who: String,
    lock: String,
    version: String,
    doc_hash: String,
    parts: Vec<Seen>,
    /// Index into `parts`, and the answer.
    marked: Vec<(usize, String)>,
    evidence: Value,
    proof: Option<String>,
    ask: Option<Value>,
}

fn prepare(ctx: &Ctx, doc: &Document, input: &ClientReview) -> HostResult<Prepared> {
    let who = person(ctx, "record a client's answer")?;
    let clan = doc.clan();
    let here = clan.document_id().to_string();
    let chain = chain_of(doc)?;
    let lock = lock_index(&chain, &here).map(|i| &chain.decisions[i]).ok_or_else(|| {
        HostError::conflict(
            "a client answers a locked document: lock it first, so the answer is tied to the version the client saw",
        )
    })?;
    if !reopened(&chain, &here).is_empty() {
        return Err(HostError::conflict(
            "a part is reopened, so the document is no longer the version the lock accepted: lock it again before recording a client's answer",
        ));
    }
    let data = data_of(clan)?;

    let mut parts: Vec<Seen> = Vec::new();
    for (raw, label) in &input.parts {
        let (address, path) = part_address(&here, raw)?;
        if let Some(p) = parts.iter().find(|p| inside(&path, &p.path) || inside(&p.path, &path)) {
            return Err(bad(if p.path == path {
                format!("part {path} is given twice")
            } else {
                format!("parts {} and {path} overlap: one is inside the other, so a change to it would be ambiguous", p.path)
            }));
        }
        parts.push(Seen { hash: part_hash(&data, &path), address, path, label: label.clone() });
    }
    let mut marked: Vec<(usize, String)> = Vec::new();
    for (raw, answer) in &input.marked {
        let (address, _) = part_address(&here, raw)?;
        let i = parts
            .iter()
            .position(|p| p.address == address)
            .ok_or_else(|| bad(format!("marked part {raw} is not one of `parts`")))?;
        if marked.iter().any(|(j, _)| *j == i) {
            return Err(bad(format!("part {raw} is marked twice")));
        }
        marked.push((i, answer.clone()));
    }

    let mut evidence = json!({ "strength": "weaker" });
    let mut extracted = None;
    if let Some(asset) = &input.asset {
        let name = asset
            .strip_prefix("human/assets/")
            .and_then(sanitize_asset_name)
            .ok_or_else(|| bad("`asset` is human/assets/<name>, a file stored with /upload-asset"))?;
        let ext = name.rsplit('.').next().unwrap_or("").to_ascii_lowercase();
        if !matches!(ext.as_str(), "eml" | "pdf") {
            return Err(bad("the attached file is the client's email (.eml) or a PDF"));
        }
        let path = format!("human/assets/{name}");
        if !clan.has_entry(&path) {
            return Err(HostError::not_found(format!("{path} is not in this document: store it with /upload-asset first")));
        }
        let bytes = clan.read_entry(&path)?;
        evidence = json!({ "asset": path, "sha256": sha256_prefixed(&bytes), "strength": "strong" });
        extracted = clan
            .read_entry(&format!("human/assets/.extracted/{name}.txt"))
            .ok()
            .and_then(|b| String::from_utf8(b).ok())
            .filter(|t| !t.trim().is_empty());
    }
    let proof = input
        .said
        .clone()
        .or(extracted)
        .map(|p| p.chars().take(MAX_EXTRACT_CHARS).collect::<String>());

    let ask = (input.answer != "accepted" && input.marked.is_empty() && !parts.is_empty())
        .then_some(proof.as_ref())
        .flatten()
        .map(|proof| {
            let parts: Vec<Value> = parts
                .iter()
                .map(|p| {
                    let value = if withheld(&chain, &p.address) {
                        None
                    } else {
                        value_text(&part_value(&data, &p.path))
                    };
                    json!({ "address": p.address, "label": p.label, "value": value })
                })
                .collect();
            json!({ "task": TASK, "input": { "answer": input.answer, "proof": proof, "parts": parts } })
        });

    Ok(Prepared {
        who,
        lock: lock.id.clone().unwrap_or_default(),
        version: lock.version.clone().unwrap_or_default(),
        doc_hash: sha256_prefixed(&clan.read_entry("shared/data.yaml").unwrap_or_default()),
        parts,
        marked,
        evidence,
        proof,
        ask,
    })
}

/// What the middleware is asked before a client review is written
/// (`napkin.middleware/1` §11), or `None` when there is nothing to ask: the
/// answer is accepted, parts were marked, the app declared none, or there
/// are no words to read. Refuses whatever [`record`] would refuse, so the
/// middleware is not asked for nothing.
pub fn request(ctx: &Ctx, doc: &Document, input: &ClientReview) -> HostResult<Option<Value>> {
    Ok(prepare(ctx, doc, input)?.ask)
}

/// A decision by the person `who`, of the client review kind.
fn by_person(ctx: &Ctx, who: &str, action: &str, targets: Vec<String>, cites: Vec<String>, rationale: String, now: &str) -> Decision {
    let mut d = attributed(ctx, "", KIND);
    d.id = Some(new_decision_id());
    d.agent = who.to_string();
    d.action = action.to_string();
    d.targets = targets;
    d.cites = cites;
    d.rationale = rationale;
    d.timestamp = now.to_string();
    d
}

fn put(d: &mut Decision, key: &str, v: Value) -> HostResult<()> {
    d.extra.insert(key.to_string(), to_yaml(&v)?);
    Ok(())
}

fn client_json(c: &Client) -> Value {
    match &c.email {
        Some(e) => json!({ "name": c.name, "email": e }),
        None => json!({ "name": c.name }),
    }
}

/// A suggestion that survived the host's check.
struct Found {
    part: usize,
    answer: String,
    quote: String,
}

/// Ellis's reply, checked again by the host (§8.2, item 1): an address among
/// the parts, one of the three answers, a quote that is a verbatim substring
/// of the proof once every run of whitespace is one space, one per address
/// (the first stays). Anything else is dropped and counted. `Err` says why
/// nothing can be taken from the reply at all.
fn check(p: &Prepared, data: &Value) -> Result<(Vec<Found>, usize), String> {
    middleware::check_api(data).map_err(|e| e.message)?;
    if let Some(state) = data.pointer("/job/state").and_then(Value::as_str).filter(|s| *s != "done") {
        return Err(format!("Ellis did not finish in one response (the job is {state})"));
    }
    if data.get("change").is_some_and(|c| !c.is_null()) {
        return Err("Ellis answered with a change, and finding the parts writes nothing".into());
    }
    let list = data
        .pointer("/result/suggestions")
        .and_then(Value::as_array)
        .ok_or_else(|| "Ellis answered without a list of suggestions".to_string())?;
    let proof = one_line(p.proof.as_deref().unwrap_or_default());
    let mut taken = BTreeSet::new();
    let mut out = Vec::new();
    let mut dropped = 0;
    for s in list {
        let part = s
            .get("address")
            .and_then(Value::as_str)
            .and_then(|a| p.parts.iter().position(|x| x.address == a || x.path == a));
        let answer = s.get("answer").and_then(Value::as_str).filter(|a| ANSWERS.contains(a));
        let quote = s
            .get("quote")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|q| !q.is_empty() && q.chars().count() <= MAX_QUOTE && proof.contains(&one_line(q)));
        match (part, answer, quote) {
            (Some(i), Some(a), Some(q)) if taken.insert(i) => out.push(Found {
                part: i,
                answer: a.to_string(),
                quote: q.to_string(),
            }),
            _ => dropped += 1,
        }
    }
    Ok((out, dropped))
}

/// The handler a suggestion is recorded under: the reply's, when it names
/// this task in the `name@major.minor` form; the built-in one otherwise.
fn handler_of(data: &Value) -> String {
    data.get("handler")
        .and_then(Value::as_str)
        .filter(|h| {
            h.strip_prefix(TASK)
                .and_then(|r| r.strip_prefix('@'))
                .is_some_and(|v| {
                    let mut it = v.split('.');
                    let whole = |s: Option<&str>| s.is_some_and(|s| !s.is_empty() && s.chars().all(|c| c.is_ascii_digit()));
                    whole(it.next()) && it.next().map_or(true, |m| whole(Some(m))) && it.next().is_none()
                })
        })
        .unwrap_or(HANDLER)
        .to_string()
}

/// Record a client's answer to the locked document: the document answer, a
/// part answer per part the recorder marked, and a suggestion per part Ellis
/// found that survives the host's check — in that order, in one change.
pub fn record(ctx: &Ctx, doc: &Document, input: ClientReview, ellis: Ellis) -> HostResult<Outcome> {
    let p = prepare(ctx, doc, &input)?;
    let here = doc.clan().document_id().to_string();
    let now = now();
    let name = input.client.name.clone();
    let client = client_json(&input.client);

    let evidence_words = match input.channel.as_str() {
        "file" => "from an attached file",
        "pasted_email" => "from pasted email text",
        "call" => "on a call",
        _ => "with no evidence attached",
    };
    let reasons = if input.reasons.is_empty() {
        String::new()
    } else {
        format!(" ({})", input.reasons.iter().map(|r| reason_words(r)).collect::<Vec<_>>().join(", "))
    };
    let what = match input.answer.as_str() {
        "accepted" => "accepted the document",
        "accepted_with_changes" => "accepted the document with changes",
        _ => "rejected the document",
    };
    let mut a = by_person(
        ctx,
        &p.who,
        CLIENT_ANSWER,
        vec![here.clone()],
        vec![p.lock.clone()],
        format!("{name} {what}{reasons}, {evidence_words}; recorded by {}.", person_name(&p.who)),
        &now,
    );
    put(&mut a, "covers", json!("document"))?;
    put(&mut a, "answer", json!(input.answer))?;
    if !input.reasons.is_empty() {
        put(&mut a, "reasons", json!(input.reasons))?;
    }
    if let Some(said) = &input.said {
        put(&mut a, "said", json!(said))?;
    }
    put(&mut a, "client", client.clone())?;
    put(&mut a, "channel", json!(input.channel))?;
    put(&mut a, "evidence", p.evidence.clone())?;
    let seen_parts: Vec<Value> = p
        .parts
        .iter()
        .map(|s| json!({ "address": s.address, "label": s.label, "part_hash": s.hash }))
        .collect();
    put(&mut a, "seen", json!({ "version": p.version, "doc_hash": p.doc_hash, "parts": seen_parts }))?;
    let a_id = a.id.clone().unwrap_or_default();
    let seen_part = |s: &Seen| json!({ "version": p.version, "doc_hash": p.doc_hash, "part_hash": s.hash });

    let mut written = vec![a];
    let mut part_ids = Vec::new();
    for (i, answer) in &p.marked {
        let s = &p.parts[*i];
        let mut d = by_person(
            ctx,
            &p.who,
            CLIENT_ANSWER_PART,
            vec![s.address.clone()],
            vec![a_id.clone()],
            format!("{} marked that {name} {} {}.", person_name(&p.who), part_verb(answer), s.label),
            &now,
        );
        put(&mut d, "covers", json!("part"))?;
        put(&mut d, "review", json!(a_id))?;
        put(&mut d, "label", json!(s.label))?;
        put(&mut d, "answer", json!(answer))?;
        put(&mut d, "client", client.clone())?;
        put(&mut d, "found_by", json!("person"))?;
        put(&mut d, "seen", seen_part(s))?;
        part_ids.push(d.id.clone().unwrap_or_default());
        written.push(d);
    }

    let (status, found, dropped, reason, reply_data) = match (&p.ask, ellis) {
        (None, _) => ("none", Vec::new(), 0, None, Value::Null),
        (Some(_), Ellis::Unavailable(why)) => ("unavailable", Vec::new(), 0, Some(why), Value::Null),
        (Some(_), Ellis::Answered(data)) => match check(&p, &data) {
            Ok((found, dropped)) => ("found", found, dropped, None, data),
            Err(why) => ("unavailable", Vec::new(), 0, Some(why), Value::Null),
        },
    };
    let handler = handler_of(&reply_data);
    let backend = reply_data.pointer("/trace/backend").and_then(Value::as_str).map(String::from);
    let mut suggestion_ids = Vec::new();
    for f in &found {
        let s = &p.parts[f.part];
        let mut d = attributed(ctx, "", KIND);
        d.actor = Some(Actor::process(middleware::JOB)?.to_string());
        d.handler = Some(handler.clone());
        d.backend = backend.clone();
        d.id = Some(new_decision_id());
        d.agent = TASK.to_string();
        d.action = SUGGEST_PART.to_string();
        d.targets = vec![s.address.clone()];
        d.cites = vec![a_id.clone()];
        d.timestamp = now.clone();
        d.rationale = format!(
            "Derived by the agent: {name} may mean {} ({}): “{}”",
            s.label,
            answer_words(&f.answer),
            f.quote
        );
        d.reasoning = Some(Reasoning {
            decided: format!("Suggested that {name}'s answer is about {}.", s.label),
            because: vec![ReasonPoint {
                point: format!("The client's words name it: “{}”", f.quote),
                cites: vec![a_id.clone()],
                ..Default::default()
            }],
            rejected: Vec::new(),
            only_option: Some(
                "the words are the client's and the part list is the app's; a person confirms or dismisses it".into(),
            ),
            certainty: Certainty {
                level: "medium".into(),
                why: "found by the agent in the client's words; not yet confirmed".into(),
                ..Default::default()
            },
            would_change_if: "a person dismisses it".into(),
            attention: Some("Derived by the agent. Confirm or dismiss it; only a confirmed part counts.".into()),
            ..Default::default()
        });
        put(&mut d, "covers", json!("part"))?;
        put(&mut d, "review", json!(a_id))?;
        put(&mut d, "label", json!(s.label))?;
        put(&mut d, "answer", json!(f.answer))?;
        put(&mut d, "quote", json!(f.quote))?;
        put(&mut d, "found_by", json!("agent"))?;
        put(&mut d, "seen", seen_part(s))?;
        suggestion_ids.push(d.id.clone().unwrap_or_default());
        written.push(d);
    }

    let mut suggestions = json!({ "status": status, "decisions": suggestion_ids, "dropped": dropped });
    if let Some(r) = reason {
        suggestions["reason"] = json!(r);
    }
    let reply = json!({
        "ok": true, "kind": KIND, "decision": a_id, "parts": part_ids, "suggestions": suggestions,
    });
    commit(doc, written, "a client's answer", &now, reply)
}

// ── confirming, dismissing, marking later ───────────────────────────────────

/// Settle a suggestion — confirm it as a part answer, or dismiss it — or mark
/// a part by hand after the review. Allowed on a locked document, a part
/// reopened or not; needs no lease.
pub fn confirm(ctx: &Ctx, doc: &Document, input: Confirm) -> HostResult<Outcome> {
    let who = person(ctx, "say which part a client meant")?;
    let chain = chain_of(doc)?;
    let here = doc.clan().document_id().to_string();
    let now = now();
    let on_here = |d: &Decision| {
        d.targets
            .first()
            .and_then(|t| t.split_once('#'))
            .is_some_and(|(on, p)| on == here && !p.is_empty())
    };
    let review_of = |id: &str| -> HostResult<&Decision> {
        let r = find(&chain, id)?;
        if !is(r, CLIENT_ANSWER) || !r.targets.contains(&here) {
            return Err(bad(format!("{id} is not a client's answer to this document")));
        }
        Ok(r)
    };
    let already = |review: &str, address: &str, label: &str, name: &str| -> HostResult<()> {
        match answered_part(&chain, review, address) {
            Some(d) => Err(HostError::conflict(format!(
                "{name}'s answer on {label} is recorded already ({}); a later answer from the client is a new client review",
                d.id.as_deref().unwrap_or("a part answer")
            ))),
            None => Ok(()),
        }
    };

    match input {
        Confirm::Suggestion { id, confirm, answer, rationale } => {
            let s = find(&chain, &id)?;
            if !is(s, SUGGEST_PART) || !on_here(s) {
                return Err(HostError::conflict(format!("{id} is not a suggestion on this document")));
            }
            if let Some(by) = chain
                .decisions
                .iter()
                .find(|d| (is(d, CLIENT_ANSWER_PART) || is(d, DISMISS_PART)) && extra_str(d, "suggestion") == Some(id.as_str()))
            {
                return Err(HostError::conflict(format!(
                    "suggestion {id} was already {} ({})",
                    if is(by, DISMISS_PART) { "dismissed" } else { "confirmed" },
                    by.id.as_deref().unwrap_or("a decision")
                )));
            }
            let review = extra_str(s, "review").unwrap_or_default().to_string();
            let r = review_of(&review)?;
            let name = client_name(r);
            let address = s.targets[0].clone();
            let label = extra_str(s, "label").unwrap_or(&address).to_string();
            already(&review, &address, &label, &name)?;
            if !confirm {
                let mut d = by_person(
                    ctx,
                    &who,
                    DISMISS_PART,
                    vec![address],
                    vec![id.clone()],
                    if rationale.is_empty() {
                        format!("Dismissed Ellis's suggestion that {name} meant {label}.")
                    } else {
                        format!("Dismissed: {rationale}")
                    },
                    &now,
                );
                put(&mut d, "covers", json!("part"))?;
                put(&mut d, "review", json!(review))?;
                put(&mut d, "suggestion", json!(id))?;
                let reply = json!({ "ok": true, "kind": KIND, "decision": d.id, "targets": d.targets });
                return commit(doc, vec![d], "a suggestion dismissed", &now, reply);
            }
            let answer = answer.unwrap_or_else(|| extra_str(s, "answer").unwrap_or("rejected").to_string());
            let mut d = by_person(
                ctx,
                &who,
                CLIENT_ANSWER_PART,
                vec![address],
                vec![review.clone(), id.clone()],
                if rationale.is_empty() {
                    format!("{} confirmed that {name} {} {label}.", person_name(&who), part_verb(&answer))
                } else {
                    rationale
                },
                &now,
            );
            put(&mut d, "covers", json!("part"))?;
            put(&mut d, "review", json!(review))?;
            put(&mut d, "label", json!(label))?;
            put(&mut d, "answer", json!(answer))?;
            put(&mut d, "client", extra_json(r, "client"))?;
            put(&mut d, "found_by", json!("agent"))?;
            put(&mut d, "suggestion", json!(id))?;
            if let Some(q) = extra_str(s, "quote") {
                put(&mut d, "quote", json!(q))?;
            }
            put(&mut d, "seen", extra_json(s, "seen"))?;
            let reply = json!({ "ok": true, "kind": KIND, "decision": d.id, "targets": d.targets });
            commit(doc, vec![d], "a suggestion confirmed", &now, reply)
        }
        Confirm::Mark { review, address, answer, rationale } => {
            let r = review_of(&review)?;
            if extra_str(r, "answer") == Some("accepted") {
                return Err(HostError::conflict(
                    "that answer accepted the document, which marks every part accepted already",
                ));
            }
            let (full, _) = part_address(&here, &address)?;
            let seen = extra_json(r, "seen");
            let entry = seen
                .get("parts")
                .and_then(Value::as_array)
                .and_then(|ps| ps.iter().find(|e| e.get("address").and_then(Value::as_str) == Some(full.as_str())))
                .cloned()
                .ok_or_else(|| bad(format!("{address} is not one of the parts the client saw")))?;
            let label = entry.get("label").and_then(Value::as_str).unwrap_or(&address).to_string();
            let name = client_name(r);
            already(&review, &full, &label, &name)?;
            let mut d = by_person(
                ctx,
                &who,
                CLIENT_ANSWER_PART,
                vec![full],
                vec![review.clone()],
                if rationale.is_empty() {
                    format!("{} marked that {name} {} {label}.", person_name(&who), part_verb(&answer))
                } else {
                    rationale
                },
                &now,
            );
            put(&mut d, "covers", json!("part"))?;
            put(&mut d, "review", json!(review))?;
            put(&mut d, "label", json!(label))?;
            put(&mut d, "answer", json!(answer))?;
            put(&mut d, "client", extra_json(r, "client"))?;
            put(&mut d, "found_by", json!("person"))?;
            put(
                &mut d,
                "seen",
                json!({ "version": seen.get("version"), "doc_hash": seen.get("doc_hash"), "part_hash": entry.get("part_hash") }),
            )?;
            let reply = json!({ "ok": true, "kind": KIND, "decision": d.id, "targets": d.targets });
            commit(doc, vec![d], "a client's part marked", &now, reply)
        }
    }
}

// ── "Make this change" (§7.5.6) ─────────────────────────────────────────────

/// Reopen the one part a client asked about, so it can be edited on the
/// locked document: an `unlock` naming the part answer and the lock. Only for
/// a part whose current answer is that part answer, asking a change or
/// rejecting it, not answered by an edit since, and not already reopened.
pub fn reopen(ctx: &Ctx, doc: &Document, answer: &str) -> HostResult<Outcome> {
    let who = person(ctx, "reopen a part")?;
    let chain = chain_of(doc)?;
    let here = doc.clan().document_id().to_string();
    let lock = lock_index(&chain, &here)
        .and_then(|i| chain.decisions[i].id.clone())
        .ok_or_else(|| HostError::conflict("the document is not locked: edit it as it is"))?;
    let a = find(&chain, answer)?;
    if !is(a, CLIENT_ANSWER_PART) {
        return Err(HostError::conflict(format!(
            "{answer} is not a client's answer on one part: mark or confirm a part first"
        )));
    }
    let view = decisions::decisions(doc)?;
    let part = view
        .client
        .parts
        .iter()
        .find(|p| p.decision == answer)
        .ok_or_else(|| HostError::conflict(format!("{answer} is not its part's current answer: a newer one has replaced it")))?;
    if part.state == "accepted" {
        return Err(HostError::conflict(format!("the client accepted {}: there is no change to make", part.label)));
    }
    if part.answered {
        return Err(HostError::conflict(format!("{} was edited since the client answered: the request is answered", part.label)));
    }
    if part.reopened {
        return Err(HostError::conflict(format!("{} is reopened already", part.label)));
    }
    let r = extra_str(a, "review").map(|id| find(&chain, id)).transpose()?;
    let name = client_name(a);
    let said = r.and_then(|r| extra_str(r, "said")).map(String::from);
    let reasons: Vec<String> = r
        .and_then(|r| r.extra.get("reasons"))
        .and_then(|v| v.as_sequence())
        .into_iter()
        .flatten()
        .filter_map(|v| v.as_str().map(reason_words))
        .collect();
    let what = extra_str(a, "quote")
        .map(String::from)
        .or(said)
        .map(|s| one_line(&s))
        .filter(|s| !s.is_empty())
        .or_else(|| (!reasons.is_empty()).then(|| reasons.join(", ")))
        .unwrap_or_else(|| "a change".to_string());
    let reason = clip(&format!("{name} asked: {what}"), MAX_REOPEN_REASON);

    let mut d = attributed(ctx, "", UNLOCK);
    d.id = Some(new_decision_id());
    d.agent = who.clone();
    d.action = REOPEN_PART.to_string();
    d.targets = vec![part.address.clone()];
    d.cites = vec![answer.to_string()];
    d.rationale = reason.clone();
    d.timestamp = now();
    put(&mut d, "answers", json!(answer))?;
    put(&mut d, "lock", json!(lock))?;
    let reply = json!({
        "ok": true, "kind": UNLOCK, "decision": d.id, "address": part.address, "label": part.label,
        "answers": answer, "reason": reason,
    });
    let at = d.timestamp.clone();
    commit(doc, vec![d], "a part reopened", &at, reply)
}

/// Write one generation with `decisions`, in order (the last ends up newest),
/// and nothing else changed.
fn commit(doc: &Document, decisions: Vec<Decision>, delta: &str, now: &str, reply: Value) -> HostResult<Outcome> {
    let clan = doc.clan();
    let first = decisions.first().and_then(|d| d.id.clone()).unwrap_or_default();
    let kind = decisions.first().and_then(|d| d.kind.clone()).unwrap_or_default();
    let bytes = assemble(clan, data_of(clan)?, Members::of(clan)?, decisions, delta, now, false)?;
    let notice = json!({ "ok": true, "source": "client-review", "kind": kind, "decision": first });
    let change = doc.change(bytes)?.with_event(HostEvent::DataChanged(notice));
    Ok(Outcome::changed(reply, change))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_part_hash_is_of_canonical_json_and_an_envelope_hashes_its_value() {
        let data = json!({
            "a": { "z": 1, "b": [true, null, "x\"y"] },
            "env": { "value": "Summer", "origin": "stated", "by": "human:x" },
        });
        let mut out = String::new();
        canonical(&part_value(&data, "a"), &mut out);
        assert_eq!(out, r#"{"b":[true,null,"x\"y"],"z":1}"#);
        assert_eq!(part_hash(&data, "env"), sha256_prefixed(br#""Summer""#));
        assert_eq!(part_hash(&data, "missing.path"), sha256_prefixed(b"null"));
    }

    #[test]
    fn a_part_is_a_dotted_data_path_on_this_document() {
        assert_eq!(part_address("d1", "audience").unwrap().0, "d1#audience");
        assert_eq!(part_address("d1", "d1#a.b").unwrap(), ("d1#a.b".into(), "a.b".into()));
        for bad in ["d2#audience", "sections[s_2]", "upstream.p.x", "projection", "facts", "text[k]", "a..b", ""] {
            assert_eq!(part_address("d1", bad).unwrap_err().status, 400, "{bad}");
        }
        assert!(inside("audience.commercial", "audience"));
        assert!(!inside("audience_long", "audience"));
    }

    #[test]
    fn only_this_tasks_handler_is_recorded() {
        assert_eq!(handler_of(&json!({ "handler": "find_client_parts@1.2" })), "find_client_parts@1.2");
        assert_eq!(handler_of(&json!({ "handler": "find_client_parts@2" })), "find_client_parts@2");
        assert_eq!(handler_of(&json!({ "handler": "draft_brief@1.0" })), HANDLER);
        assert_eq!(handler_of(&json!({ "handler": "find_client_parts@1.x" })), HANDLER);
        assert_eq!(handler_of(&Value::Null), HANDLER);
    }
}
