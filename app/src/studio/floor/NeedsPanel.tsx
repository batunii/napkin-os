// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// Right column: what waits on a person, document by document, and what has
// just been decided.

import { useState } from 'react'
import { AgentAvatar } from '../AgentFigure'
import { useStudio } from '../nav'
import { OPEN_ITEM_ACTION, agentLabel, lockBlockers } from '../model'
import type { ActivityEntry, DocProgress, OpenItem } from '../model'
import type { RecentDoc } from '../../host'
import { groupByDoc, pathFor, shortTime, stateIndex } from './derive'
import { EXAMPLE_NOTE } from './example'

interface Props {
  items: readonly OpenItem[]
  docs: readonly DocProgress[]
  activity: readonly ActivityEntry[]
  recent: readonly RecentDoc[]
  example: boolean
  /** Hide an item for this visit. Not a decision. */
  onPark: (key: string) => void
}

const KIND_TAG: Record<OpenItem['kind'], string> = {
  contest: 'Contest', finding: 'Finding', verdict: 'Bad verdict', branch: 'Branch', field: 'Gated field',
}

/** How long the card takes to slide out before it is removed. Matches Floor.css. */
const LEAVE_MS = 300

function NeedCard({ item, path, onPark }: { item: OpenItem; path?: string; onPark: () => void }) {
  const { openPath } = useStudio()
  const [leaving, setLeaving] = useState(false)
  const act = OPEN_ITEM_ACTION[item.kind]
  const reduce = typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches

  function leave(then: () => void) {
    if (reduce) { then(); return }
    setLeaving(true)
    setTimeout(then, LEAVE_MS)
  }

  return (
    <div className="fl-need" data-leaving={leaving || undefined}>
      <div className="fl-who">
        <span>{agentLabel(item.from, item.instance)}</span>
        <span>{item.carried ? 'Carried · ' : ''}{KIND_TAG[item.kind]}</span>
      </div>
      <p>{item.label}</p>
      <div className="fl-acts">
        <button
          type="button"
          disabled={!path}
          title={path ? `Opens the document. Needs: ${act.label}` : 'Example document: not in this store'}
          // TODO(W2-O2): open at `item.address`, not just the document.
          onClick={() => { if (path) openPath(path) }}
        >
          {act.verb}
        </button>
        <button type="button" className="fl-ghost" onClick={() => leave(onPark)}>Not now</button>
      </div>
    </div>
  )
}

function Entry({ e, i }: { e: ActivityEntry; i: number }) {
  const who = 'agent' in e.by ? agentLabel(e.by.agent, e.by.instance) : e.by.person
  return (
    <li className="fl-msg" style={{ animationDelay: `${Math.min(i, 8) * 70}ms` }}>
      {'agent' in e.by
        ? <AgentAvatar agent={e.by.agent} size={26} decorative />
        : <span className="fl-person" aria-hidden="true">{e.by.person.slice(0, 1)}</span>}
      <div>
        <b>{who}</b> <span className="fl-arrow">→</span> {e.text}
        <div className="fl-to">{e.doc.title} · {shortTime(e.at)}</div>
      </div>
    </li>
  )
}

export default function NeedsPanel({ items, docs, activity, recent, example, onPark }: Props) {
  const [showAll, setShowAll] = useState(false)
  const groups = groupByDoc(items)
  const blocked = new Set(groups.map(g => g.docId))
  // Brief-ready documents with nothing open: the one step left is the lock.
  const ready = docs.filter(d => d.state !== 'locked' && stateIndex(d.state) === 2 && !blocked.has(d.doc.id))
  const shownGroups = showAll ? groups : groups.slice(0, 2)
  const hidden = groups.slice(shownGroups.length).reduce((n, g) => n + g.items.length, 0)

  return (
    <>
      <section className="fl-pan">
        <header className="fl-ph">
          <h2>Needs you</h2>
          <span>{items.length} open · {groups.length} {groups.length === 1 ? 'document' : 'documents'}</span>
        </header>
        {example && <p className="fl-note">{EXAMPLE_NOTE}</p>}

        {items.length === 0 && ready.length === 0 && <div className="fl-zero">Nothing needs you.</div>}

        {shownGroups.map(g => {
          const path = pathFor(g.title, recent, g.items[0].doc.path)
          return (
            <div key={g.docId} className="fl-need-g">
              <div className="fl-gl">{g.title}<span>{lockBlockers(g.items, g.docId).length} before lock</span></div>
              {g.items.map(i => (
                <NeedCard key={`${i.doc.id}/${i.id}`} item={i} path={path} onPark={() => onPark(`${i.doc.id}/${i.id}`)} />
              ))}
            </div>
          )
        })}
        {hidden > 0 && (
          <button type="button" className="fl-link" onClick={() => setShowAll(true)}>
            Show {hidden} more in {groups.length - shownGroups.length} more {groups.length - shownGroups.length === 1 ? 'document' : 'documents'}
          </button>
        )}

        {ready.map(d => (
          <div key={d.doc.id} className="fl-ready">
            <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path d="M3.5 8.5l3 3 6-7" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" /></svg>
            <span><b>{d.doc.title}</b> · Ready to lock</span>
          </div>
        ))}
      </section>

      <section className="fl-pan">
        <header className="fl-ph">
          <h2>Activity</h2>
          <span>decisions, newest first</span>
        </header>
        {example && <p className="fl-note">{EXAMPLE_NOTE}</p>}
        {activity.length ? (
          <ol className="fl-thread">{activity.slice(0, 8).map((e, i) => <Entry key={e.id} e={e} i={i} />)}</ol>
        ) : (
          <div className="fl-zero">No decisions yet.</div>
        )}
      </section>
    </>
  )
}
