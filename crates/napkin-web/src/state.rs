// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! Server state: one workspace per tenant, one host session per open document.
//!
//! `Session` models one open document, which on the desktop means one at a
//! time. Here it means one per document per tenant, held in a small cache: two
//! tabs on the same document share a session (so neither can hold a stale
//! archive), and two tenants never share anything at all.

use std::collections::HashMap;
use std::collections::VecDeque;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use napkin_host::{DocId, DocStore, HostConfig, HostResult, Session};

use crate::events::EventBus;
use crate::exports::ExportStore;
use crate::meter::Meter;
use crate::store::TenantStore;
use crate::tenant::TenantId;
use crate::tokens::TokenStore;

/// How many documents one tenant may hold open before the least recently
/// opened is dropped. Only view state is lost — the archive is always on disk.
const SESSION_CACHE: usize = 32;

/// Open documents, plus the order they were opened in, so the cache can shed
/// the least recently opened when it is full.
#[derive(Default)]
struct SessionCache {
    open: HashMap<DocId, Arc<Session>>,
    order: VecDeque<DocId>,
}

pub struct Workspace {
    pub store: Arc<dyn DocStore>,
    sessions: Mutex<SessionCache>,
}

impl Workspace {
    fn new(store: Arc<dyn DocStore>) -> Self {
        Self {
            store,
            sessions: Mutex::new(SessionCache::default()),
        }
    }

    /// The session for one document, opening it if this is the first request.
    pub fn session(&self, doc: &DocId) -> HostResult<Arc<Session>> {
        if let Some(s) = self.sessions.lock().unwrap().open.get(doc) {
            return Ok(s.clone());
        }
        // Open outside the lock: reading and validating an archive is real work
        // and must not block every other document of this tenant.
        let session = Arc::new(Session::new(self.store.clone()));
        session.open(doc.clone())?;

        let mut cache = self.sessions.lock().unwrap();
        // A concurrent request may have won the race; its session is as good.
        if let Some(existing) = cache.open.get(doc) {
            return Ok(existing.clone());
        }
        cache.open.insert(doc.clone(), session.clone());
        cache.order.push_back(doc.clone());
        while cache.order.len() > SESSION_CACHE {
            if let Some(oldest) = cache.order.pop_front() {
                cache.open.remove(&oldest);
            }
        }
        Ok(session)
    }
}

pub struct AppCtx {
    root: PathBuf,
    /// Templates every new workspace starts with. A demo whose launcher is
    /// empty demonstrates nothing; a deployment can leave this unset.
    seed: Option<PathBuf>,
    pub config: Arc<dyn HostConfig>,
    pub tokens: TokenStore,
    pub events: EventBus,
    pub exports: ExportStore,
    pub meter: Meter,
    /// Origin the app frame loads from. Same-origin until the sandbox gets its
    /// own hostname, at which point this is the only thing that changes here.
    pub sandbox_origin: Option<String>,
    workspaces: Mutex<HashMap<TenantId, Arc<Workspace>>>,
}

impl AppCtx {
    pub fn new(
        root: PathBuf,
        config: Arc<dyn HostConfig>,
        sandbox_origin: Option<String>,
        agent_cap: u32,
    ) -> Self {
        Self {
            root,
            seed: None,
            config,
            tokens: TokenStore::default(),
            events: EventBus::default(),
            exports: ExportStore::default(),
            meter: Meter::new(agent_cap),
            sandbox_origin,
            workspaces: Mutex::new(HashMap::new()),
        }
    }

    /// Templates installed into every new workspace.
    pub fn with_seed(mut self, seed: Option<PathBuf>) -> Self {
        self.seed = seed;
        self
    }

    pub fn workspace(&self, tenant: &TenantId) -> Arc<Workspace> {
        let mut workspaces = self.workspaces.lock().unwrap();
        if let Some(existing) = workspaces.get(tenant) {
            return existing.clone();
        }
        let store: Arc<dyn DocStore> = Arc::new(TenantStore::new(self.root.join(tenant.as_str())));
        if let Some(seed) = &self.seed {
            seed_library(&*store, seed);
        }
        let workspace = Arc::new(Workspace::new(store));
        workspaces.insert(tenant.clone(), workspace.clone());
        workspace
    }
}

/// Install every template in `dir` into a fresh workspace. Best effort: a file
/// that is not a template app is skipped, not fatal — this is convenience, and
/// a broken seed must not stop someone from using the service.
fn seed_library(store: &dyn DocStore, dir: &Path) {
    let Ok(entries) = std::fs::read_dir(dir) else {
        tracing::warn!(dir = %dir.display(), "seed directory is not readable");
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if path.extension().and_then(|x| x.to_str()) != Some("clan") {
            continue;
        }
        match std::fs::read(&path)
            .map_err(|e| e.to_string())
            .and_then(|bytes| {
                napkin_host::library::install_app(store, bytes).map_err(|e| e.to_string())
            }) {
            Ok(app) => tracing::info!(app = %app.app_id, "seeded"),
            Err(e) => tracing::warn!(file = %path.display(), error = %e, "seed skipped"),
        }
    }
}
