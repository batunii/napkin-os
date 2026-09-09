// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! Composed exports waiting to be fetched.
//!
//! The host composes a standalone document and hands back a temp file, then
//! raises `ExportRequest` so the shell can decide where it goes. The desktop
//! answers with a save dialog. The browser cannot be handed a server path, so
//! the temp file is stashed here behind an opaque handle and the event carries
//! that instead — which keeps the shell contract identical on both.

use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use crate::tenant::TenantId;

/// An export the user never fetches is dead weight; a browser that is going to
/// fetch it does so immediately.
const TTL: Duration = Duration::from_secs(15 * 60);

struct Pending {
    tenant: TenantId,
    tmp_html: String,
    filename: String,
    expires: Instant,
}

#[derive(Default)]
pub struct ExportStore {
    pending: Mutex<HashMap<String, Pending>>,
}

impl ExportStore {
    pub fn stash(&self, tenant: &TenantId, tmp_html: String, filename: String) -> String {
        let handle = uuid::Uuid::new_v4().simple().to_string();
        let mut pending = self.pending.lock().unwrap();
        self.sweep(&mut pending);
        pending.insert(
            handle.clone(),
            Pending {
                tenant: tenant.clone(),
                tmp_html,
                filename,
                expires: Instant::now() + TTL,
            },
        );
        handle
    }

    /// Claim an export. Single use: the temp file is consumed by rendering it.
    /// A handle belonging to another tenant reads as absent.
    pub fn take(&self, tenant: &TenantId, handle: &str) -> Option<(String, String)> {
        let mut pending = self.pending.lock().unwrap();
        self.sweep(&mut pending);
        // Another tenant's handle reads as absent, not as forbidden.
        pending.get(handle).filter(|p| &p.tenant == tenant)?;
        let p = pending.remove(handle)?;
        Some((p.tmp_html, p.filename))
    }

    /// Drop what has expired, deleting the temp files with it.
    fn sweep(&self, pending: &mut HashMap<String, Pending>) {
        let now = Instant::now();
        pending.retain(|_, p| {
            let live = p.expires > now;
            if !live {
                let _ = std::fs::remove_file(&p.tmp_html);
            }
            live
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_handle_is_single_use_and_tenant_scoped() {
        let store = ExportStore::default();
        let a = TenantId::mint();
        let b = TenantId::mint();

        let handle = store.stash(&a, "/tmp/x.html".into(), "brief".into());
        assert!(
            store.take(&b, &handle).is_none(),
            "another tenant must not claim it"
        );
        assert_eq!(store.take(&a, &handle).unwrap().1, "brief");
        assert!(
            store.take(&a, &handle).is_none(),
            "a claimed export is gone"
        );
    }
}
