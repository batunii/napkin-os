// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// What the queue does with a call: which cards the open items make, what a
// yes or a no needs before it counts, what it writes, what it sets moving.
// Pure functions over types.ts and model.ts, so the view only renders.

import { HUMAN_STEPS, OPEN_ITEM_ACTION, address, canLock } from '../model'
import type { Address, DocRef, OpenItem } from '../model'
import type { CardItem, Citer, DecisionCard, DecisionOutcome, Evidence, LogEntry } from './types'

// ── building the queue ──────────────────────────────────────────────────────

/** Evidence for an item, keyed `<doc id>:<item id>`. */
export type EvidenceIndex = Readonly<Record<string, Evidence>>

export const evidenceKey = (docId: string, itemId: string) => `${docId}:${itemId}`

/** The human step that settles an item. Branches and absent fields are settled in the document, not here. */
function stepOf(item: CardItem): DecisionCard['step'] | null {
  switch (item.kind) {
    case 'unconfirmed': return 'confirm'
    case 'contest': return 'resolve'
    case 'finding': return 'verify'
    case 'verdict': return 'verdict'
    default: return null
  }
}

function titleOf(item: CardItem, ev: Evidence): string {
  return ev.step === 'verify' ? ev.statement : item.label
}

export interface Queue {
  cards: DecisionCard[]
  /** Open items only the document can settle (a branch to merge, a field to fill), or with no evidence yet. */
  elsewhere: CardItem[]
}

/**
 * Cards for `items`, in order, and a lock card for each of `lockDocs`.
 *
 * A carried item (travelled in at a spin-off) is shown once, on the document
 * it was opened on; the carried copy still stops the downstream lock.
 */
export function buildQueue(items: readonly CardItem[], evidence: EvidenceIndex, lockDocs: readonly DocRef[], lockFrom: DecisionCard['from'] = 'synthesis'): Queue {
  const cards: DecisionCard[] = []
  const elsewhere: CardItem[] = []
  for (const item of items) {
    if (item.kind !== 'unconfirmed' && item.carried) continue
    const step = stepOf(item)
    const ev = evidence[evidenceKey(item.doc.id, item.id)]
    if (!step || !ev || ev.step !== step) { elsewhere.push(item); continue }
    cards.push({
      id: `${step}:${item.doc.id}:${item.id}`, step, item,
      from: item.from, instance: item.instance, doc: item.doc, address: item.address,
      title: titleOf(item, ev), evidence: ev,
    })
  }
  // Each lock comes straight after the last card on its document.
  for (const doc of lockDocs) {
    const last = cards.map(c => c.doc.id).lastIndexOf(doc.id)
    cards.splice(last < 0 ? cards.length : last + 1, 0, lockCard(doc, lockFrom))
  }
  return { cards, elsewhere }
}

export function lockCard(doc: DocRef, from: DecisionCard['from']): DecisionCard {
  return {
    id: `lock:${doc.id}`, step: 'lock', from, doc,
    address: address(doc.id, ''), title: `Lock ${doc.title}?`, evidence: { step: 'lock' },
  }
}

// ── the lock ────────────────────────────────────────────────────────────────

export interface LockState {
  /** What stops it: everything open on the document except findings. */
  blockers: OpenItem[]
  /** Proposed findings. The lock verifies them (os-layer §7), so they do not block it. */
  findings: OpenItem[]
  ready: boolean
}

export function lockState(open: readonly OpenItem[], docId: string): LockState {
  const mine = open.filter(i => i.doc.id === docId)
  const blockers = mine.filter(i => i.kind !== 'finding')
  return { blockers, findings: mine.filter(i => i.kind === 'finding'), ready: canLock(blockers, docId) }
}

// ── what a call needs ───────────────────────────────────────────────────────

/** What the person has done on a card so far. */
export interface Draft {
  /** Resolve: the fact id picked. */
  pick?: string
  reason: string
  /** Verdict: how the bad verdict is answered. */
  answer: 'revised' | 'override'
  /** The direction the reason field is open for, when it is not always open. */
  reasonFor: 'yes' | 'no' | null
}

export const EMPTY_DRAFT: Draft = { reason: '', answer: 'revised', reasonFor: null }

export type Dir = 'yes' | 'no'

/** Why a call cannot go through yet, if it cannot. `blocked` is a lock with open items. */
export type Need = 'pick' | 'reason' | 'blocked' | null

/**
 * Whether `dir` writes a decision whose rationale is required.
 *   resolve, lock          — HUMAN_STEPS: required
 *   verdict override       — overriding a bad verdict needs a written reason (os-layer §7)
 *   verify, no (reject)    — a rejection is a bad verdict (campaign-clan §6.3), HUMAN_STEPS.verdict: when-bad
 */
export function reasonRequired(card: DecisionCard, dir: Dir, draft: Draft): boolean {
  if (dir === 'yes') {
    if (card.step === 'verdict') return draft.answer === 'override' && HUMAN_STEPS.verdict.rationale !== 'optional'
    return HUMAN_STEPS[card.step].rationale === 'required'
  }
  return card.step === 'verify' && HUMAN_STEPS.verdict.rationale !== 'optional'
}

/** Whether the reason field shows for `dir` right now. */
export function reasonShown(card: DecisionCard, dir: Dir, draft: Draft): boolean {
  if (!reasonRequired(card, dir, draft)) return false
  // Always open where the yes itself needs one; opened on demand for a reject.
  return dir === 'yes' || draft.reasonFor === 'no'
}

export function needs(card: DecisionCard, dir: Dir, draft: Draft, lock?: LockState): Need {
  if (dir === 'yes' && card.step === 'lock' && lock && !lock.ready) return 'blocked'
  if (dir === 'yes' && card.step === 'resolve' && !draft.pick) return 'pick'
  if (reasonRequired(card, dir, draft) && !draft.reason.trim()) return 'reason'
  return null
}

// ── what a call writes ──────────────────────────────────────────────────────

export function outcomeOf(card: DecisionCard, dir: Dir, draft: Draft, lock: LockState | undefined, at: string): DecisionOutcome {
  const accepted = dir === 'yes'
  const reason = draft.reason.trim() || undefined
  const base = { card, accepted, at }
  const ev = card.evidence
  switch (card.step) {
    case 'confirm':
      return { ...base, writes: accepted ? HUMAN_STEPS.confirm.kind : null, rationale: accepted ? reason : undefined }
    case 'resolve':
      return accepted
        ? { ...base, writes: HUMAN_STEPS.resolve.kind, choice: draft.pick, rationale: reason }
        : { ...base, writes: null }
    case 'verify':
      return accepted
        ? { ...base, writes: HUMAN_STEPS.verify.kind, rationale: reason }
        : {
            ...base, writes: HUMAN_STEPS.verdict.kind, polarity: 'bad', rationale: reason,
            flags: ev.step === 'verify' ? ev.citedBy.map(c => c.address) : [],
          }
    case 'verdict':
      if (!accepted) return { ...base, writes: null }
      return draft.answer === 'override'
        ? { ...base, writes: HUMAN_STEPS.verdict.kind, polarity: 'good', answer: 'override', rationale: reason }
        : { ...base, writes: OPEN_ITEM_ACTION.verdict.kind, answer: 'revised', rationale: reason }
    case 'lock':
      return accepted
        ? { ...base, writes: HUMAN_STEPS.lock.kind, rationale: reason, verifies: lock?.findings.map(f => f.id) ?? [] }
        : { ...base, writes: null }
    case 'classify':
      return { ...base, writes: accepted ? HUMAN_STEPS.classify.kind : null }
  }
}

// ── what it sets moving ─────────────────────────────────────────────────────

export interface Applied {
  open: OpenItem[]
  /** New cards the call made: a rejected finding flags the fields that cite it. */
  cards: DecisionCard[]
  log: LogEntry[]
}

const sameItem = (a: OpenItem, doc: DocRef, id: string) => a.doc.id === doc.id && a.id === id

/** Open items after a call, the cards it adds, and its log lines (oldest first). */
export function apply(open: readonly OpenItem[], o: DecisionOutcome, seq: number): Applied {
  const { card } = o
  const ev = card.evidence
  const entry = (tone: LogEntry['tone'], head: string, text: string): LogEntry => ({
    id: `${card.id}#${seq}`, at: o.at, agent: card.from, instance: card.instance, doc: card.doc, tone, head, text,
  })
  const settle = (): OpenItem[] => (card.item ? open.filter(i => !sameItem(i, card.doc, card.item!.id)) : [...open])
  const why = o.rationale ? ` Reason: “${o.rationale}”` : ''
  const bare = (t: string) => t.replace(/[.\s]+$/, '')

  switch (ev.step) {
    case 'confirm':
      return o.accepted
        ? { open: [...open], cards: [], log: [entry('yes', 'Confirmed', `${ev.field} → ${ev.value}. It is yours now; no re-extraction writes over it.`)] }
        : { open: [...open], cards: [], log: [entry('no', 'Left to edit', `${ev.field} — correct it in ${card.doc.title}.`)] }

    case 'resolve': {
      if (!o.accepted) {
        return { open: [...open], cards: [], log: [entry('no', 'Contest left open', `${ev.field} — only that field waits; the rest keeps syncing.`)] }
      }
      const won = ev.values.find(v => v.id === o.choice) ?? ev.values[0]
      const lost = ev.values.find(v => v !== won)!
      return {
        open: settle(), cards: [],
        log: [entry('yes', 'Contest resolved', `${ev.field} → ${won.value} (${won.market}). ${lost.value} (${lost.market}) is kept, superseded.${why}`)],
      }
    }

    case 'verify': {
      if (o.accepted) {
        return { open: settle(), cards: [], log: [entry('yes', 'Verified', `${bare(ev.statement)} — now a brand fact, with you as its source.`)] }
      }
      // A rejection flags every field citing the finding: each is a new open item, and a new card.
      const flagged = ev.citedBy.map((c): OpenItem => ({
        id: `flag:${card.item?.id ?? card.id}:${c.address}`, kind: 'verdict', doc: card.doc,
        address: c.address, from: card.from, instance: card.instance,
        label: `${c.label} cites a finding you rejected.`,
      }))
      const cards = flagged.map((item, n) => flagCard(item, ev.citedBy[n], card, o.rationale ?? ''))
      const flags = ev.citedBy.length ? `flags ${ev.citedBy.map(c => c.label).join(', ')}` : 'no field cites it'
      return {
        open: [...settle(), ...flagged], cards,
        log: [entry('no', 'Rejected', `${bare(ev.statement)} — ${flags}; it stays out of the layers.${why}`)],
      }
    }

    case 'verdict':
      if (!o.accepted) return { open: [...open], cards: [], log: [entry('no', 'Left to revise', `${ev.field} — open it in ${card.doc.title}.`)] }
      return o.answer === 'override'
        ? { open: settle(), cards: [], log: [entry('yes', 'Verdict overridden', `${ev.field} stands as it is.${why}`)] }
        : { open: settle(), cards: [], log: [entry('yes', 'Verdict answered', `${ev.field} — marked revised.`)] }

    case 'lock': {
      if (!o.accepted) return { open: [...open], cards: [], log: [entry('no', 'Not locked', `${card.doc.title} stays open.`)] }
      const n = o.verifies?.length ?? 0
      const verified = n ? ` ${n} finding${n === 1 ? '' : 's'} verified with it.` : ''
      return {
        open: open.filter(i => i.doc.id !== card.doc.id), cards: [],
        log: [entry('yes', 'Locked and accepted', `${card.doc.title}.${verified} The seal is W5-Z1’s.${why}`)],
      }
    }
  }
}

/** The log line for a lock that cannot happen yet. */
export function blockedEntry(card: DecisionCard, lock: LockState, at: string, seq: number): LogEntry {
  const n = lock.blockers.length
  const what = lock.blockers.map(b => OPEN_ITEM_ACTION[b.kind].label.toLowerCase()).filter((v, i, a) => a.indexOf(v) === i)
  return {
    id: `${card.id}#${seq}`, at, agent: card.from, instance: card.instance, doc: card.doc, tone: 'blocked',
    head: 'Lock blocked', text: `${n} open item${n === 1 ? '' : 's'} on ${card.doc.title} (${what.join(', ')}).`,
  }
}

function flagCard(item: OpenItem, citer: Citer, from: DecisionCard, reason: string): DecisionCard {
  const ev = from.evidence
  return {
    id: `verdict:${item.doc.id}:${item.id}`, step: 'verdict', item,
    from: item.from, instance: item.instance, doc: item.doc, address: item.address, title: item.label,
    evidence: {
      step: 'verdict', cause: 'rejected-finding', field: citer.label, value: citer.value ?? '',
      by: 'you', rationale: reason,
      finding: ev.step === 'verify' && from.item ? { id: from.item.id, statement: ev.statement } : undefined,
    },
  }
}

// ── display ─────────────────────────────────────────────────────────────────

/** `7c1e9a42…#findings[fi_01JA0F2B]`; a bare document address reads as the whole document. */
export function shortAddress(a: Address): string {
  const [doc, path] = a.split('#')
  return `${doc.slice(0, 8)}…#${path || ' (whole document)'}`
}
