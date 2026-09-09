// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! AI inference proxy: workspace config + host-side secrets.
//!
//! Keys NEVER live in the artifact or the template. The template issues a
//! `clan://api-proxy` call naming only a logical `request_kind`; the host
//! resolves endpoint/model/secret from per-user config and makes the
//! authenticated call.

#[cfg(feature = "native")]
use std::path::PathBuf;

use serde::Deserialize;

#[derive(Deserialize, Default, Clone)]
pub struct WorkspaceConfig {
    #[serde(default)]
    pub proxies: std::collections::HashMap<String, ProxyConfig>,
    /// The single, uniform agent endpoint the home-screen prompt posts to.
    /// One place to change: localhost for testing, a hosted URL in production.
    #[serde(default)]
    pub agent_url: Option<String>,
}

#[derive(Deserialize, Clone)]
pub struct ProxyConfig {
    pub endpoint: String,
    #[serde(default)]
    pub auth_kind: Option<String>, // "x-api-key" (default) | "bearer"
    #[serde(default)]
    pub secret_ref: Option<String>,
}

/// Where a host finds workspace settings and secrets. The desktop reads two
/// YAML files out of the per-user config dir; a hosted deployment reads its
/// environment or a secret manager, and never exposes either to the sandbox.
pub trait HostConfig: Send + Sync {
    fn workspace(&self) -> Option<WorkspaceConfig>;
    fn secret(&self, secret_ref: &str) -> Option<String>;
}

/// Config from `<config_dir>/workspace.yaml` and `<config_dir>/secrets.yaml`.
#[cfg(feature = "native")]
pub struct FsConfig {
    config_dir: PathBuf,
}

#[cfg(feature = "native")]
impl FsConfig {
    pub fn new(config_dir: PathBuf) -> Self {
        Self { config_dir }
    }
}

#[cfg(feature = "native")]
impl HostConfig for FsConfig {
    fn workspace(&self) -> Option<WorkspaceConfig> {
        let bytes = std::fs::read(self.config_dir.join("workspace.yaml")).ok()?;
        serde_yaml::from_slice(&bytes).ok()
    }

    fn secret(&self, secret_ref: &str) -> Option<String> {
        let bytes = std::fs::read(self.config_dir.join("secrets.yaml")).ok()?;
        let map: std::collections::HashMap<String, String> = serde_yaml::from_slice(&bytes).ok()?;
        map.get(secret_ref).cloned()
    }
}

/// A host with no configuration at all — every kind falls back to the default
/// agent URL (or `NAPKIN_AGENT_URL`).
pub struct NoConfig;

impl HostConfig for NoConfig {
    fn workspace(&self) -> Option<WorkspaceConfig> {
        None
    }
    fn secret(&self, _secret_ref: &str) -> Option<String> {
        None
    }
}

// ── The uniform agent endpoint ───────────────────────────────────────────────
//
// The home page sends the user's prompt to ONE uniform agent endpoint. The base
// URL has a single source of truth so it can point at a localhost dev server now
// and a hosted agent later with a one-value change — no code edits, no CSP
// fuss (the host makes the request, not the sandboxed webview).
//   precedence: NAPKIN_AGENT_URL env  →  workspace.yaml `agent_url`  →  default

pub const DEFAULT_AGENT_URL: &str = "http://localhost:8787";

pub fn agent_base_url(cfg: &dyn HostConfig) -> String {
    if let Some(v) = std::env::var_os("NAPKIN_AGENT_URL") {
        if let Ok(s) = v.into_string() {
            if !s.trim().is_empty() {
                return s;
            }
        }
    }
    if let Some(u) = cfg.workspace().and_then(|c| c.agent_url) {
        if !u.trim().is_empty() {
            return u;
        }
    }
    DEFAULT_AGENT_URL.to_string()
}

/// Resolve a `request_kind` to its endpoint + auth. A kind configured in
/// `workspace.yaml` wins; otherwise everything falls back to the single uniform
/// agent URL (env / `agent_url` / default) so one value serves every kind in
/// dev. Returns `(url, auth_kind, key)`.
pub fn resolve_proxy(cfg: &dyn HostConfig, kind: &str) -> (String, Option<String>, Option<String>) {
    if let Some(ws) = cfg.workspace() {
        if let Some(p) = ws.proxies.get(kind) {
            let key = p.secret_ref.as_ref().and_then(|r| cfg.secret(r));
            return (p.endpoint.clone(), p.auth_kind.clone(), key);
        }
    }
    (agent_base_url(cfg), None, None)
}
