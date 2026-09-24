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

/**
 * An installed app that will take the open document as a spin-off source —
 * what "Continue in…" offers. `map` is the dotted key the document's data will
 * be grafted under, when the target app declares one.
 */
export interface SpinoffTarget {
  app_id: string
  name: string
  version: string
  icon?: string | null
  map?: string | null
}

export interface RecentDoc {
  title: string
  path: string
  app_id?: string | null
  updated_at: string
}

// ── The decision view (`napkin_host::ops::decisions`) ─────────────────────────

/** One point of evidence, and the ids it rests on. */
export interface ReasonPoint {
  point: string
  cites?: string[]
}

/** A decision's structured reasoning (OS-layer contract §3). */
export interface Reasoning {
  decided: string
  because: ReasonPoint[]
  rejected: { option: string; why: string }[]
  only_option?: string
  certainty: { level: 'high' | 'medium' | 'low' | string; why: string }
  would_change_if: string
  attention?: string
}

/** A decision as the chain holds it. Unknown fields ride along. */
export interface Decision {
  id?: string
  kind?: string
  agent: string
  actor?: string
  handler?: string
  backend?: string
  action: string
  targets?: string[]
  cites?: string[]
  superseded_by?: string
  polarity?: string
  reason_code?: string
  rationale: string
  reasoning?: Reasoning
  timestamp: string
  fields_changed?: string[]
  [extra: string]: unknown
}

/** Why something needs a person. `blocks_lock` items are the lock list. */
export interface AttentionReason {
  code:
    | 'flagged' | 'low_certainty' | 'open_contest' | 'unverified_finding'
    | 'flagged_field' | 'bad_verdict' | 'unmerged_branch'
  text: string
  blocks_lock: boolean
}

export interface AttentionItem extends AttentionReason {
  /** The decision it belongs to, when there is one. */
  decision?: string
  address?: string
  label?: string
}

export interface DecisionTarget {
  address: string
  path: string
  label: string
  kind: 'field' | 'contest' | 'finding' | 'fact' | 'decision' | 'document'
  /** False when the address is on another document, carried from upstream. */
  here: boolean
}

export interface DecisionBlock {
  decision: Decision
  who: { kind: 'person' | 'agent'; id: string; name: string }
  targets: DecisionTarget[]
  attention: AttentionReason[]
  superseded: boolean
}

/** What a cite names, resolved by the host. */
export interface CiteInfo {
  kind: 'fact' | 'finding' | 'source' | 'material' | 'decision' | 'person' | 'address' | 'unknown'
  label: string
  detail?: string
  quote?: string
}

export interface DecisionsView {
  document_id: string
  version: string
  /** Newest first. */
  decisions: DecisionBlock[]
  /** Lock blockers first. */
  attention: AttentionItem[]
  cites: Record<string, CiteInfo>
  lock: { can_lock: boolean; blockers: number }
  /** Set when the chain could not be read. */
  problem?: string
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
  /**
   * Every decision in the open document, newest first, with what needs a
   * person and why — derived by the host, the same for every app.
   */
  getDecisions(): Promise<DecisionsView>

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
  /**
   * Last pass over a composed app page before the frame loads it. Identity on
   * the desktop, where `clan://` is a real scheme; on the web it rewrites that
   * base to the frame's own URL so app HTML runs unmodified.
   */
  prepareAppHtml(html: string): string
  /**
   * How the app frame gets its document. `'url'` means the host serves it and
   * the frame loads it by address; `'srcdoc'` means there is no server to
   * serve it from, so it is inlined.
   */
  readonly frameLoad: 'url' | 'srcdoc'
  /**
   * Answer a `clan://` request the app frame made, when the shim routes those
   * through the shell instead of the network. Only the serverless host needs
   * this; the others let the frame talk to the host directly.
   */
  handleFromFrame(
    path: string,
    query: string,
    body: Uint8Array,
  ): Promise<{ status: number; headers: [string, string][]; body: Uint8Array }>

  // ── The app library ───────────────────────────────────────────────────────
  listApps(): Promise<InstalledApp[]>
  installApp(srcPath: string): Promise<InstalledApp>
  /** Documents in the store, most recently updated first (the host keeps 12). */
  listRecent(): Promise<RecentDoc[]>

  // ── Branching one document into another app ───────────────────────────────
  /**
   * Which installed apps have declared they will take the open document as a
   * spin-off source. Empty is a normal answer — it means nothing downstream is
   * installed, not that anything failed.
   */
  spinoffTargets(): Promise<SpinoffTarget[]>
  /**
   * Branch the open document into `appId`, carrying its data and its decision
   * chain, and open the result. `map` overrides the target app's declared graft.
   */
  spinoffDocument(
    appId: string,
    title: string | null,
    map: string | null,
  ): Promise<OpenResult>

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
