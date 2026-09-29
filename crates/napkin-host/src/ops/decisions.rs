// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! Every decision a document holds, as the OS layer shows it: newest first,
//! each with who made it, its targets as readable labels, its cites resolved
//! to what they name, and — derived here, never by the app — what needs a
//! person's attention and why.
//!
//! What needs attention (OS-layer contract §3, §4, §7):
//!
//! - the decider said so: `reasoning.attention`;
//! - the decider was unsure: `reasoning.certainty.level` is `low`;
//! - an open contest — a `selection.contested` entry still `open`, a
//!   `contest` decision nothing has resolved, or a merge conflict;
//! - a finding still `proposed` (D1: findings need a person to verify them);
//! - anything else that blocks the lock (D7): a field citing a rejected
//!   finding, a bad verdict nobody has answered, an unmerged agent branch.
//!
//! The first two are cleared once a person has since decided about the
//! decision itself or about every one of its targets, or it has been
//! superseded; on a bad verdict, a later good verdict with a reason on a
//! target also answers it there (never on a finding, which D1 leaves to a
//! person). The rest are the lock list, items 1–5 of Contract 3 §11; an
//! app may add rules of its own (the campaign's gated fields are one), which
//! this does not know about.
//!
//! A spun-off document carries its ancestors whole (Contract 4 §5): their
//! data frozen under `upstream.<document id>`, their chain in this one, their
//! findings and pins merged into this document's members. Everything carried
//! is on the lock list too (§7.2) — a contest open in a frozen copy until a
//! `resolve` here names it, a merge report carried beside it — and an address
//! on an ancestor is labelled and resolved from its frozen copy.
//!
//! A client's answer to the locked document (Contract 4 §7.5) is derived
//! here too: each part's current answer, whether it went stale (its own value
//! changed since), whether an edit answered it, whether it is reopened; and
//! what it asks of a person — an unanswered rejection blocks locking again, a
//! change asked or one of Ellis's suggestions is attention.
//!
//! A read of one snapshot: nothing here writes.

use std::collections::{BTreeMap, BTreeSet};

use clan_sdk::decision::{Decision, DecisionChain};
use clan_sdk::merge::{MergeReport, MERGE_REPORT_PATH};
use serde::Serialize;
use serde_json::Value;

use crate::document::Document;
use crate::error::HostResult;

use super::client_review::{self as cr, extra_json, extra_str};
use super::edit::UPSTREAM_KEY;
use super::middleware::PROPOSE_ACTION;
use super::{members, read, review};

const CHAIN_PATH: &str = "agent/decision-chain.yaml";
const SCHEMA_PATH: &str = "agent/output-schema.json";

/// What `/decisions` answers.
#[derive(Debug, Serialize)]
pub struct DecisionsView {
    pub document_id: String,
    pub version: String,
    /// Newest first.
    pub decisions: Vec<DecisionBlock>,
    /// Everything that needs a person, lock blockers first. An item names the
    /// decision it belongs to when there is one; some — a finding whose
    /// decision is not in the chain, a branch — stand on their own.
    pub attention: Vec<Attention>,
    /// Every id a decision cites, resolved once.
    pub cites: BTreeMap<String, Cite>,
    pub lock: LockState,
    /// Client review (Contract 4 §7.5, §8.2 item 6).
    pub client: ClientView,
    /// Set when the chain could not be read; the rest is then empty.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub problem: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct DecisionBlock {
    pub decision: Decision,
    pub who: Who,
    pub targets: Vec<Target>,
    /// Why this decision needs a person, if it does.
    pub attention: Vec<Reason>,
    pub superseded: bool,
}

/// Who made a decision, as a person would say it.
#[derive(Debug, Serialize, PartialEq)]
pub struct Who {
    /// `person` or `agent` (a handler, a job or a tool acting under its name).
    pub kind: &'static str,
    /// The person's id, or the handler's name without its version.
    pub id: String,
    pub name: String,
    /// The person looking at the view made it: shown as "You".
    #[serde(skip_serializing_if = "std::ops::Not::not")]
    pub you: bool,
}

/// One address a decision is about.
#[derive(Debug, Serialize, PartialEq)]
pub struct Target {
    pub address: String,
    pub path: String,
    pub label: String,
    /// `field`, `contest`, `finding`, `fact`, `decision` or `document`.
    pub kind: &'static str,
    /// False when the address is on another document — carried from upstream.
    pub here: bool,
}

#[derive(Debug, Serialize, Clone, PartialEq)]
pub struct Reason {
    pub code: &'static str,
    pub text: String,
    pub blocks_lock: bool,
}

#[derive(Debug, Serialize, Clone, PartialEq)]
pub struct Attention {
    pub code: &'static str,
    pub text: String,
    pub blocks_lock: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub decision: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub address: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub label: Option<String>,
}

/// What a cite names, in words.
#[derive(Debug, Serialize, PartialEq, Default)]
pub struct Cite {
    /// `fact`, `finding`, `source`, `material`, `passage`, `capture`,
    /// `decision`, `person`, `address` or `unknown`.
    pub kind: &'static str,
    pub label: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
    /// A quote the address points at, when it holds one.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub quote: Option<String>,
    /// A fact: its value as a person reads it.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub value: Option<String>,
    /// A fact: the sources it rests on, by id (each resolved in `cites` when
    /// the document carries its record).
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub sources: Vec<String>,
    /// A source: where it was read, and who published it.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub uri: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub publisher: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub title: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub published_at: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub tier: Option<String>,
}

#[derive(Debug, Serialize, PartialEq)]
pub struct LockState {
    /// Nothing on the OS lock list is open. An app may still refuse.
    pub can_lock: bool,
    pub blockers: usize,
    /// The document is locked (Contract 4 §7.1).
    pub locked: bool,
    /// The parts reopened for a client's request since the lock (§7.5.6).
    pub reopened: Vec<ReopenedPart>,
}

#[derive(Debug, Serialize, PartialEq, Clone)]
pub struct ReopenedPart {
    pub address: String,
    pub label: String,
    /// The `unlock`.
    pub decision: String,
    /// The part answer it was reopened for.
    pub answers: String,
}

/// What `/decisions` says of client review.
#[derive(Debug, Serialize, Default)]
pub struct ClientView {
    /// `POST /client-review` would be accepted now: locked, nothing reopened.
    pub available: bool,
    /// The newest document answer.
    pub answer: Option<ClientAnswer>,
    /// Every document answer, newest first.
    pub answers: Vec<ClientAnswer>,
    /// One per address with a current answer, by address.
    pub parts: Vec<ClientPart>,
    /// Ellis's suggestions nobody has confirmed or dismissed, oldest first.
    pub suggestions: Vec<ClientSuggestion>,
}

#[derive(Debug, Serialize, Clone)]
pub struct ClientAnswer {
    pub decision: String,
    pub answer: String,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub reasons: Vec<String>,
    /// The client's words, verbatim.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub said: Option<String>,
    pub client: Value,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub channel: Option<String>,
    pub evidence: Value,
    pub recorded_by: RecordedBy,
    pub at: String,
    pub seen: Value,
    /// It answers the version the lock names now.
    pub current: bool,
    /// A part answer names it as its review.
    pub parts_known: bool,
}

#[derive(Debug, Serialize, Clone, PartialEq)]
pub struct RecordedBy {
    pub id: String,
    pub name: String,
}

#[derive(Debug, Serialize, Clone)]
pub struct ClientPart {
    pub address: String,
    pub label: String,
    /// `accepted`, `accepted_with_changes` or `rejected`.
    pub state: String,
    /// The current answer: a part answer, or an `accepted` document answer.
    pub decision: String,
    pub review: String,
    /// `person`, `agent` (a confirmed suggestion), or `document` (an accepted
    /// document answer marks every part).
    pub found_by: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub quote: Option<String>,
    pub client: Value,
    pub at: String,
    /// The part's value changed since the answer.
    pub stale: bool,
    /// A person edited the part since the answer asked for a change.
    pub answered: bool,
    pub reopened: bool,
}

#[derive(Debug, Serialize, Clone)]
pub struct ClientSuggestion {
    pub decision: String,
    pub review: String,
    pub address: String,
    pub label: String,
    pub answer: String,
    pub quote: String,
}

/// `GET /decisions` — see the module docs.
pub fn decisions(doc: &Document) -> HostResult<DecisionsView> {
    decisions_for(doc, None)
}

/// [`decisions`], for a viewer: decisions `viewer` (an actor, `human:<id>`)
/// made are marked `you`.
pub fn decisions_for(doc: &Document, viewer: Option<&str>) -> HostResult<DecisionsView> {
    let clan = doc.clan();
    let doc_id = clan.document_id().to_string();
    let mut view = DecisionsView {
        document_id: doc_id.clone(),
        version: doc.version().as_str().to_string(),
        decisions: Vec::new(),
        attention: Vec::new(),
        cites: BTreeMap::new(),
        lock: LockState {
            can_lock: true,
            blockers: 0,
            locked: false,
            reopened: Vec::new(),
        },
        client: ClientView::default(),
        problem: None,
    };

    let chain = if clan.has_entry(CHAIN_PATH) {
        match clan
            .read_entry(CHAIN_PATH)
            .map_err(|e| e.to_string())
            .and_then(|b| DecisionChain::from_yaml(&b).map_err(|e| e.to_string()))
        {
            Ok(c) => c,
            Err(e) => {
                view.problem = Some(format!("the decision chain does not read: {e}"));
                return Ok(view);
            }
        }
    } else {
        DecisionChain::default()
    };

    let data = read::data_json(doc);
    let schema: Value = clan
        .read_entry(SCHEMA_PATH)
        .ok()
        .and_then(|b| serde_json::from_slice(&b).ok())
        .unwrap_or(Value::Null);
    let by_id = |m: members::Member| -> BTreeMap<String, Value> {
        let Value::Array(items) = members::list_for_agent(clan, m) else {
            return BTreeMap::new();
        };
        items
            .into_iter()
            .filter_map(|v| Some((v.get("id")?.as_str()?.to_string(), v)))
            .collect()
    };
    let ctx = Lookup {
        doc_id: &doc_id,
        data: &data,
        schema: &schema,
        facts: by_id(members::FACTS),
        findings: by_id(members::FINDINGS),
        sources: by_id(members::SOURCES),
        chain: &chain,
    };

    // Newest first. The chain is written that way, but prepends and merges
    // can leave equal or out-of-order stamps; chain order breaks ties.
    let mut order: Vec<usize> = (0..chain.decisions.len()).collect();
    order.sort_by_key(|&i| (std::cmp::Reverse(ctx.at(i)), i));

    let client = client_review(&ctx);
    let mut attention = lock_blockers(&ctx, clan);
    attention.extend(client.blockers);
    attention.extend(asked_for(&ctx));
    attention.extend(client.attention);

    for &i in &order {
        let d = &chain.decisions[i];
        let reasons = attention
            .iter()
            .filter(|a| a.decision.is_some() && a.decision == d.id)
            .map(|a| Reason {
                code: a.code,
                text: a.text.clone(),
                blocks_lock: a.blocks_lock,
            })
            .collect();
        let mut w = who(d);
        if w.kind == "person" && viewer.is_some_and(|v| d.actor.as_deref() == Some(v)) {
            w.you = true;
            w.name = "You".into();
        }
        view.decisions.push(DecisionBlock {
            who: w,
            targets: aims(d).map(|t| ctx.target(t)).collect(),
            attention: reasons,
            superseded: d.superseded_by.is_some(),
            decision: d.clone(),
        });
    }

    let cited: BTreeSet<&str> = chain
        .decisions
        .iter()
        .flat_map(|d| {
            let because = d
                .reasoning
                .iter()
                .flat_map(|r| r.because.iter().flat_map(|p| p.cites.iter()));
            d.cites.iter().chain(because)
        })
        .map(String::as_str)
        .filter(|c| !c.trim().is_empty())
        .collect();
    let mut cites: BTreeMap<String, Cite> = cited
        .into_iter()
        .map(|c| (c.to_string(), ctx.cite(c)))
        .collect();
    let behind: BTreeSet<String> = cites
        .values()
        .flat_map(|c| c.sources.iter().cloned())
        .filter(|s| !cites.contains_key(s))
        .collect();
    for s in behind {
        let c = ctx.cite(&s);
        cites.insert(s, c);
    }
    view.cites = cites;

    let blockers = attention.iter().filter(|a| a.blocks_lock).count();
    view.lock = LockState {
        can_lock: blockers == 0,
        blockers,
        locked: client.locked,
        reopened: client.reopened,
    };
    view.client = client.view;
    view.attention = attention;
    Ok(view)
}

/// What every derivation reads from the snapshot.
struct Lookup<'a> {
    doc_id: &'a str,
    data: &'a Value,
    schema: &'a Value,
    facts: BTreeMap<String, Value>,
    findings: BTreeMap<String, Value>,
    sources: BTreeMap<String, Value>,
    chain: &'a DecisionChain,
}

impl<'a> Lookup<'a> {
    /// When decision `i` was made, in milliseconds; 0 when it does not parse.
    fn at(&self, i: usize) -> i64 {
        stamp(&self.chain.decisions[i].timestamp)
    }

    /// True when decision `a` came after decision `b`: a later stamp, or the
    /// same stamp and earlier in the (newest-first) chain.
    fn after(&self, a: usize, b: usize) -> bool {
        (self.at(a), std::cmp::Reverse(a)) > (self.at(b), std::cmp::Reverse(b))
    }

    fn index_of(&self, id: &str) -> Option<usize> {
        self.chain
            .decisions
            .iter()
            .position(|d| d.id.as_deref() == Some(id))
    }

    /// An address with its document made explicit: a bare path is this
    /// document's, and a bare document id — what an `approve` targets — is
    /// that document, whole.
    fn qualify(&self, address: &str) -> String {
        match address.split_once('#') {
            Some(_) => address.to_string(),
            None if self.data_for(address).is_some() => format!("{address}#"),
            None => format!("{}#{address}", self.doc_id),
        }
    }

    /// The data an address on `doc` is read from: this document's, or the
    /// frozen copy of an ancestor it carries (Contract 4 §5.2). `None` for a
    /// document it does not carry.
    fn data_for(&self, doc: &str) -> Option<&'a Value> {
        if doc == self.doc_id {
            return Some(self.data);
        }
        self.data.get(UPSTREAM_KEY)?.get(doc).filter(|v| v.is_object())
    }

    /// Each frozen ancestor, by document id, in key order.
    fn upstream(&self) -> impl Iterator<Item = (&'a str, &'a Value)> {
        self.data
            .get(UPSTREAM_KEY)
            .and_then(Value::as_object)
            .into_iter()
            .flatten()
            .filter(|(_, v)| v.is_object())
            .map(|(k, v)| (k.as_str(), v))
    }

    /// This document's data, then each frozen ancestor's: where a cite that
    /// is not a member entry is looked for, first match.
    fn every_data(&self) -> impl Iterator<Item = &'a Value> {
        std::iter::once(self.data).chain(self.upstream().map(|(_, v)| v))
    }

    fn address(&self, path: &str) -> String {
        format!("{}#{path}", self.doc_id)
    }

    /// Does decision `i` name `address` (or something inside it) as a target
    /// or a flag?
    ///
    /// A patch-data records no targets, only the top-level keys it wrote, and
    /// `campaign` does not say which field changed. So an entry with no
    /// targets also touches an address its rationale names — which is how
    /// the views that write it say what they changed.
    fn touches(&self, i: usize, address: &str) -> bool {
        let d = &self.chain.decisions[i];
        let flags = d
            .extra
            .get("flags")
            .and_then(|v| v.as_sequence())
            .into_iter()
            .flatten()
            .filter_map(|v| v.as_str().map(String::from));
        let named = aims(d).cloned().chain(flags).any(|t| {
            let t = self.qualify(&t);
            t == address
                || t.strip_prefix(address)
                    .is_some_and(|rest| rest.starts_with(['.', '[']))
        });
        named
            || (d.targets.is_empty()
                && address
                    .split_once('#')
                    .is_some_and(|(_, p)| mentions(&d.rationale, &format!("#{p}"))))
    }

    /// Some decision after `i` matching `pred` touches `address`.
    fn later(&self, i: usize, address: &str, pred: impl Fn(&Decision) -> bool) -> bool {
        self.chain
            .decisions
            .iter()
            .enumerate()
            .any(|(j, e)| j != i && self.after(j, i) && pred(e) && self.touches(j, address))
    }

    fn target(&self, address: &str) -> Target {
        let full = self.qualify(address);
        let (doc, path) = full.split_once('#').unwrap_or(("", full.as_str()));
        let here = doc == self.doc_id;
        let (label, kind) = self.label(doc, path);
        Target {
            address: full.clone(),
            path: path.to_string(),
            label,
            kind,
            here,
        }
    }

    /// A readable name for a path on `doc`, and what kind of thing it is.
    ///
    /// A finding, pin or source is looked up in this document's members
    /// whatever the prefix — an ancestor's are merged into them (Contract 4
    /// §8.1, item 1); anything else in the data the address is on: this
    /// document's, or an ancestor's frozen copy.
    fn label(&self, doc: &str, path: &str) -> (String, &'static str) {
        let data = self.data_for(doc);
        let carried = data.is_some();
        let segs = segments(path);
        match segs.as_slice() {
            [] => ("The document".into(), "document"),
            [Seg::Name("findings"), Seg::Key(id)] => {
                let text = carried
                    .then(|| self.findings.get(*id))
                    .flatten()
                    .and_then(|f| f.get("statement")?.as_str().map(|s| clip(s, 90)))
                    .unwrap_or_else(|| id.to_string());
                (format!("Finding · {text}"), "finding")
            }
            [Seg::Name("facts"), Seg::Key(id)] => {
                let text = carried
                    .then(|| self.facts.get(*id))
                    .flatten()
                    .map(fact_label)
                    .unwrap_or_else(|| id.to_string());
                (format!("Fact · {text}"), "fact")
            }
            [Seg::Name("selection"), Seg::Name("contested"), Seg::Key(id)] => {
                let key = data
                    .and_then(|d| contest_entry(d, id))
                    .and_then(|c| c.get("key")?.as_str().map(String::from))
                    .unwrap_or_else(|| id.to_string());
                (format!("Contest · {key}"), "contest")
            }
            [Seg::Name("text"), Seg::Key(k)] => (
                if k.starts_with("report:") { "Report › wording".to_string() } else { "Wording".to_string() },
                "field",
            ),
            [Seg::Name("decisions"), Seg::Key(id)] => (self.decision_label(id), "decision"),
            [Seg::Name("materials"), Seg::Key(id)] => {
                let name = data
                    .and_then(|d| d.get("materials")?.get(*id))
                    .and_then(|m| str_of(m, "name"))
                    .unwrap_or(id);
                (format!("Material · {name}"), "field")
            }
            [Seg::Name(id)] if id.starts_with(clan_sdk::decision::DECISION_ID_PREFIX) => {
                (self.decision_label(id), "decision")
            }
            _ => (self.field_label(&segs), "field"),
        }
    }

    fn decision_label(&self, id: &str) -> String {
        match self.index_of(id).map(|i| &self.chain.decisions[i]) {
            Some(d) => format!("Decision · {}", humanise(&d.action)),
            None => format!("Decision · {id}"),
        }
    }

    /// `campaign.success_measures[sm_x]` → "Campaign › Success measures ·
    /// sm_x", each name taken from the schema's `title` when it has one.
    fn field_label(&self, segs: &[Seg]) -> String {
        let mut out = String::new();
        let mut schema = Some(self.schema);
        for s in segs {
            match s {
                Seg::Name(n) => {
                    let node = schema.and_then(|v| v.get("properties")?.get(*n));
                    let title = node
                        .and_then(|v| v.get("title")?.as_str())
                        .map(String::from)
                        .unwrap_or_else(|| humanise(n));
                    if !out.is_empty() {
                        out.push_str(" › ");
                    }
                    out.push_str(&title);
                    schema = node;
                }
                Seg::Key(k) => {
                    out.push_str(" · ");
                    out.push_str(k);
                    schema = schema.and_then(|v| {
                        v.get("additionalProperties")
                            .or_else(|| v.get("items"))
                            .filter(|n| n.is_object())
                    });
                }
            }
        }
        out
    }

    fn cite(&self, id: &str) -> Cite {
        let cite = |kind, label: String, detail: Option<String>| Cite {
            kind,
            label,
            detail,
            ..Default::default()
        };
        if let Some(f) = self.facts.get(id) {
            return Cite {
                value: f.get("value").map(|v| format_value(v, str_of(f, "unit"))),
                sources: f
                    .get("sources")
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                    .filter_map(|s| s.as_str().filter(|s| s.starts_with("src_")).map(String::from))
                    .collect(),
                ..cite("fact", fact_label(f), Some(fact_detail(f)))
            };
        }
        if let Some(f) = self.findings.get(id) {
            let statement = str_of(f, "statement").unwrap_or(id).to_string();
            let detail = [
                str_of(f, "status").map(|s| format!("{s} finding")),
                str_of(f, "confidence").map(|c| format!("{c} confidence")),
                Some("derived by the agent".to_string()),
            ];
            return cite("finding", statement, Some(join(&detail)));
        }
        // A value held in a contest points at a layer row nobody pinned; the
        // contest entry is what the document knows of it — its own, or one
        // carried in an ancestor's frozen copy.
        let held = self.every_data().flat_map(data_contests).find_map(|c| {
            let v = c
                .get("values")?
                .as_array()?
                .iter()
                .find(|v| str_of(v, "fact_id") == Some(id))?;
            Some((c, v))
        });
        if let Some((c, v)) = held {
            let value = v.get("value").map(|x| format_value(x, str_of(v, "unit")));
            let label = join_with(&[str_of(c, "key").map(String::from), value], " = ");
            let detail = [
                str_of(v, "from").map(|f| format!("from {f}")),
                Some("a value in a contest, not pinned".to_string()),
            ];
            return cite("fact", label, Some(join(&detail)));
        }
        let material = self
            .every_data()
            .find_map(|d| d.get("materials").and_then(|m| m.get(id)));
        if let Some(m) = material {
            let label = str_of(m, "name").unwrap_or(id).to_string();
            let detail = [
                str_of(m, "kind").map(String::from),
                str_of(m, "received_at").map(|r| format!("received {r}")),
                str_of(m, "licence").map(|l| l.replace('-', " ")),
            ];
            return cite("material", label, Some(join(&detail)));
        }
        // A passage a drafter read (napkin.middleware/1 §10.4): precedent,
        // not the document's evidence, named by its citation.
        let passage = id
            .starts_with("psg_")
            .then(|| self.every_data().find_map(|d| d.get("passages")?.get(id)))
            .flatten();
        if let Some(p) = passage {
            let label = str_of(p, "citation")
                .or_else(|| str_of(p, "source"))
                .unwrap_or(id)
                .to_string();
            let detail = [
                str_of(p, "source").filter(|s| Some(*s) != str_of(p, "citation")).map(String::from),
                str_of(p, "pack").map(|k| format!("pack {k}")),
                str_of(p, "scope").map(|s| match s {
                    "house" => "house knowledge".to_string(),
                    other => other.replace("agency:", "agency "),
                }),
                str_of(p, "licence").map(|l| l.replace('-', " ")),
                str_of(p, "retrieved_at").map(|r| format!("retrieved {}", r.get(..10).unwrap_or(r))),
            ];
            return Cite {
                quote: str_of(p, "text").map(|t| clip(t, 400)),
                uri: str_of(p, "uri").map(String::from),
                ..cite("passage", label, Some(join(&detail)))
            };
        }
        // An item the capture read from the client's material: a fact with
        // its verbatim quote, or an assumption.
        let captured = id
            .starts_with("cap_")
            .then(|| {
                self.every_data()
                    .find_map(|d| d.get("capture")?.get("items")?.get(id))
            })
            .flatten();
        if let Some(c) = captured {
            let label = c
                .get("value")
                .map(|v| clip(&format_value(v, None), 140))
                .unwrap_or_else(|| id.to_string());
            let material = str_of(c, "material_id").map(|m| {
                self.every_data()
                    .find_map(|d| str_of(d.get("materials")?.get(m)?, "name"))
                    .unwrap_or(m)
                    .to_string()
            });
            let detail = [
                str_of(c, "key").map(humanise),
                match str_of(c, "status") {
                    Some("assumption") => Some("assumed".to_string()),
                    Some("fact") => Some("from your material".to_string()),
                    _ => None,
                },
                material.map(|m| format!("in {m}")),
            ];
            return Cite {
                quote: str_of(c, "quote").map(String::from),
                ..cite("capture", label, Some(join(&detail)))
            };
        }
        if let Some(i) = self.index_of(id) {
            let d = &self.chain.decisions[i];
            return cite(
                "decision",
                format!("{} — {}", humanise(&d.action), who(d).name),
                Some(clip(&d.rationale, 200)).filter(|r| !r.is_empty()),
            );
        }
        if let Some(s) = self.sources.get(id) {
            let publisher = str_of(s, "publisher").map(String::from);
            let title = str_of(s, "title").map(String::from);
            let label = join_with(&[publisher.clone(), title.clone()], " — ");
            let used = self
                .facts
                .values()
                .filter(|f| {
                    f.get("sources")
                        .and_then(Value::as_array)
                        .is_some_and(|x| x.iter().any(|v| v.as_str() == Some(id)))
                })
                .count();
            return Cite {
                uri: str_of(s, "uri").map(String::from),
                publisher,
                title,
                published_at: str_of(s, "published_at").map(String::from),
                tier: str_of(s, "tier").map(String::from),
                ..cite(
                    "source",
                    if label.is_empty() { id.to_string() } else { label },
                    Some(plural(used, "pinned fact")),
                )
            };
        }
        if id.starts_with("src_") {
            let behind: Vec<String> = self
                .facts
                .values()
                .filter(|f| {
                    f.get("sources")
                        .and_then(Value::as_array)
                        .is_some_and(|s| s.iter().any(|x| x.as_str() == Some(id)))
                })
                .map(fact_label)
                .collect();
            let detail = if behind.is_empty() {
                "Held in the knowledge layer, not in this document.".to_string()
            } else {
                format!(
                    "Held in the knowledge layer. Behind {}: {}.",
                    plural(behind.len(), "pinned fact"),
                    behind.join("; ")
                )
            };
            return cite("source", format!("Source {id}"), Some(detail));
        }
        if let Some(person) = id.strip_prefix("human:") {
            return cite("person", format!("Checked by {person}"), None);
        }
        if id.contains('#') || id.contains('.') || id.contains('[') {
            let t = self.target(id);
            let doc = t.address.split_once('#').map_or("", |(d, _)| d);
            let node = self.data_for(doc).and_then(|d| lookup(d, &t.path));
            let quote = node.and_then(|n| {
                n.get("source")
                    .and_then(|s| s.get("quote"))
                    .or_else(|| n.get("quote"))
                    .and_then(Value::as_str)
                    .map(String::from)
            });
            let detail = node.and_then(preview);
            return Cite {
                kind: "address",
                label: t.label,
                detail,
                quote,
                ..Default::default()
            };
        }
        cite("unknown", id.to_string(), None)
    }
}

/// This document's fields that cite each of `findings`, by the lock list's
/// rule (Contract 4 §7.2, item 4): an envelope's `finding_ids`, or the
/// field's current writing decision. Each field as a full address on this
/// document. What `GET /upstream` reports as a finding's `cited_by`; the
/// frozen copies are skipped, as the lock list skips them.
pub(crate) fn fields_citing(
    doc: &Document,
    findings: &BTreeSet<String>,
) -> BTreeMap<String, BTreeSet<String>> {
    let clan = doc.clan();
    let doc_id = clan.document_id().to_string();
    let chain = clan
        .read_entry(CHAIN_PATH)
        .ok()
        .and_then(|b| DecisionChain::from_yaml(&b).ok())
        .unwrap_or_default();
    let data = read::data_json(doc);
    let ctx = Lookup {
        doc_id: &doc_id,
        data: &data,
        schema: &Value::Null,
        facts: BTreeMap::new(),
        findings: BTreeMap::new(),
        sources: BTreeMap::new(),
        chain: &chain,
    };
    let wanted: BTreeMap<&str, &Value> = findings
        .iter()
        .map(|id| (id.as_str(), &Value::Null))
        .collect();
    let mut by_field: BTreeMap<String, BTreeSet<&str>> = BTreeMap::new();
    citing_findings(&data, &mut Vec::new(), &wanted, &mut by_field);
    written_citing_findings(&ctx, &wanted, &mut by_field);
    let mut out: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    for (field, ids) in by_field {
        for id in ids {
            out.entry(id.to_string())
                .or_default()
                .insert(ctx.address(&field));
        }
    }
    out
}

/// The lock list (Contract 3 §11, items 1–5; Contract 4 §7).
fn lock_blockers(ctx: &Lookup, clan: &clan_sdk::ClanFile) -> Vec<Attention> {
    let mut out = Vec::new();
    let blocker = |code, text: String, decision: Option<String>, address: Option<String>| {
        let label = address.as_deref().map(|a| ctx.target(a).label);
        Attention {
            code,
            text,
            blocks_lock: true,
            decision,
            address,
            label,
        }
    };
    let is_kind = |d: &Decision, k: &str| d.kind.as_deref() == Some(k);

    // 1. Open contests: in the selection, in the chain, and left by a merge.
    let mut contests_seen = BTreeSet::new();
    for c in data_contests(ctx.data) {
        let Some(id) = str_of(c, "id") else { continue };
        let address = ctx.address(&format!("selection.contested[{id}]"));
        contests_seen.insert(address.clone());
        if str_of(c, "status") != Some("open") {
            continue;
        }
        let opener = str_of(c, "opened_by")
            .filter(|d| ctx.index_of(d).is_some())
            .map(String::from)
            .or_else(|| {
                ctx.chain
                    .decisions
                    .iter()
                    .enumerate()
                    .find(|(i, d)| is_kind(d, "contest") && ctx.touches(*i, &address))
                    .and_then(|(_, d)| d.id.clone())
            });
        let n = c
            .get("values")
            .and_then(Value::as_array)
            .map_or(0, Vec::len);
        let key = str_of(c, "key").unwrap_or(id);
        out.push(blocker(
            "open_contest",
            format!(
                "Open contest on {key}: {} and nothing picked. A person has to resolve it before lock.",
                plural(n, "value")
            ),
            opener,
            Some(address),
        ));
    }
    // A contest carried open in an ancestor's frozen copy: the copy never
    // changes, so it is open until a `resolve` in this chain names it
    // (Contract 4 §7.2, item 1).
    for (up, frozen) in ctx.upstream() {
        for c in data_contests(frozen) {
            let Some(id) = str_of(c, "id") else { continue };
            let address = format!("{up}#selection.contested[{id}]");
            contests_seen.insert(address.clone());
            if str_of(c, "status") != Some("open") || resolved_here(ctx, &address) {
                continue;
            }
            let n = c
                .get("values")
                .and_then(Value::as_array)
                .map_or(0, Vec::len);
            let key = str_of(c, "key").unwrap_or(id);
            out.push(blocker(
                "open_contest",
                format!(
                    "Open contest on {key}: {} and nothing picked. It was carried from upstream; a person has to resolve it here before lock.",
                    plural(n, "value")
                ),
                str_of(c, "opened_by")
                    .filter(|d| ctx.index_of(d).is_some())
                    .map(String::from),
                Some(address),
            ));
        }
    }
    for (i, d) in ctx.chain.decisions.iter().enumerate() {
        if !is_kind(d, "contest") || d.superseded_by.is_some() {
            continue;
        }
        let settled = d
            .extra
            .get("status")
            .and_then(|s| s.as_str())
            .is_some_and(|s| s != "open");
        for t in &d.targets {
            let address = ctx.qualify(t);
            if settled || contests_seen.contains(&address) {
                continue;
            }
            if ctx.later(i, &address, |e| is_kind(e, "resolve")) {
                continue;
            }
            contests_seen.insert(address.clone());
            let upstream = if ctx.target(&address).here {
                ""
            } else {
                " It was carried from upstream."
            };
            out.push(blocker(
                "open_contest",
                format!(
                    "Open contest on {}: nothing picked. A person has to resolve it before lock.{upstream}",
                    ctx.target(&address).label
                ),
                d.id.clone(),
                Some(address),
            ));
        }
    }
    // This document's merge report, and each one carried beside an
    // ancestor's frozen copy (`upstream/<id>/merge-report.yaml`, §5.2). A
    // carried conflict is settled only upstream, so here it blocks.
    let paths = clan.entry_paths().unwrap_or_default();
    let carried_reports = paths.iter().filter(|p| {
        p.strip_prefix("upstream/")
            .and_then(|rest| rest.strip_suffix("/merge-report.yaml"))
            .is_some_and(|id| !id.is_empty() && !id.contains('/'))
    });
    for path in std::iter::once(MERGE_REPORT_PATH).chain(carried_reports.map(String::as_str)) {
        let Some(report) = clan
            .read_entry(path)
            .ok()
            .and_then(|b| MergeReport::from_yaml(&b).ok())
        else {
            continue;
        };
        let carried = path != MERGE_REPORT_PATH;
        for c in &report.conflicts {
            let agents: Vec<&str> = std::iter::once(c.winner.agent.as_str())
                .chain(c.losers.iter().map(|l| l.agent.as_str()))
                .collect();
            out.push(blocker(
                "open_contest",
                if carried {
                    format!(
                        "The merge upstream left {} contested between {}. It was carried from upstream and is settled there, in the parent; until then it blocks the lock.",
                        c.key,
                        agents.join(" and ")
                    )
                } else {
                    format!(
                        "The merge left {} contested between {}. A person has to settle it before lock.",
                        c.key,
                        agents.join(" and ")
                    )
                },
                c.decision.clone(),
                None,
            ));
        }
    }

    // 2. Unmerged agent branches.
    let branches: BTreeSet<String> = paths
        .iter()
        .filter_map(|p| {
            let rest = p.strip_prefix("agents/")?;
            let (id, _) = rest.split_once('/')?;
            Some(id.to_string())
        })
        .collect();
    for b in branches {
        out.push(blocker(
            "unmerged_branch",
            format!("The agent branch {b} is not merged yet."),
            None,
            None,
        ));
    }

    // 3. Findings still proposed.
    for (id, f) in &ctx.findings {
        if str_of(f, "status") != Some("proposed") {
            continue;
        }
        let statement = str_of(f, "statement")
            .map(|s| clip(s, 140))
            .unwrap_or_default();
        out.push(blocker(
            "unverified_finding",
            format!(
                "Finding not verified yet: “{statement}” It was derived by the agent; a person has to verify or reject it before lock."
            ),
            str_of(f, "decision")
                .filter(|d| ctx.index_of(d).is_some())
                .map(String::from),
            Some(ctx.address(&format!("findings[{id}]"))),
        ));
    }

    // 4. Fields that cite a rejected finding: through an envelope's
    //    `finding_ids`, or — a bare value, like a brief's — through the
    //    decision that wrote it as it stands.
    let rejected: BTreeMap<&str, &Value> = ctx
        .findings
        .iter()
        .filter(|(_, f)| str_of(f, "status") == Some("rejected"))
        .map(|(id, f)| (id.as_str(), f))
        .collect();
    if !rejected.is_empty() {
        let mut flagged: BTreeMap<String, BTreeSet<&str>> = BTreeMap::new();
        citing_findings(ctx.data, &mut Vec::new(), &rejected, &mut flagged);
        written_citing_findings(ctx, &rejected, &mut flagged);
        for (path, ids) in flagged {
            for id in ids {
                let by = rejected[id]
                    .get("rejection")
                    .and_then(|r| r.get("decision")?.as_str())
                    .filter(|d| ctx.index_of(d).is_some())
                    .map(String::from);
                let address = ctx.address(&path);
                out.push(blocker(
                    "flagged_field",
                    format!(
                        "{} cites finding {id}, which a reviewer rejected. Revise it to stop citing the finding before lock.",
                        ctx.target(&address).label
                    ),
                    by,
                    Some(address),
                ));
            }
        }
    }

    // 5. Bad verdicts nobody has answered: the field revised since, or the
    //    verdict overridden by a good one with a written reason. A bad
    //    verdict on a finding is its rejection, which item 4 follows up.
    for (i, d) in ctx.chain.decisions.iter().enumerate() {
        if !d.is_verdict() || d.polarity.as_deref() != Some("bad") || d.superseded_by.is_some() {
            continue;
        }
        for t in &d.targets {
            let address = ctx.qualify(t);
            let target = ctx.target(&address);
            if target.kind == "finding" {
                continue;
            }
            let answered = ctx.later(i, &address, |e| is_edit(e) || reasoned_good_verdict(e));
            if answered {
                continue;
            }
            // The verdict's own rationale is on its block; this says what is
            // still owed.
            out.push(blocker(
                "bad_verdict",
                format!(
                    "{} was marked bad by {}. Revise it, or override the verdict with a reason, before lock.",
                    target.label,
                    who(d).name
                ),
                d.id.clone(),
                Some(address),
            ));
        }
    }
    out
}

/// Client review as derived from the snapshot, and what it asks of a person.
struct ClientDerived {
    view: ClientView,
    locked: bool,
    reopened: Vec<ReopenedPart>,
    blockers: Vec<Attention>,
    attention: Vec<Attention>,
}

/// Contract 4 §7.5.3 and §7.5.4, over this document's own records. A carried
/// `client_review` targets a parent's address: it is the parent's record,
/// shown in the history and counted in nothing here.
fn client_review(ctx: &Lookup) -> ClientDerived {
    let chain = &ctx.chain.decisions;
    let here = ctx.doc_id;
    let is = |d: &Decision, action: &str| d.kind.as_deref() == Some(cr::KIND) && d.action == action;
    // A part record's address, when it is on this document.
    let part_here = |d: &Decision| {
        let a = ctx.qualify(d.targets.first()?);
        let (on, path) = a.split_once('#')?;
        (on == here && !path.is_empty()).then_some(a)
    };
    let newest_first = |mut v: Vec<usize>| {
        v.sort_by_key(|&i| (std::cmp::Reverse(ctx.at(i)), i));
        v
    };
    let id = |d: &Decision| d.id.clone().unwrap_or_default();

    let lock = review::lock_of(ctx.chain, here);
    let reopened_now = cr::reopened(ctx.chain, here);
    let label_of = |address: &str, fallback: Option<&str>| {
        fallback.map(String::from).unwrap_or_else(|| ctx.target(address).label)
    };
    let reopened: Vec<ReopenedPart> = reopened_now
        .iter()
        .map(|r| ReopenedPart {
            label: label_of(
                &r.address,
                ctx.index_of(&r.answers).and_then(|i| extra_str(&chain[i], "label")),
            ),
            address: r.address.clone(),
            decision: r.decision.clone(),
            answers: r.answers.clone(),
        })
        .collect();

    let documents = newest_first(
        (0..chain.len())
            .filter(|&i| is(&chain[i], cr::CLIENT_ANSWER) && chain[i].targets.iter().any(|t| t == here))
            .collect(),
    );
    let part_answers: Vec<usize> = (0..chain.len())
        .filter(|&i| is(&chain[i], cr::CLIENT_ANSWER_PART) && part_here(&chain[i]).is_some())
        .collect();

    // Each address's current answer: the newest part answer on it, or
    // `accepted` document answer that saw it.
    enum Source {
        Part,
        Document { label: String, hash: String },
    }
    let mut current: BTreeMap<String, (usize, Source)> = BTreeMap::new();
    let mut offer = |address: String, i: usize, from: Source| {
        if current.get(&address).map_or(true, |(j, _)| ctx.after(i, *j)) {
            current.insert(address, (i, from));
        }
    };
    for &i in &documents {
        if extra_str(&chain[i], "answer") != Some("accepted") {
            continue;
        }
        let seen = extra_json(&chain[i], "seen");
        for e in seen.get("parts").and_then(Value::as_array).into_iter().flatten() {
            let Some(address) = e.get("address").and_then(Value::as_str) else { continue };
            let text = |k| e.get(k).and_then(Value::as_str).unwrap_or_default().to_string();
            offer(ctx.qualify(address), i, Source::Document { label: text("label"), hash: text("part_hash") });
        }
    }
    for &i in &part_answers {
        if let Some(address) = part_here(&chain[i]) {
            offer(address, i, Source::Part);
        }
    }

    let mut parts = Vec::new();
    for (address, (i, from)) in current {
        let d = &chain[i];
        let path = address.split_once('#').map_or("", |(_, p)| p);
        let (state, label, recorded, review, found_by, quote) = match from {
            Source::Part => (
                extra_str(d, "answer").unwrap_or_default().to_string(),
                label_of(&address, extra_str(d, "label")),
                extra_json(d, "seen").get("part_hash").and_then(Value::as_str).unwrap_or_default().to_string(),
                extra_str(d, "review").unwrap_or_default().to_string(),
                extra_str(d, "found_by").unwrap_or("person").to_string(),
                extra_str(d, "quote").map(String::from),
            ),
            Source::Document { label, hash } => (
                "accepted".to_string(),
                label_of(&address, Some(label.as_str()).filter(|l| !l.is_empty())),
                hash,
                id(d),
                "document".to_string(),
                None,
            ),
        };
        // Every `/edit` says why, so any person's edit of the part since the
        // answer is "edited with a reason".
        let answered = state != "accepted"
            && (0..chain.len()).any(|j| {
                ctx.after(j, i) && is_person(&chain[j]) && is_edit(&chain[j]) && ctx.touches(j, &address)
            });
        parts.push(ClientPart {
            stale: cr::part_hash(ctx.data, path) != recorded,
            reopened: reopened_now.iter().any(|r| r.address == address),
            answered,
            address,
            label,
            state,
            decision: id(d),
            review,
            found_by,
            quote,
            client: extra_json(d, "client"),
            at: d.timestamp.clone(),
        });
    }

    let lock_version = lock.and_then(|l| l.version.as_deref());
    let answer_of = |i: usize| {
        let d = &chain[i];
        let seen = extra_json(d, "seen");
        let w = who(d);
        ClientAnswer {
            decision: id(d),
            answer: extra_str(d, "answer").unwrap_or_default().to_string(),
            reasons: d
                .extra
                .get("reasons")
                .and_then(|v| v.as_sequence())
                .into_iter()
                .flatten()
                .filter_map(|v| v.as_str().map(String::from))
                .collect(),
            said: extra_str(d, "said").map(String::from),
            client: extra_json(d, "client"),
            channel: extra_str(d, "channel").map(String::from),
            evidence: extra_json(d, "evidence"),
            recorded_by: RecordedBy { id: w.id, name: w.name },
            at: d.timestamp.clone(),
            current: lock_version.is_some() && seen.get("version").and_then(Value::as_str) == lock_version,
            parts_known: part_answers
                .iter()
                .any(|&j| extra_str(&chain[j], "review") == d.id.as_deref()),
            seen,
        }
    };
    let answers: Vec<ClientAnswer> = documents.iter().map(|&i| answer_of(i)).collect();

    let named: BTreeSet<&str> = chain
        .iter()
        .filter(|d| is(d, cr::CLIENT_ANSWER_PART) || is(d, cr::DISMISS_PART))
        .filter_map(|d| extra_str(d, "suggestion"))
        .collect();
    let mut open: Vec<usize> = (0..chain.len())
        .filter(|&i| {
            let d = &chain[i];
            is(d, cr::SUGGEST_PART) && part_here(d).is_some() && !d.id.as_deref().is_some_and(|x| named.contains(x))
        })
        .collect();
    open = newest_first(open);
    open.reverse();
    let suggestions: Vec<ClientSuggestion> = open
        .iter()
        .map(|&i| {
            let d = &chain[i];
            let address = part_here(d).unwrap_or_default();
            ClientSuggestion {
                decision: id(d),
                review: extra_str(d, "review").unwrap_or_default().to_string(),
                label: label_of(&address, extra_str(d, "label")),
                address,
                answer: extra_str(d, "answer").unwrap_or_default().to_string(),
                quote: extra_str(d, "quote").unwrap_or_default().to_string(),
            }
        })
        .collect();

    // §7.5.4: what the next lock needs, and what only asks for a look.
    let name = |client: &Value| {
        client.get("name").and_then(Value::as_str).unwrap_or("The client").to_string()
    };
    let item = |code, text: String, blocks_lock, decision: String, address: Option<String>, label: Option<String>| Attention {
        code,
        text,
        blocks_lock,
        decision: Some(decision),
        address,
        label,
    };
    let mut blockers = Vec::new();
    let mut attention = Vec::new();
    for p in parts.iter().filter(|p| !p.answered) {
        let who = name(&p.client);
        match p.state.as_str() {
            "rejected" => blockers.push(item(
                "client_rejected",
                format!("{who} rejected {}. Edit it, saying why, before locking again.", p.label),
                true,
                p.decision.clone(),
                Some(p.address.clone()),
                Some(p.label.clone()),
            )),
            "accepted_with_changes" => attention.push(item(
                "client_change_asked",
                format!("{who} asked for a change to {}.", p.label),
                false,
                p.decision.clone(),
                Some(p.address.clone()),
                Some(p.label.clone()),
            )),
            _ => {}
        }
    }
    if let Some(a) = answers.first().filter(|a| a.answer == "rejected" && !a.parts_known) {
        blockers.push(item(
            "client_rejected_parts_unknown",
            format!(
                "{} rejected the document and which parts is not known yet. Mark or confirm them, then edit them, before locking again.",
                name(&a.client)
            ),
            true,
            a.decision.clone(),
            None,
            None,
        ));
    }
    for s in &suggestions {
        let who = answers
            .iter()
            .find(|a| a.decision == s.review)
            .map(|a| name(&a.client))
            .unwrap_or_else(|| "The client".to_string());
        attention.push(item(
            "client_part_suggested",
            format!("Ellis thinks {who}'s answer is about {}. Confirm or dismiss it.", s.label),
            false,
            s.decision.clone(),
            Some(s.address.clone()),
            Some(s.label.clone()),
        ));
    }

    ClientDerived {
        view: ClientView {
            available: lock.is_some() && reopened.is_empty(),
            answer: answers.first().cloned(),
            answers,
            parts,
            suggestions,
        },
        locked: lock.is_some(),
        reopened,
        blockers,
        attention,
    }
}

/// A `resolve` in this chain, not superseded, names `address`: how a contest
/// carried open in a frozen copy is settled (Contract 4 §7.2, item 1).
fn resolved_here(ctx: &Lookup, address: &str) -> bool {
    ctx.chain.decisions.iter().enumerate().any(|(i, d)| {
        d.kind.as_deref() == Some("resolve") && d.superseded_by.is_none() && ctx.touches(i, address)
    })
}

/// A decision that writes a field's value: an edit or a resolve, not a
/// proposal (which asks a person to write it) and not superseded.
fn writes(d: &Decision) -> bool {
    (is_edit(d) || d.kind.as_deref() == Some("resolve"))
        && d.action != PROPOSE_ACTION
        && d.superseded_by.is_none()
}

/// Did `d` write `key` on this document — `napkin.middleware/1` §10.5's
/// rule, the one the middleware holds a field by: its targets name the key
/// or its top-level key; with no targets, its `fields_changed` names the
/// top-level key and its action names no other field under it.
fn wrote(ctx: &Lookup, d: &Decision, key: &str) -> bool {
    let top = key.split(['.', '[']).next().unwrap_or(key);
    if !d.targets.is_empty() {
        return d
            .targets
            .iter()
            .map(|t| ctx.qualify(t))
            .any(|t| t == ctx.address(key) || t == ctx.address(top));
    }
    if !d.fields_changed.iter().any(|f| f == top) {
        return false;
    }
    if key == top {
        return true;
    }
    // `top.<leaf>` the action names, as the view writes it.
    let prefix = format!("{top}.");
    let mut named = d.action.match_indices(&prefix).filter_map(|(at, _)| {
        let before = d.action[..at].chars().next_back();
        if before.is_some_and(|c| c.is_alphanumeric() || c == '_' || c == '.') {
            return None;
        }
        let rest = &d.action[at + prefix.len()..];
        let end = rest
            .find(|c: char| !(c.is_alphanumeric() || c == '_'))
            .unwrap_or(rest.len());
        (end > 0).then(|| format!("{top}.{}", &rest[..end]))
    });
    named.all(|n| n == key)
}

/// Every field of this document whose current writing decision — the newest
/// decision that wrote it ([`writes`], [`wrote`]) — cites a rejected finding
/// (Contract 4 §7.2, item 4). A brief's fields are bare values with no
/// envelope to carry `finding_ids`; the drafter's decision is where it says
/// what it rests on. A redraft that no longer cites the finding answers it.
fn written_citing_findings<'r>(
    ctx: &Lookup,
    rejected: &BTreeMap<&'r str, &Value>,
    out: &mut BTreeMap<String, BTreeSet<&'r str>>,
) {
    for (i, d) in ctx.chain.decisions.iter().enumerate() {
        if !writes(d) {
            continue;
        }
        let because = d
            .reasoning
            .iter()
            .flat_map(|r| r.because.iter().flat_map(|p| p.cites.iter()));
        let cited: BTreeSet<&'r str> = d
            .cites
            .iter()
            .chain(because)
            .filter_map(|c| rejected.get_key_value(c.as_str()).map(|(k, _)| *k))
            .collect();
        if cited.is_empty() {
            continue;
        }
        for t in &d.targets {
            let full = ctx.qualify(t);
            let Some(key) = full
                .strip_prefix(ctx.doc_id)
                .and_then(|r| r.strip_prefix('#'))
                .filter(|k| !k.is_empty())
            else {
                continue;
            };
            let rewritten = ctx.chain.decisions.iter().enumerate().any(|(j, e)| {
                j != i && ctx.after(j, i) && writes(e) && wrote(ctx, e, key)
            });
            if !rewritten {
                out.entry(key.to_string())
                    .or_default()
                    .extend(cited.iter().copied());
            }
        }
    }
}

/// What deciders asked a person to look at, and the calls they were unsure
/// of — until a person has decided something about the same target since.
fn asked_for(ctx: &Lookup) -> Vec<Attention> {
    let mut out = Vec::new();
    for (i, d) in ctx.chain.decisions.iter().enumerate() {
        let Some(r) = &d.reasoning else { continue };
        // Ellis's suggestions ask for a person as client review does
        // (`client_part_suggested`), once.
        if d.superseded_by.is_some() || d.kind.as_deref() == Some(cr::KIND) {
            continue;
        }
        // A person has looked when they have since decided about the decision
        // itself, or about everything it decided. One of five findings
        // verified leaves the other four as unsure as they were.
        let named = d.id.as_deref().is_some_and(|id| {
            let refs = |e: &Decision| {
                e.targets
                    .iter()
                    .chain(&e.cites)
                    .any(|r| clan_sdk::decision::referenced_decision(r, &[id].into()).is_some())
            };
            ctx.chain
                .decisions
                .iter()
                .enumerate()
                .any(|(j, e)| ctx.after(j, i) && is_person(e) && refs(e))
        });
        // A bad verdict is also answered, target by target, by a later good
        // verdict with a reason (the Judge passing the redraft). Not on a
        // finding: D1 wants a person to verify those.
        let bad_verdict = d.is_verdict() && d.polarity.as_deref() == Some("bad");
        let answered = |t: &String| {
            let address = ctx.qualify(t);
            ctx.later(i, &address, is_person)
                || (bad_verdict
                    && ctx.target(&address).kind != "finding"
                    && ctx.later(i, &address, reasoned_good_verdict))
        };
        let mut aimed = aims(d).peekable();
        let every = aimed.peek().is_some() && aimed.all(answered);
        if named || every {
            continue;
        }
        let first = aims(d).next().map(|t| ctx.qualify(t));
        let label = first.as_deref().map(|a| ctx.target(a).label);
        let item = |code, text: String| Attention {
            code,
            text,
            blocks_lock: false,
            decision: d.id.clone(),
            address: first.clone(),
            label: label.clone(),
        };
        if let Some(a) = r.attention.as_deref().filter(|a| !a.trim().is_empty()) {
            out.push(item("flagged", a.trim().to_string()));
        }
        if r.certainty.level == "low" {
            let why = r.certainty.why.trim();
            out.push(item(
                "low_certainty",
                if why.is_empty() {
                    "Low certainty.".to_string()
                } else {
                    format!("Low certainty: {why}")
                },
            ));
        }
    }
    out
}

/// What a decision is about: its targets, or — on an entry that names none,
/// like a person's patch-data — the fields it changed.
fn aims(d: &Decision) -> std::slice::Iter<'_, String> {
    if d.targets.is_empty() {
        d.fields_changed.iter()
    } else {
        d.targets.iter()
    }
}

/// `text` names `path` whole: `#campaign.name` is not named by
/// `#campaign.name_long`.
fn mentions(text: &str, path: &str) -> bool {
    text.match_indices(path).any(|(at, _)| {
        !text[at + path.len()..]
            .chars()
            .next()
            .is_some_and(|c| c.is_alphanumeric() || c == '_')
    })
}

/// A person made it: the actor is `human:…`, or — on entries from before
/// actors — the agent says so.
fn is_person(d: &Decision) -> bool {
    match d.actor.as_deref() {
        Some(a) => a.starts_with("human:"),
        None => d.agent == "human" || d.agent.starts_with("human:"),
    }
}

/// An edit to the field: `kind: edit`, or an untyped entry whose action says
/// it confirmed or edited something.
fn is_edit(d: &Decision) -> bool {
    match d.kind.as_deref() {
        Some(k) => k == "edit",
        None => ["confirm", "edit", "patch-data"]
            .iter()
            .any(|p| d.action.starts_with(p)),
    }
}

/// A good verdict that says why: it answers a bad verdict on the same target.
fn reasoned_good_verdict(d: &Decision) -> bool {
    d.is_verdict()
        && d.polarity.as_deref() == Some("good")
        && (!d.rationale.trim().is_empty() || d.reasoning.is_some())
}

fn who(d: &Decision) -> Who {
    if is_person(d) {
        let id = d
            .actor
            .as_deref()
            .and_then(|a| a.strip_prefix("human:"))
            .or_else(|| d.agent.strip_prefix("human:"))
            .unwrap_or("someone")
            .to_string();
        let name = match id.as_str() {
            "local" => "You".to_string(),
            other => other.to_string(),
        };
        return Who {
            kind: "person",
            id,
            name,
            you: false,
        };
    }
    let raw = d.handler.as_deref().unwrap_or(&d.agent);
    let id = raw.split(['@', '/']).next().unwrap_or(raw).to_string();
    Who {
        kind: "agent",
        name: humanise(&id),
        id,
        you: false,
    }
}

/// One step of an entity-keyed path.
#[derive(Debug, PartialEq)]
enum Seg<'a> {
    Name(&'a str),
    Key(&'a str),
}

/// `a.b[k].c` → `[Name(a), Name(b), Key(k), Name(c)]`. Loose: a malformed
/// path still gives something to label.
fn segments(path: &str) -> Vec<Seg<'_>> {
    let mut out = Vec::new();
    for part in path.split('.').filter(|p| !p.is_empty()) {
        let mut rest = part;
        if let Some(open) = rest.find('[') {
            if open > 0 {
                out.push(Seg::Name(&rest[..open]));
            }
            rest = &rest[open..];
            while let Some(after) = rest.strip_prefix('[') {
                let close = after.find(']').unwrap_or(after.len());
                out.push(Seg::Key(&after[..close]));
                rest = after.get(close + 1..).unwrap_or("");
            }
        } else {
            out.push(Seg::Name(rest));
        }
    }
    out
}

/// The value at an entity-keyed path: a map by key, a list by the `id`,
/// `ref` or `key` of its entries.
fn lookup<'v>(data: &'v Value, path: &str) -> Option<&'v Value> {
    segments(path).into_iter().try_fold(data, |v, s| {
        let k = match s {
            Seg::Name(k) | Seg::Key(k) => k,
        };
        match v {
            Value::Object(m) => m.get(k),
            Value::Array(items) => items.iter().find(|x| {
                ["id", "ref", "key"]
                    .iter()
                    .any(|f| x.get(f).and_then(Value::as_str) == Some(k))
            }),
            _ => None,
        }
    })
}

fn data_contests(data: &Value) -> impl Iterator<Item = &Value> {
    data.get("selection")
        .and_then(|s| s.get("contested"))
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
}

fn contest_entry<'v>(data: &'v Value, id: &str) -> Option<&'v Value> {
    data_contests(data).find(|c| str_of(c, "id") == Some(id))
}

/// Every place in the data that cites a rejected finding through
/// `finding_ids` (or `synthesis_finding_ids`), reported at the field that
/// holds it: the path stops before `value`, the field envelope's payload
/// (Contract 3 §2.1). The host's own projection is skipped, and so are the
/// ancestors' frozen copies: a field there cannot be revised here, and is its
/// own document's to answer for (`GET /upstream` names what cites it).
fn citing_findings<'r>(
    v: &Value,
    path: &mut Vec<String>,
    rejected: &BTreeMap<&'r str, &Value>,
    out: &mut BTreeMap<String, BTreeSet<&'r str>>,
) {
    match v {
        Value::Object(m) => {
            for key in ["finding_ids", "synthesis_finding_ids"] {
                for id in m.get(key).and_then(Value::as_array).into_iter().flatten() {
                    let Some((&r, _)) = id.as_str().and_then(|s| rejected.get_key_value(s)) else {
                        continue;
                    };
                    let field = path
                        .iter()
                        .position(|p| p == ".value")
                        .map_or(&path[..], |at| &path[..at]);
                    let at: String = field.concat();
                    out.entry(at.trim_start_matches('.').to_string())
                        .or_default()
                        .insert(r);
                }
            }
            for (k, child) in m {
                if path.is_empty() && (k == members::PROJECTION_KEY || k == UPSTREAM_KEY) {
                    continue;
                }
                path.push(format!(".{k}"));
                citing_findings(child, path, rejected, out);
                path.pop();
            }
        }
        Value::Array(items) => {
            for (n, child) in items.iter().enumerate() {
                let key = ["id", "ref", "key"]
                    .iter()
                    .find_map(|f| child.get(f).and_then(Value::as_str))
                    .map(String::from)
                    .unwrap_or_else(|| n.to_string());
                path.push(format!("[{key}]"));
                citing_findings(child, path, rejected, out);
                path.pop();
            }
        }
        _ => {}
    }
}

/// A fact as a person names it: its key in words, and the market.
/// `market.private_label_share` → "Private label share · IE"; a one-word
/// leaf keeps its namespace (`awareness.prompted` → "Awareness prompted").
/// A synthesis fact is named by what it says.
pub(crate) fn fact_label(f: &Value) -> String {
    let key = str_of(f, "key").unwrap_or_default();
    let words = if str_of(f, "method") == Some("synthesis") {
        f.get("value")
            .and_then(Value::as_str)
            .map(|s| clip(s, 80))
            .unwrap_or_default()
    } else {
        let parts: Vec<&str> = key.split('.').filter(|p| !p.is_empty()).collect();
        let leaf = match parts.as_slice() {
            [.., last] if parts.len() > 1 && last.contains('_') => last.to_string(),
            _ => parts.join(" "),
        };
        let mut w = leaf.replace('_', " ");
        if let Some(c) = w.get(..1) {
            w = c.to_uppercase() + &w[1..];
        }
        w.replace(" yoy", " year on year")
    };
    let label = join_with(&[Some(words).filter(|w| !w.is_empty()), str_of(f, "market").map(String::from)], " · ");
    if label.is_empty() {
        str_of(f, "id").unwrap_or("fact").to_string()
    } else {
        label
    }
}

fn fact_detail(f: &Value) -> String {
    let value = f.get("value").map(|v| format_value(v, str_of(f, "unit")));
    let sources = f
        .get("sources")
        .and_then(Value::as_array)
        .map(|s| {
            s.iter()
                .filter_map(Value::as_str)
                .collect::<Vec<_>>()
                .join(", ")
        })
        .filter(|s| !s.is_empty())
        .map(|s| format!("sources {s}"));
    let parts = [
        value,
        str_of(f, "as_of").map(|a| format!("as of {a}")),
        str_of(f, "confidence").map(|c| format!("{c} confidence")),
        sources,
        f.get("stale")
            .filter(|s| !s.is_null() && s.as_bool() != Some(false))
            .map(|_| "stale: the layer holds a newer version".to_string()),
    ];
    join(&parts)
}

/// A fact's value as a person reads it: proportions as percentages.
fn format_value(v: &Value, unit: Option<&str>) -> String {
    match (v, unit) {
        (Value::Number(n), Some("proportion")) => {
            let pct = n.as_f64().unwrap_or(0.0) * 100.0;
            format!("{}%", (pct * 10.0).round() / 10.0)
        }
        (Value::Number(n), Some("percent_abv")) => format!("{n}% ABV"),
        (Value::Number(n), Some("percent")) => format!("{n}%"),
        (Value::Number(n), Some(u @ ("eur" | "gbp" | "usd"))) => {
            let sym = match u { "eur" => "€", "gbp" => "£", _ => "$" };
            format!("{sym}{}", compact(n.as_f64().unwrap_or(0.0)))
        }
        (Value::Number(n), Some("count" | "units")) => compact(n.as_f64().unwrap_or(0.0)),
        (Value::Number(n), Some(u)) if !matches!(u, "code" | "text") => {
            format!("{} {u}", compact(n.as_f64().unwrap_or(0.0)))
        }
        (Value::String(s), _) => s.clone(),
        (other, _) => other.to_string(),
    }
}

/// A number as a person reads it: 6.1m, 21k, 4.5.
fn compact(v: f64) -> String {
    let (d, suf) = match v.abs() {
        a if a >= 1e9 => (1e9, "bn"),
        a if a >= 1e6 => (1e6, "m"),
        a if a >= 1e4 => (1e3, "k"),
        _ => (1.0, ""),
    };
    let x = ((v / d) * 10.0).round() / 10.0;
    let s = if x.fract() == 0.0 { format!("{}", x as i64) } else { format!("{x}") };
    format!("{s}{suf}")
}

/// A short preview of what a field holds: the envelope's `value` when it
/// has one, only when it is short and plain.
fn preview(node: &Value) -> Option<String> {
    let v = node.get("value").unwrap_or(node);
    match v {
        Value::String(s) => Some(clip(s, 160)),
        Value::Number(_) | Value::Bool(_) => Some(v.to_string()),
        Value::Object(m) => m.get("name").and_then(Value::as_str).map(String::from),
        _ => None,
    }
}

fn str_of<'v>(v: &'v Value, key: &str) -> Option<&'v str> {
    v.get(key).and_then(Value::as_str).filter(|s| !s.is_empty())
}

fn join(parts: &[Option<String>]) -> String {
    join_with(parts, " · ")
}

fn join_with(parts: &[Option<String>], sep: &str) -> String {
    parts
        .iter()
        .flatten()
        .cloned()
        .collect::<Vec<_>>()
        .join(sep)
}

fn plural(n: usize, what: &str) -> String {
    if n == 1 {
        format!("1 {what}")
    } else {
        format!("{n} {what}s")
    }
}

/// `start_campaign` → "Start campaign"; `in_market` → "In market".
fn humanise(id: &str) -> String {
    let words = id.replace(['_', '-'], " ");
    let mut chars = words.trim().chars();
    match chars.next() {
        Some(c) => c.to_uppercase().chain(chars).collect(),
        None => String::new(),
    }
}

fn clip(s: &str, max: usize) -> String {
    let s = s.trim();
    if s.chars().count() <= max {
        return s.to_string();
    }
    let mut out: String = s.chars().take(max - 1).collect();
    out.push('…');
    out
}

/// When, to the second. The chain mixes precisions — a packed entry is stamped
/// to the nanosecond, a review decision to the second — so within one second
/// the stamps say nothing and chain order (prepend-only, newest first) decides.
pub(crate) fn stamp(ts: &str) -> i64 {
    chrono::DateTime::parse_from_rfc3339(ts.trim())
        .map(|t| t.timestamp() * 1000)
        .unwrap_or(0)
}

#[cfg(test)]
mod tests;
