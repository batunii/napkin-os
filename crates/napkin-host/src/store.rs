// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! Where documents live, split by what a caller needs from them.
//!
//! The host never touches the filesystem directly. It asks two ports:
//!
//! - a [`PartStore`] holds document bytes: read one as it stands, and apply a
//!   [`Change`]. `apply` is the only way anything is written — creating a
//!   document is a change too — so the version check (W2-A4) and the seal
//!   check (W5-Z1) have exactly one place to live.
//! - a [`Library`] knows the layout: which documents are installed apps, which
//!   are instances, where the home app and a fork branch go. It names things;
//!   it never writes them.
//!
//! On the desktop both are the filesystem ([`FsStore`]) and a [`DocId`] is a
//! path; a hosted deployment backs the same traits with per-tenant storage and
//! no handler changes.

use std::fmt;
use std::path::{Path, PathBuf};

#[cfg(feature = "native")]
use crate::error::HostError;
use crate::error::HostResult;

pub use crate::document::{Base, Change, Version};

/// Opaque identity of one document within a store.
///
/// Desktop: an absolute filesystem path — which is why `open` accepts any
/// `.clan` the user picks, not only store-managed ones. A hosted store is free
/// to make this a tenant-scoped opaque id instead; nothing outside the store
/// parses it.
#[derive(Clone, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct DocId(String);

impl DocId {
    pub fn new(s: impl Into<String>) -> Self {
        Self(s.into())
    }
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl fmt::Display for DocId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl From<&Path> for DocId {
    fn from(p: &Path) -> Self {
        Self(p.display().to_string())
    }
}

impl From<PathBuf> for DocId {
    fn from(p: PathBuf) -> Self {
        Self(p.display().to_string())
    }
}

/// Document bytes, and the single write funnel.
pub trait PartStore: Send + Sync {
    /// The document as it stands now.
    fn read(&self, id: &DocId) -> HostResult<Vec<u8>>;

    fn exists(&self, id: &DocId) -> bool;

    /// The version `id` is at now. For a packed archive, its hash.
    fn version(&self, id: &DocId) -> HostResult<Version> {
        Ok(Version::of_archive(&self.read(id)?))
    }

    /// Make `change` true, and report the version the document is now at.
    ///
    /// The only write path in the host. Every operation returns a [`Change`]
    /// and a shell (or a library helper acting for one) hands it here.
    fn apply(&self, change: &Change) -> HostResult<Version>;
}

/// The app library's layout. Names documents; never writes them.
pub trait Library: Send + Sync {
    /// Every `.clan` in the app library that might be an installed template.
    /// The caller opens each and keeps the ones whose manifest says so.
    fn app_candidates(&self) -> Vec<DocId>;

    /// Where the template for `app_id` lives (whether or not it is installed).
    fn app_template(&self, app_id: &str) -> DocId;

    /// Every document instance in the library, in no particular order.
    fn documents(&self) -> Vec<DocId>;

    /// Allocate an id for a new instance of `app_id`. `id_short` is the first
    /// characters of the new document's manifest id, for a stable, readable name.
    fn new_document(&self, app_id: &str, id_short: &str) -> HostResult<DocId>;

    /// The home CLAN app's id. `version_tag` is bumped whenever the embedded
    /// home HTML changes, so a new build rebuilds rather than reusing a stale one.
    fn home(&self, version_tag: &str) -> HostResult<DocId>;

    /// Where a fork branch for `agent` belongs, relative to `parent`.
    fn fork_branch(&self, parent: &DocId, agent: &str) -> DocId;
}

/// A store that is both — what every shell actually has, and what the library
/// helpers take. Implemented for anything that implements the two halves.
pub trait DocStore: PartStore + Library {
    /// This store as its byte half, for code that must not see the layout.
    fn parts(&self) -> &dyn PartStore;
    /// This store as its layout half, for code that must not see the bytes.
    fn library(&self) -> &dyn Library;
}

impl<T: PartStore + Library> DocStore for T {
    fn parts(&self) -> &dyn PartStore {
        self
    }
    fn library(&self) -> &dyn Library {
        self
    }
}

/// The desktop store: the filesystem, plus the two well-known library dirs.
#[cfg(feature = "native")]
pub struct FsStore {
    data_dir: PathBuf,
    apps_dir: PathBuf,
}

#[cfg(feature = "native")]
impl FsStore {
    /// `data_dir` is the per-user app-data directory. The app library defaults
    /// to `<data_dir>/apps` unless `NAPKIN_APPS_DIR` overrides it.
    pub fn new(data_dir: PathBuf) -> Self {
        let apps_dir = std::env::var_os("NAPKIN_APPS_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(|| data_dir.join("apps"));
        Self { data_dir, apps_dir }
    }

    pub fn data_dir(&self) -> &Path {
        &self.data_dir
    }

    pub fn apps_dir(&self) -> &Path {
        &self.apps_dir
    }

    fn documents_dir(&self) -> PathBuf {
        self.data_dir.join("documents")
    }
}

#[cfg(feature = "native")]
fn path_of(id: &DocId) -> PathBuf {
    PathBuf::from(id.as_str())
}

#[cfg(feature = "native")]
impl PartStore for FsStore {
    fn read(&self, id: &DocId) -> HostResult<Vec<u8>> {
        std::fs::read(path_of(id)).map_err(|e| HostError::not_found(e.to_string()))
    }

    fn exists(&self, id: &DocId) -> bool {
        path_of(id).exists()
    }

    /// A file write. Deliberately unchecked for now: `change.base` says what
    /// the writer expected to find, and comparing it with
    /// `self.version(&change.doc)` right here — re-read from disk, not
    /// remembered from open — is the whole of W2-A4.
    fn apply(&self, change: &Change) -> HostResult<Version> {
        let path = path_of(&change.doc);
        // An install lands in a directory nobody has made yet.
        if let Some(dir) = path.parent().filter(|d| !d.as_os_str().is_empty()) {
            std::fs::create_dir_all(dir).map_err(|e| HostError::internal(e.to_string()))?;
        }
        std::fs::write(&path, &change.bytes).map_err(|e| HostError::internal(e.to_string()))?;
        Ok(change.archive_version())
    }
}

#[cfg(feature = "native")]
impl Library for FsStore {
    fn app_candidates(&self) -> Vec<DocId> {
        let mut out = Vec::new();
        let Ok(rd) = std::fs::read_dir(&self.apps_dir) else {
            return out;
        };
        for e in rd.flatten() {
            let p = e.path();
            if p.is_dir() {
                out.push(DocId::from(p.join("app.clan")));
            } else if p.extension().and_then(|x| x.to_str()) == Some("clan") {
                out.push(DocId::from(p));
            }
        }
        out
    }

    fn app_template(&self, app_id: &str) -> DocId {
        DocId::from(self.apps_dir.join(app_id).join("app.clan"))
    }

    fn documents(&self) -> Vec<DocId> {
        let mut out = Vec::new();
        let Ok(rd) = std::fs::read_dir(self.documents_dir()) else {
            return out;
        };
        for e in rd.flatten() {
            let p = e.path();
            if p.extension().and_then(|x| x.to_str()) == Some("clan") {
                out.push(DocId::from(p));
            }
        }
        out
    }

    fn new_document(&self, app_id: &str, id_short: &str) -> HostResult<DocId> {
        let docs = self.documents_dir();
        std::fs::create_dir_all(&docs).map_err(|e| HostError::internal(e.to_string()))?;
        Ok(DocId::from(docs.join(format!(
            "{}-{}.clan",
            app_id.replace('.', "-"),
            id_short
        ))))
    }

    fn home(&self, version_tag: &str) -> HostResult<DocId> {
        std::fs::create_dir_all(&self.data_dir).map_err(|e| HostError::internal(e.to_string()))?;
        Ok(DocId::from(
            self.data_dir.join(format!("home-{version_tag}.clan")),
        ))
    }

    fn fork_branch(&self, parent: &DocId, agent: &str) -> DocId {
        let p = path_of(parent);
        let dir = p.parent().map(|d| d.to_path_buf()).unwrap_or_default();
        let stem = p.file_stem().and_then(|s| s.to_str()).unwrap_or("doc");
        DocId::from(dir.join(format!("{stem}.{agent}.clan")))
    }
}
