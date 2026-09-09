// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! File logger (writes to /tmp/clan-debug.log).

#[cfg(not(feature = "native"))]
pub fn log(_msg: &str) {}

#[cfg(feature = "native")]
use std::io::Write;

#[cfg(feature = "native")]
pub fn log(msg: &str) {
    let ts = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0);
    if let Ok(mut f) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open("/tmp/clan-debug.log")
    {
        let _ = writeln!(f, "[{ts}] {msg}");
    }
}
