// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! `agent/decision-chain.yaml` model (spec §7).
//!
//! A [`Decision`] is the one primitive behind every mark a document carries
//! (OS-layer contract §3): an edit, a contest and its resolution, a verdict, a
//! confidentiality classification, a pin, a finding, a verification, an
//! approval, a lease. The original fields (`agent`, `action`, `rationale`,
//! `timestamp`, `fields_changed`, `pinned`, `trace-ref`) keep their on-disk
//! names and meaning. Every field added since is optional and skipped when
//! empty, so a file that never used them is written back byte-for-byte as it
//! was read.
//!
//! Anything the struct does not know lands in [`Decision::extra`] and is
//! written back out, so a file produced by a newer SDK survives a
//! read-modify-write through this one instead of losing its fields.

use std::collections::{BTreeMap, BTreeSet};

use serde::{Deserialize, Serialize};

/// The decision kinds the OS layer defines (contract §3). `kind` is a string,
/// not an enum: a kind this SDK has not heard of still parses and round-trips,
/// and `validate` reports it rather than the parser refusing the file.
pub const DECISION_KINDS: &[&str] = &[
    "edit", "contest", "resolve", "verdict", "classify", "pin", "finding", "verify", "approve",
    "lease", "backref",
];

/// Prefix every generated decision id carries.
pub const DECISION_ID_PREFIX: &str = "d_";

/// The ordered decision log. Newest entries first.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct DecisionChain {
    #[serde(default)]
    pub decisions: Vec<Decision>,
}

/// A single decision entry.
///
/// Field order here is serialisation order. The original fields keep their
/// relative order, so an untouched pre-change entry serialises identically.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct Decision {
    /// Stable identity, generated when the decision is created and never
    /// derived from its content — a verdict must keep pointing at the same
    /// decision after its rationale is edited or compressed. `d_` + a ULID
    /// (see [`new_decision_id`]). Absent on entries written before ids existed.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub id: Option<String>,
    /// One of [`DECISION_KINDS`] (contract §3), kept as a string.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub kind: Option<String>,

    /// Who wrote the entry, as older readers understand it. Required on disk.
    pub agent: String,
    /// What the request body said about who wrote it, when that differs from
    /// the authenticated [`actor`](Self::actor). A claim, recorded as one.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub claimed_agent: Option<String>,
    /// The principal from the request context — `human:<id>`,
    /// `process:<job-id>` or `<producer>/<version>`. Never from the body.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub actor: Option<String>,
    /// `name@major.minor` when a handler acted.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub handler: Option<String>,
    /// The judgement backend that answered, when one did.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub backend: Option<String>,
    /// The org and brand the context resolved.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub scope: Option<DecisionScope>,

    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub version: Option<String>,
    pub action: String,

    /// Addresses (`<doc-id>#<entity-keyed path>`) this decision is about.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub targets: Vec<String>,
    /// Fact pins, sources, findings and decisions this decision rests on.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub cites: Vec<String>,
    /// The decision that replaced this one. Supersession chains: a decision
    /// superseded by one that is itself later superseded keeps its own link.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub superseded_by: Option<String>,

    /// Verdicts: `good` or `bad`.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub polarity: Option<String>,
    /// Verdicts: a code from the reason taxonomy. A string, never an enum —
    /// the taxonomy is extensible (L2) and is validated where it lives.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reason_code: Option<String>,
    /// The taxonomy version `reason_code` was written under (L2).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub taxonomy_version: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reviewer_role: Option<String>,
    /// Classify: where the target may travel (C2).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub licence: Option<Licence>,

    /// The plain one-line summary older readers show. When the decision has
    /// [`reasoning`](Self::reasoning) this is its [`Reasoning::summary`] (or
    /// whatever the writer supplied); compression only ever rewrites this.
    pub rationale: String,
    /// Why, in the shape of the foundation spec's decisions: what was
    /// decided, the evidence with its cites, the alternatives that lost, the
    /// certainty and what would reverse it. Optional — a person's edit has
    /// none — and never touched by compression.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reasoning: Option<Reasoning>,
    pub timestamp: String,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub fields_changed: Vec<String>,
    #[serde(default, skip_serializing_if = "is_false")]
    pub pinned: bool,
    #[serde(rename = "trace-ref", default, skip_serializing_if = "Option::is_none")]
    pub trace_ref: Option<TraceRef>,

    /// Every field this struct does not declare, kept verbatim so it survives
    /// a read-modify-write.
    #[serde(flatten)]
    pub extra: BTreeMap<String, serde_yaml::Value>,
}

/// The org and brand a decision was made under.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct DecisionScope {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub org: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub brand: Option<String>,
    #[serde(flatten)]
    pub extra: BTreeMap<String, serde_yaml::Value>,
}

/// The three C2 destinations: may it reach the model, appear in an export,
/// enter the corpus.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct Licence {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub model: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub export: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub corpus: Option<bool>,
    #[serde(flatten)]
    pub extra: BTreeMap<String, serde_yaml::Value>,
}

/// The certainty levels a [`Reasoning`] may state.
pub const CERTAINTY_LEVELS: &[&str] = &["high", "medium", "low"];

/// A decision's structured reasoning (OS-layer contract §3): the discipline
/// the foundation spec's own decisions follow — decision / because /
/// rejected / reverses_if — written by whoever decided.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct Reasoning {
    /// One sentence: what was decided.
    #[serde(default)]
    pub decided: String,
    /// The evidence, at least one point. A point that states a figure cites
    /// where the figure is.
    #[serde(default)]
    pub because: Vec<ReasonPoint>,
    /// The alternatives considered and why each lost.
    #[serde(default)]
    pub rejected: Vec<Rejected>,
    /// Why there was nothing to reject, when `rejected` is empty: there was
    /// genuinely one option, and this says so.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub only_option: Option<String>,
    #[serde(default)]
    pub certainty: Certainty,
    /// What new evidence would reverse it.
    #[serde(default)]
    pub would_change_if: String,
    /// Why a person should look, when the decider thinks one should: an
    /// uncertain call, thin evidence, a contest, a skipped lens that might
    /// matter.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub attention: Option<String>,
    #[serde(flatten)]
    pub extra: BTreeMap<String, serde_yaml::Value>,
}

/// One piece of evidence and the ids it rests on — facts, findings, sources,
/// materials, decisions, or addresses.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct ReasonPoint {
    pub point: String,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub cites: Vec<String>,
    #[serde(flatten)]
    pub extra: BTreeMap<String, serde_yaml::Value>,
}

/// An alternative that lost, and why.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct Rejected {
    pub option: String,
    pub why: String,
    #[serde(flatten)]
    pub extra: BTreeMap<String, serde_yaml::Value>,
}

/// How sure, and on what basis. For anything resting on facts this is the
/// DERIVED confidence (source tier + corroboration), never self-reported.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct Certainty {
    pub level: String,
    pub why: String,
    #[serde(flatten)]
    pub extra: BTreeMap<String, serde_yaml::Value>,
}

impl Reasoning {
    /// The one-line rationale older readers show: `decided`, then the first
    /// point of evidence.
    pub fn summary(&self) -> String {
        let decided = self.decided.trim();
        match self.because.first().map(|p| p.point.trim()) {
            Some(p) if !p.is_empty() && !decided.is_empty() => {
                let sep = if decided.ends_with(['.', '!', '?']) {
                    " "
                } else {
                    ". "
                };
                format!("{decided}{sep}Because: {p}")
            }
            Some(p) if decided.is_empty() => p.to_string(),
            _ => decided.to_string(),
        }
    }

    /// What is wrong with its shape, if anything: an empty `decided`, no
    /// `because` point, an empty point or cite, a point that states a figure
    /// and cites nothing, a rejected alternative without its option or why,
    /// no alternatives and no `only_option`, a certainty level outside
    /// [`CERTAINTY_LEVELS`] or without its why, an empty `would_change_if`.
    pub fn problems(&self) -> Vec<String> {
        let mut out = Vec::new();
        let blank = |s: &str| s.trim().is_empty();
        if blank(&self.decided) {
            out.push("reasoning.decided is empty".to_string());
        }
        if self.because.is_empty() {
            out.push("reasoning.because has no point".to_string());
        }
        for (i, p) in self.because.iter().enumerate() {
            if blank(&p.point) {
                out.push(format!("reasoning.because[{i}] is empty"));
            }
            if p.cites.iter().any(|c| blank(c)) {
                out.push(format!("reasoning.because[{i}] has an empty cite"));
            }
            if p.cites.is_empty() && states_figure(&p.point) {
                out.push(format!(
                    "reasoning.because[{i}] states a figure and cites nothing"
                ));
            }
        }
        for (i, r) in self.rejected.iter().enumerate() {
            if blank(&r.option) || blank(&r.why) {
                out.push(format!("reasoning.rejected[{i}] needs an option and a why"));
            }
        }
        if self.rejected.is_empty() && self.only_option.as_deref().map_or(true, blank) {
            out.push(
                "reasoning.rejected is empty and only_option does not say why there was one option"
                    .to_string(),
            );
        }
        if !CERTAINTY_LEVELS.contains(&self.certainty.level.as_str()) {
            out.push(format!(
                "reasoning.certainty.level {:?} is not one of: {}",
                self.certainty.level,
                CERTAINTY_LEVELS.join(", ")
            ));
        }
        if blank(&self.certainty.why) {
            out.push("reasoning.certainty.why is empty".to_string());
        }
        if blank(&self.would_change_if) {
            out.push("reasoning.would_change_if is empty".to_string());
        }
        if self.attention.as_deref().is_some_and(blank) {
            out.push("reasoning.attention is present but empty".to_string());
        }
        out
    }
}

/// True when `text` states a figure: it holds a digit.
fn states_figure(text: &str) -> bool {
    text.chars().any(|c| c.is_ascii_digit())
}

/// Reference to full context held in an external store (spec §13).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TraceRef {
    pub store: String,
    pub entry: String,
    #[serde(rename = "content-hash")]
    pub content_hash: String,
}

fn is_false(b: &bool) -> bool {
    !*b
}

/// A fresh decision id: `d_` followed by a 26-character ULID (48-bit
/// millisecond timestamp, 80 random bits, Crockford base32). Sortable by
/// creation time, and never a function of the decision's content.
pub fn new_decision_id() -> String {
    const ALPHABET: &[u8; 32] = b"0123456789ABCDEFGHJKMNPQRSTVWXYZ";
    let millis = chrono::Utc::now().timestamp_millis().max(0) as u128 & ((1u128 << 48) - 1);
    // A v4 UUID's bytes 0..6 and 10..16 are fully random (the version and
    // variant bits live in bytes 6 and 8); ten of them are the 80 bits a
    // ULID needs.
    let uuid = uuid::Uuid::new_v4();
    let b = uuid.as_bytes();
    let random = b[0..6]
        .iter()
        .chain(&b[10..14])
        .fold(0u128, |acc, byte| (acc << 8) | u128::from(*byte));
    let value = (millis << 80) | random;
    let mut out = String::with_capacity(DECISION_ID_PREFIX.len() + 26);
    out.push_str(DECISION_ID_PREFIX);
    for i in (0..26).rev() {
        out.push(ALPHABET[((value >> (i * 5)) & 0x1f) as usize] as char);
    }
    out
}

/// The decision id a reference names, if it names one in `known`.
///
/// A reference is a bare id (`d_…`), an address whose path is the id
/// (`<doc>#d_…`), or an address into the chain (`<doc>#decisions[d_…]`).
pub fn referenced_decision<'a>(reference: &'a str, known: &BTreeSet<&str>) -> Option<&'a str> {
    let path = reference.rsplit_once('#').map_or(reference, |(_, p)| p);
    let id = path
        .strip_prefix("decisions[")
        .and_then(|p| p.strip_suffix(']'))
        .unwrap_or(path);
    known.contains(id).then_some(id)
}

impl DecisionChain {
    pub fn from_yaml(bytes: &[u8]) -> crate::Result<Self> {
        Ok(serde_yaml::from_slice(bytes)?)
    }

    pub fn to_yaml(&self) -> crate::Result<Vec<u8>> {
        Ok(serde_yaml::to_string(self)?.into_bytes())
    }

    /// Prepend a new decision (newest-first ordering).
    pub fn prepend(&mut self, decision: Decision) {
        self.decisions.insert(0, decision);
    }

    /// The ids of every entry that has one.
    pub fn ids(&self) -> BTreeSet<&str> {
        self.decisions
            .iter()
            .filter_map(|d| d.id.as_deref())
            .collect()
    }

    /// Ids some other entry points at through `superseded_by`, `cites` or
    /// `targets`. The chain's graph hangs off these entries, so nothing that
    /// rewrites the chain may alter them.
    pub fn referenced_ids(&self) -> BTreeSet<String> {
        let known = self.ids();
        let mut out = BTreeSet::new();
        for d in &self.decisions {
            let refs = d
                .superseded_by
                .iter()
                .chain(&d.cites)
                .chain(&d.targets)
                .filter_map(|r| referenced_decision(r, &known))
                .filter(|id| d.id.as_deref() != Some(*id));
            out.extend(refs.map(str::to_string));
        }
        out
    }
}

impl Decision {
    /// A minimal decision with required fields populated and a fresh id.
    pub fn new(
        agent: impl Into<String>,
        action: impl Into<String>,
        rationale: impl Into<String>,
        timestamp: impl Into<String>,
    ) -> Self {
        Self {
            id: Some(new_decision_id()),
            agent: agent.into(),
            action: action.into(),
            rationale: rationale.into(),
            timestamp: timestamp.into(),
            ..Default::default()
        }
    }

    /// True for a judgement on something: `kind: verdict`, or a polarity
    /// written straight onto the decision it judges.
    pub fn is_verdict(&self) -> bool {
        self.kind.as_deref() == Some("verdict") || self.polarity.is_some()
    }

    /// True when `kind` is absent or one the OS layer defines.
    pub fn kind_is_known(&self) -> bool {
        self.kind
            .as_deref()
            .map_or(true, |k| DECISION_KINDS.contains(&k))
    }

    /// Whether the contract requires a rationale on this decision: `resolve`,
    /// `approve`, a bad verdict, and any verdict coded `other` (§3, L4).
    pub fn requires_rationale(&self) -> bool {
        match self.kind.as_deref() {
            Some("resolve") | Some("approve") => true,
            _ => {
                self.is_verdict()
                    && (self.polarity.as_deref() == Some("bad")
                        || self.reason_code.as_deref() == Some("other"))
            }
        }
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;

    const LEGACY: &str = "decisions:
- agent: extractor
  version: '2'
  action: extracted
  rationale: Read the email.
  timestamp: 2026-05-31T10:00:00Z
  fields_changed:
  - campaign
  pinned: true
  trace-ref:
    store: s
    entry: e
    content-hash: sha256:00
- agent: human
  action: create
  rationale: Opened.
  timestamp: 2026-05-31T09:00:00Z
";

    #[test]
    fn untouched_legacy_chain_round_trips_byte_for_byte() {
        let chain = DecisionChain::from_yaml(LEGACY.as_bytes()).unwrap();
        assert_eq!(String::from_utf8(chain.to_yaml().unwrap()).unwrap(), LEGACY);
    }

    #[test]
    fn unknown_fields_survive_read_modify_write() {
        // A file from a newer SDK: a field this one does not know, a nested
        // unknown inside a known struct, and an unknown kind.
        let newer = "decisions:
- id: d_01JA0D07VER
  kind: attest
  agent: human
  action: verify
  licence: { model: true, export: false, corpus: false, region: eu }
  rationale: Checked.
  timestamp: 2026-09-22T16:05:31Z
  wrote_fact: f_01JA0C9V4X
  source: { uri: 'human:u_aoife', tier: reviewer-verified }
";
        let mut chain = DecisionChain::from_yaml(newer.as_bytes()).unwrap();
        chain.prepend(Decision::new("a", "b", "c", "2026-09-23T00:00:00Z"));
        chain.decisions[1].rationale = "Checked twice.".into();
        let back = DecisionChain::from_yaml(&chain.to_yaml().unwrap()).unwrap();
        let d = &back.decisions[1];
        assert_eq!(d.kind.as_deref(), Some("attest"));
        assert_eq!(d.rationale, "Checked twice.");
        assert_eq!(
            d.extra["wrote_fact"],
            serde_yaml::Value::from("f_01JA0C9V4X")
        );
        assert_eq!(
            d.extra["source"]["tier"],
            serde_yaml::Value::from("reviewer-verified")
        );
        let licence = d.licence.as_ref().unwrap();
        assert_eq!(licence.export, Some(false));
        assert_eq!(licence.extra["region"], serde_yaml::Value::from("eu"));
    }

    #[test]
    fn typed_fields_read_by_an_older_shape_survive() {
        // The other direction: a reader that knows only the original fields
        // and keeps the rest in its own tail loses nothing when it writes a
        // typed decision back.
        #[derive(Serialize, Deserialize)]
        struct OldDecision {
            agent: String,
            action: String,
            rationale: String,
            timestamp: String,
            #[serde(flatten)]
            rest: BTreeMap<String, serde_yaml::Value>,
        }
        let mut d = Decision::new(
            "human",
            "verdict",
            "Two objectives.",
            "2026-09-22T17:20:00Z",
        );
        d.kind = Some("verdict".into());
        d.polarity = Some("bad".into());
        d.reason_code = Some("other".into());
        d.targets = vec!["doc#campaign.objective".into()];
        let yaml = serde_yaml::to_string(&d).unwrap();
        let old: OldDecision = serde_yaml::from_str(&yaml).unwrap();
        let again: Decision = serde_yaml::from_str(&serde_yaml::to_string(&old).unwrap()).unwrap();
        assert_eq!(again, d);
    }

    const REASONED: &str = "decisions:
- id: d_01JB0MERGE1
  kind: contest
  agent: start_campaign@1.0
  action: open_contest
  rationale: 'Opened a contest on the flagship year; nothing is picked. Because: the IE run says 2019'
  reasoning:
    decided: Opened a contest on the flagship year; nothing is picked.
    because:
    - point: the IE run says 2019
      cites:
      - f_01JB0IE0001
    - point: the GB run says 2020
      cites:
      - f_01JB0GB0001
    rejected:
    - option: pick either value silently
      why: nothing says which run is right
    certainty:
      level: high
      why: the two values are recorded as found
    would_change_if: a primary source settles the year
    attention: two runs disagree
    viewer_hint: block
  timestamp: 2026-09-24T10:00:00Z
";

    pub(crate) fn sample_reasoning() -> Reasoning {
        Reasoning {
            decided: "Pinned the IE share.".into(),
            because: vec![ReasonPoint {
                point: "CSO and SIMI both state 41%".into(),
                cites: vec!["f_01JB0IE0001".into(), "src_cso".into()],
                ..Default::default()
            }],
            rejected: vec![Rejected {
                option: "the blog's 45%".into(),
                why: "tertiary and alone".into(),
                ..Default::default()
            }],
            certainty: Certainty {
                level: "high".into(),
                why: "primary tier, corroborated by an independent domain".into(),
                ..Default::default()
            },
            would_change_if: "a newer CSO release revises it".into(),
            ..Default::default()
        }
    }

    #[test]
    fn reasoning_round_trips_byte_for_byte_with_unknown_fields() {
        let chain = DecisionChain::from_yaml(REASONED.as_bytes()).unwrap();
        let r = chain.decisions[0].reasoning.as_ref().unwrap();
        assert_eq!(r.because.len(), 2);
        assert_eq!(r.certainty.level, "high");
        assert_eq!(r.attention.as_deref(), Some("two runs disagree"));
        assert_eq!(r.extra["viewer_hint"], serde_yaml::Value::from("block"));
        assert!(r.problems().is_empty(), "{:?}", r.problems());
        assert_eq!(
            String::from_utf8(chain.to_yaml().unwrap()).unwrap(),
            REASONED
        );
    }

    #[test]
    fn a_decision_without_reasoning_writes_no_reasoning_key() {
        let d = Decision::new("human", "edit", "Named it.", "2026-09-24T10:00:00Z");
        assert!(!serde_yaml::to_string(&d).unwrap().contains("reasoning"));
    }

    #[test]
    fn summary_is_decided_then_the_first_point() {
        let r = sample_reasoning();
        assert_eq!(
            r.summary(),
            "Pinned the IE share. Because: CSO and SIMI both state 41%"
        );
        let bare = Reasoning {
            decided: "Asked".into(),
            ..Default::default()
        };
        assert_eq!(bare.summary(), "Asked");
    }

    #[test]
    fn problems_name_every_broken_part() {
        assert!(sample_reasoning().problems().is_empty());
        let empty = Reasoning::default().problems().join("\n");
        for part in [
            "decided is empty",
            "because has no point",
            "only_option",
            "certainty.level",
            "certainty.why",
            "would_change_if",
        ] {
            assert!(empty.contains(part), "{part} in {empty}");
        }

        let mut r = sample_reasoning();
        r.because[0].cites = vec![];
        assert!(r.problems()[0].contains("states a figure and cites nothing"));
        r.because[0].cites = vec![" ".into()];
        assert!(r.problems()[0].contains("empty cite"));

        let mut r = sample_reasoning();
        r.rejected.clear();
        assert_eq!(r.problems().len(), 1);
        r.only_option = Some("the material names one brand".into());
        assert!(r.problems().is_empty());

        let mut r = sample_reasoning();
        r.certainty.level = "certain".into();
        assert!(r.problems()[0].contains("\"certain\" is not one of"));
        // A point with no figure needs no cite.
        let mut r = sample_reasoning();
        r.because[0] = ReasonPoint {
            point: "the email calls it ours".into(),
            ..Default::default()
        };
        assert!(r.problems().is_empty());
    }

    #[test]
    fn ids_are_prefixed_ulids_and_unique() {
        let a = new_decision_id();
        let b = new_decision_id();
        assert_ne!(a, b);
        assert_eq!(a.len(), 28);
        assert!(a.starts_with("d_0"), "{a}");
        assert!(a[2..]
            .chars()
            .all(|c| "0123456789ABCDEFGHJKMNPQRSTVWXYZ".contains(c)));
    }

    #[test]
    fn references_resolve_through_addresses() {
        let known: BTreeSet<&str> = ["d_1"].into_iter().collect();
        assert_eq!(referenced_decision("d_1", &known), Some("d_1"));
        assert_eq!(
            referenced_decision("doc#decisions[d_1]", &known),
            Some("d_1")
        );
        assert_eq!(referenced_decision("doc#d_1", &known), Some("d_1"));
        assert_eq!(referenced_decision("doc#campaign.name", &known), None);
    }
}
