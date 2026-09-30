// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// Client review, as words and a request: what the recorder has filled in, how
// strong a record it makes, the body `POST /client-review` takes, and what
// Jude and Ellis say about it afterwards (OS-layer contract §7.5, §8.2). No
// React and no host here, so the rules are tested as they are.
//
// The owner's prototype (client review, version 4) is the reference: the
// whole document's answer comes first, Accepted is one tap and Save, and the
// other two open an optional evidence area. A part is marked on the document
// itself, by the app's fields; this only carries what was marked.

import { AGENTS } from '../../studio/model'
import type {
  ClientAnswerKind, ClientAnswerView, ClientChannel, ClientPartRef, ClientReason, ClientReviewBody, ClientView,
  ClientWho, DecisionsView,
} from '../../host'

/** How the answer reached the recorder, as the evidence area offers it. */
export type How = 'email' | 'file' | 'call'

/** The client's file, held in memory until Save; nothing is stored before. */
export interface HeldFile {
  name: string
  bytes: Uint8Array
}

/** What the recorder has filled in so far. Discarded when the mode ends. */
export interface Draft {
  answer: ClientAnswerKind | null
  how: How
  /** The email as pasted — verbatim; never trimmed inside or rewritten. */
  pasted: string
  /** The recorder's note of a call. */
  note: string
  file: HeldFile | null
  reasons: ClientReason[]
  name: string
  email: string
}

/** A part the recorder marked on the document, with what they typed under it. */
export interface Mark {
  marked: boolean
  words: string
}

export const emptyDraft = (who?: { name?: string; email?: string } | null): Draft => ({
  answer: null, how: 'email', pasted: '', note: '', file: null, reasons: [],
  name: who?.name ?? '', email: who?.email ?? '',
})

export const ANSWERS: readonly { kind: ClientAnswerKind; title: string; sub: string }[] = [
  { kind: 'accepted', title: 'Accepted', sub: 'All of it. Nothing to add.' },
  { kind: 'accepted_with_changes', title: 'Accepted with changes', sub: 'Yes, if some things change.' },
  { kind: 'rejected', title: 'Rejected', sub: 'Not like this.' },
]

export const REASONS: readonly { code: ClientReason; label: string }[] = [
  { code: 'off_brief', label: 'Off brief' },
  { code: 'wrong_audience', label: 'Wrong audience' },
  { code: 'tone', label: 'Tone' },
  { code: 'facts_wrong', label: 'Facts wrong' },
  { code: 'budget', label: 'Budget' },
  { code: 'other', label: 'Other' },
]

export const reasonWords = (codes: readonly string[]) =>
  codes.map(c => (REASONS.find(r => r.code === c)?.label ?? c).toLowerCase()).join(', ')

/** The files the host keeps as the client's evidence (§7.5.2, `evidence`). */
export const EVIDENCE_TYPES = '.eml,.pdf,message/rfc822,application/pdf'
export const isEvidenceFile = (name: string) => /\.(eml|pdf)$/i.test(name)

/** "Mary" from "Mary Kelly"; the client, when no name is typed yet. */
export function firstName(name: string): string {
  return name.trim().split(/\s+/)[0] || ''
}
const whose = (name: string) => (firstName(name) ? `${firstName(name)}’s` : 'the client’s')

/** Something worth saying: more than whitespace. The text itself is kept as typed. */
const has = (s: string) => s.trim() !== ''

/** How the answer arrived, as the record's `channel` and `said` (§7.5.2). */
export function channelOf(d: Draft): { channel: ClientChannel; said?: string } {
  // Accepted is one tap: the evidence area is not open, so nothing in it counts.
  if (!d.answer || d.answer === 'accepted') return { channel: 'none' }
  if (d.how === 'email' && has(d.pasted)) return { channel: 'pasted_email', said: d.pasted }
  if (d.how === 'file' && d.file) return { channel: 'file' }
  if (d.how === 'call' && has(d.note)) return { channel: 'call', said: d.note }
  return { channel: 'none' }
}

/**
 * How strong a record this makes, in the words the record will be read by:
 * strong with the client's own file attached, weaker otherwise — pasted text,
 * a call note, or nothing (§7.5.2, `evidence.strength`; §7.5.8).
 */
export function evidenceOf(d: Draft): { strong: boolean; line: string } {
  const { channel } = channelOf(d)
  switch (channel) {
    case 'file': {
      const pdf = /\.pdf$/i.test(d.file?.name ?? '')
      return { strong: true, line: pdf ? `From the PDF ${firstName(d.name) || 'the client'} sent, file attached` : `From ${whose(d.name)} email, file attached` }
    }
    case 'pasted_email': return { strong: false, line: `From ${whose(d.name)} email, pasted by you` }
    case 'call': return { strong: false, line: 'Your note of a call. Nothing attached' }
    default: return { strong: false, line: 'Recorded by you. Nothing attached' }
  }
}

/** The evidence line as the panel shows it before Save. */
export const strengthLine = (d: Draft) => {
  const e = evidenceOf(d)
  return `${e.strong ? 'Strong record' : 'Weaker record'}: ${e.line}.`
}

const EMAIL = /^[^@\s]+@[^@\s]+$/

/** Why Save cannot go yet, in words; null when it can. */
export function problemOf(d: Draft): string | null {
  if (!d.answer) return 'Choose what the client said first.'
  if (!has(d.name)) return 'Say who answered: their name goes on the record.'
  if (d.name.trim().length > 120) return 'That name is too long. Keep it under 120 characters.'
  if (has(d.email) && !EMAIL.test(d.email.trim())) return 'That email address does not look right.'
  return null
}

/** The parts marked, for this answer: none for Accepted, which marks every part. */
export function markedOf(d: Draft, parts: readonly ClientPartRef[], marks: Readonly<Record<string, Mark>>) {
  if (!d.answer || d.answer === 'accepted') return []
  return parts
    .filter(p => marks[p.address]?.marked)
    .map(p => {
      const words = marks[p.address]?.words ?? ''
      return { address: p.address, answer: d.answer as ClientAnswerKind, ...(has(words) ? { words } : {}) }
    })
}

/**
 * The name the client's file is stored under: its own name, in characters
 * every host reads the same, after when it was attached — so a second email
 * called "Re: brief.eml" never replaces the first.
 */
export function assetNameOf(fileName: string, at: Date): string {
  const dot = fileName.lastIndexOf('.')
  const ext = dot > 0 ? fileName.slice(dot + 1).toLowerCase() : ''
  const stem = (dot > 0 ? fileName.slice(0, dot) : fileName).replace(/[^A-Za-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 60)
  const when = at.toISOString().replace(/[-:]/g, '').replace(/\.\d+Z$/, 'Z')
  return `client-${when}-${stem || 'file'}${ext ? `.${ext}` : ''}`
}

/** `POST /client-review`'s body. `asset` is the stored file's path, when there is one. */
export function bodyOf(
  d: Draft, parts: readonly ClientPartRef[], marks: Readonly<Record<string, Mark>>, asset?: string,
): ClientReviewBody {
  if (!d.answer) throw new Error('no answer yet')
  const { channel, said } = channelOf(d)
  const email = d.email.trim()
  const body: ClientReviewBody = {
    answer: d.answer,
    client: { name: d.name.trim(), ...(email ? { email } : {}) },
    channel,
    parts: parts.map(p => ({ address: p.address, label: p.label })),
  }
  if (d.answer === 'rejected' && d.reasons.length) body.reasons = [...d.reasons]
  if (said !== undefined) body.said = said
  if (channel === 'file') body.asset = asset
  const marked = markedOf(d, parts, marks)
  if (marked.length) body.marked = marked
  return body
}

/**
 * Whether the host will ask Ellis which parts were meant (§8.2 item 1): not
 * accepted, nothing marked, parts declared, and words to read. Only for
 * saying "Ellis is reading"; the host decides.
 */
export function asksEllis(d: Draft, parts: readonly ClientPartRef[], marks: Readonly<Record<string, Mark>>): boolean {
  if (!d.answer || d.answer === 'accepted' || !parts.length) return false
  if (markedOf(d, parts, marks).length) return false
  const { channel } = channelOf(d)
  return channel === 'pasted_email' || channel === 'call' || channel === 'file'
}

/** A part address the host takes (§7.5.1): dotted keys, not host-written. */
const PART_PATH = /^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$/
const inside = (a: string, b: string) => a === b || a.startsWith(`${b}.`)

/**
 * The parts an app declared (`clan:parts`), as the host will take them: a
 * dotted data path each, a label of at most 80 characters, at most 100, none
 * inside another. One the host would refuse is left out here, with a word in
 * the console for the app's author, rather than refuse the client's whole
 * answer at Save.
 */
export function partsFrom(raw: unknown): ClientPartRef[] {
  const out: ClientPartRef[] = []
  for (const p of Array.isArray(raw) ? raw : []) {
    const address = typeof p?.address === 'string' ? p.address.trim() : ''
    const label = typeof p?.label === 'string' ? p.label.trim().slice(0, 80) : ''
    const why = !PART_PATH.test(address) ? 'is not a dotted data path'
      : /^(upstream|projection)(\.|$)/.test(address) ? 'is written by the host, not a part'
        : !label ? 'has no label'
          : out.some(q => inside(q.address, address) || inside(address, q.address)) ? 'is, or holds, a part declared before it'
            : out.length >= 100 ? 'is past the 100 parts one review takes' : ''
    if (why) { console.warn(`client review: part ${JSON.stringify(address)} ${why}; left out`); continue }
    out.push({ address, label })
  }
  return out
}

/** What the thing is called: the app's own noun where the shell knows it. */
export function nounOf(appId?: string | null): string {
  const id = String(appId ?? '')
  if (/brief/.test(id)) return 'brief'
  if (/research|report/.test(id)) return 'report'
  return 'document'
}

// ── after Save ─────────────────────────────────────────────────────────────

/** The banner's first words, per answer. */
export const bannerWords = (answer: ClientAnswerKind, noun: string) =>
  answer === 'accepted' ? `Client accepted the ${noun}`
    : answer === 'accepted_with_changes' ? 'Client accepted it with changes'
      : `Client rejected the ${noun}`

/** How the saved record reads its evidence (§7.5.8), said to its recorder. */
export function recordedFrom(a: ClientAnswerView, you: boolean): string {
  const by = you ? 'you' : a.recorded_by.name || 'someone'
  const name = firstName(a.client.name)
  const asset = a.evidence.asset?.split('/').pop() ?? ''
  switch (a.channel) {
    case 'file':
      return /\.pdf$/i.test(asset) ? `From the PDF ${name || 'the client'} sent, file attached` : `From ${whose(a.client.name)} email, file attached`
    case 'pasted_email': return `From ${whose(a.client.name)} email, pasted by ${by}`
    case 'call': return `${you ? 'Your' : `${by}’s`} note of a call. Nothing attached`
    default: return `Recorded by ${by}. Nothing attached`
  }
}

/** Parts still waiting on a change the client asked for. */
export function openParts(client: ClientView): number {
  return client.parts.filter(p => p.state !== 'accepted' && !p.answered).length
}

export interface Say {
  agent: 'judge' | 'extract'
  state: 'idle' | 'working' | 'needs-you'
  text: string
}

/**
 * What the crew says while the mode is on: Jude asks for the whole
 * document's answer, then says what Save will do with it.
 */
export function modeLine(d: Draft, noun: string): Say {
  const jude = 'judge' as const
  if (!d.answer) return { agent: jude, state: 'needs-you', text: `What did the client say about the ${noun} as a whole?` }
  if (d.answer === 'accepted') return { agent: jude, state: 'needs-you', text: 'All accepted: just save. I’ll mark every part as accepted on this version.' }
  return {
    agent: jude, state: 'needs-you',
    text: `If you know which parts, mark them on the ${noun}. If you don’t, add what they sent and ${AGENTS.extract.given} will find the parts.`,
  }
}

/**
 * What the crew says once an answer is recorded, from the host's view: Ellis
 * while reading, then Jude on what is left to do. Null when there is no
 * client answer to speak of.
 */
export function afterLine(view: DecisionsView | null, noun: string, reading: string | null): Say | null {
  const c = view?.client
  if (reading) return { agent: 'extract', state: 'working', text: `Reading what ${reading} sent, to find the parts they meant.` }
  const a = c?.answer
  if (!c || !a) return null
  const who = firstName(a.client.name) || 'The client'
  const ellis = AGENTS.extract.given
  if (a.answer === 'accepted') return { agent: 'judge', state: 'idle', text: `${who} accepted the whole ${noun} on the locked version. It’s signed off.` }
  const reopened = view?.lock.reopened?.length ?? 0
  if (reopened) return { agent: 'judge', state: 'needs-you', text: `${reopened === 1 ? 'A part is' : `${reopened} parts are`} open for the change ${who} asked for. Lock it again when it’s done.` }
  if (c.suggestions.some(s => s.review === a.decision)) {
    return { agent: 'judge', state: 'needs-you', text: `${ellis} found the parts ${who} may have meant. Check each one: only the parts you confirm get the client’s answer.` }
  }
  if (!a.parts_known) {
    return {
      agent: 'judge', state: 'needs-you',
      text: `We don’t know which parts ${who} meant yet. Mark them${a.answer === 'rejected' ? ' before it can be locked again' : ''}.`,
    }
  }
  const open = openParts(c)
  const parts = `${open} part${open === 1 ? '' : 's'}`
  if (open) return { agent: 'judge', state: 'needs-you', text: `${who} ${a.answer === 'rejected' ? `rejected ${parts}` : `asked for changes on ${parts}`}. Tap a part’s mark on the ${noun}, then Make this change.` }
  return { agent: 'judge', state: 'idle', text: `Every change ${who} asked for is made. Lock it again and send the new version.` }
}

/** One of Ellis's suggestions a person has already answered, as the band keeps it. */
export interface SettledSuggestion {
  suggestion: string
  label: string
  answer: string
  quote: string
  confirmed: boolean
  /** The suggestion's place in the chain (newest first), to keep the list in its order. */
  at: number
}

/**
 * Ellis's suggestions on this answer that a person has said yes or no to,
 * oldest first like the open ones, so the recorder still sees what they just
 * did: a part answer naming its `suggestion` confirmed it, a `dismiss_part`
 * turned it down. One closed by a new lock is not shown (it was not answered).
 */
export function settledSuggestions(view: DecisionsView, review: string): SettledSuggestion[] {
  const at = (id: string) => view.decisions.findIndex(b => b.decision.id === id)
  const out: SettledSuggestion[] = []
  for (const b of view.decisions) {
    const d = b.decision
    if (b.superseded || d.kind !== 'client_review' || d.review !== review || typeof d.suggestion !== 'string') continue
    const confirmed = d.action === 'client_answer_part'
    if (!confirmed && !(d.action === 'dismiss_part' && d.closed_by == null)) continue
    if (out.some(o => o.suggestion === d.suggestion)) continue
    const i = at(d.suggestion), s = i >= 0 ? view.decisions[i].decision : d
    const str = (v: unknown) => (typeof v === 'string' ? v : '')
    out.push({
      suggestion: d.suggestion, confirmed, at: i,
      label: str(s.label) || str(d.label) || 'A part', answer: str(s.answer) || str(d.answer), quote: str(s.quote) || str(d.quote),
    })
  }
  // The chain is newest first: the oldest suggestion sits furthest down it.
  return out.sort((x, y) => y.at - x.at)
}

/**
 * The clients already on record, most recent first, so Accepted stays one tap:
 * this document's answers, then any carried from the document it was spun off
 * from (a carried `client_review` is shown, and counts for nothing, §7.5.3),
 * then names recorded in this session. One per name.
 */
export function knownClients(view: DecisionsView | null, session: readonly ClientWho[] = []): ClientWho[] {
  const out: ClientWho[] = []
  const add = (w: unknown) => {
    const c = (w && typeof w === 'object' ? w : {}) as { name?: unknown; email?: unknown }
    const name = typeof c.name === 'string' ? c.name.trim() : ''
    if (!name || out.some(o => o.name.toLowerCase() === name.toLowerCase())) return
    const email = typeof c.email === 'string' && c.email.trim() ? c.email.trim() : undefined
    out.push(email ? { name, email } : { name })
  }
  for (const a of view?.client?.answers ?? []) add(a.client)
  for (const b of view?.decisions ?? []) {
    if (b.decision.kind === 'client_review' && b.decision.action === 'client_answer') add(b.decision.client)
  }
  for (const w of session) add(w)
  return out
}

/** Whether the top bar offers Client review now: locked, and no part reopened. */
export function canReview(view: DecisionsView | null): boolean {
  if (!view || view.problem || !view.client) return false
  return !!view.lock.locked && !(view.lock.reopened?.length)
}
