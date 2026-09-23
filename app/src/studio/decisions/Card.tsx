// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// One card in the queue. The top one is live: drag it, press its buttons or
// use ← →. A call that needs something first — a pick, a written reason —
// springs back and asks for it instead of going through, because a swipe
// cannot type a reason.

import { useCallback, useEffect, useRef, useState } from 'react'
import type { CSSProperties, KeyboardEvent as ReactKeyboardEvent, PointerEvent as ReactPointerEvent, RefObject } from 'react'
import { AgentAvatar } from '../AgentFigure'
import { HUMAN_STEPS, OPEN_ITEM_ACTION, agentLabel } from '../model'
import type { HumanStepId } from '../model'
import { EMPTY_DRAFT, needs, reasonShown, shortAddress } from './rules'
import type { Dir, Draft, LockState, Need } from './rules'
import type { Citation, DecisionCard } from './types'

/** Past this many px a drag counts as a call. */
const THRESHOLD = 110
const FLY_MS = 280

const LABELS: Readonly<Record<HumanStepId, { yes: string; no: string }>> = {
  confirm: { yes: 'Confirm', no: 'Edit it' },
  resolve: { yes: 'Resolve', no: 'Leave open' },
  verify: { yes: 'Verify', no: 'Reject' },
  verdict: { yes: 'Mark revised', no: 'Open the field' },
  classify: { yes: 'Mark confidential', no: 'Leave it' },
  lock: { yes: 'Lock and accept', no: 'Not yet' },
}

const REASON_LABEL: Readonly<Record<HumanStepId, string>> = {
  confirm: 'Note',
  resolve: 'Why this value?',
  verify: 'Why reject it?',
  verdict: 'Why does it stand?',
  classify: 'Note',
  lock: 'Why is it ready?',
}

function tagOf(card: DecisionCard): string {
  const item = card.item
  if (!item || item.kind === 'unconfirmed') return HUMAN_STEPS[card.step].label
  return OPEN_ITEM_ACTION[item.kind].label
}

function stampOf(label: string, need: Need): string {
  if (need === 'pick') return 'Pick one'
  if (need === 'reason') return 'Say why'
  if (need === 'blocked') return 'Blocked'
  return label
}

const reducedMotion = () => window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false

/** Keys typed into a field are the field's, not the queue's. */
function isTyping(t: EventTarget | null): boolean {
  if (!(t instanceof HTMLElement)) return false
  return t.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(t.tagName)
}

/** Stamps fade in with the drag: yes to the right, no to the left. */
function stamp(yes: HTMLElement | null, no: HTMLElement | null, dx: number) {
  if (yes) yes.style.opacity = String(Math.max(0, Math.min(1, dx / 100)))
  if (no) no.style.opacity = String(Math.max(0, Math.min(1, -dx / 100)))
}

/** Back to rest, animated. */
function settle(card: HTMLElement | null, yes: HTMLElement | null, no: HTMLElement | null) {
  if (card) { card.style.transition = ''; card.style.transform = '' }
  stamp(yes, no, 0)
}

// ── the header both kinds of card share ─────────────────────────────────────

function Who({ card }: { card: DecisionCard }) {
  return (
    <div className="dk-who">
      <AgentAvatar agent={card.from} size={34} decorative />
      <div>
        <b>{agentLabel(card.from, card.instance)}</b>
        <span>{card.doc.title}</span>
      </div>
      <span className="dk-tag">{tagOf(card)}</span>
    </div>
  )
}

/** A card under the top one: header and title only, out of the tab order. */
export function StackCard({ card, depth }: { card: DecisionCard; depth: number }) {
  const style = { '--dk-depth': depth } as CSSProperties
  return (
    <div className="dk-card dk-under" style={style} aria-hidden="true" inert>
      <Who card={card} />
      <h3>{card.title}</h3>
    </div>
  )
}

// ── the live card ───────────────────────────────────────────────────────────

interface TopProps {
  card: DecisionCard
  /** Lock cards: what stops it, what it verifies. */
  lock?: LockState
  /** Take focus on mount, when the last call came from a button or a key. */
  autoFocus: boolean
  /** The call went through (or, for a blocked lock, was tried). */
  onDone: (dir: Dir, draft: Draft, via: 'pointer' | 'keys') => void
}

export function TopCard({ card, lock, autoFocus, onDone }: TopProps) {
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT)
  /** What the last refused call asked for, and a counter so asking twice refocuses. */
  const [asked, setAsked] = useState<{ need: 'pick' | 'reason'; n: number } | null>(null)
  const leaving = useRef(false)
  const el = useRef<HTMLElement>(null)
  const head = useRef<HTMLHeadingElement>(null)
  const reason = useRef<HTMLInputElement>(null)
  const firstChoice = useRef<HTMLButtonElement>(null)
  const stampYes = useRef<HTMLSpanElement>(null)
  const stampNo = useRef<HTMLSpanElement>(null)
  const drag = useRef<{ x: number; dx: number; id: number } | null>(null)

  useEffect(() => {
    if (autoFocus) head.current?.focus({ preventScroll: true })
  }, [autoFocus])

  useEffect(() => {
    if (!asked) return
    if (asked.need === 'reason') reason.current?.focus()
    else firstChoice.current?.focus()
  }, [asked])

  const setStamps = (dx: number) => stamp(stampYes.current, stampNo.current, dx)
  const springBack = () => settle(el.current, stampYes.current, stampNo.current)

  const attempt = useCallback((dir: Dir, via: 'pointer' | 'keys') => {
    if (leaving.current) return
    const need = needs(card, dir, draft, lock)
    if (need === 'pick' || need === 'reason') {
      settle(el.current, stampYes.current, stampNo.current)
      if (dir === 'no') setDraft(d => ({ ...d, reasonFor: 'no' }))
      setAsked(a => ({ need, n: (a?.n ?? 0) + 1 }))
      return
    }
    // A blocked lock is tried, logged, and goes back: it flies left.
    const fly = need === 'blocked' ? 'no' : dir
    leaving.current = true
    const c = el.current
    const reduce = reducedMotion()
    if (c && !reduce) {
      c.style.transition = ''
      c.style.transform = `translateX(${fly === 'yes' ? 420 : -420}px) rotate(${fly === 'yes' ? 18 : -18}deg)`
      c.style.opacity = '0'
    }
    window.setTimeout(() => onDone(dir, draft, via), reduce ? 0 : FLY_MS)
  }, [card, draft, lock, onDone])

  // ← → decide the top card, unless a field has the keys.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.altKey || e.ctrlKey || e.metaKey || e.shiftKey || isTyping(e.target)) return
      if (e.key === 'ArrowRight') { e.preventDefault(); attempt('yes', 'keys') }
      if (e.key === 'ArrowLeft') { e.preventDefault(); attempt('no', 'keys') }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [attempt])

  const onPointerDown = (e: ReactPointerEvent<HTMLElement>) => {
    if (leaving.current || e.button !== 0) return
    if ((e.target as HTMLElement).closest('button, input, textarea, select, a, label')) return
    drag.current = { x: e.clientX, dx: 0, id: e.pointerId }
    if (el.current) el.current.style.transition = 'none'
    e.currentTarget.setPointerCapture(e.pointerId)
  }
  const onPointerMove = (e: ReactPointerEvent<HTMLElement>) => {
    const d = drag.current
    if (!d || d.id !== e.pointerId) return
    d.dx = e.clientX - d.x
    if (el.current) el.current.style.transform = `translateX(${d.dx}px) rotate(${d.dx / 22}deg)`
    setStamps(d.dx)
  }
  const onPointerEnd = () => {
    const d = drag.current
    if (!d) return
    drag.current = null
    if (Math.abs(d.dx) > THRESHOLD) attempt(d.dx > 0 ? 'yes' : 'no', 'pointer')
    else springBack()
  }

  const yesNeed = needs(card, 'yes', draft, lock)
  const noNeed = needs(card, 'no', draft, lock)
  const showYesReason = reasonShown(card, 'yes', draft) && yesNeed !== 'blocked'
  const showNoReason = reasonShown(card, 'no', draft)
  const ev = card.evidence

  let yesLabel = LABELS[card.step].yes
  if (ev.step === 'resolve' && draft.pick) yesLabel = `Resolve → ${ev.values.find(v => v.id === draft.pick)?.value}`
  if (ev.step === 'verdict' && draft.answer === 'override') yesLabel = 'Override'
  if (yesNeed === 'blocked') yesLabel = 'Try to lock'
  const noLabel = LABELS[card.step].no

  const yesDisabled = yesNeed === 'pick' || (yesNeed === 'reason' && showYesReason)
  const noDisabled = noNeed === 'reason' && showNoReason

  const onReasonKey = (e: ReactKeyboardEvent<HTMLInputElement>) => {
    if (e.key !== 'Enter') return
    e.preventDefault()
    attempt(showNoReason ? 'no' : 'yes', 'keys')
  }

  const hintId = `${card.id}-hint`
  let hint = ''
  if (asked?.need === 'pick' && !draft.pick) hint = 'Pick a value first, then resolve.'
  else if (asked?.need === 'reason' && !draft.reason.trim()) hint = 'This goes on the record with a reason. Type one line, then decide again.'

  return (
    <article
      ref={el}
      className="dk-card dk-top"
      aria-labelledby={`${card.id}-title`}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerEnd}
      onPointerCancel={onPointerEnd}
    >
      <span ref={stampYes} className="dk-stamp dk-stamp-yes" aria-hidden="true">{stampOf(yesLabel, yesNeed)}</span>
      <span ref={stampNo} className="dk-stamp dk-stamp-no" aria-hidden="true">{stampOf(noLabel, noNeed)}</span>

      <Who card={card} />
      <h3 id={`${card.id}-title`} ref={head} tabIndex={-1}>{card.title}</h3>

      <div className="dk-ev">
        <Evidence card={card} draft={draft} setDraft={setDraft} lock={lock} firstChoice={firstChoice} />

        {(showYesReason || showNoReason) && (
          <label className="dk-reason">
            <span>{REASON_LABEL[showNoReason ? 'verify' : card.step]} <em>On the record</em></span>
            <input
              ref={reason}
              type="text"
              value={draft.reason}
              maxLength={240}
              placeholder="One line"
              aria-required="true"
              aria-invalid={asked?.need === 'reason' && !draft.reason.trim() ? true : undefined}
              aria-describedby={hint ? hintId : undefined}
              onChange={e => setDraft(d => ({ ...d, reason: e.target.value }))}
              onKeyDown={onReasonKey}
            />
          </label>
        )}
        <p id={hintId} className="dk-hint-inline" aria-live="polite">{hint}</p>
      </div>

      <code className="dk-address" title={card.address}>{shortAddress(card.address)}</code>

      <div className="dk-acts">
        <button type="button" disabled={noDisabled} onClick={() => attempt('no', 'keys')}>{noLabel}</button>
        <button type="button" className="dk-yes" disabled={yesDisabled} onClick={() => attempt('yes', 'keys')}>{yesLabel}</button>
      </div>
    </article>
  )
}

// ── evidence ────────────────────────────────────────────────────────────────

function Facts({ facts }: { facts: readonly Citation[] }) {
  return (
    <ul className="dk-facts">
      {facts.map(f => (
        <li key={f.id}>
          <span>{f.text}</span>
          <small>
            <code>{f.id}</code>
            {[f.market, f.asOf && `as of ${f.asOf}`, f.confidence && `${f.confidence} confidence`].filter(Boolean).map(s => ` · ${s}`)}
          </small>
        </li>
      ))}
    </ul>
  )
}

interface EvidenceProps {
  card: DecisionCard
  draft: Draft
  setDraft: (f: (d: Draft) => Draft) => void
  lock?: LockState
  firstChoice: RefObject<HTMLButtonElement | null>
}

function Evidence({ card, draft, setDraft, lock, firstChoice }: EvidenceProps) {
  const ev = card.evidence
  switch (ev.step) {
    case 'confirm':
      return ev.origin === 'extracted' ? (
        <>
          <div className="dk-origin">You said this</div>
          <div className="dk-field"><span>{ev.field}</span><b>{ev.value}</b></div>
          <blockquote className="dk-quote">
            “{ev.quote}”
            <cite>{ev.material}{ev.locator ? ` · ${ev.locator}` : ''}</cite>
          </blockquote>
          <p className="dk-note">Confirm it and the field is yours: no later extraction writes over it.</p>
        </>
      ) : (
        <>
          <div className="dk-origin dk-origin-inferred">We inferred this</div>
          <div className="dk-field"><span>{ev.field}</span><b>{ev.value}</b></div>
          <div className="dk-sub">From</div>
          <Facts facts={ev.facts} />
          <p className="dk-note">Nobody said it in the material. Confirm it, or correct it in the document.</p>
        </>
      )

    case 'resolve':
      return (
        <>
          <div className="dk-field"><span>{ev.field}</span><code>{ev.key}</code></div>
          <div className="dk-choices" role="group" aria-label={`Pick the value for ${ev.field}`}>
            {ev.values.map((v, n) => (
              <button
                key={v.id}
                ref={n === 0 ? firstChoice : undefined}
                type="button"
                aria-pressed={draft.pick === v.id}
                onClick={() => setDraft(d => ({ ...d, pick: v.id }))}
              >
                <b>{v.value}</b>
                <span>{v.market} run</span>
                <small><code>{v.id}</code> · {v.sources.join(', ')}</small>
              </button>
            ))}
          </div>
          {ev.note && <p className="dk-note">{ev.note} Leave it open and only this field waits.</p>}
        </>
      )

    case 'verify':
      return (
        <>
          <div className="dk-origin-row"><span className="dk-origin dk-origin-derived">Derived by the agent</span><span>{ev.confidence} confidence</span></div>
          <div className="dk-sub">Rests on</div>
          <Facts facts={ev.facts} />
          <p className="dk-note">
            Verify it and it becomes a brand fact, with you as its source.{' '}
            {ev.citedBy.length
              ? `Reject it and ${ev.citedBy.map(c => c.label).join(', ')} ${ev.citedBy.length === 1 ? 'is' : 'are'} flagged until revised.`
              : 'No field cites it yet.'}
          </p>
        </>
      )

    case 'verdict':
      return (
        <>
          <div className="dk-field dk-field-long"><span>{ev.field}</span><q>{ev.value}</q></div>
          {ev.cause === 'bad-verdict' ? (
            <div className="dk-mark">
              <b>Marked bad by {ev.by}{ev.reasonCode ? ` · ${ev.reasonCode}` : ''}</b>
              <p>{ev.rationale}</p>
            </div>
          ) : (
            <div className="dk-mark">
              <b>Cites a rejected finding{ev.finding ? <> · <code>{ev.finding.id}</code></> : null}</b>
              {ev.finding && <p>{ev.finding.statement}</p>}
              {ev.rationale && <p className="dk-mark-why">Rejected by {ev.by}: “{ev.rationale}”</p>}
            </div>
          )}
          {ev.cause === 'bad-verdict' ? (
            <div className="dk-seg" role="group" aria-label="How is it answered?">
              <button type="button" aria-pressed={draft.answer === 'revised'} onClick={() => setDraft(d => ({ ...d, answer: 'revised' }))}>I revised it</button>
              <button type="button" aria-pressed={draft.answer === 'override'} onClick={() => setDraft(d => ({ ...d, answer: 'override' }))}>Override it</button>
            </div>
          ) : (
            <p className="dk-note">Only a revision that stops citing it answers this. Mark it once the field is revised.</p>
          )}
        </>
      )

    case 'lock': {
      const l = lock
      return (
        <>
          {l && !l.ready ? (
            <>
              <div className="dk-sub">Still open — the lock waits on {l.blockers.length === 1 ? 'this' : `these ${l.blockers.length}`}</div>
              <ul className="dk-blockers">
                {l.blockers.map(b => (
                  <li key={`${b.kind}:${b.id}`}><AgentAvatar agent={b.from} size={18} decorative /><span>{b.label}</span></li>
                ))}
              </ul>
            </>
          ) : (
            <div className="dk-origin dk-origin-ready">Nothing open stops it</div>
          )}
          <div className="dk-sub">Findings it verifies</div>
          {l && l.findings.length ? (
            <ul className="dk-blockers">
              {l.findings.map(f => <li key={f.id}><AgentAvatar agent={f.from} size={18} decorative /><span>{f.label}</span></li>)}
            </ul>
          ) : (
            <p className="dk-note">None left: every finding is verified or rejected.</p>
          )}
          <p className="dk-note">Locking accepts the whole document, carried content included, with one approve over this version. The seal over it lands with W5-Z1.</p>
        </>
      )
    }
  }
}
