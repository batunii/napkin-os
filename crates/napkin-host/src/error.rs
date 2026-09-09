// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! One error type for every host operation, carrying the HTTP status the
//! `clan://` (and, later, the web) surface should answer with.
//!
//! `Display` is the bare message, so a shell that reports errors as plain
//! strings (`Result<T, String>` Tauri commands) produces exactly the text it
//! did when these functions lived in the shell.

use std::fmt;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HostError {
    pub status: u16,
    pub message: String,
}

pub type HostResult<T> = Result<T, HostError>;

impl HostError {
    pub fn new(status: u16, message: impl Into<String>) -> Self {
        Self {
            status,
            message: message.into(),
        }
    }
    /// 400 — the request itself is malformed.
    pub fn bad_request(message: impl Into<String>) -> Self {
        Self::new(400, message)
    }
    /// 404 — the named entry does not exist in the artifact.
    pub fn not_found(message: impl Into<String>) -> Self {
        Self::new(404, message)
    }
    /// 409 — the operation is impossible in the current state (typically
    /// "no file open", or a write that would clobber an existing branch).
    pub fn conflict(message: impl Into<String>) -> Self {
        Self::new(409, message)
    }
    /// 500 — the host failed (storage, serialization).
    pub fn internal(message: impl Into<String>) -> Self {
        Self::new(500, message)
    }

    /// The error a route raises when nothing is open. Its message is load
    /// bearing: the shell surfaces it verbatim.
    pub fn no_file_open() -> Self {
        Self::conflict("no file open")
    }
}

impl fmt::Display for HostError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.message)
    }
}

impl std::error::Error for HostError {}

/// Map an SDK error to an HTTP status: a namespace violation (writing a forked
/// branch through the direct path) is a 409; anything else a 422.
impl From<clan_sdk::Error> for HostError {
    fn from(e: clan_sdk::Error) -> Self {
        let status = match e {
            clan_sdk::Error::NamespaceViolation(_) => 409,
            _ => 422,
        };
        Self::new(status, e.to_string())
    }
}

impl From<HostError> for String {
    fn from(e: HostError) -> String {
        e.message
    }
}
