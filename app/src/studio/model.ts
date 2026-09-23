// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// The agents: who they are, what they do, and how each one looks.
//
// It restates our contracts; where it disagrees with them they win:
//   docs/contracts/os-layer.md      parallel and serial work (§6)
//   docs/contracts/campaign-clan.md the research lenses (§7)

// ── lenses ─────────────────────────────────────────────────────

/** The Planner Research Taxonomy, as the campaign document names it (§7). */
export type LensId =
  | 'market_structure' | 'brands_positioning' | 'consumer_culture' | 'category_codes'
  | 'rhythm_moments' | 'media_spend' | 'regulation_clearance' | 'effectiveness_evidence'

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
