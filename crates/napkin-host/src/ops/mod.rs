// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! The operations of the OS layer, as functions.
//!
//! `(Ctx, Document@version, input) → Outcome` for everything that changes a
//! document ([`edit`], and [`middleware`] for applying what the middleware
//! computed), and `Document → answer` for everything that only reads one
//! ([`read`], and [`decisions`] for the decision view). Nothing here holds state or writes: an [`Outcome`] carries
//! the [`Change`]s a caller may apply through
//! [`PartStore::apply`](crate::store::PartStore::apply), inside whatever
//! transaction it owns. That is what lets the desktop apply a change as a file
//! write and the server as one Postgres transaction without either copy of the
//! operation knowing which.

pub mod assemble;
pub mod client_review;
pub mod decisions;
pub mod edit;
pub mod members;
pub mod middleware;
pub mod read;
pub mod review;

use serde_json::Value;

use crate::document::Change;
use crate::event::HostEvent;

pub use edit::{attribute, attributed};

/// Cap on extracted text we cache + send, to bound the agent's token cost
/// (~6k tokens). The full asset always stays in the archive; this only limits
/// what the agent reads.
pub(crate) const MAX_EXTRACT_CHARS: usize = 24_000;

/// What an operation produced: the reply for whoever asked, and the changes
/// that would make it true. No changes means nothing needs writing — a no-op
/// edit, or a refusal the reply explains.
#[derive(Debug)]
pub struct Outcome {
    pub reply: Value,
    pub changes: Vec<Change>,
}

impl Outcome {
    pub fn changed(reply: Value, change: Change) -> Self {
        Self {
            reply,
            changes: vec![change],
        }
    }

    pub fn unchanged(reply: Value) -> Self {
        Self {
            reply,
            changes: Vec::new(),
        }
    }

    pub fn is_noop(&self) -> bool {
        self.changes.is_empty()
    }

    /// Every event the changes carry, in order — what the shell fans out once
    /// they have committed.
    pub fn events(&self) -> Vec<HostEvent> {
        self.changes
            .iter()
            .flat_map(|c| c.events.iter().cloned())
            .collect()
    }
}

/// RFC 7396 JSON Merge Patch applied in place — used only to test whether a
/// patch would actually change anything (the no-op guard).
fn json_merge(target: &mut Value, patch: &Value) {
    match patch {
        Value::Object(pm) => {
            if !target.is_object() {
                *target = Value::Object(Default::default());
            }
            let tm = target.as_object_mut().unwrap();
            for (k, v) in pm {
                if v.is_null() {
                    tm.remove(k);
                } else {
                    json_merge(tm.entry(k.clone()).or_insert(Value::Null), v);
                }
            }
        }
        _ => *target = patch.clone(),
    }
}

/// Reject asset names with path separators or traversal — the SDK does NOT
/// sanitize, so the host must.
pub fn sanitize_asset_name(name: &str) -> Option<String> {
    let n = name.trim();
    if n.is_empty() || n.contains("..") || n.contains('/') || n.contains('\\') {
        return None;
    }
    Some(n.to_string())
}

/// Pull readable text out of an uploaded asset so the agent sees document
/// *contents*, not just a filename. Plain-text family is decoded directly;
/// PDFs go through `pdf_extract` (wrapped in `catch_unwind` — malformed PDFs
/// can panic deep in the parser). Returns None for binary formats and empties.
fn extract_text(name: &str, bytes: &[u8]) -> Option<String> {
    let ext = name.rsplit('.').next().unwrap_or("").to_ascii_lowercase();
    let raw = match ext.as_str() {
        "txt" | "text" | "md" | "markdown" | "csv" | "tsv" | "json" | "yaml" | "yml" | "log" => {
            String::from_utf8_lossy(bytes).into_owned()
        }
        #[cfg(feature = "native")]
        "pdf" => {
            let owned = bytes.to_vec();
            std::panic::catch_unwind(move || pdf_extract::extract_text_from_mem(&owned).ok())
                .ok()
                .flatten()?
        }
        _ => return None,
    };
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return None;
    }
    Some(clamp_chars(trimmed, MAX_EXTRACT_CHARS))
}

fn clamp_chars(s: &str, max: usize) -> String {
    if s.chars().count() <= max {
        return s.to_string();
    }
    let mut out: String = s.chars().take(max).collect();
    out.push_str("\n\n[… truncated for length …]");
    out
}

pub fn content_type_for(rel: &str) -> &'static str {
    match rel
        .rsplit('.')
        .next()
        .map(|e| e.to_ascii_lowercase())
        .as_deref()
    {
        Some("png") => "image/png",
        Some("jpg") | Some("jpeg") => "image/jpeg",
        Some("gif") => "image/gif",
        Some("webp") => "image/webp",
        Some("svg") => "image/svg+xml",
        Some("pdf") => "application/pdf",
        Some("css") => "text/css",
        Some("js") => "text/javascript",
        Some("json") => "application/json",
        Some("woff2") => "font/woff2",
        Some("woff") => "font/woff",
        _ => "application/octet-stream",
    }
}
