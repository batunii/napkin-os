// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! Documents, in memory.
//!
//! The desktop store is the filesystem and the server's is a directory per
//! tenant. Here there is neither, and — deliberately — nothing persistent
//! either: work exists for as long as the tab does, and leaves as a `.clan` the
//! visitor downloads. A demo anyone can open should not quietly accumulate
//! other people's client briefs in their browser.
//!
//! Persistence would be an OPFS-backed `PartStore` and nothing else would move.

use std::collections::HashMap;
use std::sync::Mutex;

use napkin_host::{Change, DocId, HostError, HostResult, Library, PartStore, Version};

/// The id shapes the rest of the host expects: `doc-…`, `app-…`, `home-…`.
/// With no paths involved there is nothing to sanitise — a key is a key.
#[derive(Default)]
pub struct MemStore {
    docs: Mutex<HashMap<DocId, Vec<u8>>>,
}

fn slug(s: &str) -> String {
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

impl MemStore {
    pub fn new() -> Self {
        Self::default()
    }

    /// Everything currently held, for a shell that wants to list or save it.
    pub fn ids(&self) -> Vec<DocId> {
        self.docs.lock().unwrap().keys().cloned().collect()
    }
}

impl PartStore for MemStore {
    fn read(&self, id: &DocId) -> HostResult<Vec<u8>> {
        self.docs
            .lock()
            .unwrap()
            .get(id)
            .cloned()
            .ok_or_else(|| HostError::not_found(format!("no such document: {id}")))
    }

    fn exists(&self, id: &DocId) -> bool {
        self.docs.lock().unwrap().contains_key(id)
    }

    /// One tab, one thread, one writer: there is nothing for the version check
    /// (W2-A4) to catch here yet, but this is where it would go.
    fn apply(&self, change: &Change) -> HostResult<Version> {
        self.docs
            .lock()
            .unwrap()
            .insert(change.doc.clone(), change.bytes.clone());
        Ok(change.archive_version())
    }
}

impl Library for MemStore {
    fn app_candidates(&self) -> Vec<DocId> {
        self.ids()
            .into_iter()
            .filter(|d| d.as_str().starts_with("app-"))
            .collect()
    }

    fn app_template(&self, app: &str) -> DocId {
        DocId::new(format!("app-{}", slug(app)))
    }

    fn documents(&self) -> Vec<DocId> {
        self.ids()
            .into_iter()
            .filter(|d| d.as_str().starts_with("doc-"))
            .collect()
    }

    fn new_document(&self, app: &str, id_short: &str) -> HostResult<DocId> {
        Ok(DocId::new(format!(
            "doc-{}-{}",
            slug(&app.replace('.', "-")),
            slug(id_short)
        )))
    }

    fn home(&self, version_tag: &str) -> HostResult<DocId> {
        Ok(DocId::new(format!("home-{}", slug(version_tag))))
    }

    fn fork_branch(&self, parent: &DocId, agent: &str) -> DocId {
        let stem = parent
            .as_str()
            .strip_prefix("doc-")
            .unwrap_or(parent.as_str());
        DocId::new(format!("doc-{}.{}", stem, slug(agent)))
    }
}
