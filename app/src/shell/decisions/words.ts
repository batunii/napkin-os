// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// A decision, in words a person reads: who (the agent, or "You"), what they
// did, where in the document, and what it rests on. Never a raw id, a handler
// version or a model name — those stay in "Technical details".

import { AGENTS, agentForWork, agentOfDecision } from '../../studio/model'
import type { AgentKey } from '../../studio/model'
import type { CiteInfo, DecisionBlock, DecisionsView } from '../../host'

export interface WhoIs {
  /** The agent's figure, when an agent decided. */
  agent: AgentKey | null
  /** What the line calls them: an agent's own name ("Jude"), "You", or a person's name. */
  name: string
  /** An agent's job in plain words, for its signature ("Jude · checks"); empty for a person. */
  job: string
  person: boolean
}

/** The agent behind a block — the step's agent, not the job that ran it. */
export function agentOf(block: DecisionBlock): AgentKey | null {
  if (block.who.kind !== 'agent') return null
  const d = block.decision
  // A suggestion of which parts a client meant is Ellis's: the host's
  // matcher (process:host, client_parts_match, os-layer §7.5.2), or, in an
  // older chain, the middleware's find_client_parts it replaced.
  if (d.kind === 'client_review' || /^(client_parts_match|find_client_parts)\b/.test(d.handler ?? block.who.id)) return 'extract'
  // The step's own agent ("draft_brief@1.0/extract" is Ellis), as the apps'
  // fields read it; who.id is only the job that ran it.
  return agentOfDecision({
    agent: typeof d.agent === 'string' && d.agent ? d.agent : block.who.id, action: d.action, kind: d.kind, polarity: d.polarity,
    lens: d.lens == null ? undefined : String(d.lens), targets: d.targets,
  })
}

export function whoOf(block: DecisionBlock): WhoIs {
  if (block.who.kind === 'person') {
    return { agent: null, name: block.who.you ? 'You' : personName(block.who.name), job: '', person: true }
  }
  const a = agentOf(block)
  if (a) return { agent: a, name: AGENTS[a].given, job: AGENTS[a].job, person: false }
  // Research that names no lens (the merge across lenses) is the researchers',
  // together: no one of them did it.
  if (/^research/.test(block.decision.action)) return { agent: 'market_structure', name: 'The researchers', job: '', person: false }
  return { agent: null, name: block.who.name, job: '', person: false }
}

/**
 * A person as the line names them. The host knows a person only by their id;
 * on the web that is an opaque session id, which is nobody's name. An id an
 * app made from a typed name reads as the name again: hyphens were spaces, and
 * a word held wholly as letter codes (another script) is spelt out.
 */
function personName(name: string): string {
  const n = String(name ?? '').trim()
  if (!n || n === 'someone' || /^[0-9a-f]{8}-[0-9a-f]{4}-/i.test(n)) return 'Someone'
  return n.split('-').map(w => (/^(\.[0-9a-f]{2,4})+$/.test(w)
    ? w.slice(1).split('.').map(h => String.fromCharCode(parseInt(h, 16))).join('') : w)).join(' ')
}

/** "Jude · checks": an agent's signature, or just the name. */
export function signOf(w: WhoIs): string {
  return w.job ? `${w.name} · ${w.job}` : w.name
}

const fixPlurals = (s: string) =>
  s.replace(/(\d+) (\w+)\((e?s)\)/g, (_, n: string, w: string, suf: string) => `${n} ${w}${n === '1' ? '' : suf}`)

/**
 * An agent's own words, said of what it did. The chain holds them as the
 * agent wrote them ("Research 2 lenses…; skip 6", "Asked the person…"); a
 * line reads in the past, to you.
 */
function pastTense(s: string): string {
  const m = /^Research (\d+) lens(?:\(es\)|es)? (?:across|in) (.+?); skip (\d+)/i.exec(s)
  if (m) return `chose ${m[1]} thing${m[1] === '1' ? '' : 's'} to look into in ${m[2]} and left out ${m[3]}`
  let out = s.replace(/\bthe person\b/g, 'you').replace(/^ran (?=\S)/i, 'looked into ')
  const first = out.split(' ')[0].toLowerCase()
  const past = /ed$/.test(first) || ['ran', 'read', 'set', 'found', 'wrote', 'made', 'took', 'chose', 'kept', 'left', 'held', 'gave'].includes(first)
  if (!past) out = `decided to ${lower(out)}`
  return out
}

const lower = (s: string) => (/^[A-Z][a-z]/.test(s) ? s.charAt(0).toLowerCase() + s.slice(1) : s)
/** A line that starts a sentence: "Proposed …" under a signature. */
export const upper = (s: string) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : s)
const clip = (s: string, n: number) => (s.length > n ? s.slice(0, n - 1).trimEnd() + '…' : s)
const strip = (label: string) => label.replace(/^(Finding|Fact|Contest|Decision) · /, '')

let REGIONS: Intl.DisplayNames | null | undefined
/** "IE" → "Ireland"; the code itself where the browser has no name for it. */
export function countryOf(code: string): string {
  if (REGIONS === undefined) {
    try { REGIONS = new Intl.DisplayNames(['en'], { type: 'region' }) } catch { REGIONS = null }
  }
  try { return REGIONS?.of(code) ?? code } catch { return code }
}

/** A label's closing market code in words: "Private label share · IE" → "… · Ireland". */
export const withCountry = (label: string) => label.replace(/ · ([A-Z]{2})$/, (_, c: string) => ` · ${countryOf(c)}`)

/**
 * Where a decision landed, as its link reads: "Your request" for what the
 * person sent, a lens run by the researcher on it ("Cam · the shoppers"),
 * market codes as countries, no ids, and a long label cut with an ellipsis.
 */
export function whereOf(label: string): string | null {
  if (/^Intake\b/.test(label)) return 'Your request'
  const run = /^Selection › Lenses run · ([a-z_]+)\/([A-Z]{2})$/.exec(label)
  if (run) { const a = AGENTS[agentForWork('research', run[1])]; return `${a.given} · ${a.job}` }
  if (/^Selection › Lenses skipped/.test(label)) return 'What was left out'
  if (/^Selection › Lenses/.test(label)) return 'What to look into'
  return clip(plain(withCountry(label.replace(/ · [a-z]+_[0-9A-Za-z]{6,}$/, ''))), 60)
}

/**
 * A part of the document inside a sentence: "the campaign’s name", "the
 * private label share in Ireland". An id-like key on the end is dropped.
 */
function partOf(label: string): string {
  const s = strip(label).replace(/ · [a-z]+_[0-9A-Za-z]{6,}$/, '')
  const fact = /^(.+) · ([A-Z]{2})$/.exec(s)
  if (fact) return `the ${lower(fact[1])} in ${countryOf(fact[2])}`
  const parts = s.split(' › ')
  if (parts.length > 1) return `the ${lower(parts[parts.length - 2])}’s ${lower(parts[parts.length - 1])}`
  return lower(s)
}

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
  const client = clientLineOf(block, view)
  if (client) return client.rest
  if (block.who.kind === 'person') {
    switch (d.action) {
      case 'verify_finding': return `said “${clip(what, 90)}” is right`
      case 'reject_finding': return `turned down “${clip(what, 90)}”`
      case 'looks_right': return `agreed with the crew: ${lower(clip(what, 80))}`
      case 'resolve_contest': {
        const chosen = (d.cites ?? []).map(c => view.cites[c]).find(c => c?.kind === 'fact')
        return chosen?.value ? `chose ${chosen.value} for ${what}` : `settled ${what}`
      }
      case 'classify': return `marked ${what} confidential`
      case 'lock': return lockedBefore(block, view) ? 'locked it again' : 'locked the document'
      case 'start_campaign': case 'create': return 'started the research'
      case 'answer_question': return 'answered a question from the crew'
      case 'edit_text': {
        const part = typeof d.part === 'string' && d.part ? d.part : 'wording'
        return `rewrote the ${/^Report/.test(what) ? `report’s ${part}` : part}`
      }
      case 'restore_text': return 'put the original wording back'
      case 'edit_field': {
        // "Make this change": the edit answers a client's request (§7.5.5).
        const asked = typeof d.answers === 'string' ? clientOfDecision(d.answers, view) : null
        return `changed ${t ? partOf(t.label) : what}${asked ? `, as ${asked} asked` : typeof d.answers === 'string' ? ', as the client asked' : ''}`
      }
      case 'correct_fact': return `corrected ${t ? partOf(t.label) : what}`
    }
    if (d.kind === 'verdict') return `marked ${what} ${d.polarity === 'bad' ? 'wrong' : 'right'}`
    return `changed ${t ? partOf(t.label) : what}`
  }
  if (d.action === 'research_merge') {
    const facts = (d.cites ?? []).filter(c => view.cites[c]?.kind === 'fact').length
    const n = sourcesOf(block, view).length
    return `found ${facts} fact${facts === 1 ? '' : 's'}${n ? ` in ${n} source${n === 1 ? '' : 's'}` : ''}`
  }
  const said = d.reasoning?.decided || d.rationale.split(/\. Because/)[0] || d.action.replace(/_/g, ' ')
  return lower(pastTense(plain(said.replace(/\.$/, ''))))
}

// ── client review (OS-layer contract §7.5) ──────────────────────────────────

/** The part a client decision is about, as the app called it. */
// The part as the app declared it: the record's label, else the label of the
// suggestion it settles (a dismissal carries none), else the target's.
const partLabel = (block: DecisionBlock, view: DecisionsView) => {
  const own = block.decision.label
  if (typeof own === 'string' && own) return own
  const s = typeof block.decision.suggestion === 'string'
    ? view.decisions.find(b => b.decision.id === block.decision.suggestion)?.decision.label : undefined
  return typeof s === 'string' && s ? s : strip(mainTarget(block)?.label ?? 'a part')
}

/** The client named on a decision in the chain, by its id: an answer's, or the answer it belongs to. */
function clientOfDecision(id: string, view: DecisionsView, depth = 0): string | null {
  const d = view.decisions.find(b => b.decision.id === id)?.decision
  if (!d) return null
  const c = d.client as { name?: unknown } | undefined
  if (typeof c?.name === 'string' && c.name.trim()) return c.name.trim()
  if (depth > 2) return null
  const up = typeof d.review === 'string' ? d.review : typeof d.answers === 'string' ? d.answers : null
  return up ? clientOfDecision(up, view, depth + 1) : null
}

/** Who a client decision is about: its own client, or its review's. */
function clientOfBlock(block: DecisionBlock, view: DecisionsView): string {
  const d = block.decision
  const own = (d.client as { name?: unknown } | undefined)?.name
  if (typeof own === 'string' && own.trim()) return own.trim()
  for (const id of [d.review, d.answers, ...(d.cites ?? [])]) {
    if (typeof id !== 'string') continue
    const n = clientOfDecision(id, view)
    if (n) return n
  }
  return 'The client'
}

const PART_VERB: Record<string, string> = {
  accepted: 'accepted', accepted_with_changes: 'asked for a change to', rejected: 'rejected',
}
const DOC_VERB: Record<string, string> = {
  accepted: 'accepted it', accepted_with_changes: 'accepted it with changes', rejected: 'rejected it',
}
const REASON: Record<string, string> = {
  off_brief: 'off brief', wrong_audience: 'wrong audience', tone: 'tone', facts_wrong: 'facts wrong', budget: 'budget', other: 'other',
}

/**
 * How a client's answer was recorded, after "recorded by you": from what, in
 * the record's own evidence (§7.5.8) — the client's first name, never a
 * pronoun the record does not hold.
 */
function fromWhat(d: DecisionBlock['decision'], client: string): string {
  const first = client.split(/\s+/)[0]
  const whose = client === 'The client' ? 'the client’s' : `${first}’s`
  const asset = String((d.evidence as { asset?: unknown } | undefined)?.asset ?? '')
  switch (d.channel) {
    case 'file': return /\.pdf$/i.test(asset) ? `from the PDF ${client === 'The client' ? 'the client' : first} sent, attached` : `from ${whose} email, attached`
    case 'pasted_email': return `from ${whose} email, pasted`
    case 'call': return 'from a note of a call'
    default: return 'with nothing attached'
  }
}

/**
 * A client review line (§7.5.2), and the reopen it leads to: "Mary Kelly
 * (client) accepted it with changes · recorded by you from Mary’s email,
 * pasted". `subject` leads the line in bold; for the rest the line's own
 * signature is the subject. Null for any other decision.
 */
export function clientLineOf(block: DecisionBlock, view: DecisionsView): { subject: string | null; rest: string } | null {
  const d = block.decision
  if (d.kind !== 'client_review' && d.kind !== 'unlock') return null
  const client = clientOfBlock(block, view)
  const by = block.who.you ? 'you' : whoOf(block).name
  const label = partLabel(block, view)
  const answer = String(d.answer ?? '')
  switch (d.action) {
    case 'client_answer': {
      const reasons = Array.isArray(d.reasons) && d.reasons.length
        ? ` (${d.reasons.map(r => REASON[String(r)] ?? String(r)).join(', ')})` : ''
      return {
        subject: `${client} (client)`,
        rest: `${DOC_VERB[answer] ?? 'answered'}${reasons} · recorded by ${by} ${fromWhat(d, client)}`,
      }
    }
    case 'client_answer_part':
      return {
        subject: null,
        rest: d.found_by === 'agent'
          ? `confirmed ${AGENTS.extract.given}’s suggestion: ${client} ${PART_VERB[answer] ?? 'answered on'} ${label}`
          : `marked that ${client} ${PART_VERB[answer] ?? 'answered on'} ${label}`,
      }
    case 'suggest_part':
      return { subject: null, rest: `thinks ${client}’s answer is about ${label}. Only a person’s yes makes it count` }
    case 'dismiss_part':
      // Locking again closes each suggestion still open (§7.5.3): nobody said no to it.
      return typeof d.closed_by === 'string'
        ? { subject: null, rest: `locked it again; ${AGENTS.extract.given}’s suggestion about ${label} was left unanswered and is closed` }
        : { subject: null, rest: `said ${client}’s answer is not about ${label}` }
    case 'reopen_part':
      return { subject: null, rest: `reopened ${label} to make the change: ${clip(plain(d.rationale), 120)}` }
  }
  return { subject: null, rest: d.kind === 'unlock' ? `reopened ${label}` : `recorded a client’s answer on ${label}` }
}

/** An `approve` of this document older than this one: the lock after a reopen. */
function lockedBefore(block: DecisionBlock, view: DecisionsView): boolean {
  const at = view.decisions.indexOf(block)
  if (at < 0) return false
  return view.decisions.slice(at + 1).some(b =>
    b.decision.kind === 'approve' && !b.superseded && (b.decision.targets ?? []).includes(view.document_id))
}

const LENS_WORDS: Record<string, string> = {
  market_structure: 'market structure', brands_positioning: 'brands and positioning', consumer_culture: 'consumer culture',
  category_codes: 'category codes', rhythm_moments: 'timing', media_spend: 'media and spend',
  regulation_clearance: 'regulation', effectiveness_evidence: 'effectiveness',
}

/**
 * An agent's sentence without the machine in it: lens ids in words, material
 * and decision ids dropped (the chips carry them), "(s)" plurals settled, and
 * the crew's working words said the way a person would ("pins" are facts).
 */
export function plain(text: string): string {
  return fixPlurals(String(text ?? ''))
    .replace(/\s*\((?:from |in )?(?:mat|d|msg|src|f|fi)_[0-9A-Za-z]{6,}\)/g, '')
    .replace(/\b(?:mat|msg)_[0-9A-Z]{8,}\b/g, 'your request')
    .replace(/\b([a-z]+_[a-z]+)\b/g, (w: string) => LENS_WORDS[w] ?? w)
    .replace(/\bfinding for (?:a person|the person|you) to (?:verify|check)\b/g, 'point for you to check')
    .replace(/\bfor (?:a person|the person) to (?:verify|check)\b/g, 'for you to check')
    .replace(/\b(?:the )?client org\b/g, 'the client')
    .replace(/\bcoverage (?:was )?thin\b/g, 'found little')
    .replace(/\bpins\b/g, 'facts')
    .replace(/\bpin\b/g, 'fact')
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
    g.title = person ? (b0.who.you ? 'Your review' : `${whoOf(b0).name}’s review`) : crewOf(g.blocks)
    const from = hhmm(bn.decision.timestamp), to = hhmm(b0.decision.timestamp)
    g.sub = `${dayOf(b0.decision.timestamp)} ${from === to ? from : `${from} → ${to}`} · ${g.blocks.length} step${g.blocks.length === 1 ? '' : 's'}`
  }
  return out
}

/** "Ellis, Max and 3 more": who worked in a run, in the order they started. */
function crewOf(blocks: DecisionBlock[]): string {
  const n = [...new Set([...blocks].reverse().map(b => whoOf(b).name))]
  if (n.length < 2) return n.join('')
  if (n.length > 3) return `${n.slice(0, 2).join(', ')} and ${n.length - 2} more`
  return `${n.slice(0, -1).join(', ')} and ${n[n.length - 1]}`
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
  for (const g of out) g.sub = `${g.blocks.length} thing${g.blocks.length === 1 ? '' : 's'} done`
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

/** The name of the agent who does a kind of work, for "Ask Sam again". */
const givenFor = (work: Parameters<typeof agentForWork>[0], lens?: string) => AGENTS[agentForWork(work, lens)].given

/**
 * The step again, as the app's own task: a lens's research for its market,
 * synthesis, the audience without the rejected findings, the report. `null`
 * for a step no task reruns (reading the request, choosing lenses). The button
 * asks the agent who does that work, by name.
 */
export function redoOf(block: DecisionBlock): Redo | null {
  const d = block.decision
  if (block.who.kind !== 'agent') return null
  if (d.action === 'research_run') {
    const m = (d.targets ?? []).map(t => /lenses_run\[([a-z_]+)\/([A-Z]{2})\]/.exec(t)).find(Boolean)
    return m ? { label: `Ask ${givenFor('research', m[1])} to look again`, task: 'research_lens', input: { lenses: [m[1]], markets: [m[2]] } } : null
  }
  if (d.action === 'propose_audience') return { label: `Ask ${givenFor('synthesise')} to redo the audience`, task: 'synthesise_findings', input: { redo: 'audience' } }
  if (d.action === 'synthesise_finding' || d.kind === 'finding') return { label: `Ask ${givenFor('synthesise')} again`, task: 'synthesise_findings', input: {} }
  if (d.action === 'report' || d.action === 'compose_report') return { label: `Ask ${givenFor('draft')} to write the report again`, task: 'compose_report', input: {} }
  return null
}

/**
 * The lenses a Judge's selection skipped, each researchable on its own, by the
 * researcher who would look: "Remy · the timing".
 */
export function skippedOf(block: DecisionBlock): { lens: string; name: string }[] {
  if (block.decision.action !== 'select') return []
  return [...new Set((block.decision.targets ?? [])
    .map(t => /lenses_skipped\[([a-z_]+)\]/.exec(t)?.[1]).filter((x): x is string => !!x))]
    .map(lens => {
      const a = AGENTS[agentForWork('research', lens)]
      return { lens, name: a.lens === lens ? `${a.given} · ${a.job}` : LENS_NAME[lens] ?? lens }
    })
}

/**
 * What an edit changed, word by word: the words both versions share at the
 * start and the end are trimmed to a little context either side, so the line
 * shows the change itself — "…sameness, [with the sale…] [Flipkart can help?]".
 */
export function changeOf(was: string, now: string, context = 4): { before: string; cut: string; put: string; after: string } {
  const a = was.split(/\s+/).filter(Boolean)
  const b = now.split(/\s+/).filter(Boolean)
  let p = 0
  while (p < a.length && p < b.length && a[p] === b[p]) p++
  let q = 0
  while (q < a.length - p && q < b.length - p && a[a.length - 1 - q] === b[b.length - 1 - q]) q++
  const lead = a.slice(Math.max(0, p - context), p).join(' ')
  const tail = a.slice(a.length - q, a.length - q + context).join(' ')
  return {
    before: (p > context ? '…' : '') + lead,
    cut: a.slice(p, a.length - q).join(' '),
    put: b.slice(p, b.length - q).join(' '),
    after: tail + (q > context ? '…' : ''),
  }
}
