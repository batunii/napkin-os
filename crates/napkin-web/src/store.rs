// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! One tenant's documents, addressed by opaque ids.
//!
//! The desktop's `FsStore` makes a [`DocId`] a filesystem path, which is right
//! there — the user picks files and the shell hands the path back. On the web a
//! document id travels in URLs and comes from a browser, so a path would be
//! both a traversal hazard and a leak of the server's layout. Here an id is an
//! opaque `<kind>-<name>` token this module alone knows how to resolve, and
//! everything lives under the tenant's own directory:
//!
//! ```text
//! <root>/<tenant>/documents/<slug>.clan     doc-<slug>
//! <root>/<tenant>/apps/<app_id>/app.clan    app-<app_id>
//! <root>/<tenant>/home-<version>.clan       home-<version>
//! ```
//!
//! That the same handlers serve both shapes is the whole point of `DocStore`.

use std::path::PathBuf;

use napkin_host::{DocId, DocStore, HostError, HostResult};

pub struct TenantStore {
    root: PathBuf,
}

impl TenantStore {
    pub fn new(root: PathBuf) -> Self {
        Self { root }
    }

    /// Resolve an id to a path inside this tenant's tree, or refuse it.
    ///
    /// Refusal is the security boundary: ids arrive from the browser, so
    /// anything that is not one of the three known shapes — or that carries a
    /// separator or a `..` — never becomes a path at all.
    fn path_for(&self, id: &DocId) -> HostResult<PathBuf> {
        let raw = id.as_str();
        let (kind, name) = raw
            .split_once('-')
            .ok_or_else(|| HostError::bad_request(format!("malformed document id: {raw}")))?;
        if name.is_empty() || name.contains('/') || name.contains('\\') || name.contains("..") {
            return Err(HostError::bad_request(format!("unsafe document id: {raw}")));
        }
        Ok(match kind {
            "doc" => self.documents_dir().join(format!("{name}.clan")),
            "app" => self.apps_dir().join(name).join("app.clan"),
            "home" => self.root.join(format!("home-{name}.clan")),
            _ => {
                return Err(HostError::bad_request(format!(
                    "unknown document id kind: {raw}"
                )))
            }
        })
    }

    fn documents_dir(&self) -> PathBuf {
        self.root.join("documents")
    }

    fn apps_dir(&self) -> PathBuf {
        self.root.join("apps")
    }
}

/// Keep an id URL- and path-safe: everything outside the allowed set collapses
/// to `-`. Dots survive because app ids are reverse-DNS (`ie.napkin.home`) and
/// fork branches are `<slug>.<agent>`.
fn slugify(s: &str) -> String {
    s.chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || c == '.' || c == '-' || c == '_' {
                c
            } else {
                '-'
            }
        })
        .collect()
}

pub fn doc_id(slug: &str) -> DocId {
    DocId::new(format!("doc-{}", slugify(slug)))
}

pub fn app_id(app: &str) -> DocId {
    DocId::new(format!("app-{}", slugify(app)))
}

impl DocStore for TenantStore {
    fn read(&self, id: &DocId) -> HostResult<Vec<u8>> {
        let path = self.path_for(id)?;
        std::fs::read(&path).map_err(|e| HostError::not_found(format!("{id}: {e}")))
    }

    fn write(&self, id: &DocId, bytes: &[u8]) -> HostResult<()> {
        let path = self.path_for(id)?;
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).map_err(|e| HostError::internal(e.to_string()))?;
        }
        std::fs::write(&path, bytes).map_err(|e| HostError::internal(e.to_string()))
    }

    fn exists(&self, id: &DocId) -> bool {
        self.path_for(id).map(|p| p.exists()).unwrap_or(false)
    }

    fn app_candidates(&self) -> Vec<DocId> {
        let Ok(rd) = std::fs::read_dir(self.apps_dir()) else {
            return Vec::new();
        };
        rd.flatten()
            .filter(|e| e.path().is_dir())
            .filter_map(|e| e.file_name().into_string().ok())
            .map(|name| app_id(&name))
            .collect()
    }

    fn app_template(&self, app: &str) -> DocId {
        app_id(app)
    }

    fn install_template(&self, app: &str, bytes: &[u8]) -> HostResult<DocId> {
        let id = app_id(app);
        self.write(&id, bytes)?;
        Ok(id)
    }

    fn documents(&self) -> Vec<DocId> {
        let Ok(rd) = std::fs::read_dir(self.documents_dir()) else {
            return Vec::new();
        };
        rd.flatten()
            .filter_map(|e| {
                let p = e.path();
                (p.extension().and_then(|x| x.to_str()) == Some("clan"))
                    .then(|| p.file_stem().and_then(|s| s.to_str()).map(doc_id))
                    .flatten()
            })
            .collect()
    }

    fn new_document(&self, app: &str, id_short: &str) -> HostResult<DocId> {
        std::fs::create_dir_all(self.documents_dir())
            .map_err(|e| HostError::internal(e.to_string()))?;
        Ok(doc_id(&format!("{}-{}", app.replace('.', "-"), id_short)))
    }

    fn home(&self, version_tag: &str) -> HostResult<DocId> {
        std::fs::create_dir_all(&self.root).map_err(|e| HostError::internal(e.to_string()))?;
        Ok(DocId::new(format!("home-{}", slugify(version_tag))))
    }

    fn fork_branch(&self, parent: &DocId, agent: &str) -> DocId {
        // Branches are siblings of the parent, exactly as on disk: the id keeps
        // the parent's slug and gains the agent's name.
        let slug = parent
            .as_str()
            .strip_prefix("doc-")
            .unwrap_or(parent.as_str());
        doc_id(&format!("{slug}.{agent}"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn store() -> (tempfile::TempDir, TenantStore, PathBuf) {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join("tenant-a");
        let store = TenantStore::new(root.clone());
        (dir, store, root)
    }

    #[test]
    fn ids_resolve_inside_the_tenant_tree() {
        let (_d, s, _root) = store();
        assert!(s
            .path_for(&DocId::new("doc-brief-1a2b"))
            .unwrap()
            .ends_with("documents/brief-1a2b.clan"));
        assert!(s
            .path_for(&DocId::new("app-ie.napkin.home"))
            .unwrap()
            .ends_with("apps/ie.napkin.home/app.clan"));
        assert!(s
            .path_for(&DocId::new("home-v6"))
            .unwrap()
            .ends_with("home-v6.clan"));
    }

    // Document ids come from the browser. Anything that is not one of the three
    // known shapes must never reach the filesystem.
    #[test]
    fn hostile_ids_never_become_paths() {
        let (_d, s, _root) = store();
        for bad in [
            "doc-../../etc/passwd",
            "doc-..",
            "app-../../../root/.ssh/id_rsa",
            "home-a/b",
            "doc-a\\b",
            "secret-x",
            "nodash",
            "doc-",
        ] {
            assert!(
                s.path_for(&DocId::new(bad)).is_err(),
                "{bad} must be refused"
            );
        }
    }

    #[test]
    fn two_tenants_cannot_see_each_others_documents() {
        let dir = tempfile::tempdir().unwrap();
        let a = TenantStore::new(dir.path().join("a"));
        let b = TenantStore::new(dir.path().join("b"));
        let id = doc_id("shared-name");

        a.write(&id, b"tenant a's bytes").unwrap();
        assert!(
            !b.exists(&id),
            "the same id in another tenant must not resolve"
        );
        assert!(b.read(&id).is_err());
        assert_eq!(a.read(&id).unwrap(), b"tenant a's bytes");
    }

    #[test]
    fn fork_branches_stay_documents_of_the_same_tenant() {
        let (_d, s, root) = store();
        let branch = s.fork_branch(&doc_id("brief-1a2b"), "researcher");
        assert_eq!(branch.as_str(), "doc-brief-1a2b.researcher");
        assert!(s.path_for(&branch).unwrap().starts_with(&root));
    }

    #[test]
    fn listings_round_trip_through_ids() {
        let (_d, s, _root) = store();
        let id = doc_id("brief-1a2b");
        s.write(&id, b"x").unwrap();
        assert_eq!(s.documents(), vec![id]);

        let app = s.install_template("ie.napkin.home", b"y").unwrap();
        assert_eq!(s.app_candidates(), vec![app.clone()]);
        assert_eq!(s.read(&app).unwrap(), b"y");
    }
}
