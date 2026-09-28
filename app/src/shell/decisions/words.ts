// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// A decision, in words a person reads: who (the agent, or "You"), what they
// did, where in the document, and what it rests on. Never a raw id, a handler
// version or a model name — those stay in "Technical details".

import { AGENTS, agentOfDecision } from '../../studio/model'
import type { AgentKey } from '../../studio/model'
import type { CiteInfo, DecisionBlock, DecisionsView } from '../../host'

export interface WhoIs {
  /** The agent's figure, when an agent decided. */
  agent: AgentKey | null
  name: string
  person: boolean
}

/** The agent behind a block — the step's agent, not the job that ran it. */
export function agentOf(block: DecisionBlock): AgentKey | null {
  if (block.who.kind !== 'agent') return null
  const d = block.decision
  return agentOfDecision({
    agent: block.who.id, action: d.action, kind: d.kind, polarity: d.polarity,
    lens: d.lens == null ? undefined : String(d.lens), targets: d.targets,
  })
}

export function whoOf(block: DecisionBlock): WhoIs {
  if (block.who.kind === 'person') return { agent: null, name: block.who.you ? 'You' : block.who.name, person: true }
  const a = agentOf(block)
  if (a) {
    const ag = AGENTS[a]
    return { agent: a, name: ag.lens ? (RESEARCHER[a] ?? `${ag.name} researcher`) : ag.name, person: false }
  }
  // Research that names no lens (the merge across lenses) is the researchers'.
  if (/^research/.test(block.decision.action)) return { agent: 'market_structure', name: 'The researchers', person: false }
  return { agent: null, name: block.who.name, person: false }
}

const fixPlurals = (s: string) =>
  s.replace(/(\d+) (\w+)\((e?s)\)/g, (_, n: string, w: string, suf: string) => `${n} ${w}${n === '1' ? '' : suf}`)

/** A short name per lens researcher. */
const RESEARCHER: Record<string, string> = {
  market_structure: 'Market researcher', positioning: 'Brands researcher', culture: 'Culture researcher',
  codes: 'Codes researcher', rhythm: 'Timing researcher', media: 'Media researcher',
  regulation: 'Regulation researcher', effectiveness: 'Effectiveness researcher',
}

/**
 * An agent's own words, said of what it did. The chain holds them as the
 * agent wrote them ("Research 2 lenses…; skip 6", "Asked the person…"); a
 * line reads in the past, to you.
 */
function pastTense(s: string): string {
  const m = /^Research (\d+) lens(?:\(es\)|es)? (?:across|in) (.+?); skip (\d+)/i.exec(s)
  if (m) return `chose ${m[1]} lens${m[1] === '1' ? '' : 'es'} to research in ${m[2]} and skipped ${m[3]}`
  let out = s.replace(/\bthe person\b/g, 'you')
  const first = out.split(' ')[0].toLowerCase()
  const past = /ed$/.test(first) || ['ran', 'read', 'set', 'found', 'wrote', 'made', 'took', 'chose', 'kept', 'left', 'held', 'gave'].includes(first)
  if (!past) out = `decided to ${lower(out)}`
  return out
}

const lower = (s: string) => (/^[A-Z][a-z]/.test(s) ? s.charAt(0).toLowerCase() + s.slice(1) : s)
const clip = (s: string, n: number) => (s.length > n ? s.slice(0, n - 1).trimEnd() + '…' : s)
const strip = (label: string) => label.replace(/^(Finding|Fact|Contest|Decision) · /, '')

/** The first place in this document a decision is about. */
export function mainTarget(block: DecisionBlock) {
  return block.targets.find(t => t.here && t.kind !== 'document' && !/^Intake/.test(t.label))
    ?? block.targets.find(t => t.here)
}

/**
 * What the decision did, after the name: "verified “…”", "picked 2 of 8
 * lenses …". A person's decision is said from its kind; an agent's from what
 * it decided.
 */
export function didWhat(block: DecisionBlock, view: DecisionsView): string {
  const d = block.decision
  const t = mainTarget(block)
  const what = t ? strip(t.label) : 'the document'
  if (block.who.kind === 'person') {
    switch (d.action) {
      case 'verify_finding': return `verified “${clip(what, 90)}”`
      case 'reject_finding': return `rejected “${clip(what, 90)}”`
      case 'looks_right': return `accepted an agent’s call: ${lower(clip(what, 80))}`
      case 'resolve_contest': {
        const chosen = (d.cites ?? []).map(c => view.cites[c]).find(c => c?.kind === 'fact')
        return chosen?.value ? `chose ${chosen.value} for ${what}` : `settled ${what}`
      }
      case 'classify': return `marked ${what} confidential`
      case 'lock': return 'locked the document'
      case 'start_campaign': case 'create': return 'started the research'
      case 'answer_question': return 'answered a question from the agents'
      case 'edit_text': return /^Report/.test(what) ? 'rewrote part of the report' : 'rewrote some wording'
      case 'restore_text': return 'put the original wording back'
      case 'edit_field': return `changed ${what}`
      case 'correct_fact': return `corrected ${what}`
    }
    if (d.kind === 'verdict') return `marked ${what} ${d.polarity === 'bad' ? 'wrong' : 'right'}`
    return `changed ${what}`
  }
  if (d.action === 'research_merge') {
    const facts = (d.cites ?? []).filter(c => view.cites[c]?.kind === 'fact').length
    const n = sourcesOf(block, view).length
    return `pinned ${facts} fact${facts === 1 ? '' : 's'}${n ? ` from ${n} source${n === 1 ? '' : 's'}` : ''}`
  }
  const said = d.reasoning?.decided || d.rationale.split(/\. Because/)[0] || d.action.replace(/_/g, ' ')
  return lower(pastTense(fixPlurals(said.replace(/\.$/, ''))))
}

const LENS_WORDS: Record<string, string> = {
  market_structure: 'market structure', brands_positioning: 'brands and positioning', consumer_culture: 'consumer culture',
  category_codes: 'category codes', rhythm_moments: 'timing', media_spend: 'media and spend',
  regulation_clearance: 'regulation', effectiveness_evidence: 'effectiveness',
}

/**
 * An agent's sentence without the machine in it: lens ids in words, material
 * and decision ids dropped (the chips carry them), "(s)" plurals settled.
 */
export function plain(text: string): string {
  return fixPlurals(String(text ?? ''))
    .replace(/\s*\((?:from |in )?(?:mat|d|msg|src|f|fi)_[0-9A-Za-z]{6,}\)/g, '')
    .replace(/\b(?:mat|msg)_[0-9A-Z]{8,}\b/g, 'your request')
    .replace(/\b([a-z]+_[a-z]+)\b/g, (w: string) => LENS_WORDS[w] ?? w)
    .replace(/\s{2,}/g, ' ')
    .trim()
}

/** "Unsure", "Fairly sure", "Confident" — one scale, words only. */
export function sureWord(level?: string): string | null {
  return level === 'low' ? 'Unsure' : level === 'medium' ? 'Fairly sure' : level === 'high' ? 'Confident' : null
}

/** The id an address names, for the app to open: a pin, finding, contest… or the path. */
export function refOfAddress(address?: string): string {
  const p = String(address ?? '').split('#')[1] ?? String(address ?? '')
  const m = /^(?:findings|facts|materials)\[([^\]]+)\]$/.exec(p) ?? /^selection\.contested\[([^\]]+)\]$/.exec(p)
  return m ? m[1] : p
}

/** Every source a decision rests on: cited directly, or behind a fact it cites. */
export function sourcesOf(block: DecisionBlock, view: DecisionsView): { id: string; cite: CiteInfo; facts: number }[] {
  const all = new Map<string, number>()
  for (const c of citesOf(block)) {
    const info = view.cites[c]
    if (!info) continue
    if (info.kind === 'source') all.set(c, all.get(c) ?? 0)
    for (const s of info.sources ?? []) all.set(s, (all.get(s) ?? 0) + 1)
  }
  return [...all.entries()]
    .map(([id, facts]) => ({ id, cite: view.cites[id], facts }))
    .filter(x => x.cite)
    .sort((a, b) => b.facts - a.facts)
}

/** Everything a decision cites: its own cites and each reason's. */
export function citesOf(block: DecisionBlock): string[] {
  const d = block.decision
  return [...new Set([...(d.cites ?? []), ...(d.reasoning?.because ?? []).flatMap(p => p.cites ?? [])])]
}

/** A short name for a cite, on a chip: "52.8% · ShelfLife Magazine", "a finding", "your request". */
export function chipOf(id: string, view: DecisionsView): string | null {
  const c = view.cites[id]
  if (!c) return null
  if (c.kind === 'fact') {
    const src = (c.sources ?? []).map(s => view.cites[s]?.publisher).find(Boolean)
    const v = c.value && c.value.length <= 24 ? c.value : clip(c.label.replace(/ · [A-Z]{2}$/, ''), 36)
    return [v, src].filter(Boolean).join(' · ')
  }
  if (c.kind === 'source') return c.publisher ?? clip(c.label, 40)
  if (c.kind === 'finding') return `finding: ${clip(c.label, 40)}`
  if (c.kind === 'material') return /prompt|request/i.test(c.label) ? 'your request' : clip(c.label, 40)
  if (c.kind === 'person') return c.label
  return null
}

/** Whether a cite opens something in the app (its evidence). */
export function opensInApp(id: string, view: DecisionsView): boolean {
  const k = view.cites[id]?.kind
  return k === 'fact' || k === 'finding' || k === 'source'
}

export interface Group {
  key: string
  title: string
  sub: string
  blocks: DecisionBlock[]
  /** What "Show" opens, for a section. */
  open?: { ref: string; path?: string }
}

const TIME = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit' })
const DAY = new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'short' })
export function hhmm(ts: string): string {
  const t = Date.parse(ts)
  return Number.isNaN(t) ? '' : TIME.format(t)
}
function dayOf(ts: string): string {
  const t = Date.parse(ts)
  if (Number.isNaN(t)) return ''
  return new Date(t).toDateString() === new Date().toDateString() ? 'today' : DAY.format(t)
}

/**
 * Runs, newest first: a job's steps together, a person's decisions together,
 * split where more than half an hour passes. Within a run, newest first.
 */
export function runsOf(view: DecisionsView): Group[] {
  const out: Group[] = []
  let cur: Group | null = null
  let last = 0
  for (const b of view.decisions) {
    const person = b.who.kind === 'person'
    const k = person ? `p:${b.who.id}` : `a:${b.decision.handler ?? b.who.id}`
    const t = Date.parse(b.decision.timestamp) || 0
    if (!cur || cur.key.split('|')[0] !== k || Math.abs(last - t) > 30 * 60 * 1000) {
      cur = { key: `${k}|${out.length}`, title: '', sub: '', blocks: [] }
      out.push(cur)
    }
    cur.blocks.push(b)
    last = t
  }
  for (const g of out) {
    const b0 = g.blocks[0], bn = g.blocks[g.blocks.length - 1]
    const person = b0.who.kind === 'person'
    g.title = person ? (b0.who.you ? 'Your review' : `${b0.who.name}’s review`)
      : /campaign|research/i.test(b0.decision.handler ?? b0.who.id) ? 'Research run' : `${b0.who.name} run`
    const from = hhmm(bn.decision.timestamp), to = hhmm(b0.decision.timestamp)
    g.sub = `${dayOf(b0.decision.timestamp)} ${from === to ? from : `${from} → ${to}`} · ${g.blocks.length} step${g.blocks.length === 1 ? '' : 's'}`
  }
  return out
}

/** By where in the document each decision landed, the latest section first. */
export function sectionsOf(view: DecisionsView): Group[] {
  const by = new Map<string, Group>()
  for (const b of view.decisions) {
    const t = mainTarget(b)
    const label = t ? sectionLabel(t.label) : 'The document'
    let g = by.get(label)
    if (!g) {
      g = { key: `s:${label}`, title: label, sub: '', blocks: [],
        open: t ? { ref: refOfAddress(t.address), path: t.path } : undefined }
      by.set(label, g)
    }
    g.blocks.push(b)
  }
  const out = [...by.values()]
  for (const g of out) g.sub = `${g.blocks.length} decision${g.blocks.length === 1 ? '' : 's'}`
  return out
}

function sectionLabel(label: string): string {
  if (/^Finding · /.test(label)) return 'Findings'
  if (/^Fact · /.test(label)) return 'Facts'
  if (/^Contest · /.test(label)) return 'Where sources disagree'
  if (/^Selection/.test(label)) return 'What research covered'
  return label.split(' · ')[0]
}

export interface Redo {
  label: string
  task: string
  input: Record<string, unknown>
}

const LENS_NAME: Record<string, string> = {
  market_structure: 'Market structure', brands_positioning: 'Brands and positioning', consumer_culture: 'Consumer culture',
  category_codes: 'Category codes', rhythm_moments: 'Timing', media_spend: 'Media and spend',
  regulation_clearance: 'Regulation', effectiveness_evidence: 'Effectiveness',
}

/**
 * The step again, as the app's own task: a lens's research for its market,
 * synthesis, the audience without the rejected findings, the report. `null`
 * for a step no task reruns (reading the request, choosing lenses).
 */
export function redoOf(block: DecisionBlock): Redo | null {
  const d = block.decision
  if (block.who.kind !== 'agent') return null
  if (d.action === 'research_run') {
    const m = (d.targets ?? []).map(t => /lenses_run\[([a-z_]+)\/([A-Z]{2})\]/.exec(t)).find(Boolean)
    return m ? { label: 'Research again', task: 'research_lens', input: { lenses: [m[1]], markets: [m[2]] } } : null
  }
  if (d.action === 'propose_audience') return { label: 'Redo the audience', task: 'synthesise_findings', input: { redo: 'audience' } }
  if (d.action === 'synthesise_finding' || d.kind === 'finding') return { label: 'Ask Synthesis again', task: 'synthesise_findings', input: {} }
  if (d.action === 'report' || d.action === 'compose_report') return { label: 'Write the report again', task: 'compose_report', input: {} }
  return null
}

/** The lenses a Judge's selection skipped, each researchable on its own. */
export function skippedOf(block: DecisionBlock): { lens: string; name: string }[] {
  if (block.decision.action !== 'select') return []
  return [...new Set((block.decision.targets ?? [])
    .map(t => /lenses_skipped\[([a-z_]+)\]/.exec(t)?.[1]).filter((x): x is string => !!x))]
    .map(lens => ({ lens, name: LENS_NAME[lens] ?? lens }))
}
