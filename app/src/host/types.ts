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
    // A client's answer to the locked document (OS-layer contract §7.5.4).
    | 'client_rejected' | 'client_rejected_parts_unknown' | 'client_change_asked' | 'client_part_suggested'
  text: string
  blocks_lock: boolean
}

export interface AttentionItem extends AttentionReason {
  /** The decision it belongs to, when there is one. */
  decision?: string
  address?: string
  label?: string
  /** A carried item this document cannot settle (a frozen field citing a finding
   *  rejected here, a carried agent branch or merge-report conflict): a person may
   *  set it aside with a written reason (`/acknowledge {target, rationale}`,
   *  OS-layer contract §7.2.1). Absent when false. */
  can_set_aside?: boolean
}

export interface DecisionTarget {
  address: string
  path: string
  label: string
  kind: 'field' | 'contest' | 'finding' | 'fact' | 'decision' | 'document' | 'branch'
  /** False when the address is on another document, carried from upstream. */
  here: boolean
}

export interface DecisionBlock {
  decision: Decision
  who: { kind: 'person' | 'agent'; id: string; name: string; you?: boolean }
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
  /** A fact: its value as a person reads it, and the sources behind it. */
  value?: string
  sources?: string[]
  /** A source the document carries. */
  uri?: string
  publisher?: string
  title?: string
  published_at?: string
  tier?: string
}

export interface DecisionsView {
  document_id: string
  version: string
  /** Newest first. */
  decisions: DecisionBlock[]
  /** Lock blockers first. */
  attention: AttentionItem[]
  cites: Record<string, CiteInfo>
  lock: {
    can_lock: boolean
    blockers: number
    /** An `approve` of this document holds (§7.1). Absent from an older host. */
    locked?: boolean
    /** Parts reopened for a client's request since the lock (§7.5.6). */
    reopened?: ReopenedPart[]
  }
  /** Client review (§7.5, §8.2 item 6). Absent from an older host. */
  client?: ClientView
  /** Set when the chain could not be read. */
  problem?: string
}

// ── Client review (OS-layer contract §7.5, §8.2) ──────────────────────────────

export type ClientAnswerKind = 'accepted' | 'accepted_with_changes' | 'rejected'
export type ClientReason = 'off_brief' | 'wrong_audience' | 'tone' | 'facts_wrong' | 'budget' | 'other'
export type ClientChannel = 'pasted_email' | 'file' | 'call' | 'none'

/** Who answered, as the recorder typed it. Data, not identity. */
export interface ClientWho {
  name: string
  email?: string
}

/** A part the app declared: a dotted data path and what to call it. */
export interface ClientPartRef {
  address: string
  label: string
  /**
   * Other words the client may call it by (`data-clan-part-aliases`), for the
   * host's matcher. The host adds the label and its last word itself.
   */
  aliases?: string[]
}

export interface ReopenedPart {
  address: string
  label: string
  /** The `unlock`. */
  decision: string
  /** The part answer it was reopened for. */
  answers: string
}

/** One document answer, as `/decisions` reads it. */
export interface ClientAnswerView {
  decision: string
  answer: ClientAnswerKind
  reasons?: ClientReason[]
  /** The client's words, verbatim — or, on a call, the recorder's note of them. */
  said?: string
  client: ClientWho
  channel?: ClientChannel
  evidence: { asset?: string; sha256?: string; strength: 'strong' | 'weaker' }
  recorded_by: { id: string; name: string }
  at: string
  seen: { version: string; doc_hash: string; parts: (ClientPartRef & { part_hash: string })[] }
  /** It answers the version the lock names now. */
  current: boolean
  /** A part answer names it as its review. */
  parts_known: boolean
}

export interface ClientPartView {
  address: string
  label: string
  state: ClientAnswerKind
  decision: string
  review: string
  /** `document`: an accepted document answer marks every part. */
  found_by: 'person' | 'agent' | 'document'
  quote?: string
  /** What the client said about this part, as the recorder typed it under it: verbatim. */
  said?: string
  client: ClientWho
  at: string
  stale: boolean
  answered: boolean
  reopened: boolean
}

/** One of Ellis's suggestions nobody has confirmed or dismissed yet. */
export interface ClientSuggestionView {
  decision: string
  review: string
  address: string
  label: string
  answer: ClientAnswerKind
  quote: string
}

export interface ClientView {
  /** `POST /client-review` would be accepted now: locked, nothing reopened. */
  available: boolean
  answer: ClientAnswerView | null
  /** Every document answer, newest first. */
  answers: ClientAnswerView[]
  parts: ClientPartView[]
  /** Oldest first. */
  suggestions: ClientSuggestionView[]
}

/** `POST /client-review`. The recorder is whoever is signed in; never sent. */
export interface ClientReviewBody {
  answer: ClientAnswerKind
  client: ClientWho
  reasons?: ClientReason[]
  channel: ClientChannel
  said?: string
  /** `human/assets/<name>`, stored first with `uploadAsset`. */
  asset?: string
  /** The app's whole list, every time. */
  parts: ClientPartRef[]
  /**
   * The parts the recorder marked. `said` is what they typed under the part
   * ("What they said about it"), verbatim; absent when it is only whitespace.
   */
  marked?: { address: string; answer: ClientAnswerKind; said?: string }[]
}

export interface ClientReviewReply {
  ok: boolean
  decision: string
  parts: string[]
  suggestions: {
    /**
     * From the host's own matcher, in the same reply: `none`, nothing was
     * looked for (accepted, parts marked, no parts, no words); `found`, it
     * looked (`decisions` may be empty: no part is named in the words).
     */
    status: 'none' | 'found' | 'unavailable'
    decisions: string[]
    dropped: number
    reason?: string
  }
}

/** `POST /client-review/confirm`: settle a suggestion, or mark a part after the fact. */
export type ClientConfirmBody =
  | { suggestion: string; confirm: boolean; answer?: ClientAnswerKind; rationale?: string; /** confirm only: the part's words, verbatim */ said?: string }
  | { review: string; address: string; answer: ClientAnswerKind; rationale?: string; /** the part's words, verbatim */ said?: string }

export interface ClientConfirmReply {
  ok: boolean
  decision: string
  targets: string[]
}

/** `POST /client-review/reopen`: what edit mode opens with. */
export interface ClientReopenReply {
  ok: boolean
  decision: string
  address: string
  label: string
  /** The part answer the edit answers: send it as the `/edit`'s `answers`. */
  answers: string
  /** "<client> asked: …" — the edit's reason, pre-filled. */
  reason: string
}

/** `POST /upload-asset`. */
export interface UploadedAsset {
  ok: boolean
  internal_path: string
  extracted_chars: number
}

// ── What changed upstream (§8.1, item 6) ──────────────────────────────────────

export interface UpstreamCarried {
  document_id: string
  data_sha256: string
  facts_sha256?: string
  findings_sha256?: string
  sources_sha256?: string
  last_decision?: string
}

export interface UpstreamEntry {
  document_id: string
  direct: boolean
  in_store: boolean
  title?: string
  app_id?: string
  version?: string
  locked?: boolean
  status: 'current' | 'changed' | 'not_compared' | 'unknown'
  changed?: { data: boolean; facts: boolean; findings: boolean; sources: boolean }
  decisions_since?: number | null
  pins?: { id: string; change: 'added' | 'replaced' | 'changed'; label: string; replaced_by?: string }[]
  findings?: { id: string; change: 'added' | 'verified' | 'rejected'; statement: string; reason?: string; cited_by: string[] }[]
  contests?: { id: string; change: 'opened' | 'resolved'; key: string; chosen?: string; resolved_here: boolean }[]
}

export interface UpstreamView {
  document_id: string
  carried: UpstreamCarried | null
  upstream: UpstreamEntry[]
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

/**
 * Who this page is signed in as: the actor the host records a person's writes
 * under (`Ctx.actor`), which is what the decision view calls "You". Nobody
 * gives the host a name yet, so `name` is null until an account system does.
 */
export interface Person {
  /** `human:<id>`, as the chain records it. */
  actor: string
  id: string
  name: string | null
}

export interface Host {
  // ── Who is here ───────────────────────────────────────────────────────────
  /** The signed-in person, as the host acts for them. Read-only. */
  whoAmI(): Promise<Person>

  // ── Documents ─────────────────────────────────────────────────────────────
  openClan(path: string): Promise<OpenResult>
  openHome(): Promise<OpenResult>
  newDocumentFromApp(appId: string, title: string | null): Promise<OpenResult>
  /** The .clan path the app was launched with, if any. Cleared by reading it. */
  takeLaunchFile(): Promise<string | null>

  // ── Reading the open document ─────────────────────────────────────────────
  getHumanHtml(): Promise<string>
  getData(): Promise<string>
  /**
   * Every decision in the open document, newest first, with what needs a
   * person and why — derived by the host, the same for every app.
   */
  getDecisions(): Promise<DecisionsView>
  /** A person accepts an agent's call ("Looks right"): `/acknowledge`. */
  acknowledge(decision: string): Promise<void>
  /** What changed upstream since this document was spun off (`GET /upstream`). */
  upstream(): Promise<UpstreamView>

  // ── Client review (OS-layer contract §7.5) ────────────────────────────────
  /**
   * Record a client's answer to the locked document: `POST /client-review`.
   * The host's matcher suggests which parts it was about in the same reply
   * (shown as Ellis's; nothing waits on a model).
   */
  clientReview(body: ClientReviewBody): Promise<ClientReviewReply>
  /** Confirm or dismiss one of Ellis's suggestions, or mark a part after the fact. */
  clientReviewConfirm(body: ClientConfirmBody): Promise<ClientConfirmReply>
  /** "Make this change": reopen the one part a client's answer asked about. */
  clientReviewReopen(answer: string): Promise<ClientReopenReply>
  /** Store a file in the open document (`/upload-asset`) — the client's email or PDF. */
  uploadAsset(name: string, bytes: Uint8Array): Promise<UploadedAsset>

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
