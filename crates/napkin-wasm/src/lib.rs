// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! # napkin-wasm
//!
//! Napkin Studio OS with no server behind it.
//!
//! The same `napkin-host` the desktop and the web service run, compiled to
//! WebAssembly and handed to the page. Documents are real `.clan` archives —
//! created, patched with attribution, validated and exported by the same SDK —
//! they just never leave the browser.
//!
//! The shape is deliberately the same as the other two shells: a
//! [`DocStore`](napkin_host::DocStore) underneath, and one `handle` entry point
//! that takes a request and gives back a response plus the events the shell
//! must act on. What was HTTP on the server is a function call here.

mod store;

use std::sync::Arc;

use napkin_host::{library, DocId, DocStore, HostRequest, NoConfig, Session};
use wasm_bindgen::prelude::*;

use crate::store::MemStore;

/// What a route answered, on its way back to JavaScript.
#[derive(serde::Serialize)]
struct JsResponse {
    status: u16,
    headers: Vec<(String, String)>,
    body: Vec<u8>,
    /// Same names the desktop emits as Tauri events and the server pushes over
    /// SSE, so the shell's listener list does not change.
    events: Vec<JsEvent>,
}

#[derive(serde::Serialize)]
struct JsEvent {
    name: String,
    payload: serde_json::Value,
}

fn to_js<T: serde::Serialize>(value: &T) -> JsValue {
    serde_wasm_bindgen::to_value(value).unwrap_or(JsValue::NULL)
}

fn err(e: impl std::fmt::Display) -> JsValue {
    JsValue::from_str(&e.to_string())
}

/// One browser session's Napkin: a store, and the document currently open in it.
#[wasm_bindgen]
pub struct NapkinHost {
    store: Arc<MemStore>,
    session: Session,
}

#[wasm_bindgen]
impl NapkinHost {
    #[wasm_bindgen(constructor)]
    pub fn new() -> NapkinHost {
        // Without this a Rust panic surfaces as "unreachable executed", which
        // tells nobody anything.
        console_error_panic_hook::set_once();
        let store = Arc::new(MemStore::new());
        NapkinHost {
            store: store.clone(),
            session: Session::new(store),
        }
    }

    /// Install a template app from its packed bytes — fetched from wherever the
    /// site publishes them.
    #[wasm_bindgen(js_name = installApp)]
    pub fn install_app(&self, bytes: &[u8]) -> Result<JsValue, JsValue> {
        library::install_app(&*self.store, bytes.to_vec())
            .map(|a| to_js(&a))
            .map_err(err)
    }

    #[wasm_bindgen(js_name = listApps)]
    pub fn list_apps(&self) -> JsValue {
        to_js(&library::scan_apps(&*self.store))
    }

    #[wasm_bindgen(js_name = listRecent)]
    pub fn list_recent(&self) -> JsValue {
        to_js(&library::scan_recent(&*self.store))
    }

    /// Instantiate a working document from an installed app and open it.
    #[wasm_bindgen(js_name = newDocument)]
    pub fn new_document(&self, app_id: &str, title: Option<String>) -> Result<JsValue, JsValue> {
        let id = library::create_instance(&*self.store, app_id, title).map_err(err)?;
        self.session.open(id).map(|o| to_js(&o)).map_err(err)
    }

    /// The launcher, which is itself a CLAN app. Built here on first use — the
    /// SDK needs nothing but bytes, so there is nothing to download.
    #[wasm_bindgen(js_name = openHome)]
    pub fn open_home(&self) -> Result<JsValue, JsValue> {
        let id = library::ensure_home(&*self.store).map_err(err)?;
        self.session.open(id).map(|o| to_js(&o)).map_err(err)
    }

    /// Take an uploaded `.clan` into the store and open it.
    pub fn upload(&self, bytes: &[u8], suggested_id: &str) -> Result<JsValue, JsValue> {
        clan_sdk::ClanFile::from_bytes(bytes.to_vec())
            .map_err(|e| err(format!("not a readable .clan: {e}")))?;
        let id = DocId::new(format!("doc-{suggested_id}"));
        self.store.write(&id, bytes).map_err(err)?;
        self.session.open(id).map(|o| to_js(&o)).map_err(err)
    }

    pub fn open(&self, doc: &str) -> Result<JsValue, JsValue> {
        self.session
            .open(DocId::new(doc))
            .map(|o| to_js(&o))
            .map_err(err)
    }

    #[wasm_bindgen(js_name = humanHtml)]
    pub fn human_html(&self) -> Result<String, JsValue> {
        self.session.human_html().map_err(err)
    }

    pub fn entry(&self, path: &str) -> Result<String, JsValue> {
        self.session.entry_string(path).map_err(err)
    }

    /// The packed archive of the open document — the single-file handoff, and
    /// on the web the only way work leaves the browser.
    pub fn download(&self) -> Result<Vec<u8>, JsValue> {
        self.session.raw_bytes().map_err(err)
    }

    /// Compose a standalone document from the open `.clan` — bindings
    /// resolved, assets inlined, scripts stripped, brand chrome. The same SDK
    /// call every other shell makes; only the delivery differs, because there
    /// is no headless browser here to turn it into a PDF.
    #[wasm_bindgen(js_name = composeExport)]
    pub fn compose_export(&self, provenance: bool, no_brand: bool) -> Result<JsValue, JsValue> {
        let (html, filename) = self
            .session
            .compose_export(provenance, no_brand)
            .map_err(err)?;
        Ok(to_js(
            &serde_json::json!({ "html": html, "filename": filename }),
        ))
    }

    pub fn title(&self) -> Result<String, JsValue> {
        self.session.title().map_err(err)
    }

    #[wasm_bindgen(js_name = setEditMode)]
    pub fn set_edit_mode(&self, active: bool) {
        self.session.set_edit_mode(active);
    }

    #[wasm_bindgen(js_name = setPreviewHtml)]
    pub fn set_preview_html(&self, html: String) {
        self.session.set_preview_html(html);
    }

    /// The `clan://` surface, as a function call.
    ///
    /// `/patch-data`, `/assets/…`, `/chain`, `/apps`, `/launch` — the same
    /// table the desktop reaches through a custom URI scheme and the server
    /// reaches over HTTP. `/api-proxy` is the one route that does not arrive
    /// here: the page holds the credentials and answers it before we are asked.
    pub fn handle(&self, path: &str, query: &str, body: &[u8]) -> JsValue {
        let resp = napkin_host::handle(
            &self.session,
            &NoConfig,
            HostRequest::new(path, query, body.to_vec()),
        );
        to_js(&JsResponse {
            status: resp.status,
            headers: resp.headers,
            body: resp.body,
            events: resp
                .events
                .iter()
                .map(|e| JsEvent {
                    name: e.name().to_string(),
                    payload: e.payload(),
                })
                .collect(),
        })
    }
}

impl Default for NapkinHost {
    fn default() -> Self {
        Self::new()
    }
}
