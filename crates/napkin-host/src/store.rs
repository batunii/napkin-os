// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! Where `.clan` bytes live.
//!
//! The host never touches the filesystem directly — it asks a [`DocStore`].
//! On the desktop that store is the filesystem itself ([`FsStore`]) and a
//! [`DocId`] is a path; a hosted deployment can back the same trait with
//! per-tenant object storage without any handler changing.

use std::fmt;
use std::path::{Path, PathBuf};

use crate::error::{HostError, HostResult};

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

pub trait DocStore: Send + Sync {
    fn read(&self, id: &DocId) -> HostResult<Vec<u8>>;
    fn write(&self, id: &DocId, bytes: &[u8]) -> HostResult<()>;
    fn exists(&self, id: &DocId) -> bool;

    /// Every `.clan` in the app library that might be an installed template.
    /// The caller opens each and keeps the ones whose manifest says so.
    fn app_candidates(&self) -> Vec<DocId>;

    /// Where the template for `app_id` lives (whether or not it is installed).
    fn app_template(&self, app_id: &str) -> DocId;

    /// Install template `bytes` for `app_id`, returning where it landed.
    fn install_template(&self, app_id: &str, bytes: &[u8]) -> HostResult<DocId>;

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

/// The desktop store: the filesystem, plus the two well-known library dirs.
pub struct FsStore {
    data_dir: PathBuf,
    apps_dir: PathBuf,
}

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

fn path_of(id: &DocId) -> PathBuf {
    PathBuf::from(id.as_str())
}

impl DocStore for FsStore {
    fn read(&self, id: &DocId) -> HostResult<Vec<u8>> {
        std::fs::read(path_of(id)).map_err(|e| HostError::not_found(e.to_string()))
    }

    fn write(&self, id: &DocId, bytes: &[u8]) -> HostResult<()> {
        std::fs::write(path_of(id), bytes).map_err(|e| HostError::internal(e.to_string()))
    }

    fn exists(&self, id: &DocId) -> bool {
        path_of(id).exists()
    }

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

    fn install_template(&self, app_id: &str, bytes: &[u8]) -> HostResult<DocId> {
        let dir = self.apps_dir.join(app_id);
        std::fs::create_dir_all(&dir).map_err(|e| HostError::internal(e.to_string()))?;
        let dest = DocId::from(dir.join("app.clan"));
        self.write(&dest, bytes)?;
        Ok(dest)
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
