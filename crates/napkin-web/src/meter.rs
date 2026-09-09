// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! Per-tenant agent quota.
//!
//! The keys are the server's, so the bill is too: a draft is roughly $0.30–0.40.
//! An uncapped demo link is a real invoice, so every call through the inference
//! proxy passes here first. The counter is in-process and resets on restart —
//! deliberately the simplest thing that makes the seam exist; a hosted
//! deployment swaps the body for a durable counter without moving the call site.

use std::collections::HashMap;
use std::sync::Mutex;

use crate::tenant::TenantId;

pub struct Meter {
    cap: u32,
    used: Mutex<HashMap<TenantId, u32>>,
}

#[derive(Clone, Copy, serde::Serialize)]
pub struct Usage {
    pub used: u32,
    pub cap: u32,
}

impl Meter {
    pub fn new(cap: u32) -> Self {
        Self {
            cap,
            used: Mutex::new(HashMap::new()),
        }
    }

    /// Charge one call, or refuse. Refusal is a 429 the shell can show.
    pub fn try_spend(&self, tenant: &TenantId) -> Result<Usage, Usage> {
        let mut used = self.used.lock().unwrap();
        let n = used.entry(tenant.clone()).or_insert(0);
        if *n >= self.cap {
            return Err(Usage {
                used: *n,
                cap: self.cap,
            });
        }
        *n += 1;
        Ok(Usage {
            used: *n,
            cap: self.cap,
        })
    }

    pub fn usage(&self, tenant: &TenantId) -> Usage {
        Usage {
            used: self.used.lock().unwrap().get(tenant).copied().unwrap_or(0),
            cap: self.cap,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_tenant_spends_its_own_budget_only() {
        let meter = Meter::new(2);
        let a = TenantId::mint();
        let b = TenantId::mint();

        assert!(meter.try_spend(&a).is_ok());
        assert!(meter.try_spend(&a).is_ok());
        assert!(meter.try_spend(&a).is_err(), "the cap must bite");
        assert!(meter.try_spend(&b).is_ok(), "another tenant is unaffected");
        assert_eq!(meter.usage(&a).used, 2);
    }
}
