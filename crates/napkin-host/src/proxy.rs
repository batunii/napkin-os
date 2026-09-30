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

/// `POST /verify` — a person verifies a finding (Contract 4 §4, Contract 3
/// §6.2). The middleware writes it to the agency's knowledge first — a
/// `human:<id>` source and a synthesis fact citing it — and answers with the
/// pin; the host then records the verification and the pin as one change.
/// Without a middleware nothing is written: an unverifiable "verified" would
/// be a finding that never reached the layer.
pub async fn verify(
    ctx: &Ctx,
    session: &Session,
    cfg: &dyn HostConfig,
    body: &str,
) -> HostResult<(Value, Vec<HostEvent>)> {
    use crate::ops::review;
    let (finding, rationale) = review::parse_verify(body)?;
    let payload = session.read(|d| review::verify_request(ctx, d, &finding))?;
    let decision_id = payload["input"]["decision_id"]
        .as_str()
        .unwrap_or_default()
        .to_string();
    let outgoing = serde_json::json!({
        "request_kind": middleware::REQUEST_KIND,
        "payload": payload,
        "clan": session.clan_context_for_agent(),
    });
    let reply = proxy_call(cfg, middleware::REQUEST_KIND, outgoing).await;
    if reply.get("ok").and_then(Value::as_bool) != Some(true) {
        let why = match reply.get("error") {
            Some(Value::String(s)) => s.clone(),
            Some(e) => e
                .get("message")
                .and_then(Value::as_str)
                .unwrap_or("the middleware refused")
                .to_string(),
            None => "the middleware refused".to_string(),
        };
        return Err(HostError::new(
            502,
            format!("Not verified: the finding could not be written to the agency's knowledge ({why})."),
        ));
    }
    let data = reply.get("data").cloned().unwrap_or(Value::Null);
    middleware::check_api(&data)?;
    let pin = data
        .pointer("/result/pin")
        .filter(|p| p.is_object())
        .cloned()
        .ok_or_else(|| HostError::new(502, "the middleware answered verify_finding without a pin"))?;
    let source = data.pointer("/result/source").and_then(Value::as_str).map(String::from);
    let applied = session.perform(ctx, |c, d| {
        review::verify_finding(c, d, &finding, &rationale, &decision_id, &pin, source.as_deref())
    })?;
    let mut reply = applied.reply;
    reply["clan"] = session.document_now().unwrap_or(Value::Null);
    Ok((reply, applied.events))
}

/// `POST /correct` — a person corrects a fact. Like [`verify`]: the
/// middleware writes the person's value to the agency's knowledge first (a
/// `human:<id>` source, a new row for the same fact), then the host pins it
/// and keeps the old pin, marked replaced.
pub async fn correct(
    ctx: &Ctx,
    session: &Session,
    cfg: &dyn HostConfig,
    body: &str,
) -> HostResult<(Value, Vec<HostEvent>)> {
    use crate::ops::review;
    let input = review::parse_correct(body)?;
    let payload = session.read(|d| review::correct_request(ctx, d, &input))?;
    let decision_id = payload["input"]["decision_id"].as_str().unwrap_or_default().to_string();
    let outgoing = serde_json::json!({
        "request_kind": middleware::REQUEST_KIND,
        "payload": payload,
        "clan": session.clan_context_for_agent(),
    });
    let reply = proxy_call(cfg, middleware::REQUEST_KIND, outgoing).await;
    if reply.get("ok").and_then(Value::as_bool) != Some(true) {
        let why = match reply.get("error") {
            Some(Value::String(s)) => s.clone(),
            Some(e) => e.get("message").and_then(Value::as_str).unwrap_or("the middleware refused").to_string(),
            None => "the middleware refused".to_string(),
        };
        return Err(HostError::new(502, format!("Not corrected: the value could not be written to the agency's knowledge ({why}).")));
    }
    let data = reply.get("data").cloned().unwrap_or(Value::Null);
    middleware::check_api(&data)?;
    let pin = data
        .pointer("/result/pin")
        .filter(|p| p.is_object())
        .cloned()
        .ok_or_else(|| HostError::new(502, "the middleware answered correct_fact without a pin"))?;
    let source = data.pointer("/result/source_record").cloned();
    let (fact, _, _, rationale) = &input;
    let applied = session.perform(ctx, |c, d| {
        review::correct_fact(c, d, fact, rationale, &decision_id, &pin, source.as_ref())
    })?;
    let mut reply = applied.reply;
    reply["clan"] = session.document_now().unwrap_or(Value::Null);
    Ok((reply, applied.events))
}

/// `POST /client-review` — a client's answer to the locked document
/// (Contract 4 §8.2, item 1). When the parts are not marked and there are
/// words to read, the middleware's `find_client_parts` (Ellis) is asked first
/// which parts they were about, as `/verify` asks `verify_finding`; the host
/// then checks every suggestion again and records the answer with what
/// survives. No middleware, or one that fails, and the answer is recorded
/// without suggestions — the reply says why.
pub async fn client_review(
    ctx: &Ctx,
    session: &Session,
    cfg: &dyn HostConfig,
    body: &str,
) -> HostResult<(Value, Vec<HostEvent>)> {
    use crate::ops::client_review::{self as cr, Ellis};
    let input = cr::ClientReview::parse(body)?;
    let ask = session.read(|d| cr::request(ctx, d, &input))?;
    let ellis = match ask {
        None => Ellis::Unavailable(String::new()),
        Some(payload) => {
            let outgoing = serde_json::json!({
                "request_kind": middleware::REQUEST_KIND,
                "payload": payload,
                "clan": session.clan_context_for_agent(),
            });
            let reply = proxy_call(cfg, middleware::REQUEST_KIND, outgoing).await;
            if reply.get("ok").and_then(Value::as_bool) == Some(true) {
                Ellis::Answered(reply.get("data").cloned().unwrap_or(Value::Null))
            } else {
                let why = match reply.get("error") {
                    Some(Value::String(s)) => s.clone(),
                    Some(e) => e.get("message").and_then(Value::as_str).unwrap_or("the middleware refused").to_string(),
                    None => "the middleware refused".to_string(),
                };
                Ellis::Unavailable(format!("Ellis could not be asked which parts were meant ({why})"))
            }
        }
    };
    let applied = session.perform(ctx, |c, d| cr::record(c, d, input, ellis))?;
    let mut reply = applied.reply;
    reply["clan"] = session.document_now().unwrap_or(Value::Null);
    Ok((reply, applied.events))
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
