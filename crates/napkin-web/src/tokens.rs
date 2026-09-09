// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! Capability tokens for the app sandbox.
//!
//! App HTML is third-party code. It runs in a frame with no ambient authority —
//! no cookie reaches it, and (once it is on its own origin) no same-origin
//! access to the shell either. What it gets instead is a token in its own URL,
//! good for exactly one document of one tenant, for a while. The app's JS can
//! read that token, and that is fine: it *is* precisely the authority the app
//! is supposed to have.

use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use napkin_host::DocId;

use crate::tenant::TenantId;

/// Long enough to survive a working session on one document, short enough that
/// a token pasted somewhere it should not be goes stale.
const TTL: Duration = Duration::from_secs(12 * 60 * 60);

#[derive(Clone)]
pub struct Grant {
    pub tenant: TenantId,
    pub doc: DocId,
}

struct Entry {
    grant: Grant,
    expires: Instant,
}

#[derive(Default)]
pub struct TokenStore {
    entries: Mutex<HashMap<String, Entry>>,
}

impl TokenStore {
    /// Mint (or re-use) the token for one tenant's view of one document.
    ///
    /// Re-use keeps a reload from stranding the previous token, and means the
    /// set never grows with page views — only with documents.
    pub fn mint(&self, tenant: &TenantId, doc: &DocId) -> String {
        let mut entries = self.entries.lock().unwrap();
        let now = Instant::now();
        entries.retain(|_, e| e.expires > now);

        if let Some((token, entry)) = entries
            .iter_mut()
            .find(|(_, e)| &e.grant.tenant == tenant && &e.grant.doc == doc)
        {
            entry.expires = now + TTL;
            return token.clone();
        }

        let token = uuid::Uuid::new_v4().simple().to_string();
        entries.insert(
            token.clone(),
            Entry {
                grant: Grant {
                    tenant: tenant.clone(),
                    doc: doc.clone(),
                },
                expires: now + TTL,
            },
        );
        token
    }

    pub fn resolve(&self, token: &str) -> Option<Grant> {
        let entries = self.entries.lock().unwrap();
        entries
            .get(token)
            .filter(|e| e.expires > Instant::now())
            .map(|e| e.grant.clone())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_token_names_one_document_of_one_tenant() {
        let store = TokenStore::default();
        let a = TenantId::mint();
        let b = TenantId::mint();
        let doc = DocId::new("doc-x");

        let ta = store.mint(&a, &doc);
        let tb = store.mint(&b, &doc);
        assert_ne!(ta, tb, "the same document for two tenants is two grants");

        let g = store.resolve(&ta).unwrap();
        assert_eq!(g.tenant, a);
        assert_eq!(g.doc, doc);
        assert_eq!(store.resolve(&tb).unwrap().tenant, b);
        assert!(store.resolve("not-a-token").is_none());
    }

    #[test]
    fn reopening_a_document_reuses_its_token() {
        let store = TokenStore::default();
        let t = TenantId::mint();
        let doc = DocId::new("doc-x");
        assert_eq!(store.mint(&t, &doc), store.mint(&t, &doc));
        assert_ne!(store.mint(&t, &doc), store.mint(&t, &DocId::new("doc-y")));
    }
}
