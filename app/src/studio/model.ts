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
  /** The name it introduces itself by, the same in every app: "Ellis". */
  given: string
  /** Its job in two or three plain words, after the name: "Ellis · reads". */
  job: string
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

const lens = (key: LensAgentKey, name: string, id: LensId, role: string, given: string, job: string): Agent =>
  ({ key, name, role, given, job, stage: 'research', run: 'parallel', fanOut: 'market', lens: id, handler: 'research_lens@1' })

export const AGENTS: Readonly<Record<AgentKey, Agent>> = {
  extract: {
    key: 'extract', name: 'Extract', stage: 'research', run: 'serial', handler: 'extract_ask@1',
    role: 'Reads the prompt and attachments and proposes the ask.',
    given: 'Ellis', job: 'reads',
  },
  market_structure: lens('market_structure', 'Market structure', 'market_structure', 'Sizes the category: value, volume, share and growth.', 'Max', 'the market'),
  positioning: lens('positioning', 'Brands and positioning', 'brands_positioning', 'Maps the brand and its comparators: awareness, claims, prices.', 'Bo', 'the brands'),
  culture: lens('culture', 'Consumer and culture', 'consumer_culture', 'Finds who buys, when and why, and what is shifting.', 'Cam', 'the shoppers'),
  codes: lens('codes', 'Category codes', 'category_codes', 'Reads the category’s visual and verbal conventions.', 'Cody', 'the ads'),
  rhythm: lens('rhythm', 'Rhythm and moments', 'rhythm_moments', 'Charts the seasons, peaks and moments that matter.', 'Remy', 'the timing'),
  media: lens('media', 'Media and spend', 'media_spend', 'Tracks who spends what, where.', 'Mo', 'the spend'),
  regulation: lens('regulation', 'Regulation', 'regulation_clearance', 'Lists what may and may not be said, and where.', 'Lex', 'the rules'),
  effectiveness: lens('effectiveness', 'Effectiveness', 'effectiveness_evidence', 'Finds work in the category that is proven to have worked.', 'Eden', 'what worked'),
  synthesis: {
    key: 'synthesis', name: 'Synthesis', stage: 'research', run: 'serial', handler: 'synthesise_findings@1',
    role: 'Turns pinned facts into findings, derived by the agent until a person verifies them.',
    given: 'Sam', job: 'finds the points',
  },
  drafter: {
    key: 'drafter', name: 'Drafter', stage: 'brief', run: 'parallel', fanOut: 'field',
    role: 'Drafts one brief field from the campaign’s evidence.',
    given: 'Dara', job: 'writes',
  },
  judge: {
    key: 'judge', name: 'Judge', stage: 'brief', run: 'serial',
    role: 'Reads the whole brief after the drafts and checks it holds together.',
    given: 'Jude', job: 'checks',
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

// ── what an app's step is doing ────────────────────────────────────────────
// The shared crew every app draws from. An app does not pick figures: it says
// what each of its steps is doing, and the step gets that agent. Brief Maker
// reads, drafts and judges; the Research Tool reads, researches, synthesises,
// judges (which lenses) and drafts (the report). A new app names its steps the
// same way and gets the same people. The template apps get this through the
// shared snippet (figureSnippet.tsx, `NapkinAgents`).

/** The five kinds of work. `research` also names the lens it is on. */
export type Work = 'read' | 'research' | 'synthesise' | 'draft' | 'judge'

export const WORKS: readonly Work[] = ['read', 'research', 'synthesise', 'draft', 'judge']

/** Who does each kind of work. Research is per lens (AGENT_OF_LENS). */
export const AGENT_FOR_WORK: Readonly<Record<Exclude<Work, 'research'>, AgentKey>> = {
  read: 'extract',
  synthesise: 'synthesis',
  draft: 'drafter',
  judge: 'judge',
}

/** Research with no lens named: the first lens researcher stands for the stage. */
const RESEARCH_DEFAULT: LensAgentKey = 'market_structure'

/** The agent doing a kind of work; for research, on that lens. */
export function agentForWork(work: Work, lens?: string): AgentKey {
  if (work === 'research') return AGENT_OF_LENS[lens as LensId] ?? RESEARCH_DEFAULT
  return AGENT_FOR_WORK[work]
}

/**
 * The kind of work a handler, or a step inside one, is doing. Keys are
 * handler names without the @version, and the actions multi-step handlers
 * record (`start_campaign` records extract, identify, select, …).
 */
export const WORK_OF_STEP: Readonly<Record<string, Work>> = {
  extract: 'read', extract_ask: 'read', capture: 'read', identify: 'read', lookup: 'read', transcribe: 'read',
  research: 'research', research_lens: 'research', research_run: 'research', research_merge: 'research',
  synthesise: 'synthesise', synthesise_findings: 'synthesise', synthesise_finding: 'synthesise', propose_audience: 'synthesise',
  draft: 'draft', draft_brief: 'draft', regenerate_field: 'draft', report: 'draft', compose_report: 'draft', drafter: 'draft',
  judge: 'judge', select: 'judge', verdict: 'judge', golden_critic: 'judge',
}

/** What a decision says about who made it, in whichever words the writer used. */
export interface WhoWrote {
  /** The decision's `agent`, or the handler that acted (`napkin/brief/judge`, `draft_brief@1`). */
  agent?: string
  action?: string
  kind?: string
  polarity?: string
  lens?: string
  targets?: readonly string[]
}

const lastPart = (s: string) => s.split('/').pop()!.replace(/@.*$/, '').toLowerCase()

/**
 * The agent behind a decision, or null for a person or a process with no
 * figure. The one rule every app and the shell use (the snippet carries a
 * copy; tests/agentFigures.test.ts holds the two together).
 */
export function agentOfDecision(d: WhoWrote): AgentKey | null {
  const who = String(d.agent ?? '')
  if (!who || who === 'human' || /^human:/.test(who)) return null
  if (d.kind === 'verdict' || d.polarity) return 'judge'
  if (d.kind === 'finding') return 'synthesis'
  // The scorecard is the Judge's review, whichever worker the middleware
  // tagged it with (briefs before 2026-09-30 carry `…/extract`).
  if (String(d.action ?? '').toLowerCase() === 'score') return 'judge'
  const work = WORK_OF_STEP[lastPart(who)] ?? WORK_OF_STEP[String(d.action ?? '').toLowerCase()]
    ?? (/judge|critic/i.test(who) ? 'judge' : /extract|capture/i.test(who + ' ' + (d.action ?? '')) ? 'read'
      : /draft/i.test(who) ? 'draft' : undefined)
  if (!work) return null
  if (work !== 'research') return AGENT_FOR_WORK[work]
  const lens = LENS_IDS.find(l => [d.lens ?? '', d.action ?? '', ...(d.targets ?? [])].some(t => t.includes(l)))
  return lens ? AGENT_OF_LENS[lens] : null
}

// ── how each agent looks ────────────────────────────────────────────────────
// The owner's character sheet, "01 / Graphic characters" (2026-09-24): 2D, bold
// colour, clear personalities. Flat bodies, thin ink legs, a face, and one mark
// each where the sheet gives one.

export type ShapeName =
  | 'star' | 'crescent' | 'stack' | 'triangle' | 'column' | 'card' | 'diamond' | 'dome' | 'hex'

/** A small mark on the body, so agents that share a shape still read apart. */
export type Emblem = 'none' | 'bars' | 'target' | 'dots' | 'mouth' | 'wave' | 'page' | 'spark' | 'tick'

/**
 * The sheet's six colours. Ink follows the theme (it is the studio's ink, and
 * goes light on a Plan band or in dark mode); the other five are fixed. Coral
 * is the sheet's, kept apart from --create, which stays "needs you".
 */
export type Tone = 'ink' | 'cobalt' | 'coral' | 'marigold' | 'iris' | 'mint'

export interface AgentLook {
  shape: ShapeName
  emblem: Emblem
  tone: Tone
  /** A second colour on the body (Rhythm's small coral diamond). */
  accent?: Tone
  /** The mark in white rather than the face colour (Synthesis's spark). */
  lightMark?: boolean
}

export const LOOK_OF: Readonly<Record<AgentKey, AgentLook>> = {
  extract:          { shape: 'triangle', emblem: 'page',   tone: 'ink' },
  market_structure: { shape: 'column',   emblem: 'bars',   tone: 'cobalt' },
  positioning:      { shape: 'diamond',  emblem: 'target', tone: 'iris' },
  culture:          { shape: 'dome',     emblem: 'dots',   tone: 'coral' },
  codes:            { shape: 'hex',      emblem: 'mouth',  tone: 'mint' },
  rhythm:           { shape: 'stack',    emblem: 'none',   tone: 'iris', accent: 'coral' },
  media:            { shape: 'triangle', emblem: 'wave',   tone: 'cobalt' },
  regulation:       { shape: 'crescent', emblem: 'spark',  tone: 'mint' },
  effectiveness:    { shape: 'star',     emblem: 'none',   tone: 'marigold' },
  synthesis:        { shape: 'diamond',  emblem: 'spark',  tone: 'iris', lightMark: true },
  drafter:          { shape: 'crescent', emblem: 'none',   tone: 'marigold' },
  judge:            { shape: 'card',     emblem: 'tick',   tone: 'ink' },
}

/**
 * CSS colours for a tone: the body, and the face and mark drawn on it. Ink
 * uses the studio's --plan pair, so it inverts wherever the page does; the
 * rest are AgentFigure.css's fixed palette with a fixed dark face.
 */
export const TONE_VARS: Readonly<Record<Tone, { body: string; mark: string }>> = {
  ink: { body: 'var(--plan)', mark: 'var(--plan-eye)' },
  cobalt: { body: 'var(--af-cobalt)', mark: 'var(--af-mark)' },
  coral: { body: 'var(--af-coral)', mark: 'var(--af-mark)' },
  marigold: { body: 'var(--af-marigold)', mark: 'var(--af-mark)' },
  iris: { body: 'var(--af-iris)', mark: 'var(--af-mark)' },
  mint: { body: 'var(--af-mint)', mark: 'var(--af-mark)' },
}
