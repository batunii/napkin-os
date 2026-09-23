// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// Centre column: documents in progress (gates, agents, coverage, lease), then
// every recent document in the store.

import { AgentFigure } from '../AgentFigure'
import { useStudio } from '../nav'
import {
  AGENTS, AGENT_OF_LENS, DOC_STATES, DOC_STATE_LABEL, FLOW, LENS_IDS, agentLabel, flowStepOf,
} from '../model'
import { lockBlockers } from '../model'
import type { DocProgress, OpenItem } from '../model'
import type { RecentDoc } from '../../host'
import { fanOutLabel, pathFor, shortTime, stateIndex } from './derive'
import { EXAMPLE_NOTE } from './example'

interface Props {
  docs: readonly DocProgress[]
  open: readonly OpenItem[]
  recent: readonly RecentDoc[]
  example: boolean
}

const COVERAGE_WORD = { filled: 'filled', thin: 'thin', empty: 'empty' } as const

function GateBar({ d, openCount }: { d: DocProgress; openCount: number }) {
  const at = stateIndex(d.state)
  return (
    <ol className="fl-gates" aria-label={`At ${DOC_STATE_LABEL[d.state]}`}>
      {DOC_STATES.map((s, i) => {
        const done = i <= at
        const next = i === at + 1
        const note = done ? (s === 'locked' ? 'sealed' : 'passed')
          : next ? (s === 'locked' ? (openCount ? `${openCount} open` : 'ready') : 'next') : ''
        return (
          <li key={s} data-done={done || undefined} data-next={next || undefined}>
            <span className="fl-gate-h"><b>{DOC_STATE_LABEL[s]}</b><span>{note}</span></span>
            <span className="fl-bar"><i /></span>
          </li>
        )
      })}
    </ol>
  )
}

function DocCard({ d, openCount, path }: { d: DocProgress; openCount: number; path?: string }) {
  const { openPath } = useStudio()
  const step = FLOW.find(s => s.kind === d.kind)
  const working = d.agents.filter(a => a.state === 'working')

  return (
    <article className="fl-doc">
      <header className="fl-doc-hd">
        <div>
          <div className="fl-who">{step?.name} · {step?.app}{d.from ? ` · from ${d.from.title}` : ''}</div>
          <h3>{d.doc.title}</h3>
        </div>
        <div className="fl-pills">
          {d.markets?.length ? <span className="fl-pill">{d.markets.join(' · ')}</span> : null}
          <span className="fl-pill" title="Who holds the editing lease">{d.lease ? `Editing · ${d.lease}` : 'No one editing'}</span>
        </div>
      </header>

      <GateBar d={d} openCount={openCount} />

      <div className="fl-doc-body">
        <div className="fl-on">
          <div className="fl-sub">On it</div>
          {working.length ? (
            <ul className="fl-chips">
              {working.map(a => (
                <li key={a.agent} className="fl-chip">
                  <AgentFigure agent={a.agent} size={26} state="working" />
                  <span>{agentLabel(a.agent)} <em>{fanOutLabel(a.agent, a.count ?? 1)}</em></span>
                </li>
              ))}
            </ul>
          ) : (
            <div className="fl-quiet">No agent running.</div>
          )}
        </div>

        {d.coverage && (
          <div className="fl-cov">
            <div className="fl-sub">Coverage <span>filled · thin · empty</span></div>
            <ul>
              {LENS_IDS.map(l => {
                const c = d.coverage?.[l]
                return (
                  <li key={l} data-cov={c ?? 'pending'} title={c ? COVERAGE_WORD[c] : 'not yet merged'}>
                    <span className="fl-bar"><i /></span>
                    <span className="fl-cov-l">{AGENTS[AGENT_OF_LENS[l]].name}</span>
                    <span className="fl-cov-v">{c ?? '—'}</span>
                  </li>
                )
              })}
            </ul>
          </div>
        )}
      </div>

      {openCount ? (
        <div className="fl-art fl-art-dark">
          <div className="fl-who">Before it can lock</div>
          {openCount} open {openCount === 1 ? 'item needs' : 'items need'} a person. See Needs you.
        </div>
      ) : d.state === 'locked' ? (
        <div className="fl-art fl-art-wip"><div className="fl-who">Locked</div>{step?.next}.</div>
      ) : stateIndex(d.state) === 2 ? (
        <div className="fl-art">
          <div className="fl-who">Ready to lock</div>
          Nothing is open. Locking accepts it and seals this version.
          {path && <div className="fl-acts"><button type="button" onClick={() => openPath(path)}>Open to lock</button></div>}
        </div>
      ) : (
        <div className="fl-art fl-art-wip"><div className="fl-who">Next</div>{d.state === 'created' ? 'Research runs once categories and markets are set.' : 'Brief-ready once problem, objective, audience and budget band are in.'}</div>
      )}
    </article>
  )
}

function RecentRow({ r }: { r: RecentDoc }) {
  const { openPath } = useStudio()
  const step = flowStepOf(r.app_id)
  return (
    <li className="fl-row">
      <span className="fl-row-t">{r.title || 'Untitled'}</span>
      <span className="fl-row-a">{step ? step.app : r.app_id ?? 'No app'}</span>
      <span className="fl-row-w">{shortTime(r.updated_at)}</span>
      <button type="button" className="fl-ghost" onClick={() => openPath(r.path)}>Open</button>
    </li>
  )
}

export default function DocsPanel({ docs, open, recent, example }: Props) {
  const { openFile, go } = useStudio()
  return (
    <>
      <section className="fl-pan">
        <header className="fl-ph">
          <h2>In progress</h2>
          <span>{example ? EXAMPLE_NOTE : `${docs.length} documents`}</span>
        </header>
        <div className="fl-flow" aria-label="The flow">
          {FLOW.map(s => <span key={s.kind}>{s.name} <em>{s.app}</em></span>)}
          <span>Next document <em>by spin-off</em></span>
        </div>
        <div className="fl-docs">
          {docs.map(d => (
            <DocCard
              key={d.doc.id}
              d={d}
              openCount={lockBlockers(open, d.doc.id).length}
              path={pathFor(d.doc.title, recent, d.doc.path)}
            />
          ))}
        </div>
      </section>

      <section className="fl-pan">
        <header className="fl-ph">
          <h2>Recent documents</h2>
          <span>live · this host</span>
        </header>
        {recent.length ? (
          <ul className="fl-rows">{recent.map(r => <RecentRow key={r.path} r={r} />)}</ul>
        ) : (
          <div className="fl-zero">
            No documents yet. <button type="button" className="fl-link" onClick={() => go('apps')}>Start one from Apps</button> or{' '}
            <button type="button" className="fl-link" onClick={openFile}>open a .clan</button>.
          </div>
        )}
      </section>
    </>
  )
}
