// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// Decisions: the open items only a person can settle, one card at a time.
//
// Left, what each call set moving, newest first. Right, the queue: three
// cards visible, the top one live. Every call becomes a `DecisionOutcome`
// kept here; nothing is sent to the host yet — a later task turns outcomes
// into `/resolve`, `/verify`, `/verdict` and `/approve` calls, and W2-O2's
// `/open` replaces the example items.

import { useCallback, useMemo, useRef, useState } from 'react'
import { AgentAvatar, AgentFigure } from '../AgentFigure'
import { EXAMPLE, agentLabel } from '../model'
import type { AgentKey, OpenItem } from '../model'
import { StackCard, TopCard } from './Card'
import { EXAMPLE_EVIDENCE, EXAMPLE_ITEMS, EXAMPLE_LOCK_DOCS, EXAMPLE_OPEN } from './example'
import { apply, blockedEntry, buildQueue, lockState, outcomeOf } from './rules'
import type { Dir, Draft } from './rules'
import type { DecisionCard, DecisionOutcome, LogEntry } from './types'
import './Decisions.css'

interface State {
  queue: DecisionCard[]
  pos: number
  open: OpenItem[]
  log: LogEntry[]
  outcomes: DecisionOutcome[]
  /** Focus the next card: the last call came from a button or a key. */
  focusNext: boolean
}

export default function DecisionsView() {
  const initial = useMemo(() => buildQueue(EXAMPLE_ITEMS, EXAMPLE_EVIDENCE, EXAMPLE_LOCK_DOCS), [])
  const fresh = useCallback((): State => ({
    queue: initial.cards, pos: 0, open: [...EXAMPLE_OPEN], log: [], outcomes: [], focusNext: false,
  }), [initial])
  const [s, setS] = useState<State>(fresh)
  const seq = useRef(0)

  const top = s.queue[s.pos]
  const topLock = top?.step === 'lock' ? lockState(s.open, top.doc.id) : undefined

  const onDone = useCallback((dir: Dir, draft: Draft, via: 'pointer' | 'keys') => {
    setS(prev => {
      const card = prev.queue[prev.pos]
      if (!card) return prev
      const at = new Date().toISOString()
      const n = ++seq.current
      const lock = card.step === 'lock' ? lockState(prev.open, card.doc.id) : undefined
      const next = { ...prev, pos: prev.pos + 1, focusNext: via === 'keys' }
      if (dir === 'yes' && lock && !lock.ready) {
        return { ...next, log: [blockedEntry(card, lock, at, n), ...prev.log] }
      }
      const outcome = outcomeOf(card, dir, draft, lock, at)
      const done = apply(prev.open, outcome, n)
      const queue = [...prev.queue]
      // What a call makes comes next: a flagged field waits right behind the rejection.
      queue.splice(prev.pos + 1, 0, ...done.cards)
      return {
        ...next, queue, open: done.open,
        log: [...done.log.slice().reverse(), ...prev.log],
        outcomes: [...prev.outcomes, outcome],
      }
    })
  }, [])

  const replay = () => setS(fresh())

  const left = s.queue.length - s.pos
  const empty = initial.cards.length === 0
  const done = !empty && left === 0
  const held = s.outcomes.filter(o => o.writes).length

  return (
    <section className="dk-view" aria-labelledby="dk-title">
      <div className="dk">
        <div className="dk-copy">
          <div className="eyebrow">Decisions</div>
          <h1 id="dk-title">Only a person settles these.</h1>
          <p>
            Agents propose. Nothing they find reaches a document or the brand’s knowledge until someone says so.
            Each card is one open item: swipe right to settle it, left to leave it or send it back.
            Where a reason goes on the record, you type it.
          </p>

          <div className="dk-log">
            <div className="dk-log-head">
              <h2>What your calls set moving</h2>
              {held > 0 && <span className="dk-mono">{held} call{held === 1 ? '' : 's'} recorded · not sent yet</span>}
            </div>
            <ol aria-live="polite" aria-relevant="additions">
              {s.log.length === 0 && <li className="dk-log-none">Nothing yet. Make a call on the queue.</li>}
              {s.log.map(e => <LogLine key={e.id} entry={e} />)}
            </ol>
          </div>
        </div>

        <div className="dk-side">
          <div className="dk-phone">
            <div className="dk-phone-top">
              <b>Your calls</b>
              <span>{empty ? 'None' : done ? 'All settled' : `${left} to go`}</span>
            </div>
            {!empty && (
              <div className="dk-dots" aria-hidden="true">
                {s.queue.map((c, j) => <i key={c.id} className={j < s.pos ? 'is-done' : undefined} />)}
              </div>
            )}
            <div className="dk-stack">
              {empty && <Zero />}
              {done && <Done outcomes={s.outcomes} open={s.open} replay={replay} focus={s.focusNext} />}
              {!empty && !done && (
                <>
                  {s.queue.slice(s.pos + 1, s.pos + 3).map((c, j) => <StackCard key={c.id} card={c} depth={j + 1} />).reverse()}
                  <TopCard key={`${top.id}@${s.pos}`} card={top} lock={topLock} autoFocus={s.focusNext} onDone={onDone} />
                </>
              )}
            </div>
          </div>
          <p className="dk-hint">Drag the card, use its buttons, or ← →.</p>
          {EXAMPLE && <p className="dk-mono dk-example">Example — open items become live with W2-O2.</p>}
          {initial.elsewhere.length > 0 && (
            <p className="dk-elsewhere">
              Not in this queue: {initial.elsewhere.length} item{initial.elsewhere.length === 1 ? '' : 's'} only the
              document can settle — {initial.elsewhere.map(i => i.label.replace(/\.$/, '').toLowerCase()).join('; ')}.
            </p>
          )}
        </div>
      </div>
    </section>
  )
}

function LogLine({ entry }: { entry: LogEntry }) {
  return (
    <li className={`dk-log-${entry.tone}`}>
      <AgentAvatar agent={entry.agent} size={26} decorative />
      <div>
        <p><b>{entry.head}:</b> {entry.text}</p>
        <small>{entry.doc.title} · from {agentLabel(entry.agent, entry.instance)}</small>
      </div>
    </li>
  )
}

function Zero() {
  const idle: AgentKey[] = ['extract', 'synthesis', 'judge']
  return (
    <div className="dk-end">
      <div className="dk-end-art" aria-hidden="true">{idle.map(k => <AgentFigure key={k} agent={k} size={48} />)}</div>
      <b>Nothing needs you.</b>
      <p>When an agent’s work needs a person — an ask to confirm, a contest, a finding, a verdict, a lock — it lands here with what it rests on.</p>
    </div>
  )
}

interface DoneProps {
  outcomes: DecisionOutcome[]
  open: OpenItem[]
  replay: () => void
  /** Take focus on the Replay button: the last call came from a button or a key. */
  focus: boolean
}

function Done({ outcomes, open, replay, focus }: DoneProps) {
  const settled = outcomes.filter(o => o.writes).length
  const left = outcomes.length - settled
  const stillOpen = open.filter(i => !i.carried).length
  const agents = [...new Set(outcomes.map(o => o.card.from))].slice(0, 4)
  return (
    <div className="dk-end">
      <div className="dk-end-art" aria-hidden="true">{agents.map(k => <AgentFigure key={k} agent={k} size={48} state="working" />)}</div>
      <b>All settled for now.</b>
      <p>
        {settled} call{settled === 1 ? '' : 's'} recorded{left ? `, ${left} left or sent back` : ''}.{' '}
        {stillOpen ? `${stillOpen} open item${stillOpen === 1 ? '' : 's'} still wait in their documents.` : 'Nothing is open.'}
      </p>
      <button type="button" autoFocus={focus} onClick={replay}>Replay the queue</button>
    </div>
  )
}
