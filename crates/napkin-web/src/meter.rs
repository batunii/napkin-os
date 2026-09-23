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

/// Whether an `/api-proxy` body is charged to the quota.
///
/// Only work is charged: every call is, except a `request_kind: middleware`
/// call whose `payload.task` is `job_status` — the poll of a job already
/// paid for when it was submitted (owner decision, chat intake). A long job
/// such as `start_campaign` is polled for minutes; charging each poll would
/// spend a session's budget on waiting. `start_campaign`, `answer_question`,
/// `compose_report` and every other task are submissions and are charged.
///
/// The body is peeked the way the host will read it (`serde_json` over the
/// same bytes), and anything short of an unambiguous poll is charged: a body
/// that is not JSON, not an object, names no kind or another kind, or has no
/// `payload.task`. A malformed request is never free.
pub fn charged(body: &[u8]) -> bool {
    let Ok(req) = serde_json::from_slice::<serde_json::Value>(body) else {
        return true;
    };
    let kind = req.get("request_kind").and_then(|v| v.as_str());
    let task = req
        .get("payload")
        .and_then(|p| p.get("task"))
        .and_then(|v| v.as_str());
    !(kind == Some(napkin_host::ops::middleware::REQUEST_KIND)
        && task == Some(napkin_host::ops::middleware::JOB_STATUS))
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

    #[test]
    fn only_a_job_status_poll_is_free() {
        let free = br#"{"request_kind":"middleware","payload":{"task":"job_status","input":{"job_id":"job_1"}}}"#;
        assert!(!charged(free));
        for body in [
            &br#"{"request_kind":"middleware","payload":{"task":"start_campaign","input":{}}}"#[..],
            br#"{"request_kind":"middleware","payload":{"task":"answer_question","input":{}}}"#,
            br#"{"request_kind":"middleware","payload":{"task":"compose_report","input":{}}}"#,
            br#"{"request_kind":"agent","payload":{"task":"job_status"}}"#,
            br#"{"payload":{"task":"job_status"}}"#,
            br#"{"request_kind":"middleware","payload":{}}"#,
            br#"{"request_kind":"middleware","payload":{"task":["job_status"]}}"#,
            br#"{"request_kind":"middleware","task":"job_status"}"#,
            br#"["job_status"]"#,
            br#"{"request_kind":"middleware","payload":{"task":"job_status"}"#,
            b"",
        ] {
            assert!(charged(body), "{}", String::from_utf8_lossy(body));
        }
    }
}
