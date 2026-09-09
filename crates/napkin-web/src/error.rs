// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! `HostError` on the wire.
//!
//! The host already decided the status; this only carries it out, in the same
//! `{ ok: false, error }` shape the `clan://` surface has always used.

use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::Json;
use napkin_host::HostError;

pub struct ApiError(pub HostError);

pub type ApiResult<T> = Result<T, ApiError>;

impl From<HostError> for ApiError {
    fn from(e: HostError) -> Self {
        ApiError(e)
    }
}

impl ApiError {
    pub fn new(status: u16, message: impl Into<String>) -> Self {
        ApiError(HostError::new(status, message))
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let status =
            StatusCode::from_u16(self.0.status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR);
        (
            status,
            Json(serde_json::json!({ "ok": false, "error": self.0.message })),
        )
            .into_response()
    }
}
