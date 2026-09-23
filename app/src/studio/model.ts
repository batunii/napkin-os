// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// What the studio is made of: the documents a job passes through, the agents
// that work on them, the steps only a person takes, and what stops a lock.
//
// Every view (Floor, Decisions) renders from this, so the shape is decided
// once. It restates our contracts; where it disagrees with them they win:
//
//   docs/contracts/os-layer.md      decision kinds (§3), lease (§6), lock (§7)
//   docs/contracts/campaign-clan.md gates (§2.3), lenses and coverage (§7),
//                                   research per market (§8), review marks (§10)
//
// Everything here is STRUCTURE. Nothing in this file is sample data; the
// Floor's sample story lives in floor/example.ts behind `EXAMPLE`.

/** True while the Floor's per-document progress, open items and feed are sample data. */
export const EXAMPLE = true

// ── the flow ────────────────────────────────────────────────────────────────

/**
 * The Research Tool's app id. Provisional: `app/templates/campaign-research`
 * is not packaged yet, and this is the id it will be packaged under.
 */
export const RESEARCH_TOOL_APP_ID = 'ie.napkin.campaign-research'
export const BRIEF_MAKER_APP_ID = 'ie.napkin.brief-maker'

export type DocKind = 'campaign' | 'brief'

export interface FlowStep {
  kind: DocKind
  /** What the document is called. */
  name: string
  /** The app that is its view. */
  app: string
  appId: string
  /** How the next document is made from this one. */
  next: string
}

/**
 * A job's documents, in order. Each hop is a spin-off, which carries the
 * source whole — data, pins, findings, the chain, open items (os-layer §5).
 */
export const FLOW: readonly FlowStep[] = [
  { kind: 'campaign', name: 'Campaign', app: 'Research Tool', appId: RESEARCH_TOOL_APP_ID, next: 'Spin off a brief' },
  { kind: 'brief', name: 'Brief', app: 'Brief Maker', appId: BRIEF_MAKER_APP_ID, next: 'Spin off the next document' },
]

/** The flow step an installed app id belongs to, if any. */
export function flowStepOf(appId: string | null | undefined): FlowStep | undefined {
  return FLOW.find(s => s.appId === appId)
}

// ── gates ───────────────────────────────────────────────────────────────────

/** The state a document cannot reach without a field (campaign-clan §2.3, D4). */
export type Gate = 'created' | 'research' | 'brief'

/** Where a document is: past a gate, or locked. Lock = accept = seal. */
export type DocState = Gate | 'locked'

export interface GateInfo {
  id: Gate
  label: string
  /** What passing it means. */
  description: string
}

export const GATES: readonly GateInfo[] = [
  { id: 'created', label: 'Created', description: 'The ask has a name, a brand, a client and its source. The document exists.' },
  { id: 'research', label: 'Research', description: 'Categories and markets are set, so research can run: every lens, once per market.' },
  { id: 'brief', label: 'Brief', description: 'Problem, objective, audience and budget band are in. The document is brief-ready.' },
]

/** A document's states in order, gates then the lock. */
export const DOC_STATES: readonly DocState[] = ['created', 'research', 'brief', 'locked']

export const DOC_STATE_LABEL: Readonly<Record<DocState, string>> = {
  created: 'Created', research: 'Research', brief: 'Brief', locked: 'Locked',
}

// ── lenses and coverage ─────────────────────────────────────────────────────

/** The Planner Research Taxonomy, as the campaign document names it (§7). */
export type LensId =
  | 'market_structure' | 'brands_positioning' | 'consumer_culture' | 'category_codes'
  | 'rhythm_moments' | 'media_spend' | 'regulation_clearance' | 'effectiveness_evidence'

/** filled = corroborated · thin = single-source · empty = nothing found. */
export type Coverage = 'filled' | 'thin' | 'empty'

// ── agents ──────────────────────────────────────────────────────────────────

/** The eight research agents, one per lens. */
export type LensAgentKey =
  | 'market_structure' | 'positioning' | 'culture' | 'codes'
  | 'rhythm' | 'media' | 'regulation' | 'effectiveness'

export type AgentKey = 'extract' | LensAgentKey | 'synthesis' | 'drafter' | 'judge'

/** Which document the agent works on: the campaign, or the brief spun off it. */
export type Stage = 'research' | 'brief'

/**
 * `parallel` agents run side by side, each on its own branch that merges
 * under the lease; `serial` ones run alone, after (os-layer §6).
 */
export type Run = 'parallel' | 'serial'

/** What a parallel agent fans out over: one instance per market, or per brief field. */
export type FanOut = 'market' | 'field'

export interface Agent {
  key: AgentKey
  /** Plain name, as a person would say it. */
  name: string
  /** One line: what it does. */
  role: string
  stage: Stage
  run: Run
  fanOut?: FanOut
  /** Lens researchers only. */
  lens?: LensId
  /** The pipeline handler, where a contract names one. */
  handler?: string
}

/** What an agent is doing right now. */
export type AgentState = 'idle' | 'working' | 'needs-you'

const lens = (key: LensAgentKey, name: string, id: LensId, role: string): Agent =>
  ({ key, name, role, stage: 'research', run: 'parallel', fanOut: 'market', lens: id, handler: 'research_lens@1' })

export const AGENTS: Readonly<Record<AgentKey, Agent>> = {
  extract: {
    key: 'extract', name: 'Extract', stage: 'research', run: 'serial', handler: 'extract_ask@1',
    role: 'Reads the prompt and attachments and proposes the ask.',
  },
  market_structure: lens('market_structure', 'Market structure', 'market_structure', 'Sizes the category: value, volume, share and growth.'),
  positioning: lens('positioning', 'Brands and positioning', 'brands_positioning', 'Maps the brand and its comparators: awareness, claims, prices.'),
  culture: lens('culture', 'Consumer and culture', 'consumer_culture', 'Finds who buys, when and why, and what is shifting.'),
  codes: lens('codes', 'Category codes', 'category_codes', 'Reads the category’s visual and verbal conventions.'),
  rhythm: lens('rhythm', 'Rhythm and moments', 'rhythm_moments', 'Charts the seasons, peaks and moments that matter.'),
  media: lens('media', 'Media and spend', 'media_spend', 'Tracks who spends what, where.'),
  regulation: lens('regulation', 'Regulation', 'regulation_clearance', 'Lists what may and may not be said, and where.'),
  effectiveness: lens('effectiveness', 'Effectiveness', 'effectiveness_evidence', 'Finds work in the category that is proven to have worked.'),
  synthesis: {
    key: 'synthesis', name: 'Synthesis', stage: 'research', run: 'serial', handler: 'synthesise_findings@1',
    role: 'Turns pinned facts into findings, derived by the agent until a person verifies them.',
  },
  drafter: {
    key: 'drafter', name: 'Drafter', stage: 'brief', run: 'parallel', fanOut: 'field',
    role: 'Drafts one brief field from the campaign’s evidence.',
  },
  judge: {
    key: 'judge', name: 'Judge', stage: 'brief', run: 'serial',
    role: 'Reads the whole brief after the drafts and checks it holds together.',
  },
}

/** All twelve: research in the order they run, then the brief. */
export const AGENT_KEYS: readonly AgentKey[] = [
  'extract',
  'market_structure', 'positioning', 'culture', 'codes', 'rhythm', 'media', 'regulation', 'effectiveness',
  'synthesis',
  'drafter', 'judge',
]

export const LENS_AGENTS: readonly LensAgentKey[] =
  ['market_structure', 'positioning', 'culture', 'codes', 'rhythm', 'media', 'regulation', 'effectiveness']

export const STAGES: readonly { id: Stage; label: string; agents: readonly AgentKey[] }[] = [
  { id: 'research', label: 'Research', agents: AGENT_KEYS.filter(k => AGENTS[k].stage === 'research') },
  { id: 'brief', label: 'Brief', agents: AGENT_KEYS.filter(k => AGENTS[k].stage === 'brief') },
]

/** The lens researcher for a lens id. */
export const AGENT_OF_LENS: Readonly<Record<LensId, LensAgentKey>> =
  Object.fromEntries(LENS_AGENTS.map(k => [AGENTS[k].lens, k])) as Record<LensId, LensAgentKey>

/** Lens ids in taxonomy order. */
export const LENS_IDS: readonly LensId[] = LENS_AGENTS.map(k => AGENTS[k].lens as LensId)

/**
 * How one running instance is named: "Drafter · Proposition",
 * "Market structure · IE". No instance, just the name.
 */
export function agentLabel(key: AgentKey, instance?: string): string {
  return instance ? `${AGENTS[key].name} · ${instance}` : AGENTS[key].name
}

// ── how each agent looks ────────────────────────────────────────────────────

/** Folded-paper bodies. `dome` and `hex` joined the prototype's six. */
export type ShapeName = 'star' | 'tube' | 'zig' | 'pyramid' | 'block' | 'gem' | 'dome' | 'hex'

/** A small mark on the body, so agents that share a shape still read apart. */
export type Emblem = 'none' | 'bars' | 'target' | 'dots' | 'stripe' | 'wave' | 'lines' | 'seal' | 'spark' | 'tick'

/**
 * Body colours, palette only. `--create` is not here: it is reserved for
 * "needs you", and a body in it would read as a permanent alarm.
 *   ink   — the agents either side of the lenses (Extract, Synthesis) and the Judge
 *   slate — the eight lens researchers
 *   mist  — the Drafter
 */
export type Tone = 'ink' | 'slate' | 'mist'

export interface AgentLook {
  shape: ShapeName
  emblem: Emblem
  tone: Tone
}

/** Every (shape, emblem, tone) triple is different; the eight lenses also differ by shape alone. */
export const LOOK_OF: Readonly<Record<AgentKey, AgentLook>> = {
  extract:          { shape: 'pyramid', emblem: 'lines',  tone: 'ink' },
  market_structure: { shape: 'block',   emblem: 'bars',   tone: 'slate' },
  positioning:      { shape: 'gem',     emblem: 'target', tone: 'slate' },
  culture:          { shape: 'dome',    emblem: 'dots',   tone: 'slate' },
  codes:            { shape: 'hex',     emblem: 'stripe', tone: 'slate' },
  rhythm:           { shape: 'zig',     emblem: 'none',   tone: 'slate' },
  media:            { shape: 'pyramid', emblem: 'wave',   tone: 'slate' },
  regulation:       { shape: 'tube',    emblem: 'seal',   tone: 'slate' },
  effectiveness:    { shape: 'star',    emblem: 'none',   tone: 'slate' },
  synthesis:        { shape: 'gem',     emblem: 'spark',  tone: 'ink' },
  drafter:          { shape: 'tube',    emblem: 'none',   tone: 'mist' },
  judge:            { shape: 'block',   emblem: 'tick',   tone: 'ink' },
}

/**
 * Theme-following CSS colours for a tone: the body, and the eyes and emblem
 * drawn on it. The tokens still carry the prototype's department names;
 * only their colours are used.
 */
export const TONE_VARS: Readonly<Record<Tone, { body: string; mark: string }>> = {
  ink: { body: 'var(--plan)', mark: 'var(--plan-eye)' },
  slate: { body: 'var(--produce)', mark: 'var(--produce-eye)' },
  mist: { body: 'var(--learn)', mark: 'var(--learn-eye)' },
}

// ── decisions and the steps only a person takes ─────────────────────────────

/** Every mark is one of these (os-layer §3 `Decision.kind`). */
export type DecisionKind =
  | 'edit' | 'contest' | 'resolve' | 'verdict' | 'classify' | 'pin'
  | 'finding' | 'verify' | 'approve' | 'lease' | 'backref'

/** `<doc-id>#<entity-keyed path>` (os-layer §3). */
export type Address = string

export function address(docId: string, path: string): Address {
  return `${docId}#${path}`
}

/**
 * What no agent does. Our agents never write to a document directly: they
 * propose, on branches and as findings, and a person settles it here.
 */
export type HumanStepId = 'confirm' | 'resolve' | 'verify' | 'verdict' | 'classify' | 'lock'

export interface HumanStep {
  id: HumanStepId
  /** What the person does. */
  label: string
  /** The button. */
  verb: string
  /** The decision it writes. */
  kind: DecisionKind
  /** For `confirm`: the field's origin becomes `confirmed`. */
  origin?: 'confirmed'
  /** When a written reason is required. */
  rationale: 'required' | 'when-bad' | 'optional'
  /** Whether it needs the editing lease (os-layer §6). */
  lease: boolean
}

export const HUMAN_STEPS: Readonly<Record<HumanStepId, HumanStep>> = {
  confirm: { id: 'confirm', label: 'Confirm the ask', verb: 'Confirm', kind: 'edit', origin: 'confirmed', rationale: 'optional', lease: true },
  resolve: { id: 'resolve', label: 'Resolve a contest', verb: 'Resolve', kind: 'resolve', rationale: 'required', lease: true },
  verify: { id: 'verify', label: 'Verify a finding', verb: 'Verify', kind: 'verify', rationale: 'optional', lease: true },
  verdict: { id: 'verdict', label: 'Mark a field good or bad', verb: 'Mark', kind: 'verdict', rationale: 'when-bad', lease: false },
  classify: { id: 'classify', label: 'Mark confidential', verb: 'Mark confidential', kind: 'classify', rationale: 'optional', lease: false },
  lock: { id: 'lock', label: 'Lock', verb: 'Lock', kind: 'approve', rationale: 'required', lease: true },
}

// ── open items: what stops a lock ───────────────────────────────────────────

/**
 * Anything unresolved (os-layer §7, campaign-clan §11). A document cannot
 * lock while it has one, carried ones included.
 *   contest  — two writers, two values, one address; nothing picked
 *   finding  — agent synthesis not yet verified or rejected
 *   verdict  — a bad verdict not yet answered (field revised, or overridden with a reason)
 *   branch   — an agent's branch not yet merged
 *   field    — a created, research or brief gated field still absent
 */
export type OpenItemKind = 'contest' | 'finding' | 'verdict' | 'branch' | 'field'

export interface OpenItemAction {
  /** Short: what the person does about it. */
  label: string
  /** The button. */
  verb: string
  /** The decision settling it writes (a revise or a merge is an `edit`). */
  kind: DecisionKind
}

export const OPEN_ITEM_ACTION: Readonly<Record<OpenItemKind, OpenItemAction>> = {
  contest: { label: 'Resolve', verb: 'Resolve', kind: 'resolve' },
  finding: { label: 'Verify', verb: 'Verify', kind: 'verify' },
  verdict: { label: 'Answer a bad verdict', verb: 'Answer', kind: 'edit' },
  branch: { label: 'Merge', verb: 'Merge', kind: 'edit' },
  field: { label: 'Fill a gated field', verb: 'Fill', kind: 'edit' },
}

export interface DocRef {
  id: string
  title: string
  /** Where the host opens it from, when known. */
  path?: string
}

export interface OpenItem {
  /** Stable id of what is open: contest id, finding id, decision id, branch, field path. */
  id: string
  kind: OpenItemKind
  doc: DocRef
  /** What it is about. */
  address: Address
  /** The agent whose work it came from. */
  from: AgentKey
  /** Which run of that agent: a market for a lens, a field for a drafter. */
  instance?: string
  /** One short line: what is open. */
  label: string
  /** Travelled in from an upstream document at a spin-off. */
  carried?: boolean
}

/** A document can lock only when nothing is open on it. */
export function canLock(items: readonly OpenItem[], docId: string): boolean {
  return !items.some(i => i.doc.id === docId)
}

// ── a document in progress ──────────────────────────────────────────────────

/** One agent (or group of its instances) on a document. */
export interface AgentOnDoc {
  agent: AgentKey
  state: AgentState
  /** Parallel agents: how many instances are running (markets or fields). */
  count?: number
}

/** What the Floor shows about a document. W2-O2's `/open` route will serve most of it. */
export interface DocProgress {
  doc: DocRef
  kind: DocKind
  /** The furthest state reached. */
  state: DocState
  agents: AgentOnDoc[]
  /** Campaigns: merged coverage per lens. */
  coverage?: Partial<Record<LensId, Coverage>>
  markets?: string[]
  /** Who holds the editing lease, if anyone. */
  lease?: string
  /** The upstream document it was spun off, if any. */
  from?: DocRef
}

/** One line in the activity feed: a decision, by an agent or a person. */
export interface ActivityEntry {
  id: string
  at: string
  by: { agent: AgentKey; instance?: string } | { person: string }
  kind: DecisionKind
  doc: DocRef
  text: string
}
