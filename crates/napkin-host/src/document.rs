// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! A document as an operation sees it, and what an operation hands back.
//!
//! The store's version is the truth. A [`Document`] is a snapshot of one
//! document at one version — read-only, and possibly already stale by the time
//! anyone looks at it. An operation never writes: it takes a snapshot and
//! returns a [`Change`] saying what the document should become and which
//! version it was computed from. Applying that change is the store's job and
//! happens in exactly one place ([`crate::store::PartStore::apply`]), which is
//! where the version check (W2-A4) and the seal check (W5-Z1) belong.
//!
//! Until the loop has closed once, the SDK operations still run over a packed
//! `ClanFile` (the Tier 0 rule), so a snapshot wraps one and a change carries
//! the whole new archive. Moving both onto parts is W2-O4; the shapes here are
//! the ones that move.

use std::fmt;

use clan_sdk::{ClanFile, Decision, DecisionChain};

use crate::error::HostResult;
use crate::event::HostEvent;
use crate::session::NAPKIN_PUBLIC_KEY;
use crate::store::{DocId, PartStore};

const CHAIN: &str = "agent/decision-chain.yaml";

/// Which state of a document a snapshot or a change refers to.
///
/// Opaque outside the store. For a packed archive it is the archive's sha256 —
/// so two byte-identical files are the same version, which is correct — and a
/// server store is free to make it a row version or an ETag.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct Version(String);

impl Version {
    pub fn new(s: impl Into<String>) -> Self {
        Self(s.into())
    }

    /// The version of a packed archive: its content hash.
    pub fn of_archive(bytes: &[u8]) -> Self {
        Self(clan_sdk::hash::sha256_prefixed(bytes))
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl fmt::Display for Version {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

/// What a change expects to find in the store when it is applied.
///
/// Creating a document is its own case rather than "no version": a write that
/// means to make something new must never silently replace what is there, and
/// a write that means to update must never silently create.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Base {
    /// The document must not exist yet.
    Absent,
    /// The document must still be at this version.
    At(Version),
}

impl Base {
    pub fn expected_version(&self) -> Option<&Version> {
        match self {
            Base::Absent => None,
            Base::At(v) => Some(v),
        }
    }
}

/// One document at one version.
pub struct Document {
    id: DocId,
    version: Version,
    clan: ClanFile,
    // Whether the app is validly signed by Napkin's key. Decided when the
    // document is opened and carried through its own changes, exactly as the
    // open file always was: a write does not re-run the trust gate.
    trusted: bool,
}

impl Document {
    /// Read `id` from the store as it stands now.
    pub fn load(store: &dyn PartStore, id: DocId) -> HostResult<Self> {
        let bytes = store.read(&id)?;
        Self::from_bytes(id, bytes)
    }

    /// A snapshot of `bytes`, which are `id` at the version they hash to.
    pub fn from_bytes(id: DocId, bytes: Vec<u8>) -> HostResult<Self> {
        let clan = ClanFile::from_bytes(bytes)?;
        let trusted = clan_sdk::verify_app(&clan, NAPKIN_PUBLIC_KEY);
        Ok(Self {
            version: Version::of_archive(clan.raw_bytes()),
            id,
            clan,
            trusted,
        })
    }

    pub fn id(&self) -> &DocId {
        &self.id
    }

    pub fn version(&self) -> &Version {
        &self.version
    }

    pub fn clan(&self) -> &ClanFile {
        &self.clan
    }

    pub fn trusted(&self) -> bool {
        self.trusted
    }

    /// The packed archive exactly as stored at this version.
    pub fn bytes(&self) -> &[u8] {
        self.clan.raw_bytes()
    }

    pub fn title(&self) -> &str {
        &self.clan.manifest().title
    }

    /// The document's app id, when it is an instance of one.
    pub fn app_id(&self) -> Option<&str> {
        self.clan.manifest().app.as_ref().map(|a| a.app_id.as_str())
    }

    /// A change that replaces this document with `bytes`, based on this
    /// snapshot's version. The decisions it appends are read off the chain, so
    /// a caller cannot record one thing and write another.
    pub fn change(&self, bytes: Vec<u8>) -> HostResult<Change> {
        let decisions = appended_decisions(&self.clan, &bytes)?;
        Ok(Change {
            doc: self.id.clone(),
            base: Base::At(self.version.clone()),
            bytes,
            decisions,
            events: Vec::new(),
        })
    }

    /// The snapshot this document becomes once `change` has been applied and
    /// the store reported `version` for it. Trust carries over: see the field.
    pub fn advance(&self, change: &Change, version: Version) -> HostResult<Self> {
        let clan = ClanFile::from_bytes(change.bytes.clone())?;
        Ok(Self {
            id: change.doc.clone(),
            version,
            clan,
            trusted: self.trusted,
        })
    }
}

/// What an operation returns instead of writing.
///
/// Nothing has happened until a store applies it. `bytes` is the whole new
/// archive for now; the contract's per-part fields replace it when the SDK
/// operations move onto parts (W2-O4).
#[derive(Clone, Debug)]
pub struct Change {
    pub doc: DocId,
    /// What the store must find before it applies this — `expected_version`
    /// in the contract.
    pub base: Base,
    pub bytes: Vec<u8>,
    /// Decisions this change appends to the chain, newest first. Already
    /// inside `bytes`; listed so a store that keeps decisions as rows can
    /// insert them without re-reading the chain.
    pub decisions: Vec<Decision>,
    /// Fanned out to every view of the document after the change commits.
    pub events: Vec<HostEvent>,
}

impl Change {
    /// A new document. The store refuses to apply it over an existing one once
    /// W2-A4's check is in place. Every decision it arrives with is appended —
    /// a spin-off brings its source's whole chain.
    pub fn create(doc: DocId, bytes: Vec<u8>) -> Self {
        let decisions = chain_of_bytes(&bytes)
            .map(|c| c.decisions)
            .unwrap_or_default();
        Self {
            doc,
            base: Base::Absent,
            bytes,
            decisions,
            events: Vec::new(),
        }
    }

    /// Replace a document the caller has not opened as a [`Document`], based on
    /// the version it read. For callers that hold only bytes (installing an app
    /// over an older copy of itself).
    pub fn replace(doc: DocId, base: Version, bytes: Vec<u8>) -> Self {
        Self {
            doc,
            base: Base::At(base),
            bytes,
            decisions: Vec::new(),
            events: Vec::new(),
        }
    }

    pub fn with_event(mut self, e: HostEvent) -> Self {
        self.events.push(e);
        self
    }

    pub fn expected_version(&self) -> Option<&Version> {
        self.base.expected_version()
    }

    /// The version of the archive this change writes. A store whose versions
    /// are content hashes reports exactly this from `apply`; one with row
    /// versions reports its own.
    pub fn archive_version(&self) -> Version {
        Version::of_archive(&self.bytes)
    }
}

/// The decision chain inside a packed archive; empty when it has none.
fn chain_of(clan: &ClanFile) -> HostResult<DecisionChain> {
    if !clan.has_entry(CHAIN) {
        return Ok(DecisionChain::default());
    }
    Ok(DecisionChain::from_yaml(&clan.read_entry(CHAIN)?)?)
}

fn chain_of_bytes(bytes: &[u8]) -> HostResult<DecisionChain> {
    chain_of(&ClanFile::from_bytes(bytes.to_vec())?)
}

/// Decisions `after` has that `before` did not. The chain is newest-first and
/// append-only, so they are the leading entries beyond the old length.
fn appended_decisions(before: &ClanFile, after: &[u8]) -> HostResult<Vec<Decision>> {
    let old = chain_of(before)?.decisions.len();
    let new = chain_of_bytes(after)?.decisions;
    let added = new.len().saturating_sub(old);
    Ok(new.into_iter().take(added).collect())
}
