// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// Every decision in the open document, as a block, in the OS layer — beside
// the app, never inside it. The host decides what needs attention
// (`/decisions`); this only lays it out: those blocks pinned on top with
// their reasons, then the rest, newest first.

import { useCallback, useEffect, useMemo, useState } from 'react'
import { host } from '../../host'
import type { AttentionItem, DecisionBlock, DecisionsView } from '../../host'
import { AgentFigure } from '../../studio/AgentFigure'
import { AGENTS } from '../../studio/model'
import { agentOf } from './agentOf'
import { LogoSpinner } from '../../brand/LogoSpinner'
import { Block, StandaloneBlock } from './DecisionBlock'
import './DecisionPanel.css'

type Filter = 'all' | 'attention' | `who:${string}`

interface Props {
  /** The open document; the panel reloads when it changes. */
  docPath: string
}

export default function DecisionPanel({ docPath }: Props) {
  const [open, setOpen] = useState(false)
  const [view, setView] = useState<DecisionsView | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [filter, setFilter] = useState<Filter>('all')

  const load = useCallback(() => {
    host.getDecisions().then(
      v => { setView(v); setError(null) },
      e => setError(String(e instanceof Error ? e.message : e)),
    )
  }, [])

  // A new document starts from nothing, so the old one's blocks never show
  // against it.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setView(null)
    setFilter('all')
    load()
  }, [docPath, load])

  // Every write the host fans out can change the chain.
  useEffect(() => {
    const offs = [
      host.on('clan-data-changed', load),
      host.on('clan-patch-saved', load),
      host.on('clan-title-changed', load),
    ]
    return () => { for (const off of offs) off.then(f => f()) }
  }, [load])

  const groups = useMemo(() => group(view), [view])
  const needs = groups.pinned.length + groups.standalone.length

  if (!open) {
    return (
      <button
        type="button"
        className="dp-rail"
        onClick={() => setOpen(true)}
        aria-label={needs ? `Decisions, ${needs} need attention` : 'Decisions'}
        title={needs ? `${needs} need attention` : 'Decisions'}
      >
        <span className="dp-rail-label">Decisions</span>
        {needs > 0 && <span className="dp-count">{needs}</span>}
      </button>
    )
  }

  const who = people(view)
  const shown = pick(groups, filter)

  return (
    <aside className="dp-panel" aria-labelledby="dp-title">
      <div className="dp-head">
        <div>
          <div className="eyebrow">This document</div>
          <h2 id="dp-title" className="dp-title">Decisions</h2>
        </div>
        <button className="ch-btn ch-btn-icon ch-btn-quiet" onClick={() => setOpen(false)} aria-label="Close decisions">✕</button>
      </div>

      {view && (
        <p className={`dp-lock ${view.lock.can_lock ? '' : 'dp-lock-blocked'}`}>
          {view.lock.can_lock
            ? 'Nothing on the lock list is open.'
            : `${plural(view.lock.blockers, 'item')} must be resolved before this can be locked.`}
        </p>
      )}

      <div className="dp-filters">
        <div className="ch-seg" role="tablist" aria-label="Show">
          <button role="tab" aria-selected={filter === 'all'} onClick={() => setFilter('all')}>All</button>
          <button role="tab" aria-selected={filter === 'attention'} onClick={() => setFilter('attention')}>
            Needs attention{needs > 0 ? ` · ${needs}` : ''}
          </button>
        </div>
        {who.length > 1 && (
          <label className="dp-who">
            <span className="dp-sr">Decided by</span>
            <select
              value={filter.startsWith('who:') ? filter : ''}
              onChange={e => setFilter((e.target.value || 'all') as Filter)}
            >
              <option value="">Decided by anyone</option>
              {who.map(w => <option key={w.id} value={`who:${w.id}`}>{w.name}</option>)}
            </select>
          </label>
        )}
      </div>

      <div className="dp-scroll">
        {error && <div className="ch-empty">The decisions could not be read: {error}</div>}
        {!error && !view && <div className="dp-loading"><LogoSpinner label="Reading the decisions" /></div>}
        {view?.problem && <div className="ch-empty">{view.problem}</div>}
        {view && !view.problem && (
          <>
            {shown.pinned.length + shown.standalone.length > 0 && (
              <section aria-label="Needs attention">
                <div className="eyebrow dp-section">Needs attention</div>
                {shown.pinned.map((b, i) => <Block key={key(b, i)} block={b} cites={view.cites} who={<Who block={b} />} />)}
                {shown.standalone.map((a, i) => <StandaloneBlock key={`s${i}`} item={a} />)}
              </section>
            )}
            {shown.rest.length > 0 && (
              <section aria-label="Decisions">
                {shown.pinned.length + shown.standalone.length > 0 && <div className="eyebrow dp-section">Everything else</div>}
                {shown.rest.map((b, i) => <Block key={key(b, i)} block={b} cites={view.cites} who={<Who block={b} />} />)}
              </section>
            )}
            {shown.pinned.length + shown.standalone.length + shown.rest.length === 0 && (
              <div className="ch-empty">
                {filter === 'attention' ? 'Nothing needs attention.' : 'No decisions yet.'}
              </div>
            )}
          </>
        )}
      </div>
    </aside>
  )
}

/** The agent's figure and plain name, or the person. */
function Who({ block }: { block: DecisionBlock }) {
  const agent = agentOf(block)
  return (
    <span className="dp-who-line">
      {agent
        ? <AgentFigure agent={agent} size={34} />
        : <span className={`dp-mark ${block.who.kind === 'person' ? 'dp-mark-person' : ''}`} aria-hidden>
            {(block.who.name[0] ?? '?').toUpperCase()}
          </span>}
      <span className="dp-name">{whoOf(block).name}</span>
    </span>
  )
}

interface Groups {
  pinned: DecisionBlock[]
  standalone: AttentionItem[]
  rest: DecisionBlock[]
}

function group(view: DecisionsView | null): Groups {
  if (!view) return { pinned: [], standalone: [], rest: [] }
  const ids = new Set(view.decisions.map(b => b.decision.id).filter(Boolean))
  return {
    pinned: view.decisions.filter(b => b.attention.length > 0),
    standalone: view.attention.filter(a => !a.decision || !ids.has(a.decision)),
    rest: view.decisions.filter(b => b.attention.length === 0),
  }
}

function pick(g: Groups, filter: Filter): Groups {
  if (filter === 'all') return g
  if (filter === 'attention') return { ...g, rest: [] }
  const id = filter.slice('who:'.length)
  const mine = (b: DecisionBlock) => whoOf(b).id === id
  return { pinned: g.pinned.filter(mine), standalone: [], rest: g.rest.filter(mine) }
}

/**
 * Who a block is filed under: the agent when one is recognised (one handler
 * can run several agents), else whoever the host named.
 */
function whoOf(b: DecisionBlock): { id: string; name: string } {
  const agent = agentOf(b)
  return agent
    ? { id: `agent:${agent}`, name: AGENTS[agent].name }
    : { id: `${b.who.kind}:${b.who.id}`, name: b.who.name }
}

/** Everyone who decided something, most decisions first. */
function people(view: DecisionsView | null): { id: string; name: string }[] {
  const seen = new Map<string, { id: string; name: string; n: number }>()
  for (const b of view?.decisions ?? []) {
    const w = whoOf(b)
    const entry = seen.get(w.id) ?? { ...w, n: 0 }
    entry.n++
    seen.set(w.id, entry)
  }
  return [...seen.values()].sort((a, b) => b.n - a.n)
}

/** Older entries have no id; their place in the list stands in. */
function key(b: DecisionBlock, i: number): string {
  return b.decision.id ?? `n${i}`
}

function plural(n: number, what: string): string {
  return `${n} ${what}${n === 1 ? '' : 's'}`
}
