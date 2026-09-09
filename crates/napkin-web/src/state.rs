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
use std::path::PathBuf;
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
            config,
            tokens: TokenStore::default(),
            events: EventBus::default(),
            exports: ExportStore::default(),
            meter: Meter::new(agent_cap),
            sandbox_origin,
            workspaces: Mutex::new(HashMap::new()),
        }
    }

    pub fn workspace(&self, tenant: &TenantId) -> Arc<Workspace> {
        self.workspaces
            .lock()
            .unwrap()
            .entry(tenant.clone())
            .or_insert_with(|| {
                let root = self.root.join(tenant.as_str());
                Arc::new(Workspace::new(Arc::new(TenantStore::new(root))))
            })
            .clone()
    }
}
