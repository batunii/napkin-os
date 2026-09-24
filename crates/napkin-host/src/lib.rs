// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! # napkin-host
//!
//! Everything Napkin Studio OS does with a `.clan` file that is not drawing
//! pixels: opening a document, the `clan://` API an app inside the file calls,
//! the app library, export, and the inference proxy.
//!
//! Nothing here knows what a shell is, and nothing here holds a document. An
//! operation ([`ops`]) takes a [`ctx::Ctx`] saying who is asking and a
//! [`document::Document`] — a snapshot at a version — and returns the
//! [`document::Change`]s it implies; a store applies them through
//! [`store::PartStore::apply`], the single write funnel, and a
//! [`store::Library`] names where documents go. [`session::Session`] is the
//! thin wrapper the desktop and browser shells use to hold one open snapshot
//! and apply its changes; the routing table ([`routes`]) runs on top of it and
//! returns bytes plus [`event::HostEvent`]s for the shell to act on. The
//! desktop binds that to a custom URI scheme and Tauri events; a web server
//! binds the same table to HTTP.

pub mod config;
pub mod ctx;
pub mod document;
pub mod error;
pub mod event;
#[cfg(feature = "native")]
pub mod export;
pub mod html;
pub mod library;
pub mod log;
pub mod ops;
#[cfg(feature = "native")]
pub mod proxy;
pub mod routes;
pub mod session;
pub mod store;
pub mod view;

#[cfg(feature = "native")]
pub use config::FsConfig;
pub use config::{agent_base_url, resolve_proxy, HostConfig, NoConfig, WorkspaceConfig};
pub use ctx::{Actor, Ctx, Scope};
pub use document::{Base, Change, Document, Version};
pub use error::{HostError, HostResult};
pub use event::HostEvent;
pub use library::{
    create_instance, ensure_home, home_change, install_app, install_change, instance_change,
    scan_apps, scan_recent, spinoff_change, spinoff_document, spinoff_targets, InstalledApp,
    RecentDoc, SpinoffTarget,
};
pub use ops::Outcome;
pub use routes::{
    dispatch, dispatch_async, handle, handle_async, is_async, HostRequest, HostResponse,
};
pub use session::{AppMeta, Applied, LineageInfo, ManifestInfo, OpenResult, Session, ViewState};
#[cfg(feature = "native")]
pub use store::FsStore;
pub use store::{DocId, DocStore, Library, PartStore};
pub use view::{LibraryView, ViewSource};
