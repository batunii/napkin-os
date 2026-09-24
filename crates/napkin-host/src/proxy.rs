// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! The one network primitive the sandbox may reach: a host-authenticated POST
//! to whatever endpoint a logical `request_kind` resolves to.

use serde_json::Value;

use crate::config::{configured, resolve_proxy, HostConfig};
use crate::ctx::Ctx;
use crate::error::{HostError, HostResult};
use crate::event::HostEvent;
use crate::ops::middleware;
use crate::session::Session;

/// POST `payload` verbatim to the endpoint resolved for `request_kind`, add
/// host-side auth, return a structured envelope. RAG, model choice, prompt
/// assembly — all backend concerns behind the endpoint.
pub async fn proxy_call(cfg: &dyn HostConfig, request_kind: &str, payload: Value) -> Value {
    // The middleware never falls back to the agent URL: that endpoint does not
    // speak napkin.middleware/1, and sending it the request (and the document)
    // to refuse its answer afterwards is the fall-through M4 forbids. The app
    // is told plainly that none is configured.
    if let Some(refused) = unconfigured(cfg, request_kind) {
        return refused;
    }
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
            let status = resp.status().as_u16();
            let body = resp.text().await.unwrap_or_default();
            reply_envelope(request_kind, &url, status, body)
        }
        Err(e) => serde_json::json!({
            "ok": false, "status": 0, "endpoint": url,
            "error": format!("could not reach {url}: {e}"),
        }),
    }
}

/// The envelope for a request this host will not send: a middleware request
/// with no middleware configured.
pub fn unconfigured(cfg: &dyn HostConfig, request_kind: &str) -> Option<Value> {
    (request_kind == middleware::REQUEST_KIND && !configured(cfg, request_kind))
        .then(middleware::no_middleware)
}

/// The envelope the app gets for an upstream answer with `status` and `body`.
///
/// Upstream error bodies are not surfaced verbatim to the sandbox: on a
/// non-2xx the app sees the status and `upstream returned <status>`. The one
/// exception is `request_kind: "middleware"`, whose error bodies are part of
/// `napkin.middleware/1` (§4) and carry no request content: its
/// `{error: {type, message}}` is passed through as the envelope's `error`,
/// those two strings and nothing else, so the app can tell `invalid_input`
/// from `unknown_job`. A middleware error body that is not that shape falls
/// back to the generic message.
pub fn reply_envelope(request_kind: &str, url: &str, status: u16, body: String) -> Value {
    let ok = (200..300).contains(&status);
    let data: Value = serde_json::from_str(&body).unwrap_or(Value::String(body));
    let error = if ok {
        Value::Null
    } else {
        (request_kind == middleware::REQUEST_KIND)
            .then(|| middleware_error(&data))
            .flatten()
            .unwrap_or_else(|| Value::String(format!("upstream returned {status}")))
    };
    serde_json::json!({
        "ok": ok,
        "status": status,
        "endpoint": url,
        "data": if ok { data } else { Value::Null },
        "error": error,
    })
}

/// `{type, message}` out of a middleware error body, when it is one.
fn middleware_error(body: &Value) -> Option<Value> {
    let e = body.get("error")?;
    let kind = e.get("type")?.as_str()?;
    let message = e.get("message").and_then(Value::as_str).unwrap_or("");
    Some(serde_json::json!({ "type": kind, "message": message }))
}

/// `POST /api-proxy {request_kind, payload}` — the single, uniform network
/// route. Keys stay host-side; `payload` is forwarded verbatim, enriched with
/// the open artifact's intelligence layer so lineage, decisions, schema and
/// context travel to the agent — not just what the iframe sent.
///
/// `request_kind: "middleware"` is the one kind whose reply the host acts on:
/// see [`Session::settle_middleware`]. Every other kind comes back to the app
/// exactly as the endpoint answered, with no events.
pub async fn api_proxy(
    ctx: &Ctx,
    session: &Session,
    cfg: &dyn HostConfig,
    body: &str,
) -> HostResult<(Value, Vec<HostEvent>)> {
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
    let reply = proxy_call(cfg, &kind, outgoing).await;
    if kind == middleware::REQUEST_KIND {
        return Ok(session.settle_middleware(ctx, reply));
    }
    Ok((reply, Vec::new()))
}

/// Home-screen prompt → the unified proxy with `request_kind = "agent"`.
pub async fn agent_prompt(cfg: &dyn HostConfig, text: &str) -> Value {
    proxy_call(cfg, "agent", serde_json::json!({ "input": text })).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::NoConfig;

    #[test]
    fn a_middleware_request_never_falls_back_to_the_agent_url() {
        assert!(unconfigured(&NoConfig, "agent").is_none());
        let env = unconfigured(&NoConfig, "middleware").unwrap();
        assert_eq!(env["ok"], false);
        assert_eq!(env["error"]["type"], middleware::NO_MIDDLEWARE);
        assert_eq!(env["endpoint"], Value::Null, "nothing was sent anywhere");
    }

    #[test]
    fn a_middleware_error_body_reaches_the_app() {
        let body = r#"{"error":{"type":"unknown_job","message":"no such job","detail":"x"}}"#;
        let env = reply_envelope("middleware", "http://m/v1/tasks", 404, body.into());
        assert_eq!(env["ok"], false);
        assert_eq!(env["status"], 404);
        assert_eq!(
            env["error"],
            serde_json::json!({ "type": "unknown_job", "message": "no such job" }),
            "type and message only"
        );
        assert_eq!(env["data"], Value::Null);
    }

    #[test]
    fn other_kinds_and_other_bodies_stay_hidden() {
        let body = r#"{"error":{"type":"invalid_input","message":"secret prompt text"}}"#;
        let env = reply_envelope("agent", "http://a", 400, body.into());
        assert_eq!(env["error"], "upstream returned 400");
        assert_eq!(env["data"], Value::Null);

        let env = reply_envelope("middleware", "http://m", 502, "<html>bad gateway".into());
        assert_eq!(env["error"], "upstream returned 502");

        let ok = reply_envelope("middleware", "http://m", 200, r#"{"api":"x"}"#.into());
        assert_eq!(ok["ok"], true);
        assert_eq!(ok["error"], Value::Null);
        assert_eq!(ok["data"]["api"], "x");
    }
}
