// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// The contract between the shell and whatever is hosting it.
//
// Every call the UI makes into the host goes through `Host`; nothing in
// `src/` imports `@tauri-apps/api` directly. The desktop implementation maps
// these onto Tauri commands and events; a web build maps the same names onto
// HTTP and a server-sent event stream, and no component changes.

/** App metadata surfaced to the shell (launcher cards, "running an app" chrome). */
export interface AppMeta {
  name: string
  app_id: string
  version: string
  icon?: string | null
}

export interface LineageInfo {
  parent_id: string
  parent_uri: string
  parent_sha256?: string
  delta: string
}

export interface ManifestInfo {
  title: string
  id: string
  version: string
  created_at: string
  updated_at: string
  document_type?: string
  sha256: string
  file_count: number
  lineage?: LineageInfo
  app?: AppMeta
}

export interface OpenResult {
  /** The document's id in its store — a filesystem path on the desktop. */
  path: string
  manifest: ManifestInfo
  validation: string
  has_human_view: boolean
  render_model: 'authored' | 'legacy'
  is_template: boolean
  trusted: boolean
}

/** A template app installed in the local library, listed on the launcher. */
export interface InstalledApp {
  app_id: string
  name: string
  version: string
  path: string
  icon?: string | null
}

export interface RecentDoc {
  title: string
  path: string
  app_id?: string | null
  updated_at: string
}

/**
 * Events the host pushes at the shell. The names are the host's own
 * (`napkin_host::event::HostEvent::name`), so this map and the Rust route
 * table stay in step by construction.
 */
export interface HostEvents {
  /** The OS handed us a .clan to open (double-click, "Open with"). */
  'open-file': string
  /** A launch from inside a CLAN app, or a freshly instantiated document. */
  'clan-open-document': string
  'clan-open-file-request': null
  'clan-request-save': null
  'clan-export-request': { kind: string; filename: string; tmpHtml: string }
  'clan-title-changed': string
  'napkin-notify': { title: string; body: string }
  'clan-theme-changed': Record<string, string>
  /** Informational: a legacy fragment patch was saved. Never save in response (#9). */
  'clan-patch-saved': { id: string; content: string }
  'clan-data-changed': unknown
}

export type Unlisten = () => void

export interface Host {
  // ── Documents ─────────────────────────────────────────────────────────────
  openClan(path: string): Promise<OpenResult>
  openHome(): Promise<OpenResult>
  newDocumentFromApp(appId: string, title: string | null): Promise<OpenResult>
  /** The .clan path the app was launched with, if any. Cleared by reading it. */
  takeLaunchFile(): Promise<string | null>

  // ── Reading the open document ─────────────────────────────────────────────
  getHumanHtml(): Promise<string>
  getData(): Promise<string>
  getChain(): Promise<string>
  getAgentState(): Promise<string>
  getContext(): Promise<string>

  // ── The render surface ────────────────────────────────────────────────────
  setEditMode(active: boolean): Promise<void>
  updatePreviewHtml(html: string): Promise<void>
  savePatch(id: string, content: string): Promise<void>
  /**
   * Origin the sandboxed app frame reaches the clan:// API on. A custom URI
   * scheme on the desktop; a separate sandbox origin on the web, so a
   * third-party app's JS can never touch the shell's own.
   */
  clanOrigin(): string

  // ── The app library ───────────────────────────────────────────────────────
  listApps(): Promise<InstalledApp[]>
  installApp(srcPath: string): Promise<InstalledApp>

  // ── Getting bytes out ─────────────────────────────────────────────────────
  saveClanTo(path: string): Promise<void>
  exportCurrent(kind: 'html' | 'pdf', provenance: boolean, noBrand: boolean): Promise<void>
  finishExport(kind: string, tmpHtml: string, dest: string): Promise<string>
  /** Ask the user where a .clan or an export should go. `null` if cancelled. */
  pickSaveDestination(defaultName: string, ext: string): Promise<string | null>
  /** Ask the user for a .clan to open. `null` if cancelled. */
  pickClanToOpen(): Promise<string | null>

  // ── The agent ─────────────────────────────────────────────────────────────
  agentEndpoint(): Promise<string>
  agentPrompt(text: string): Promise<unknown>

  // ── Host → shell ──────────────────────────────────────────────────────────
  on<K extends keyof HostEvents>(
    event: K,
    handler: (payload: HostEvents[K]) => void,
  ): Promise<Unlisten>
}
