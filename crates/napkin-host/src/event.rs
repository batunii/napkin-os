// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! Things a route needs the shell to do.
//!
//! A handler cannot open a file dialog or repaint a toolbar, so it returns the
//! request instead. The desktop shell turns each of these into a Tauri event of
//! the same name; a web shell will push the same names down a stream.

use serde_json::Value;

#[derive(Debug, Clone, PartialEq)]
pub enum HostEvent {
    /// A legacy HTML-fragment patch landed. Informational only — a listener
    /// that saves in response writes the file twice (#9).
    PatchSaved(Value),
    /// The data layer changed; views bound to it should refresh.
    DataChanged(Value),
    /// Open this document (a launch from inside a CLAN app, or an OS "open with").
    OpenDocument(String),
    /// The open document's title changed.
    TitleChanged(String),
    /// A document asks to be saved/exported (e.g. a locked brief).
    RequestSave,
    /// A document asks the shell to run its "open a file" flow.
    OpenFileRequest,
    /// Composed export HTML is waiting at `tmp_html`; the shell picks a
    /// destination and calls `finish_export`.
    ExportRequest {
        kind: String,
        filename: String,
        tmp_html: String,
    },
    /// A trusted app raised a notification.
    Notify { title: String, body: String },
    /// A trusted app recolored the shell chrome.
    ThemeChanged(Value),
}

impl HostEvent {
    /// The wire name. Stable — the shell listens for exactly these.
    pub fn name(&self) -> &'static str {
        match self {
            Self::PatchSaved(_) => "clan-patch-saved",
            Self::DataChanged(_) => "clan-data-changed",
            Self::OpenDocument(_) => "clan-open-document",
            Self::TitleChanged(_) => "clan-title-changed",
            Self::RequestSave => "clan-request-save",
            Self::OpenFileRequest => "clan-open-file-request",
            Self::ExportRequest { .. } => "clan-export-request",
            Self::Notify { .. } => "napkin-notify",
            Self::ThemeChanged(_) => "clan-theme-changed",
        }
    }

    pub fn payload(&self) -> Value {
        match self {
            Self::PatchSaved(v) | Self::DataChanged(v) | Self::ThemeChanged(v) => v.clone(),
            Self::OpenDocument(s) | Self::TitleChanged(s) => Value::String(s.clone()),
            Self::RequestSave | Self::OpenFileRequest => Value::Null,
            Self::ExportRequest {
                kind,
                filename,
                tmp_html,
            } => {
                serde_json::json!({ "kind": kind, "filename": filename, "tmpHtml": tmp_html })
            }
            Self::Notify { title, body } => serde_json::json!({ "title": title, "body": body }),
        }
    }
}
