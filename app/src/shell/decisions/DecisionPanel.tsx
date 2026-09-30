// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// The document's decisions, in the OS layer, beside the app. Three parts
// (owner, 2026-09-28):
//
// - Needs you: only what blocks the lock, as the host derives it from the
//   document's state. Each item points at where it is settled — "Open it"
//   opens the value's evidence in the app, where it is verified, rejected or
//   picked. The panel does not verify a second time.
// - Worth a look: what agents flagged or were unsure of. Nothing waits on it.
//   A person can accept the call here ("Looks right"), or open the value.
// - Who did what: who did what and why, newest first, grouped by time or by
//   part of the document. A line opens to the decision, its reasons with what each rests
//   on, the sources, what was set aside and how sure the agent was. Machine
//   detail sits last, behind "Technical details".
//
// Everything here is the host's view of the chain (`/decisions`); the store
// behind it keeps far more than this shows. The panel is called "What
// happened", in the words of the people it is for: a "decision" is the
// chain's word, not theirs.

import { useCallback, useEffect, useMemo, useState } from 'react'
import { host } from '../../host'
import type { AttentionItem, DecisionBlock, DecisionsView } from '../../host'
import { LogoSpinner } from '../../brand/LogoSpinner'
import { onRunnable, openInApp } from '../appExport'
import { HistoryLine, NeedsCard, QuietItem } from './DecisionBlock'
import { refOfAddress, runsOf, sectionsOf } from './words'
import './DecisionPanel.css'

interface Props {
  /** The open document; the panel reloads when it changes. */
  docPath: string
}

export default function DecisionPanel({ docPath }: Props) {
  const [open, setOpen] = useState(false)
  const [view, setView] = useState<DecisionsView | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [by, setBy] = useState<'run' | 'section'>('run')
  const [quietOpen, setQuietOpen] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)
  const [actError, setActError] = useState<string | null>(null)

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

  // The app says which steps it can run again; redraw when it does.
  const [, setRunnable] = useState(0)
  useEffect(() => onRunnable(() => setRunnable(n => n + 1)), [])

  const needs = useMemo(() => needsOf(view), [view])
  const quiet = useMemo(() => quietOf(view), [view])
  const blocks = useMemo(() => new Map((view?.decisions ?? []).map(b => [b.decision.id ?? '', b])), [view])

  const looksRight = useCallback((id: string) => {
    setBusy(id); setActError(null)
    host.acknowledge(id).then(load, e => setActError(String(e instanceof Error ? e.message : e)))
      .finally(() => setBusy(null))
  }, [load])

  if (!open) {
    return (
      <button
        type="button"
        className="dp-rail"
        onClick={() => setOpen(true)}
        aria-label={needs.length ? `What happened: ${needs.length} need${needs.length === 1 ? 's' : ''} you` : 'What happened'}
        title={needs.length ? `${needs.length} need${needs.length === 1 ? 's' : ''} you` : 'What happened'}
      >
        <span className="dp-rail-label">What happened</span>
        {needs.length > 0 && <span className="dp-count">{needs.length}</span>}
      </button>
    )
  }

  const groups = view ? (by === 'run' ? runsOf(view) : sectionsOf(view)) : []

  return (
    <aside className="dp-panel" aria-labelledby="dp-title">
      <div className="dp-head">
        <div className="dp-head-t">
          <h2 id="dp-title" className="dp-title">What happened</h2>
          {view && <span className="dp-sum">{needs.length ? `${needs.length} need${needs.length === 1 ? 's' : ''} you` : 'nothing needs you'}</span>}
        </div>
        <button className="ch-btn ch-btn-icon ch-btn-quiet" onClick={() => setOpen(false)} aria-label="Close what happened">✕</button>
      </div>
      {view && !view.problem && (
        <p className={`dp-lock ${view.lock.can_lock ? 'dp-lock-ok' : ''}`}>
          <span className="dp-dot" aria-hidden />
          {view.lock.can_lock ? 'Nothing stops you locking it.' : `You can lock it once ${needs.length === 1 ? 'this is' : 'these are'} settled.`}
        </p>
      )}

      <div className="dp-scroll">
        {error && <div className="ch-empty">What happened could not be read: {error}</div>}
        {!error && !view && <div className="dp-loading"><LogoSpinner label="Reading what happened" /></div>}
        {view?.problem && <div className="ch-empty">{view.problem}</div>}
        {actError && <div className="dp-err" role="alert">{actError}</div>}
        {view && !view.problem && (
          <>
            <section aria-label="Needs you">
              <h3 className="dp-sec">Needs you <span className="dp-n">{needs.length}</span></h3>
              {needs.length === 0 && <div className="dp-done">Nothing is waiting on you.</div>}
              {needs.map(item => (
                <NeedsCard key={`${item.code}:${item.address ?? item.text}`} item={item}
                  block={item.decision ? blocks.get(item.decision) : undefined} />
              ))}
            </section>

            {quiet.length > 0 && (
              <section aria-label="Worth a look">
                <details className="dp-quiet" open={quietOpen} onToggle={e => setQuietOpen((e.target as HTMLDetailsElement).open)}>
                  <summary>
                    <span className="dp-sec dp-sec-inline">Worth a look <span className="dp-n dp-n-quiet">{quiet.length}</span></span>
                    <span className="dp-hint">the crew noticed these; nothing waits on them</span>
                  </summary>
                  {quiet.map(q => (
                    <QuietItem key={q.block.decision.id} block={q.block} text={q.text}
                      busy={busy === q.block.decision.id} onLooksRight={looksRight} />
                  ))}
                </details>
              </section>
            )}

            <section aria-label="Who did what">
              <div className="dp-hist-h">
                <h3 className="dp-sec">Who did what</h3>
                <div className="ch-seg dp-by" role="group" aria-label="Show it">
                  <button aria-pressed={by === 'run'} onClick={() => setBy('run')}>By time</button>
                  <button aria-pressed={by === 'section'} onClick={() => setBy('section')}>By part</button>
                </div>
              </div>
              {groups.length === 0 && <div className="dp-done">Nothing has happened yet.</div>}
              {groups.map(g => (
                <div className="dp-run" key={g.key}>
                  <div className="dp-run-h">
                    <b>{g.title}</b><span>{g.sub}</span>
                    {g.open && <button className="dp-link dp-link-end" onClick={() => openInApp(g.open!.ref, g.open!.path)}>Show</button>}
                  </div>
                  {g.blocks.map((b, i) => <HistoryLine key={b.decision.id ?? `n${i}`} block={b} view={view} />)}
                </div>
              ))}
            </section>
          </>
        )}
      </div>
    </aside>
  )
}

/** What blocks the lock, one item per place. */
function needsOf(view: DecisionsView | null): AttentionItem[] {
  if (!view) return []
  const seen = new Set<string>()
  return view.attention.filter(a => {
    if (!a.blocks_lock) return false
    const k = `${a.code}:${a.address ?? a.text}`
    if (seen.has(k)) return false
    seen.add(k)
    return true
  })
}

/**
 * What agents flagged or were unsure of, one item per decision. A decision
 * whose target is already under "Needs you" (a finding waiting for its check)
 * is not repeated here, and neither is a finding: it is checked, not waved
 * through.
 */
function quietOf(view: DecisionsView | null): { block: DecisionBlock; text: string }[] {
  if (!view) return []
  const needy = new Set(view.attention.filter(a => a.blocks_lock).map(a => refOfAddress(a.address)))
  const out: { block: DecisionBlock; text: string }[] = []
  for (const b of view.decisions) {
    if (b.superseded || b.who.kind !== 'agent' || b.decision.kind === 'finding') continue
    const quiet = b.attention.filter(r => !r.blocks_lock)
    if (!quiet.length) continue
    if (b.targets.some(t => needy.has(refOfAddress(t.address)))) continue
    const flag = quiet.find(r => r.code === 'flagged') ?? quiet[0]
    out.push({ block: b, text: flag.text })
  }
  return out
}
