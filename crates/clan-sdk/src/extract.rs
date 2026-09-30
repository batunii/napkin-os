// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! The extract's `upstream` printer (`docs/contracts/clan-extract.md` §1.1,
//! §10.2, §11.2–§11.3): the research a spun-off document carries, as Sai's
//! brief engine takes it — `parse_brief.run(upstream={brand, category,
//! competitors, facts, decisions})`.
//!
//! It is the agent version (`redact_confidential` off): a value marked
//! confidential for the corpus (`corpus: false`, `model: true`) is shown,
//! and a value marked `model: false` reads `[Marked confidential]` in the
//! row that holds it and wherever that exact value appears in another row
//! (§5.4, §10.5).
//!
//! The output is built from the document's own bytes (the carried copy,
//! frozen at `data.upstream.<id>`, the merged members and the carried chain)
//! and a small context (the names the account gives each person, §3.1.2).
//! There is no clock, no randomness, no network and no model: the same bytes
//! and context give the same JSON.
//!
//! **Nothing here halts a brief.** A member that does not parse, a row that
//! cannot be built, a mark that closes the whole research: each is left out
//! with its reason in [`Upstream::skipped`], and the rest is printed. The
//! engine runs as it does without research when there is nothing at all.

use std::collections::{BTreeMap, BTreeSet};

use serde_json::{json, Map, Value as Json};
use serde_yaml::Value as Yaml;

use crate::container::ClanFile;
use crate::decision::{Decision, DecisionChain};

/// The grammar version this printer implements (`clan-extract/1`).
pub const GRAMMAR_VERSION: &str = "1.0.0";

/// What a hidden value reads, in every slot that held it.
pub const MARKED: &str = "[Marked confidential]";

/// `upstream.category` is one of these or absent: the engine's categories
/// (`engine/rag/jev_checks.py` `CATEGORY_DESC`) less `other`, which
/// `brief_facets` never takes from upstream.
pub const ENGINE_CATEGORIES: &[&str] = &[
    "fmcg",
    "food_drink",
    "alcohol",
    "financial_services",
    "retail",
    "luxury",
    "automotive",
    "technology",
    "b2b",
    "telecoms",
    "travel",
    "public_sector",
    "charity",
    "healthcare",
    "media_entertainment",
    "gambling_betting",
    "fashion_beauty",
];

/// A research category code (`<vertical>.<leaf>`, the planner taxonomy,
/// `docs/contracts/peripherals/taxonomy.json`) as the engine's category: a
/// leaf's own row first, else its vertical's (`<vertical>.*`). A code no row
/// maps (energy, property, recruitment; a code outside the taxonomy) sends
/// no category, and jev chooses one (§11.3). Every value is one of
/// [`ENGINE_CATEGORIES`].
pub const CATEGORY_MAP: &[(&str, &str)] = &[
    ("food.pet_food", "fmcg"),
    ("beauty_fashion.luxury_designer", "luxury"),
    ("beauty_fashion.jewellery_watches", "luxury"),
    ("beauty_fashion.aesthetic_procedures", "healthcare"),
    ("telco.handsets_devices", "technology"),
    ("telco.b2b_connectivity", "b2b"),
    ("software.b2b_saas", "b2b"),
    ("software.developer_infra", "b2b"),
    ("software.cybersecurity", "b2b"),
    ("technology.gaming", "media_entertainment"),
    ("health.private_health_insurance", "financial_services"),
    ("travel.travel_insurance_fx", "financial_services"),
    ("retail.retail_media", "media_entertainment"),
    ("home_property.furniture_homeware", "retail"),
    ("public.government_agencies", "public_sector"),
    ("public.political_referenda", "public_sector"),
    ("public.universities_colleges", "public_sector"),
    ("public.charities_ngos", "charity"),
    ("public.betting_gaming", "gambling_betting"),
    ("automotive.*", "automotive"),
    ("alcohol.*", "alcohol"),
    ("soft_drinks.*", "food_drink"),
    ("food.*", "food_drink"),
    ("household.*", "fmcg"),
    ("beauty_fashion.*", "fashion_beauty"),
    ("retail.*", "retail"),
    ("qsr.*", "food_drink"),
    ("finance.*", "financial_services"),
    ("telco.*", "telecoms"),
    ("technology.*", "technology"),
    ("software.*", "technology"),
    ("media.*", "media_entertainment"),
    ("travel.*", "travel"),
    ("health.*", "healthcare"),
];

/// The engine's category for a research category code ([`CATEGORY_MAP`]);
/// an engine category given as it is stands for itself.
pub fn engine_category(code: &str) -> Option<&'static str> {
    let code = code.trim();
    if let Some(c) = ENGINE_CATEGORIES.iter().find(|c| **c == code) {
        return Some(c);
    }
    let vertical = code.split_once('.').map(|(v, _)| v)?;
    CATEGORY_MAP
        .iter()
        .find(|(k, _)| *k == code)
        .or_else(|| {
            CATEGORY_MAP
                .iter()
                .find(|(k, _)| k.strip_suffix(".*") == Some(vertical))
        })
        .map(|(_, c)| *c)
}

/// A decision row's `kind` (`engine/research_decisions.py` `KINDS`).
pub const DECISION_KINDS: &[&str] = &[
    "rejected_finding",
    "verified_finding",
    "resolved_contest",
    "open_contest",
    "edit",
    "verdict",
    "client_review",
];

/// The fields of a fact row (`research_facts.current`, `line`, `ref`,
/// `scope`) and of a decision row (`research_decisions._why_malformed`,
/// `line`), plus the two a fact row carries for the reader and the engine
/// keeps unread (`market`, `superseded_by` is read by `current`).
pub const FACT_ROW_FIELDS: &[&str] = &[
    "id",
    "version",
    "status",
    "superseded_by",
    "entity",
    "key",
    "value",
    "unit",
    "as_of",
    "scope",
    "sources",
    "market",
];
pub const DECISION_ROW_FIELDS: &[&str] = &[
    "id",
    "kind",
    "who",
    "role",
    "about",
    "statement",
    "reason",
    "as_of",
    "status",
];

/// At most this many verified findings, pins and decision rows (§10.2;
/// `research_decisions.MAX_DECISIONS`).
pub const MAX_FINDINGS: usize = 12;
pub const MAX_PINS: usize = 40;
pub const MAX_DECISIONS: usize = 40;

/// The lenses the engine's hero writers serve (loops 4, 5 and 6): every lens
/// but media and spend (§10.2).
pub const HERO_LENSES: &[&str] = &[
    "consumer_culture",
    "category_codes",
    "rhythm_moments",
    "brands_positioning",
    "effectiveness_evidence",
    "market_structure",
    "regulation_clearance",
];

/// The Research Tool's campaign fields, in profile order (§1.6, §10.3).
const CAMPAIGN_FIELDS: &[&str] = &[
    "id",
    "name",
    "brand",
    "client_org",
    "categories",
    "markets",
    "campaign_type",
    "problem",
    "objective",
    "audience_stated",
    "audience",
    "competitor_set",
    "success_measures",
    "budget_band",
    "in_market",
    "channels_mandated",
    "deliverables",
    "constraints",
    "ask_source",
];

/// Units that name a value's type rather than measure it: the engine would
/// print them after the value (`… The grown-up alcohol-free text`), so a row
/// leaves them out.
const TYPE_UNITS: &[&str] = &["text", "code", "date"];

const DATA_PATH: &str = "shared/data.yaml";
const CHAIN_PATH: &str = "agent/decision-chain.yaml";
const UPSTREAM_KEY: &str = "upstream";
const SPINOFF_AGENT: &str = "napkin-spinoff";

/// A person as the account names them (§3.1.2).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Person {
    pub name: String,
    pub role: Option<String>,
    /// An erased account reads as nobody in particular.
    pub erased: bool,
}

/// What the printer is given besides the file: `ctx.people` (§1.1).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Context {
    /// `human:<id>` → the account's person.
    pub people: BTreeMap<String, Person>,
}

/// Something left out of the payload, and why. `what` is an id, a member
/// path or a fixed word; never a value.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Skipped {
    pub what: String,
    pub why: String,
}

/// The printer's output.
#[derive(Debug, Clone, PartialEq)]
pub struct Upstream {
    /// The carried research's document id: the key of `data.upstream`.
    pub research: String,
    /// Exactly the engine's `upstream` object.
    pub payload: Json,
    /// What was left out, in the order it was met.
    pub skipped: Vec<Skipped>,
}

/// The research a document carries: the key of `data.upstream` whose frozen
/// copy holds `campaign` — the direct parent (`lineage.carried`) first, else
/// the first such key in code point order. `None` when it carries none.
pub fn carried_research(clan: &ClanFile) -> Option<String> {
    let data = read_yaml(clan, DATA_PATH).ok()?;
    let up = data.get(UPSTREAM_KEY)?.as_mapping()?;
    let is_research = |id: &str| {
        up.get(Yaml::from(id))
            .and_then(|c| c.get("campaign"))
            .is_some_and(Yaml::is_mapping)
    };
    if let Some(c) = clan.manifest().carried() {
        if is_research(&c.document_id) {
            return Some(c.document_id.clone());
        }
    }
    let mut ids: Vec<&str> = up.keys().filter_map(Yaml::as_str).collect();
    ids.sort_unstable();
    ids.into_iter().find(|id| is_research(id)).map(String::from)
}

/// The engine's `upstream` for the research `clan` carries
/// ([`carried_research`]), or `None` when it carries none.
pub fn upstream(clan: &ClanFile, ctx: &Context) -> Option<Upstream> {
    let research = carried_research(clan)?;
    upstream_of(clan, &research, ctx)
}

/// The engine's `upstream` for the carried research `research`
/// (`extract(B, false, {view: upstream:<R>})`, §9.5). `None` when `clan`
/// does not carry it.
pub fn upstream_of(clan: &ClanFile, research: &str, ctx: &Context) -> Option<Upstream> {
    let data = read_yaml(clan, DATA_PATH).ok()?;
    let frozen = data.get(UPSTREAM_KEY)?.get(research)?.clone();
    if !frozen.is_mapping() {
        return None;
    }
    let mut skipped = Vec::new();

    let chain = match clan.read_entry(CHAIN_PATH) {
        Ok(b) => DecisionChain::from_yaml(&b).unwrap_or_else(|_| {
            skipped.push(skip(
                CHAIN_PATH,
                "does not parse; no decisions and no marks were read",
            ));
            DecisionChain::default()
        }),
        Err(_) => DecisionChain::default(),
    };
    let facts = member(
        clan,
        "pinned-facts",
        "shared/facts.yaml",
        "facts",
        &mut skipped,
    );
    let findings = member(
        clan,
        "findings",
        "shared/findings.yaml",
        "findings",
        &mut skipped,
    );
    let sources = member(
        clan,
        "sources",
        "shared/sources.yaml",
        "sources",
        &mut skipped,
    );

    let direct = clan.manifest().carried().map(|c| c.document_id.as_str()) == Some(research);
    let own = Own {
        id: clan.document_id(),
        data: &data,
    };
    let doc = Doc::new(
        research, own, frozen, chain, facts, findings, sources, direct,
    );

    if doc.marks.closes_document() {
        skipped.push(skip(
            research,
            "the research is marked not for agents as a whole",
        ));
        return Some(Upstream {
            research: research.to_string(),
            payload: json!({ "competitors": [], "facts": [], "decisions": [] }),
            skipped,
        });
    }

    let hider = doc.hider();
    let mut out = Map::new();
    if let Some(brand) = doc.brand(&hider) {
        out.insert("brand".into(), Json::String(brand));
    }
    if let Some(category) = doc.category(&mut skipped) {
        out.insert("category".into(), Json::String(category));
    }
    out.insert("competitors".into(), json!(doc.competitors(&hider)));
    out.insert(
        "facts".into(),
        Json::Array(doc.fact_rows(&hider, &mut skipped)),
    );
    out.insert(
        "decisions".into(),
        Json::Array(doc.decision_rows(ctx, &hider, &mut skipped)),
    );
    Some(Upstream {
        research: research.to_string(),
        payload: Json::Object(out),
        skipped,
    })
}

// ── reading ─────────────────────────────────────────────────────────────────

fn skip(what: &str, why: &str) -> Skipped {
    Skipped {
        what: what.to_string(),
        why: why.to_string(),
    }
}

fn read_yaml(clan: &ClanFile, path: &str) -> crate::Result<Yaml> {
    let bytes = clan.read_entry(path)?;
    serde_yaml::from_slice(&bytes)
        .map_err(|e| crate::Error::OutputRejected(format!("{path} does not parse: {e}")))
}

/// A list member's entries, by its manifest role, else its default path; an
/// absent member is empty, and one that does not parse is empty and noted.
fn member(
    clan: &ClanFile,
    role: &str,
    default_path: &str,
    key: &str,
    skipped: &mut Vec<Skipped>,
) -> Vec<Yaml> {
    let path = clan
        .manifest()
        .files_with_role(role)
        .next()
        .map_or(default_path, |f| f.path.as_str())
        .to_string();
    if !clan.has_entry(&path) {
        return Vec::new();
    }
    match read_yaml(clan, &path) {
        Ok(v) => match v.get(key).or(Some(&v)).and_then(Yaml::as_sequence) {
            Some(items) => items.clone(),
            None if v.is_null() || v.get(key).is_some_and(Yaml::is_null) => Vec::new(),
            None => {
                skipped.push(skip(&path, "holds no list; nothing was read from it"));
                Vec::new()
            }
        },
        Err(_) => {
            skipped.push(skip(&path, "does not parse; nothing was read from it"));
            Vec::new()
        }
    }
}

fn s<'a>(v: &'a Yaml, key: &str) -> Option<&'a str> {
    v.get(key).and_then(Yaml::as_str)
}

/// A scalar as text, the way the `.clan` holds it: strings as they are,
/// numbers in the ECMAScript form RFC 8785 uses (§6.6), anything else as
/// JSON. `None` for null.
fn text_of(v: &Yaml) -> Option<String> {
    match v {
        Yaml::Null => None,
        Yaml::String(s) => Some(s.clone()),
        Yaml::Bool(b) => Some(b.to_string()),
        Yaml::Number(n) => Some(number_text(n)),
        Yaml::Tagged(t) => text_of(&t.value),
        other => serde_json::to_value(other)
            .ok()
            .and_then(|j| serde_json::to_string(&j).ok()),
    }
}

fn number_text(n: &serde_yaml::Number) -> String {
    if let Some(i) = n.as_i64() {
        return i.to_string();
    }
    if let Some(u) = n.as_u64() {
        return u.to_string();
    }
    let f = n.as_f64().unwrap_or(f64::NAN);
    float_text(f)
}

fn float_text(f: f64) -> String {
    if !f.is_finite() {
        return if f.is_nan() {
            "NaN".into()
        } else if f > 0.0 {
            "Infinity".into()
        } else {
            "-Infinity".into()
        };
    }
    if f.fract() == 0.0 && f.abs() < 1e15 {
        return format!("{}", f as i64);
    }
    serde_json::Number::from_f64(f)
        .map(|n| n.to_string())
        .unwrap_or_else(|| f.to_string())
}

fn number_of(v: &Yaml) -> Option<f64> {
    match v {
        Yaml::Number(n) => n.as_f64(),
        _ => None,
    }
}

/// `2026-09-22T16:09:12Z` → `2026-09-22`, in UTC; a date as stored. `None`
/// for a stamp that does not parse (§6.2).
fn date_of(stamp: &str) -> Option<String> {
    let t = stamp.trim();
    if let Ok(d) = chrono::DateTime::parse_from_rfc3339(t) {
        return Some(d.with_timezone(&chrono::Utc).format("%Y-%m-%d").to_string());
    }
    let naive = t.replacen(' ', "T", 1);
    for f in ["%Y-%m-%dT%H:%M:%S%.f", "%Y-%m-%dT%H:%M:%S"] {
        if let Ok(d) = chrono::NaiveDateTime::parse_from_str(&naive, f) {
            return Some(d.format("%Y-%m-%d").to_string());
        }
    }
    chrono::NaiveDate::parse_from_str(t, "%Y-%m-%d")
        .ok()
        .map(|d| d.format("%Y-%m-%d").to_string())
}

/// `<doc>#<path>` → (`Some(doc)`, path); a bare string is a document id.
fn split_address(target: &str) -> (Option<&str>, Option<&str>) {
    match target.split_once('#') {
        Some((doc, path)) => (Some(doc).filter(|d| !d.is_empty()), Some(path)),
        None => (Some(target), None),
    }
}

/// `findings[fi_x]` → `("findings", "fi_x")`.
fn bracket<'a>(path: &'a str, member: &str) -> Option<&'a str> {
    path.strip_prefix(member)?
        .strip_prefix('[')?
        .strip_suffix(']')
        .filter(|id| !id.contains(']'))
}

// ── marks ───────────────────────────────────────────────────────────────────

/// What a mark governs, normalised (§5.2 "What a mark covers").
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
enum Governs {
    /// A member entry or a decision, by id, whatever the document prefix:
    /// ids are never remapped.
    Entry(&'static str, String),
    /// A data path of the research's frozen copy.
    Path(String),
    /// A data path of the document itself (a brief's own field), whose
    /// values are matched in every row like any hidden value (§5.4).
    Own(String),
    /// The research as a whole.
    Document,
}

struct Mark {
    governs: Governs,
    model: bool,
}

/// Every classify decision in the chain, own or carried, not superseded,
/// newest first: for an address, the first that covers it decides.
struct Marks(Vec<Mark>);

impl Marks {
    fn of(chain: &DecisionChain, research: &str, own: &str) -> Self {
        let mut out = Vec::new();
        for d in &chain.decisions {
            if d.kind.as_deref() != Some("classify") || d.superseded_by.is_some() {
                continue;
            }
            // A flag the mark does not set reads as false (§5.2).
            let model = d.licence.as_ref().and_then(|l| l.model).unwrap_or(false);
            for t in &d.targets {
                if let Some(governs) = governs(t, research, own) {
                    out.push(Mark { governs, model });
                }
            }
        }
        Marks(out)
    }

    fn closes_document(&self) -> bool {
        self.0
            .iter()
            .find(|m| m.governs == Governs::Document)
            .is_some_and(|m| !m.model)
    }

    /// Whether the newest mark covering `what` says `model: false`.
    fn hidden(&self, what: &Governs) -> bool {
        self.0
            .iter()
            .find(|m| covers(&m.governs, what))
            .is_some_and(|m| !m.model)
    }

    fn entry_hidden(&self, member: &'static str, id: &str) -> bool {
        self.hidden(&Governs::Entry(member, id.to_string()))
    }

    fn path_hidden(&self, path: &str) -> bool {
        self.hidden(&Governs::Path(path.to_string()))
    }

    /// Every record a `model: false` mark still governs, once each.
    fn hidden_records(&self) -> BTreeSet<Governs> {
        self.0
            .iter()
            .filter(|m| !m.model && self.hidden(&m.governs))
            .map(|m| m.governs.clone())
            .collect()
    }
}

fn governs(target: &str, research: &str, own: &str) -> Option<Governs> {
    let (doc, path) = split_address(target);
    let Some(path) = path else {
        return (doc == Some(research)).then_some(Governs::Document);
    };
    for (member, key) in [
        ("facts", "facts"),
        ("findings", "findings"),
        ("sources", "sources"),
        ("decisions", "decisions"),
    ] {
        if let Some(id) = bracket(path, key) {
            return Some(Governs::Entry(member, id.to_string()));
        }
    }
    for (prefix, member) in [
        ("projection.pins.", "facts"),
        ("projection.findings.", "findings"),
        ("projection.sources.", "sources"),
    ] {
        if let Some(id) = path.strip_prefix(prefix) {
            let id = id.split(['.', '[']).next().unwrap_or_default();
            return Some(Governs::Entry(member, id.to_string()));
        }
    }
    // A data path of the document itself: its own fields (a brief's client).
    if doc == Some(own) && own != research {
        return Some(Governs::Own(path.to_string()));
    }
    // A data path holds for the frozen copy only when it names the research.
    if doc != Some(research) {
        return None;
    }
    if let Some(rest) = path.strip_prefix("campaign.") {
        let field = rest.split(['.', '[']).next().unwrap_or_default();
        return Some(Governs::Path(format!("campaign.{field}")));
    }
    Some(Governs::Path(path.to_string()))
}

/// A mark on `mark` covers `what`: the same entry, or `what` at or under the
/// marked path, by whole segments.
fn covers(mark: &Governs, what: &Governs) -> bool {
    match (mark, what) {
        (Governs::Document, _) => true,
        (Governs::Entry(a, x), Governs::Entry(b, y)) => a == b && x == y,
        (Governs::Path(p), Governs::Path(q)) | (Governs::Own(p), Governs::Own(q)) => {
            q == p
                || q.strip_prefix(p.as_str())
                    .is_some_and(|rest| rest.starts_with('.') || rest.starts_with('['))
        }
        _ => false,
    }
}

// ── exact occurrences (§5.4) ────────────────────────────────────────────────

/// The hidden values, and where their exact occurrences are replaced.
#[derive(Default)]
struct Hider {
    /// Normalised prose of at least 4 scalars.
    prose: Vec<Vec<char>>,
    /// A number with its unit.
    numbers: Vec<(f64, Option<String>)>,
}

impl Hider {
    fn add(&mut self, v: &Yaml, unit: Option<&str>) {
        match v {
            Yaml::Number(_) => {
                if let Some(n) = number_of(v) {
                    self.numbers.push((n, unit.map(String::from)));
                }
            }
            Yaml::String(s) => {
                let t = s.trim();
                if let Ok(n) = t.parse::<f64>() {
                    if t.chars()
                        .all(|c| c.is_ascii_digit() || c == '.' || c == '-')
                    {
                        self.numbers.push((n, unit.map(String::from)));
                        return;
                    }
                }
                let norm: Vec<char> = normalise(t).into_iter().map(|(c, _)| c).collect();
                if norm.len() >= 4 {
                    self.prose.push(norm);
                }
            }
            Yaml::Sequence(items) => items.iter().for_each(|i| self.add(i, unit)),
            Yaml::Mapping(m) => {
                let unit = m.get("unit").and_then(Yaml::as_str).or(unit);
                for (k, item) in m {
                    // Ids and references are shared vocabulary, never a value.
                    let key = k.as_str().unwrap_or_default();
                    let is_id = matches!(key, "unit" | "id" | "ref")
                        || key.ends_with("_id")
                        || key.ends_with("_ids");
                    if !is_id {
                        self.add(item, unit);
                    }
                }
            }
            Yaml::Tagged(t) => self.add(&t.value, unit),
            _ => {}
        }
    }

    fn is_empty(&self) -> bool {
        self.prose.is_empty() && self.numbers.is_empty()
    }

    /// `text` with every exact occurrence of a hidden value replaced.
    fn redact(&self, text: &str) -> String {
        if self.is_empty() || text.is_empty() {
            return text.to_string();
        }
        let mut spans: Vec<(usize, usize)> = Vec::new();
        let norm = normalise(text);
        let chars: Vec<char> = norm.iter().map(|(c, _)| *c).collect();
        for needle in &self.prose {
            let mut i = 0;
            while i + needle.len() <= chars.len() {
                if chars[i..i + needle.len()] == needle[..]
                    && (i == 0 || !is_word(chars[i - 1]))
                    && chars.get(i + needle.len()).map_or(true, |c| !is_word(*c))
                {
                    spans.push((norm[i].1 .0, norm[i + needle.len() - 1].1 .1));
                    i += needle.len();
                } else {
                    i += 1;
                }
            }
        }
        for tok in number_tokens(text) {
            if self
                .numbers
                .iter()
                .any(|(n, unit)| number_matches(*n, unit.as_deref(), &tok))
            {
                spans.push((tok.start, tok.end));
            }
        }
        if spans.is_empty() {
            return text.to_string();
        }
        spans.sort_unstable();
        let mut merged: Vec<(usize, usize)> = Vec::new();
        for (a, b) in spans {
            match merged.last_mut() {
                Some(last) if a <= last.1 => last.1 = last.1.max(b),
                _ => merged.push((a, b)),
            }
        }
        let mut out = String::with_capacity(text.len());
        let mut at = 0;
        for (a, b) in merged {
            out.push_str(&text[at..a]);
            out.push_str(MARKED);
            at = b;
        }
        out.push_str(&text[at..]);
        out
    }
}

fn is_word(c: char) -> bool {
    c.is_alphanumeric() || c == '_'
}

/// Case-folded characters, each with the byte span of the source it came
/// from; a whitespace run is one space, and the text is trimmed.
fn normalise(text: &str) -> Vec<(char, (usize, usize))> {
    let mut out: Vec<(char, (usize, usize))> = Vec::new();
    for (i, c) in text.char_indices() {
        let end = i + c.len_utf8();
        if c.is_whitespace() {
            match out.last_mut() {
                Some((' ', span)) => span.1 = end,
                _ => out.push((' ', (i, end))),
            }
            continue;
        }
        for l in c.to_lowercase() {
            out.push((l, (i, end)));
        }
    }
    while out.first().is_some_and(|(c, _)| *c == ' ') {
        out.remove(0);
    }
    while out.last().is_some_and(|(c, _)| *c == ' ') {
        out.pop();
    }
    out
}

/// A numeric token in running text (§5.4 "Figures").
#[derive(Debug)]
struct NumberToken {
    start: usize,
    end: usize,
    /// The number as written, its multiplier applied.
    value: f64,
    currency: bool,
    /// `k`, `m`, `bn` or their words.
    scaled: bool,
    percent: bool,
    /// Significant decimal digits as written, and the integer part's digits.
    decimals: usize,
    int_digits: usize,
}

fn number_tokens(text: &str) -> Vec<NumberToken> {
    let b = text.as_bytes();
    let mut out = Vec::new();
    let mut i = 0;
    while i < b.len() {
        if !b[i].is_ascii_digit() {
            i += 1;
            continue;
        }
        // A digit inside an id or a word, or after a decimal point, is not
        // the start of a figure.
        let prev = text[..i].chars().next_back();
        if prev.is_some_and(|c| is_word(c) || c == '.' || c == ',') {
            while i < b.len() && (b[i].is_ascii_digit()) {
                i += 1;
            }
            continue;
        }
        let digits_start = i;
        let mut j = i;
        let mut int_part = String::new();
        while j < b.len()
            && (b[j].is_ascii_digit()
                || (b[j] == b',' && j + 1 < b.len() && b[j + 1].is_ascii_digit()))
        {
            if b[j] != b',' {
                int_part.push(b[j] as char);
            }
            j += 1;
        }
        let mut frac = String::new();
        if j + 1 < b.len() && b[j] == b'.' && b[j + 1].is_ascii_digit() {
            j += 1;
            while j < b.len() && b[j].is_ascii_digit() {
                frac.push(b[j] as char);
                j += 1;
            }
        }
        // Inside an id (`01JA0`) or a date (`2026-09-22`), not a figure.
        let next = text[j..].chars().next();
        if next.is_some_and(|c| {
            c == '_' || c == '-' && text[j + 1..].starts_with(|c: char| c.is_ascii_digit())
        }) {
            i = j;
            continue;
        }
        let mut value: f64 = format!("{int_part}.{}", if frac.is_empty() { "0" } else { &frac })
            .parse()
            .unwrap_or(f64::NAN);
        let mut end = j;
        let mut scaled = false;
        for (word, mult) in [
            (" billion", 1e9),
            (" million", 1e6),
            (" thousand", 1e3),
            ("billion", 1e9),
            ("million", 1e6),
            ("thousand", 1e3),
            ("bn", 1e9),
            ("k", 1e3),
            ("m", 1e6),
        ] {
            if starts_ci(&text[end..], word)
                && text[end + word.len()..]
                    .chars()
                    .next()
                    .map_or(true, |c| !is_word(c))
            {
                value *= mult;
                end += word.len();
                scaled = true;
                break;
            }
        }
        let mut percent = false;
        for word in ["% abv", "%", " percent"] {
            if starts_ci(&text[end..], word) {
                end += word.len();
                percent = true;
                break;
            }
        }
        if !percent && text[end..].chars().next().is_some_and(is_word) {
            i = j;
            continue;
        }
        let before = &text[..digits_start];
        let mut start = digits_start;
        let mut currency = false;
        for sign in ["€", "£", "$", "EUR ", "GBP ", "USD ", "EUR", "GBP", "USD"] {
            if before.ends_with(sign) {
                start -= sign.len();
                currency = true;
                break;
            }
        }
        let decimals = frac.trim_end_matches('0').len();
        out.push(NumberToken {
            start,
            end,
            value,
            currency,
            scaled,
            percent,
            decimals,
            int_digits: int_part.trim_start_matches('0').len(),
        });
        i = end.max(j);
    }
    out
}

/// `text` starts with the ASCII `word`, ignoring ASCII case; never splits a
/// character.
fn starts_ci(text: &str, word: &str) -> bool {
    text.len() >= word.len()
        && text.is_char_boundary(word.len())
        && text.as_bytes()[..word.len()].eq_ignore_ascii_case(word.as_bytes())
}

fn same(a: f64, b: f64) -> bool {
    (a - b).abs() <= 1e-9 * a.abs().max(b.abs()).max(1.0)
}

/// Whether a written token is the marked number `n` in one of its unit's
/// normal forms. A bare number matches only when it is specific: three or
/// more integer digits, or two or more significant decimals.
fn number_matches(n: f64, unit: Option<&str>, t: &NumberToken) -> bool {
    let specific = t.int_digits >= 3 || t.decimals >= 2;
    let bare = !t.currency && !t.scaled && !t.percent;
    match unit.unwrap_or_default() {
        "proportion" => {
            (t.percent && same(t.value, n * 100.0)) || (bare && specific && same(t.value, n))
        }
        "percent" | "percent_abv" => {
            (t.percent && same(t.value, n)) || (bare && specific && same(t.value, n))
        }
        _ => {
            if !same(t.value, n) || t.percent {
                return false;
            }
            t.currency || t.scaled || specific
        }
    }
}

// ── the document as the printer reads it ────────────────────────────────────

/// The document the printer runs on: its id and its own data.
struct Own<'a> {
    id: &'a str,
    data: &'a Yaml,
}

struct Doc<'a> {
    research: &'a str,
    this: Own<'a>,
    frozen: Yaml,
    chain: DecisionChain,
    /// Stored indexes of the decisions the research made (below the marker).
    carried: Vec<usize>,
    /// Stored indexes of this document's own decisions (above the marker).
    own: Vec<usize>,
    facts: BTreeMap<String, Yaml>,
    findings: Vec<Yaml>,
    sources: BTreeMap<String, Yaml>,
    marks: Marks,
}

impl<'a> Doc<'a> {
    #[allow(clippy::too_many_arguments)]
    fn new(
        research: &'a str,
        this: Own<'a>,
        frozen: Yaml,
        chain: DecisionChain,
        facts: Vec<Yaml>,
        findings: Vec<Yaml>,
        sources: Vec<Yaml>,
        direct: bool,
    ) -> Self {
        // Position decides what was carried (§9.2): the research's own
        // decisions sit below the newest spin-off marker (for a hoisted
        // research, below the next one down) and above the marker after it.
        let markers: Vec<usize> = chain
            .decisions
            .iter()
            .enumerate()
            .filter(|(_, d)| d.agent == SPINOFF_AGENT && d.action.starts_with("spin off "))
            .map(|(i, _)| i)
            .collect();
        let n = chain.decisions.len();
        let (own_end, from, to) = match (direct, markers.as_slice()) {
            (_, []) => (n, n, n),
            (true, [m, rest @ ..]) => (*m, m + 1, rest.first().copied().unwrap_or(n)),
            (false, [m]) => (*m, m + 1, n),
            (false, [m, k, rest @ ..]) => (*m, k + 1, rest.first().copied().unwrap_or(n)),
        };
        let own = (0..own_end).collect();
        let carried = (from..to).collect();
        let marks = Marks::of(&chain, research, this.id);
        let mut by_id = BTreeMap::new();
        for f in facts {
            if let Some(id) = s(&f, "id").map(String::from) {
                by_id.entry(id).or_insert(f);
            }
        }
        let sources = sources
            .into_iter()
            .filter_map(|v| Some((s(&v, "id")?.to_string(), v)))
            .collect();
        Doc {
            research,
            this,
            frozen,
            chain,
            carried,
            own,
            facts: by_id,
            findings,
            sources,
            marks,
        }
    }

    fn campaign(&self, field: &str) -> Option<&Yaml> {
        self.frozen.get("campaign")?.get(field)
    }

    fn field_value(&self, field: &str) -> Option<&Yaml> {
        self.campaign(field)?.get("value")
    }

    fn contests(&self) -> impl Iterator<Item = &Yaml> {
        self.frozen
            .get("selection")
            .and_then(|s| s.get("contested"))
            .and_then(Yaml::as_sequence)
            .into_iter()
            .flatten()
    }

    fn finding(&self, id: &str) -> Option<&Yaml> {
        self.findings.iter().find(|f| s(f, "id") == Some(id))
    }

    /// Every value a `model: false` mark hides (§5.2 "The marked value").
    fn hider(&self) -> Hider {
        let mut h = Hider::default();
        for g in self.marks.hidden_records() {
            match &g {
                Governs::Entry("facts", id) => {
                    if let Some(f) = self.facts.get(id) {
                        if let Some(v) = f.get("value") {
                            h.add(v, s(f, "unit"));
                        }
                    }
                }
                Governs::Entry("findings", id) => {
                    if let Some(st) = self.finding(id).and_then(|f| f.get("statement")) {
                        h.add(st, None);
                    }
                }
                Governs::Entry("decisions", id) => {
                    if let Some(d) = self
                        .chain
                        .decisions
                        .iter()
                        .find(|d| d.id.as_deref() == Some(id))
                    {
                        h.add(&Yaml::String(d.rationale.clone()), None);
                    }
                }
                Governs::Entry(_, _) => {}
                Governs::Path(p) => {
                    if let Some(ct) = bracket(p, "selection.contested") {
                        if let Some(c) = self.contests().find(|c| s(c, "id") == Some(ct)) {
                            for v in c
                                .get("values")
                                .and_then(Yaml::as_sequence)
                                .into_iter()
                                .flatten()
                            {
                                if let Some(x) = v.get("value") {
                                    h.add(x, s(v, "unit"));
                                }
                            }
                        }
                    } else if let Some(field) = p.strip_prefix("campaign.") {
                        if let Some(v) = self.field_value(field) {
                            h.add(v, None);
                        }
                    } else if let Some(v) = get_path(&self.frozen, p) {
                        h.add(v, None);
                    }
                }
                Governs::Own(p) => {
                    // A field stored as `{value, …}` hides its value, not its
                    // provenance words.
                    if let Some(v) = get_path(self.this.data, p) {
                        h.add(v.get("value").unwrap_or(v), s(v, "unit"));
                    }
                }
                Governs::Document => {}
            }
        }
        h
    }

    // ── the three facets ────────────────────────────────────────────────

    /// Left out when its field is marked, or its name holds a hidden value
    /// (a keyword is exact: a partly replaced one would match nothing).
    fn brand(&self, hider: &Hider) -> Option<String> {
        if self.marks.path_hidden("campaign.brand") {
            return None;
        }
        let v = self.field_value("brand")?;
        v.get("name")
            .and_then(Yaml::as_str)
            .or_else(|| v.as_str())
            .map(str::trim)
            .filter(|n| !n.is_empty() && hider.redact(n) == *n)
            .map(String::from)
    }

    /// The first of the research's categories that maps to one of the
    /// engine's ([`engine_category`]). None maps: left out, and said so.
    fn category(&self, skipped: &mut Vec<Skipped>) -> Option<String> {
        if self.marks.path_hidden("campaign.categories") {
            return None;
        }
        let codes: Vec<&str> = self
            .field_value("categories")?
            .as_sequence()?
            .iter()
            .filter_map(Yaml::as_str)
            .collect();
        let got = codes.iter().find_map(|c| engine_category(c));
        if got.is_none() && !codes.is_empty() {
            skipped.push(skip(
                "campaign.categories",
                "no category maps to one of the engine's; the engine chooses its own",
            ));
        }
        got.map(String::from)
    }

    /// The names, less any that holds a hidden value (as for the brand).
    fn competitors(&self, hider: &Hider) -> Vec<String> {
        if self.marks.path_hidden("campaign.competitor_set") {
            return Vec::new();
        }
        let mut out: Vec<String> = Vec::new();
        for c in self
            .field_value("competitor_set")
            .and_then(Yaml::as_sequence)
            .into_iter()
            .flatten()
        {
            let name = c.get("name").and_then(Yaml::as_str).or_else(|| c.as_str());
            if let Some(n) = name
                .map(str::trim)
                .filter(|n| !n.is_empty() && hider.redact(n) == *n)
            {
                if !out.iter().any(|o| o == n) {
                    out.push(n.to_string());
                }
            }
        }
        out
    }

    // ── fact rows (§10.2, §11.3) ────────────────────────────────────────

    /// Fact ids no row may carry: values of open contests, the losing
    /// values of resolved ones, and facts a person left out.
    fn not_current(&self) -> BTreeSet<String> {
        let mut out = BTreeSet::new();
        for c in self.contests() {
            let id = s(c, "id").unwrap_or_default();
            let chosen = match s(c, "status") {
                Some("open") => self.resolved_here(id).map(|(_, f)| f),
                _ => s(c, "chosen").map(String::from),
            };
            for v in c
                .get("values")
                .and_then(Yaml::as_sequence)
                .into_iter()
                .flatten()
            {
                if let Some(f) = s(v, "fact_id") {
                    if chosen.as_deref() != Some(f) {
                        out.insert(f.to_string());
                    }
                }
            }
        }
        for e in self
            .frozen
            .get("selection")
            .and_then(|s| s.get("excluded"))
            .and_then(Yaml::as_sequence)
            .into_iter()
            .flatten()
        {
            if let Some(f) = s(e, "fact_id") {
                out.insert(f.to_string());
            }
        }
        out
    }

    /// A resolve in this document, not superseded, of the carried contest
    /// `ct`: (its stored index, the chosen fact).
    fn resolved_here(&self, ct: &str) -> Option<(usize, String)> {
        let address = format!("{}#selection.contested[{ct}]", self.research);
        self.own.iter().find_map(|&i| {
            let d = &self.chain.decisions[i];
            (d.kind.as_deref() == Some("resolve")
                && d.superseded_by.is_none()
                && d.targets.contains(&address))
            .then(|| (i, d.cites.first().cloned().unwrap_or_default()))
        })
    }

    fn fact_rows(&self, hider: &Hider, skipped: &mut Vec<Skipped>) -> Vec<Json> {
        let barred = self.not_current();
        let mut rows = Vec::new();
        let mut seen: BTreeSet<String> = BTreeSet::new();

        // 1. Verified findings of the hero lenses: confidence, then newest.
        let rank = |c: Option<&str>| match c {
            Some("high") => 0,
            Some("medium") => 1,
            Some("low") => 2,
            _ => 3,
        };
        let mut chosen: Vec<&Yaml> = self
            .findings
            .iter()
            .filter(|f| s(f, "status") == Some("verified"))
            .filter(|f| s(f, "lens").is_some_and(|l| HERO_LENSES.contains(&l)))
            .collect();
        chosen.sort_by(|a, b| {
            rank(s(a, "confidence"))
                .cmp(&rank(s(b, "confidence")))
                .then_with(|| s(b, "derived_at").cmp(&s(a, "derived_at")))
        });
        chosen.truncate(MAX_FINDINGS);
        for fi in &chosen {
            match self.finding_row(fi, hider) {
                Some(row) => {
                    let id = row["id"].as_str().unwrap_or_default().to_string();
                    if seen.insert(id) {
                        rows.push(row);
                    }
                }
                None => skipped.push(skip(
                    s(fi, "id").unwrap_or("a finding"),
                    "a verified finding with no statement",
                )),
            }
        }

        // 2. The pins the chosen findings cite, then those the campaign
        //    fields rest on, then the chosen values of resolved contests.
        let mut wanted: Vec<String> = Vec::new();
        for fi in &chosen {
            for c in fi
                .get("cites")
                .and_then(Yaml::as_sequence)
                .into_iter()
                .flatten()
            {
                if let Some(id) = c.as_str().filter(|c| c.starts_with("f_")) {
                    wanted.push(id.to_string());
                }
            }
        }
        let mut fields: Vec<&str> = CAMPAIGN_FIELDS.to_vec();
        let mut others: Vec<&str> = self
            .frozen
            .get("campaign")
            .and_then(Yaml::as_mapping)
            .into_iter()
            .flatten()
            .filter_map(|(k, _)| k.as_str())
            .filter(|k| !CAMPAIGN_FIELDS.contains(k))
            .collect();
        others.sort_unstable();
        fields.extend(others);
        for field in fields {
            for c in self
                .campaign(field)
                .and_then(|e| e.get("fact_ids"))
                .and_then(Yaml::as_sequence)
                .into_iter()
                .flatten()
            {
                if let Some(id) = c.as_str() {
                    wanted.push(id.to_string());
                }
            }
        }
        for c in self.contests() {
            let chosen = match s(c, "status") {
                Some("open") => self
                    .resolved_here(s(c, "id").unwrap_or_default())
                    .map(|(_, f)| f),
                _ => s(c, "chosen").map(String::from),
            };
            wanted.extend(chosen);
        }

        let mut pins = 0;
        for id in wanted {
            if pins >= MAX_PINS {
                break;
            }
            let Some(id) = self.current_pin(&id, &barred) else {
                continue;
            };
            if seen.contains(&id) {
                continue;
            }
            let Some(f) = self.facts.get(&id) else {
                continue;
            };
            match self.pin_row(f, hider) {
                Some(row) => {
                    seen.insert(id);
                    rows.push(row);
                    pins += 1;
                }
                None => skipped.push(skip(&id, "a pin with no value")),
            }
        }
        rows
    }

    /// The pin a row carries for `id`: itself, or the pin that replaced it;
    /// `None` when neither is held or it is not current.
    fn current_pin(&self, id: &str, barred: &BTreeSet<String>) -> Option<String> {
        let mut id = id.to_string();
        for _ in 0..8 {
            let f = self.facts.get(&id)?;
            let by = f
                .get("replaced_by")
                .map(|r| r.get("fact_id").unwrap_or(r))
                .and_then(Yaml::as_str);
            match by {
                Some(next) => id = next.to_string(),
                None => break,
            }
        }
        (!barred.contains(&id) && self.facts.contains_key(&id)).then_some(id)
    }

    fn pin_row(&self, f: &Yaml, hider: &Hider) -> Option<Json> {
        let id = s(f, "id")?;
        let raw = text_of(f.get("value")?).filter(|v| !v.trim().is_empty())?;
        let value = if self.marks.entry_hidden("facts", id) {
            MARKED.to_string()
        } else {
            hider.redact(&raw)
        };
        let mut row = Map::new();
        row.insert("id".into(), json!(id));
        if let Some(v) = origin_version(f) {
            row.insert("version".into(), json!(v));
        }
        let status = match s(f, "status") {
            None | Some("active" | "contested" | "current") => "current",
            Some(other) => other,
        };
        row.insert("status".into(), json!(status));
        if let Some(cur) = f.get("stale").and_then(|st| s(st, "current_fact_id")) {
            row.insert("superseded_by".into(), json!(cur));
        }
        insert_identity(&mut row, f);
        row.insert("value".into(), json!(value));
        if let Some(u) = s(f, "unit").filter(|u| !TYPE_UNITS.contains(u)) {
            row.insert("unit".into(), json!(u));
        }
        if let Some(a) = f.get("as_of").and_then(text_of) {
            row.insert("as_of".into(), json!(a));
        }
        if let Some(scope) = layer_of(f) {
            row.insert("scope".into(), json!(scope));
        }
        let sources = self.source_refs(f, hider);
        if !sources.is_empty() {
            row.insert("sources".into(), Json::Array(sources));
        }
        Some(Json::Object(row))
    }

    /// A verified finding, as the fact it became in the layer (§11.3).
    fn finding_row(&self, fi: &Yaml, hider: &Hider) -> Option<Json> {
        let fid = s(fi, "id")?;
        let statement = s(fi, "statement").filter(|t| !t.trim().is_empty())?;
        let fact_id = fi
            .get("verification")
            .and_then(|v| s(v, "fact_id"))
            .unwrap_or(fid);
        let fact = self.facts.get(fact_id);
        let hidden = self.marks.entry_hidden("findings", fid)
            || fact.is_some() && self.marks.entry_hidden("facts", fact_id);
        let mut row = Map::new();
        row.insert("id".into(), json!(fact_id));
        if let Some(v) = fact.and_then(origin_version) {
            row.insert("version".into(), json!(v));
        }
        row.insert("status".into(), json!("current"));
        match fact {
            Some(f) if s(f, "entity").is_some() => insert_identity(&mut row, f),
            _ => {
                let entity = fi
                    .get("cites")
                    .and_then(Yaml::as_sequence)
                    .into_iter()
                    .flatten()
                    .filter_map(Yaml::as_str)
                    .find_map(|c| self.facts.get(c).and_then(|f| s(f, "entity")));
                if let Some(e) = entity {
                    row.insert("entity".into(), json!(e));
                }
                let lens = s(fi, "lens").unwrap_or("research");
                row.insert("key".into(), json!(format!("finding.{lens}")));
            }
        }
        row.insert(
            "value".into(),
            json!(if hidden {
                MARKED.to_string()
            } else {
                hider.redact(statement)
            }),
        );
        row.insert(
            "scope".into(),
            json!(fact.and_then(layer_of).unwrap_or_else(|| "research".into())),
        );
        if let Some(f) = fact {
            let sources = self.source_refs(f, hider);
            if !sources.is_empty() {
                row.insert("sources".into(), Json::Array(sources));
            }
        }
        Some(Json::Object(row))
    }

    /// `{id, title, uri}` per `src_` id, as the sources member stores them;
    /// `{id}` alone when it holds no record, or the record is a person's.
    /// A title is matched like any other slot (§5.4).
    fn source_refs(&self, f: &Yaml, hider: &Hider) -> Vec<Json> {
        let mut out = Vec::new();
        let mut seen = BTreeSet::new();
        for id in f
            .get("sources")
            .and_then(Yaml::as_sequence)
            .into_iter()
            .flatten()
            .filter_map(Yaml::as_str)
            .filter(|id| id.starts_with("src_"))
        {
            if !seen.insert(id) {
                continue;
            }
            let mut r = Map::new();
            r.insert("id".into(), json!(id));
            if let Some(rec) = self.sources.get(id) {
                let uri = s(rec, "uri").unwrap_or_default();
                let personal =
                    uri.starts_with("human:") || s(rec, "tier") == Some("reviewer-verified");
                if !personal && !self.marks.entry_hidden("sources", id) {
                    if let Some(t) = s(rec, "title").filter(|t| !t.trim().is_empty()) {
                        r.insert("title".into(), json!(hider.redact(t)));
                    }
                    if !uri.is_empty() {
                        r.insert("uri".into(), json!(uri));
                    }
                }
            }
            out.push(Json::Object(r));
        }
        out
    }

    // ── decision rows (ADR 0015) ────────────────────────────────────────

    fn decision_rows(&self, ctx: &Context, hider: &Hider, skipped: &mut Vec<Skipped>) -> Vec<Json> {
        let mut rows: Vec<(u8, Json)> = Vec::new();
        let names = Names { ctx };

        // The research's own decisions, and this document's that settle
        // what it carried; oldest first (§6.3, write order).
        let mut picked: Vec<usize> = self.carried.clone();
        picked.extend(
            self.own
                .iter()
                .copied()
                .filter(|&i| self.settles_carried(&self.chain.decisions[i])),
        );
        picked.sort_unstable_by(|a, b| b.cmp(a));
        for i in picked {
            let d = &self.chain.decisions[i];
            if d.superseded_by.is_some() {
                continue;
            }
            if let Some((group, row)) = self.row_of(d, &names, hider) {
                match d.id.as_deref().filter(|id| !id.is_empty()) {
                    Some(_) => rows.push((group, row)),
                    None => skipped.push(skip(&d.action, "a decision with no id cannot be cited")),
                }
            }
        }

        // Contests still open: nobody has chosen.
        for c in self.contests() {
            let ct = s(c, "id").unwrap_or_default();
            if s(c, "status") != Some("open") || self.resolved_here(ct).is_some() {
                continue;
            }
            rows.push((4, self.open_contest_row(c, hider)));
        }

        rows.sort_by_key(|(g, _)| *g);
        // At most MAX_DECISIONS: groups 5 onwards are cut from the end,
        // newest first; rejections, corrections and contests never.
        while rows.len() > MAX_DECISIONS {
            let Some(pos) = rows.iter().rposition(|(g, _)| *g >= 5) else {
                break;
            };
            let (_, row) = rows.remove(pos);
            skipped.push(skip(
                row["id"].as_str().unwrap_or("a decision"),
                "over the engine's cap of 40 decisions",
            ));
        }
        rows.into_iter().map(|(_, r)| r).collect()
    }

    /// A decision of this document that answers something it carried: a
    /// resolve, verification, rejection, verdict or dismissal whose target is
    /// in the research, or a carried finding.
    fn settles_carried(&self, d: &Decision) -> bool {
        if !is_person(d) {
            return false;
        }
        let prefix = format!("{}#", self.research);
        d.targets.iter().any(|t| {
            t.starts_with(&prefix)
                || split_address(t)
                    .1
                    .and_then(|p| bracket(p, "findings"))
                    .is_some_and(|fi| self.finding(fi).is_some())
        })
    }

    fn open_contest_row(&self, c: &Yaml, hider: &Hider) -> Json {
        let ct = s(c, "id").unwrap_or_default();
        let hidden = self
            .marks
            .path_hidden(&format!("selection.contested[{ct}]"));
        let values: Vec<String> = c
            .get("values")
            .and_then(Yaml::as_sequence)
            .into_iter()
            .flatten()
            .map(|v| {
                let shown = if hidden {
                    MARKED.to_string()
                } else {
                    value_display(v)
                };
                let mut t = shown;
                if let Some(f) = s(v, "fact_id") {
                    t.push_str(&format!(" (fact {f}"));
                    if let Some(from) = s(v, "from") {
                        t.push_str(&format!(", from {from}"));
                    }
                    t.push(')');
                }
                t
            })
            .collect();
        let what = contest_subject(s(c, "key").unwrap_or(ct));
        let opener = s(c, "opened_by");
        let as_of = opener
            .and_then(|o| {
                self.chain
                    .decisions
                    .iter()
                    .find(|d| d.id.as_deref() == Some(o))
            })
            .and_then(|d| date_of(&d.timestamp));
        let statement = format!(
            "has settled the contest on {what}: the research holds {}; do not state either as settled",
            join_and(&values)
        );
        let mut row = row_base(
            opener.unwrap_or(ct),
            "open_contest",
            "No one on the team",
            "person",
        );
        row.insert("about".into(), json!(hider.redact(&what)));
        row.insert("statement".into(), json!(hider.redact(&statement)));
        row.insert("reason".into(), Json::Null);
        row.insert("as_of".into(), json!(as_of));
        Json::Object(row)
    }

    /// One decision's row and its group (§10.3 WHAT PEOPLE DECIDED): 1
    /// rejections, 2 corrections, 3 resolved contests, 4 open contests, 5
    /// changes, 6 acceptances, 7 client answers. `None` for a decision no
    /// row holds.
    fn row_of(&self, d: &Decision, names: &Names, hider: &Hider) -> Option<(u8, Json)> {
        let id = d.id.as_deref().unwrap_or_default();
        let as_of = date_of(&d.timestamp);
        if d.kind.as_deref() == Some("client_review") {
            return self.client_row(d, as_of, hider);
        }
        if !is_person(d) {
            return None;
        }
        let who = names.of(&actor_of(d));
        let reason = self.reason_of(d, names);
        let target = d.targets.first().map(String::as_str).unwrap_or_default();
        let path = split_address(target).1.unwrap_or_default();
        let finding = bracket(path, "findings").and_then(|fi| self.finding(fi).map(|f| (fi, f)));
        let action = d.action.as_str();
        let kind = d.kind.as_deref().unwrap_or_default();

        let (group, row_kind, statement, about, words) = if let Some((fi, f)) =
            finding.filter(|_| kind == "verify" || action == "verify_finding")
        {
            let st = self.finding_quote(fi, f, hider);
            let now = f
                .get("verification")
                .and_then(|v| s(v, "fact_id"))
                .map(|x| format!(", now fact {x}"))
                .unwrap_or_default();
            (
                6,
                "verified_finding",
                format!("verified the finding {fi} ({st}){now}"),
                lens_label(s(f, "lens")),
                reason,
            )
        } else if let Some((fi, f)) =
            finding.filter(|_| action == "reject_finding" || d.polarity.as_deref() == Some("bad"))
        {
            let st = self.finding_quote(fi, f, hider);
            let words = reason.or_else(|| {
                f.get("rejection")
                    .and_then(|r| s(r, "reason"))
                    .filter(|r| !r.trim().is_empty())
                    .map(|r| Reason::Words(r.to_string()))
            });
            (
                1,
                "rejected_finding",
                format!(
                    "rejected the finding {fi} ({st}); neither it nor its reasoning is evidence"
                ),
                lens_label(s(f, "lens")),
                words,
            )
        } else if kind == "resolve" {
            let ct = bracket(path, "selection.contested")?;
            let c = self.contests().find(|c| s(c, "id") == Some(ct))?;
            let hidden = self
                .marks
                .path_hidden(&format!("selection.contested[{ct}]"));
            let chosen = if s(c, "decided_by") == d.id.as_deref() {
                s(c, "chosen").map(String::from)
            } else {
                d.cites.first().cloned()
            }?;
            let (mut keep, mut drop) = (Vec::new(), Vec::new());
            for v in c
                .get("values")
                .and_then(Yaml::as_sequence)
                .into_iter()
                .flatten()
            {
                let f = s(v, "fact_id").unwrap_or_default();
                let shown = if hidden {
                    MARKED.to_string()
                } else {
                    value_display(v)
                };
                let t = format!("{shown} (fact {f})");
                if f == chosen {
                    keep.push(t);
                } else {
                    drop.push(t);
                }
            }
            let what = contest_subject(s(c, "key").unwrap_or(ct));
            let mut st = format!("settled the contest on {what}: use {}", join_and(&keep));
            if !drop.is_empty() {
                st.push_str(&format!("; do not use {}", join_and(&drop)));
            }
            let words = reason.or_else(|| s(c, "reason").map(|r| Reason::Words(r.to_string())));
            (3, "resolved_contest", st, what, words)
        } else if action == "exclude_fact" {
            let f = bracket(path, "selection.excluded")
                .or_else(|| d.cites.first().map(String::as_str))
                .unwrap_or("a fact");
            let words = reason.or_else(|| {
                self.frozen
                    .get("selection")
                    .and_then(|s| s.get("excluded"))
                    .and_then(Yaml::as_sequence)
                    .into_iter()
                    .flatten()
                    .find(|e| s(e, "fact_id") == Some(f))
                    .and_then(|e| s(e, "reason"))
                    .map(|r| Reason::Words(r.to_string()))
            });
            (
                1,
                "edit",
                format!("left fact {f} out of the research; do not use it"),
                format!("fact {f}"),
                words,
            )
        } else if action == SET_ASIDE || action.starts_with("dismiss") {
            // A carried item this document cannot settle, set aside with a
            // reason (Contract 4 §7.2.1): the host records it as a `verdict`
            // with action `set_aside` and no polarity, so it is taken here,
            // before the verdicts, and never read as "marked as right".
            reason.as_ref()?;
            let label = label_of(path);
            (
                5,
                "edit",
                format!("set {label} aside as something this document cannot settle"),
                label,
                reason,
            )
        } else if kind == "verdict" || d.polarity.is_some() {
            reason.as_ref()?;
            let label = label_of(path);
            let phrase = d
                .reason_code
                .as_deref()
                .and_then(reason_phrase)
                .map(|p| format!(" ({p})"))
                .unwrap_or_default();
            let (g, st) = if d.polarity.as_deref() == Some("bad") {
                (1, format!("marked {label} as wrong{phrase}"))
            } else {
                (6, format!("marked {label} as right{phrase}"))
            };
            (g, "verdict", st, label, reason)
        } else if kind == "edit" {
            if INTAKE_ACTIONS.contains(&action) {
                return None;
            }
            reason.as_ref()?;
            let label = label_of(path);
            let (g, st) = match action {
                "correct_fact" => (2, format!("corrected {label}")),
                "confirm" => (5, format!("confirmed {label}")),
                _ => (5, format!("changed {label}")),
            };
            (g, "edit", st, label, reason)
        } else {
            return None;
        };

        let mut row = row_base(id, row_kind, &who, "person");
        let (statement, reason) = match words {
            Some(Reason::Chip(chip)) => (format!("{statement}, picking the reason “{chip}”"), None),
            Some(Reason::Words(w)) => (statement, Some(w)),
            None => (statement, None),
        };
        let reason = reason.map(|r| {
            if self.reason_hidden(d) {
                MARKED.to_string()
            } else {
                hider.redact(&r)
            }
        });
        row.insert("about".into(), json!(hider.redact(&about)));
        row.insert("statement".into(), json!(hider.redact(&statement)));
        row.insert("reason".into(), json!(reason));
        row.insert("as_of".into(), json!(as_of));
        Some((group, Json::Object(row)))
    }

    fn reason_hidden(&self, d: &Decision) -> bool {
        d.id.as_deref()
            .is_some_and(|id| self.marks.entry_hidden("decisions", id))
    }

    /// A finding's statement, quoted, or the mark when it is hidden.
    fn finding_quote(&self, fi: &str, f: &Yaml, hider: &Hider) -> String {
        let fact = f.get("verification").and_then(|v| s(v, "fact_id"));
        let hidden = self.marks.entry_hidden("findings", fi)
            || fact.is_some_and(|x| self.marks.entry_hidden("facts", x));
        if hidden {
            return MARKED.to_string();
        }
        let st = s(f, "statement").unwrap_or_default().trim();
        format!("\"{}\"", hider.redact(st))
    }

    /// A client's answer to the research, or to one part of it (§3.6): the
    /// client's words verbatim; Ellis's suggestions and the host's own
    /// records are not rows.
    fn client_row(&self, d: &Decision, as_of: Option<String>, hider: &Hider) -> Option<(u8, Json)> {
        let extra = |k: &str| d.extra.get(k);
        let answer = extra("answer").and_then(Yaml::as_str).unwrap_or("rejected");
        let who = extra("client")
            .and_then(|c| c.get("name"))
            .and_then(Yaml::as_str)
            .map(|n| sanitise_name(n, 120))
            .filter(|n| !n.is_empty())
            .unwrap_or_else(|| "The client".to_string());
        let said = extra("said")
            .and_then(Yaml::as_str)
            .filter(|t| !t.trim().is_empty())
            .map(|t| hider.redact(t));
        let (statement, about) = match d.action.as_str() {
            "client_answer" => {
                let verb = match answer {
                    "accepted" => "accepted the research",
                    "accepted_with_changes" => "accepted the research with changes",
                    _ => "rejected the research",
                };
                let reasons: Vec<&str> = extra("reasons")
                    .and_then(Yaml::as_sequence)
                    .into_iter()
                    .flatten()
                    .filter_map(Yaml::as_str)
                    .map(client_reason_phrase)
                    .collect();
                let st = if reasons.is_empty() {
                    verb.to_string()
                } else {
                    format!("{verb} ({})", reasons.join(", "))
                };
                (st, "the whole research".to_string())
            }
            "client_answer_part" => {
                let label = extra("label")
                    .and_then(Yaml::as_str)
                    .map(|l| l.trim().to_string())
                    .filter(|l| !l.is_empty())
                    .unwrap_or_else(|| {
                        let target = d.targets.first().map(String::as_str).unwrap_or_default();
                        label_of(split_address(target).1.unwrap_or_default())
                    });
                let verb = match answer {
                    "accepted" => "accepted",
                    "accepted_with_changes" => "accepted with changes",
                    _ => "rejected",
                };
                (format!("{verb} {label}"), label)
            }
            _ => return None,
        };
        let mut row = row_base(
            d.id.as_deref().unwrap_or_default(),
            "client_review",
            &who,
            "client",
        );
        row.insert("about".into(), json!(hider.redact(&about)));
        row.insert("statement".into(), json!(hider.redact(&statement)));
        row.insert("reason".into(), json!(said));
        row.insert("as_of".into(), json!(as_of));
        Some((7, Json::Object(row)))
    }

    /// A person's own reason (§3.3 R1): the rationale, unless it is a text
    /// the host or an app wrote for them, or a reason chip. Person ids in it
    /// read as names (§6.4).
    fn reason_of(&self, d: &Decision, names: &Names) -> Option<Reason> {
        let r = d.rationale.trim();
        if r.is_empty() || is_default_text(r) {
            return None;
        }
        if CHIPS.contains(&r) {
            return Some(Reason::Chip(r.to_string()));
        }
        if let Some(rest) = r.strip_prefix("Dismissed: ") {
            return Some(Reason::Words(names.in_text(rest)));
        }
        Some(Reason::Words(names.in_text(&d.rationale)))
    }
}

/// A person's set-aside of a carried item this document cannot settle
/// (Contract 4 §7.2.1): a `verdict` with this action and no polarity.
const SET_ASIDE: &str = "set_aside";

/// Intake decisions: their result is in the campaign fields (§10.3).
const INTAKE_ACTIONS: &[&str] = &[
    "start_campaign",
    "create",
    "answer_question",
    "upload-asset",
];

/// The reason chips, exactly (§3.3 R1 item 5).
const CHIPS: &[&str] = &[
    "Clearer wording",
    "Fix a mistake",
    "The client’s words",
    "Newer information",
];

enum Reason {
    Words(String),
    Chip(String),
}

/// Texts the host or an app writes for a person (§3.3 R1 items 2 and 4).
fn is_default_text(r: &str) -> bool {
    const FIXED: &[&str] = &[
        "Marked good.",
        "Verified by a person.",
        "Edited by a person.",
        "Rewrote the wording.",
        "Put the original wording back.",
        "Accepted and locked.",
        "The ask, in the planner’s words",
        "Brief locked for handoff",
        "Applied a brand palette",
    ];
    if FIXED.contains(&r) || r.starts_with("Looks right: ") {
        return true;
    }
    let changed = r
        .strip_prefix("Changed from \"")
        .is_some_and(|rest| rest.ends_with('"') && rest.matches("\" to \"").count() == 1);
    changed
        || r.starts_with("Accepted d_")
        || r.starts_with("Dismissed d_")
        || r.starts_with("Human locked ")
        || r.starts_with("Human unlocked ")
}

fn is_person(d: &Decision) -> bool {
    match d.actor.as_deref() {
        Some(a) => a.starts_with("human:"),
        None => d.agent == "human" || d.agent.starts_with("human:"),
    }
}

fn actor_of(d: &Decision) -> String {
    d.actor
        .clone()
        .filter(|a| a.starts_with("human:"))
        .or_else(|| Some(d.agent.clone()).filter(|a| a.starts_with("human:")))
        .unwrap_or_default()
}

/// Names from the account (§3.1.2).
struct Names<'c> {
    ctx: &'c Context,
}

impl Names<'_> {
    /// A person's name, capitalised to start a line.
    fn of(&self, actor: &str) -> String {
        let named = self
            .ctx
            .people
            .get(actor)
            .filter(|p| !p.erased)
            .map(|p| sanitise_name(&p.name, 60))
            .filter(|n| !n.is_empty());
        match named {
            Some(n) => capitalise(&n),
            None if actor == "human:local" => "The document owner".into(),
            None => "A person on the team".into(),
        }
    }

    /// `text` with every `human:<id>` replaced by that person's name.
    fn in_text(&self, text: &str) -> String {
        let mut out = String::with_capacity(text.len());
        let mut rest = text;
        while let Some(at) = rest.find("human:") {
            let before_ok = rest[..at].chars().next_back().map_or(true, |c| !is_word(c));
            let id_len = rest[at + 6..]
                .find(|c: char| !(c.is_alphanumeric() || c == '_' || c == '-' || c == '.'))
                .unwrap_or(rest.len() - at - 6);
            let id = rest[at + 6..at + 6 + id_len].trim_end_matches('.');
            if !before_ok || id.is_empty() {
                out.push_str(&rest[..at + 6]);
                rest = &rest[at + 6..];
                continue;
            }
            out.push_str(&rest[..at]);
            let name = self.of(&format!("human:{id}"));
            // Mid-sentence, the fallbacks read in lower case.
            if self.ctx.people.contains_key(&format!("human:{id}")) {
                out.push_str(&name);
            } else {
                out.push_str(&lower_first(&name));
            }
            rest = &rest[at + 6 + id.len()..];
        }
        out.push_str(rest);
        out
    }
}

/// A name as the grammar prints it: letters, marks, digits, spaces and
/// `'’-.` kept, whitespace collapsed, at most `max` scalars (§3.1.2).
fn sanitise_name(name: &str, max: usize) -> String {
    let kept: String = name
        .chars()
        .map(|c| if c.is_whitespace() { ' ' } else { c })
        .filter(|c| c.is_alphanumeric() || matches!(c, ' ' | '\'' | '’' | '-' | '.'))
        .collect();
    kept.split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .chars()
        .take(max)
        .collect::<String>()
        .trim()
        .to_string()
}

fn capitalise(s: &str) -> String {
    let mut c = s.chars();
    match c.next() {
        Some(f) => f.to_uppercase().chain(c).collect(),
        None => String::new(),
    }
}

fn lower_first(s: &str) -> String {
    let mut c = s.chars();
    match c.next() {
        Some(f) => f.to_lowercase().chain(c).collect(),
        None => String::new(),
    }
}

fn row_base(id: &str, kind: &str, who: &str, role: &str) -> Map<String, Json> {
    let mut row = Map::new();
    row.insert("id".into(), json!(id));
    row.insert("kind".into(), json!(kind));
    row.insert("who".into(), json!(who));
    row.insert("role".into(), json!(role));
    row.insert("status".into(), json!("current"));
    row
}

/// `entity` (with the market, which the engine's line does not print, so two
/// markets' values never read as one), `key` and `market`.
fn insert_identity(row: &mut Map<String, Json>, f: &Yaml) {
    let market = s(f, "market").filter(|m| !m.is_empty());
    if let Some(e) = s(f, "entity") {
        let e = match market {
            Some(m) => format!("{e} ({m})"),
            None => e.to_string(),
        };
        row.insert("entity".into(), json!(e));
    }
    if let Some(k) = s(f, "key") {
        row.insert("key".into(), json!(k));
    }
    if let Some(m) = market {
        row.insert("market".into(), json!(m));
    }
}

/// The layer version: the `@<version>` of the pin's `origin`.
fn origin_version(f: &Yaml) -> Option<u64> {
    s(f, "origin")?.rsplit_once('@')?.1.parse().ok()
}

/// The pin's layer, from `fact://<layer>/…`, else its `layer`.
fn layer_of(f: &Yaml) -> Option<String> {
    s(f, "origin")
        .and_then(|o| o.strip_prefix("fact://"))
        .and_then(|o| o.split('/').next())
        .filter(|l| !l.is_empty())
        .or_else(|| s(f, "layer"))
        .map(String::from)
}

/// A contest value as the `.clan` holds it: `0.71 proportion`.
fn value_display(v: &Yaml) -> String {
    let value = v.get("value").and_then(text_of).unwrap_or_default();
    match s(v, "unit").filter(|u| !TYPE_UNITS.contains(u)) {
        Some(u) => format!("{value} {u}"),
        None => value,
    }
}

/// `category/drinks.cider:market.volume_share_top3@IE` →
/// `category/drinks.cider market.volume_share_top3 in IE`.
fn contest_subject(key: &str) -> String {
    let (what, market) = match key.rsplit_once('@') {
        Some((w, m)) if !m.contains('/') => (w, Some(m)),
        _ => (key, None),
    };
    let what = what.replacen(':', " ", 1);
    match market {
        Some(m) => format!("{what} in {m}"),
        None => what,
    }
}

/// A data path's label: its campaign field, or its first segment,
/// humanised (`campaign.success_measures[…]` → `Success measures`).
fn label_of(path: &str) -> String {
    // A carried agent branch's id and a merge conflict's key are taken whole
    // (a key may hold brackets of its own, an id dots), as the host labels them.
    let whole = |name: &str| {
        path.strip_prefix(name)?
            .strip_prefix('[')?
            .strip_suffix(']')
    };
    if let Some(key) = whole("merge-report") {
        return format!("the merge conflict {key}");
    }
    if let Some(id) = whole("agents") {
        return format!("the agent branch {id}");
    }
    for (member, noun) in [
        ("facts", "fact"),
        ("findings", "finding"),
        ("selection.contested", "contest"),
    ] {
        if let Some(id) = bracket(path, member) {
            return format!("{noun} {id}");
        }
    }
    let field = path.strip_prefix("campaign.").unwrap_or(path);
    let field = field.split(['.', '[']).next().unwrap_or_default();
    if field.is_empty() {
        return "the research".into();
    }
    capitalise(&field.replace('_', " "))
}

fn lens_label(lens: Option<&str>) -> String {
    match lens.unwrap_or_default() {
        "market_structure" => "market structure",
        "brands_positioning" => "brands and positioning",
        "consumer_culture" => "consumer and culture",
        "category_codes" => "category codes",
        "rhythm_moments" => "rhythm and moments",
        "media_spend" => "media and spend",
        "regulation_clearance" => "regulation",
        "effectiveness_evidence" => "effectiveness",
        _ => "the research",
    }
    .to_string()
}

fn reason_phrase(code: &str) -> Option<&'static str> {
    Some(match code {
        "off_strategy" => "off strategy",
        "off_brand" => "off brand",
        "factually_wrong" => "factually wrong",
        "tone" => "the wrong tone",
        "cliche" => "a cliché",
        "legal_risk" => "a legal risk",
        "client_preference" => "the client's preference",
        "client_words" => "in the client's words",
        "not_single_minded" => "not single-minded",
        "unfeasible" => "not feasible",
        _ => return None,
    })
}

fn client_reason_phrase(code: &str) -> &str {
    match code {
        "off_brief" => "off brief",
        "wrong_audience" => "the wrong audience",
        "tone" => "the wrong tone",
        "facts_wrong" => "wrong facts",
        "budget" => "the budget",
        "other" => "another reason",
        other => other,
    }
}

/// `a, b and c` (§6.6).
fn join_and(items: &[String]) -> String {
    match items {
        [] => String::new(),
        [one] => one.clone(),
        [init @ .., last] => format!("{} and {last}", init.join(", ")),
    }
}

/// The value at a dotted data path, with `[id]` picking a list entry by id.
fn get_path<'v>(root: &'v Yaml, path: &str) -> Option<&'v Yaml> {
    let mut at = root;
    for seg in path.split('.') {
        let (name, id) = match seg.split_once('[') {
            Some((n, rest)) => (n, rest.strip_suffix(']')),
            None => (seg, None),
        };
        if !name.is_empty() {
            at = at.get(name)?;
        }
        if let Some(id) = id {
            at = match at {
                Yaml::Sequence(items) => items.iter().find(|i| s(i, "id") == Some(id))?,
                Yaml::Mapping(_) => at.get(id)?,
                _ => return None,
            };
        }
    }
    Some(at)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn hider_of(values: &[(Yaml, Option<&str>)]) -> Hider {
        let mut h = Hider::default();
        for (v, u) in values {
            h.add(v, *u);
        }
        h
    }

    #[test]
    fn a_proportion_is_replaced_in_its_normal_forms() {
        let h = hider_of(&[(Yaml::from(0.47), Some("proportion"))]);
        assert_eq!(
            h.redact("awareness is 47% in IE"),
            format!("awareness is {MARKED} in IE")
        );
        assert_eq!(h.redact("47 percent know it"), format!("{MARKED} know it"));
        assert_eq!(h.redact("a share of 0.47"), format!("a share of {MARKED}"));
        // Not specific enough bare, and never inside an id or a date.
        assert_eq!(h.redact("47 people; f_01JA0B47"), "47 people; f_01JA0B47");
        assert_eq!(h.redact("0.470 proportion"), format!("{MARKED} proportion"));
    }

    #[test]
    fn money_matches_with_a_sign_or_a_scale_and_bare_only_when_specific() {
        let h = hider_of(&[(Yaml::from(400000), Some("eur"))]);
        for t in ["€400k", "EUR 400,000", "400,000", "400k", "€0.4m"] {
            assert_eq!(h.redact(t), MARKED, "{t}");
        }
        let small = hider_of(&[(Yaml::from(0.5), Some("percent_abv"))]);
        assert_eq!(small.redact("0.5% ABV"), MARKED);
        assert_eq!(small.redact("0.5 of it"), "0.5 of it");
    }

    #[test]
    fn prose_matches_whole_at_word_boundaries_ignoring_case_and_spacing() {
        let h = hider_of(&[(Yaml::from("Lúnasa"), None), (Yaml::from("No"), None)]);
        assert_eq!(
            h.redact("A LÚNASA  launch; lúnasas stay"),
            format!("A {MARKED}  launch; lúnasas stay")
        );
        // Under four scalars, a value is never matched elsewhere.
        assert_eq!(h.redact("No, not now"), "No, not now");
    }

    #[test]
    fn marks_cover_by_whole_segments_and_the_newest_decides() {
        let r = "R";
        let m = |t: &str, model| Mark {
            governs: governs(t, r, "B").unwrap(),
            model,
        };
        let marks = Marks(vec![
            m("R#campaign.name.value", true),
            m("R#campaign.name", false),
        ]);
        assert!(
            !marks.path_hidden("campaign.name"),
            "the newest reopened it"
        );
        assert!(!marks.path_hidden("campaign.name_long"));
        let marks = Marks(vec![m("X#facts[f_1]", false)]);
        assert!(marks.entry_hidden("facts", "f_1"), "ids are never remapped");
        assert_eq!(
            governs("X#campaign.name", r, "B"),
            None,
            "a data path of another document"
        );
        assert_eq!(governs("R", r, "B"), Some(Governs::Document));
        assert_eq!(
            governs("B#client", r, "B"),
            Some(Governs::Own("client".into())),
            "the document's own field"
        );
        assert_eq!(
            governs("B", r, "B"),
            None,
            "no document-level mark on itself"
        );
    }

    #[test]
    fn stamps_read_as_utc_dates() {
        assert_eq!(
            date_of("2026-09-22T23:30:00-02:00").as_deref(),
            Some("2026-09-23")
        );
        assert_eq!(
            date_of("2026-09-22 16:09:12").as_deref(),
            Some("2026-09-22")
        );
        assert_eq!(date_of("2026-09-22").as_deref(), Some("2026-09-22"));
        assert_eq!(date_of("yesterday"), None);
    }

    #[test]
    fn names_come_from_the_account_and_never_print_a_raw_id() {
        let mut ctx = Context::default();
        ctx.people.insert(
            "human:u_1".into(),
            Person {
                name: "  Alex\tDoe <x> ".into(),
                ..Default::default()
            },
        );
        let n = Names { ctx: &ctx };
        assert_eq!(n.of("human:u_1"), "Alex Doe x");
        assert_eq!(n.of("human:local"), "The document owner");
        assert_eq!(n.of("human:u_2"), "A person on the team");
        assert_eq!(
            n.in_text("corrected by human:u_1. Checked with human:u_2."),
            "corrected by Alex Doe x. Checked with a person on the team."
        );
    }

    #[test]
    fn numbers_print_as_the_clan_holds_them() {
        assert_eq!(text_of(&Yaml::from(0.47)).unwrap(), "0.47");
        assert_eq!(text_of(&Yaml::from(2400000.0)).unwrap(), "2400000");
        assert_eq!(text_of(&Yaml::from(7)).unwrap(), "7");
        assert_eq!(
            contest_subject("brand/orchard-hill:product.abv"),
            "brand/orchard-hill product.abv"
        );
        assert_eq!(
            contest_subject("category/drinks.cider:market.volume_share_top3@IE"),
            "category/drinks.cider market.volume_share_top3 in IE"
        );
        assert_eq!(
            join_and(&["a".into(), "b".into(), "c".into()]),
            "a, b and c"
        );
    }
}
