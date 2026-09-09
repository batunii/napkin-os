// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! # napkin-host
//!
//! Everything Napkin Studio OS does with a `.clan` file that is not drawing
//! pixels: opening a document, the `clan://` API an app inside the file calls,
//! the app library, export, and the inference proxy.
//!
//! Nothing here knows what a shell is. A handler takes a [`session::Session`]
//! and a [`config::HostConfig`], reaches storage only through a
//! [`store::DocStore`], and returns bytes plus a list of [`event::HostEvent`]s
//! for the shell to act on. The desktop shell binds that to a custom URI scheme
//! and Tauri events; a web server binds the same table to HTTP.

pub mod config;
pub mod error;
pub mod event;
#[cfg(feature = "native")]
pub mod export;
pub mod html;
pub mod library;
pub mod log;
pub mod prompt;
#[cfg(feature = "native")]
pub mod proxy;
pub mod routes;
pub mod session;
pub mod store;

#[cfg(feature = "native")]
pub use config::FsConfig;
pub use config::{agent_base_url, resolve_proxy, HostConfig, NoConfig, WorkspaceConfig};
pub use error::{HostError, HostResult};
pub use event::HostEvent;
pub use library::{
    create_instance, ensure_home, install_app, scan_apps, scan_recent, InstalledApp, RecentDoc,
};
pub use prompt::{build as build_prompt, AgentPrompt};
pub use routes::{handle, handle_async, is_async, HostRequest, HostResponse};
pub use session::{AppMeta, LineageInfo, ManifestInfo, OpenResult, Session};
#[cfg(feature = "native")]
pub use store::FsStore;
pub use store::{DocId, DocStore};
