// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! Host → shell, over one SSE stream per tenant.
//!
//! On the desktop a [`HostEvent`] becomes a Tauri event. Here it becomes an SSE
//! frame under the same name, so the shell's listener list is identical on both
//! and the route table stays the only source of truth for what can be raised.

use std::collections::HashMap;
use std::sync::Mutex;

use napkin_host::HostEvent;
use serde_json::Value;
use tokio::sync::broadcast;

use crate::tenant::TenantId;

/// Enough that a burst (a save that emits several events while a tab is
/// mid-reconnect) is not dropped; small enough to bound memory per tenant.
const BACKLOG: usize = 64;

#[derive(Clone, Debug)]
pub struct ServerEvent {
    pub name: String,
    pub data: Value,
}

#[derive(Default)]
pub struct EventBus {
    channels: Mutex<HashMap<TenantId, broadcast::Sender<ServerEvent>>>,
}

impl EventBus {
    fn channel(&self, tenant: &TenantId) -> broadcast::Sender<ServerEvent> {
        self.channels
            .lock()
            .unwrap()
            .entry(tenant.clone())
            .or_insert_with(|| broadcast::channel(BACKLOG).0)
            .clone()
    }

    pub fn subscribe(&self, tenant: &TenantId) -> broadcast::Receiver<ServerEvent> {
        self.channel(tenant).subscribe()
    }

    /// Publish one event. A tenant with no tab open has no receivers, and
    /// dropping the event on the floor is the right outcome — these are
    /// requests to a live shell, not durable state.
    pub fn publish(&self, tenant: &TenantId, event: &HostEvent) {
        let _ = self.channel(tenant).send(ServerEvent {
            name: event.name().to_string(),
            data: event.payload(),
        });
    }

    pub fn publish_all(&self, tenant: &TenantId, events: &[HostEvent]) {
        for e in events {
            self.publish(tenant, e);
        }
    }
}
