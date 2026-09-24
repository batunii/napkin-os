// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

//! Which copy of an app's view a document is shown with.
//!
//! "Latest app, same major" (owner decision, 2026-09-24): opening an instance
//! of an app serves the view of the app as it is installed in the library now,
//! matched on app id and major version — the M2 `handler: name@major` rule
//! applied to views. A fix or a restyle reaches every document of that app at
//! once; a breaking view change bumps the major, and documents of the old major
//! keep the copy they carry.
//!
//! The document still packs its own copy of the view. That copy is what the
//! host falls back to — silently for the user, logged for developers — when
//! the library has nothing it may use: the app is not installed, the installed
//! major differs, either version does not parse as `MAJOR[.MINOR[.PATCH]]`,
//! the document has no app block, it is a legacy agent-HTML document (its view
//! is its own, not an app's), or it is itself a template.
//!
//! What the library supplies is the view and nothing else: `human/index.html`,
//! `human/styles.css`, `human/export.html`, and any `human/assets/*` the
//! document does not hold itself. Data, schema, chain and the document's own
//! assets (uploads live in `human/assets/` too) are always the document's.
//!
//! **Trust.** A view served from the library is the library app's code, so the
//! trust the session grants it is the library template's — `verify_app` run on
//! the template — never the document's. A document carrying a valid signature
//! from an older version lends nothing to the code it is now shown with, and an
//! unsigned document gains trust only if the installed app is itself signed.
//! Apps are unsigned today; this is the rule for when they are not.
//!
//! **Handoff.** A packed copy handed out ("save as", the web download) and an
//! export carry the view the user saw: [`served_archive`] swaps the library
//! view into the outgoing bytes. That is a transform of what leaves, not a
//! write — the stored document never changes because of it, and no decision
//! is recorded.

use std::borrow::Cow;

use clan_sdk::{ClanBuilder, ClanFile, FileEntry, MANIFEST_PATH};
use serde::{Deserialize, Serialize};

use crate::document::Document;
use crate::error::HostResult;
use crate::log::log;
use crate::store::{DocId, DocStore};

/// The entries that are an app's view. Everything else in a document is the
/// document's, whichever view it is shown with.
const VIEW_ENTRIES: &[&str] = &["human/index.html", "human/styles.css", "human/export.html"];

const ENTRY: &str = "human/index.html";
const ASSETS: &str = "human/assets/";

/// Where the view a document is shown with came from.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ViewSource {
    /// The installed app's current view, same app id and major.
    Library,
    /// The copy the document carries.
    #[default]
    Document,
}

/// The installed template whose view a document is being shown with.
pub struct LibraryView {
    template: ClanFile,
    template_id: DocId,
    version: String,
    trusted: bool,
}

impl LibraryView {
    /// The installed template the view is read from.
    pub fn template(&self) -> &ClanFile {
        &self.template
    }

    /// Where the template lives in the library.
    pub fn template_id(&self) -> &DocId {
        &self.template_id
    }

    /// The installed app's version — the view's.
    pub fn version(&self) -> &str {
        &self.version
    }

    /// Whether the installed template verifies against the publisher key.
    pub fn trusted(&self) -> bool {
        self.trusted
    }
}

/// The major of a `MAJOR[.MINOR[.PATCH]]` version, every part plain digits.
/// Anything else — a pre-release tag, a `v` prefix, a fourth part — is `None`,
/// and a `None` on either side means the library view is not used.
pub fn major(version: &str) -> Option<u64> {
    let parts: Vec<&str> = version.trim().split('.').collect();
    if parts.is_empty() || parts.len() > 3 {
        return None;
    }
    if parts
        .iter()
        .any(|p| p.is_empty() || !p.bytes().all(|b| b.is_ascii_digit()))
    {
        return None;
    }
    parts[0].parse().ok()
}

/// `doc`, shown with the library's view of its app when the library has one
/// it may use, else as it is. `public_key` is the publisher key the library
/// template's trust is verified against — [`crate::session::NAPKIN_PUBLIC_KEY`]
/// for every shell today.
pub fn resolve(store: &dyn DocStore, doc: Document, public_key: &str) -> Document {
    match find(store, &doc, public_key) {
        Ok(view) => {
            log(&format!(
                "view: {} shown with library view {}@{} (from {})",
                doc.id(),
                doc.app_id().unwrap_or_default(),
                view.version,
                view.template_id
            ));
            doc.with_library_view(view)
        }
        Err(why) => {
            log(&format!(
                "view: {} shown with its own view: {why}",
                doc.id()
            ));
            doc
        }
    }
}

/// The library view `doc` may be shown with, or why not.
fn find(store: &dyn DocStore, doc: &Document, public_key: &str) -> Result<LibraryView, String> {
    let clan = doc.clan();
    let m = clan.manifest();
    if m.document_type.as_deref() == Some("template") {
        return Err("it is a template".into());
    }
    if m.view.as_ref().and_then(|v| v.source.as_deref()) != Some("app") {
        return Err("not an app-authored view (legacy)".into());
    }
    let app = m.app.as_ref().ok_or("no app block")?;
    let want = major(&app.version)
        .ok_or_else(|| format!("document app version {:?} does not parse", app.version))?;

    let template_id = store.app_template(&app.app_id);
    let bytes = store
        .read(&template_id)
        .map_err(|_| format!("{} is not installed", app.app_id))?;
    let template = ClanFile::from_bytes(bytes)
        .map_err(|e| format!("installed {} does not open: {e}", app.app_id))?;
    let tm = template.manifest();
    if tm.document_type.as_deref() != Some("template") {
        return Err(format!("installed {} is not a template", app.app_id));
    }
    let installed = tm
        .app
        .as_ref()
        .ok_or_else(|| format!("installed {} has no app block", app.app_id))?;
    if installed.app_id != app.app_id {
        return Err(format!(
            "installed template at {template_id} is {}, not {}",
            installed.app_id, app.app_id
        ));
    }
    let have = major(&installed.version).ok_or_else(|| {
        format!(
            "installed {} version {:?} does not parse",
            app.app_id, installed.version
        )
    })?;
    if have != want {
        return Err(format!(
            "installed {}@{} is major {have}, the document is major {want}",
            app.app_id, installed.version
        ));
    }
    if !template.has_entry(ENTRY) {
        return Err(format!("installed {} has no {ENTRY}", app.app_id));
    }
    let trusted = clan_sdk::verify_app(&template, public_key);
    Ok(LibraryView {
        version: installed.version.clone(),
        template,
        template_id,
        trusted,
    })
}

/// The archive to hand out for `doc`: its stored bytes when it is shown with
/// its own view, and otherwise those bytes with the library view swapped in —
/// the view entries replaced (or dropped, when the installed app has none) and
/// the template's assets added under any name the document does not already
/// hold. The manifest keeps the document's identity, app block and every other
/// field; only its file registry follows the swapped entries. Nothing is
/// written.
pub fn served_archive(doc: &Document) -> HostResult<Cow<'_, [u8]>> {
    let Some(view) = doc.library_view() else {
        return Ok(Cow::Borrowed(doc.bytes()));
    };
    let clan = doc.clan();
    let tpl = view.template();
    let tm = tpl.manifest();
    let mut manifest = clan.manifest().clone();

    let mut entries: Vec<(String, Vec<u8>)> = Vec::new();
    for (path, bytes) in clan.read_all_entries()? {
        if path == MANIFEST_PATH || VIEW_ENTRIES.contains(&path.as_str()) {
            continue;
        }
        entries.push((path, bytes));
    }

    for &path in VIEW_ENTRIES {
        if tpl.has_entry(path) {
            entries.push((path.to_string(), tpl.read_entry(path)?));
            register(&mut manifest.files, tm.file_by_path(path), path);
        } else {
            manifest.files.retain(|f| f.path != path);
        }
    }

    for path in tpl.entry_paths()? {
        if !path.starts_with(ASSETS) || clan.has_entry(&path) {
            continue;
        }
        entries.push((path.clone(), tpl.read_entry(&path)?));
        register(&mut manifest.files, tm.file_by_path(&path), &path);
    }

    let mut b = ClanBuilder::new(manifest);
    for (path, bytes) in entries {
        b.add_entry(path, bytes);
    }
    Ok(Cow::Owned(b.build()?))
}

/// Make sure `files` registers `path`, copying the template's entry when it has
/// one. An id already taken by another path gets a suffix, so the registry
/// never holds two entries under one id.
fn register(files: &mut Vec<FileEntry>, from: Option<&FileEntry>, path: &str) {
    if files.iter().any(|f| f.path == path) {
        return;
    }
    let Some(from) = from else { return };
    let mut entry = from.clone();
    entry.sha256 = None;
    let base = entry.id.clone();
    let mut n = 2;
    while files.iter().any(|f| f.id == entry.id) {
        entry.id = format!("{base}-{n}");
        n += 1;
    }
    files.push(entry);
}

#[cfg(test)]
mod tests {
    use super::major;

    #[test]
    fn majors_parse_only_plain_semver_shapes() {
        assert_eq!(major("1"), Some(1));
        assert_eq!(major("1.4"), Some(1));
        assert_eq!(major("2.0.3"), Some(2));
        assert_eq!(major(" 0.1.0 "), Some(0));
        for bad in ["", "v1.2.0", "1.2.3-beta", "1.2.3.4", "1..2", "x", "1.2.x"] {
            assert_eq!(major(bad), None, "{bad:?}");
        }
    }
}
