// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! The one network primitive the sandbox may reach: a host-authenticated POST
//! to whatever endpoint a logical `request_kind` resolves to.

use serde_json::Value;

use crate::config::{resolve_proxy, HostConfig};
use crate::error::{HostError, HostResult};
use crate::session::Session;

/// POST `payload` verbatim to the endpoint resolved for `request_kind`, add
/// host-side auth, return a structured envelope. RAG, model choice, prompt
/// assembly — all backend concerns behind the endpoint.
pub async fn proxy_call(cfg: &dyn HostConfig, request_kind: &str, payload: Value) -> Value {
    let (url, auth_kind, key) = resolve_proxy(cfg, request_kind);
    if url.trim().is_empty() {
        return serde_json::json!({ "ok": false, "status": 0, "error": format!("no endpoint configured for kind '{request_kind}'") });
    }
    let client = reqwest::Client::new();
    let mut rb = client.post(&url).json(&payload);
    if let Some(k) = &key {
        rb = match auth_kind.as_deref() {
            Some("bearer") => rb.bearer_auth(k),
            _ => rb.header("x-api-key", k),
        };
    }
    match rb.send().await {
        Ok(resp) => {
            let status = resp.status();
            let body = resp.text().await.unwrap_or_default();
            let data: Value = serde_json::from_str(&body).unwrap_or(Value::String(body));
            serde_json::json!({
                "ok": status.is_success(),
                "status": status.as_u16(),
                "endpoint": url,
                // Don't surface upstream error bodies verbatim to the sandbox.
                "data": if status.is_success() { data } else { Value::Null },
                "error": if status.is_success() { Value::Null } else { Value::String(format!("upstream returned {}", status.as_u16())) },
            })
        }
        Err(e) => serde_json::json!({
            "ok": false, "status": 0, "endpoint": url,
            "error": format!("could not reach {url}: {e}"),
        }),
    }
}

/// `POST /api-proxy {request_kind, payload}` — the single, uniform network
/// route. Keys stay host-side; `payload` is forwarded verbatim, enriched with
/// the open artifact's intelligence layer so lineage, decisions, schema and
/// context travel to the agent — not just what the iframe sent.
pub async fn api_proxy(session: &Session, cfg: &dyn HostConfig, body: &str) -> HostResult<Value> {
    let req: Value = serde_json::from_str(body)
        .map_err(|e| HostError::bad_request(format!("invalid JSON: {e}")))?;
    let kind = req
        .get("request_kind")
        .and_then(|v| v.as_str())
        .unwrap_or("agent")
        .to_string();
    let mut payload = req.get("payload").cloned().unwrap_or(Value::Null);
    // Splice each attachment's cached extracted text (the `.extracted/` sidecar)
    // into the payload so the agent reads document contents, not just filenames.
    session.attach_extracted_text(&mut payload);
    let clan_ctx = session.clan_context_for_agent();
    let outgoing =
        serde_json::json!({ "request_kind": kind, "payload": payload, "clan": clan_ctx });
    Ok(proxy_call(cfg, &kind, outgoing).await)
}

/// Home-screen prompt → the unified proxy with `request_kind = "agent"`.
pub async fn agent_prompt(cfg: &dyn HostConfig, text: &str) -> Value {
    proxy_call(cfg, "agent", serde_json::json!({ "input": text })).await
}
