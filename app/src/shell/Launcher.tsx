// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

import { useEffect, useState } from 'react'
import { host } from '../host'
import type { RecentDoc } from '../host'
import { PoweredByClan } from '../brand/PoweredByClan'
import { AgentFigure } from '../studio/AgentFigure'
import { AGENTS, type AgentKey } from '../studio/model'
import type { InstalledApp } from './types'
import '../components/chrome.css'
import './Launcher.css'

interface Props {
  installed: InstalledApp[]
  loading: boolean
  onLaunchApp: (appId: string) => void
  onOpenFile: () => void
  /** Open a document already in the store: one of the recent ones. */
  onOpenDocument: (path: string) => void
}

/**
 * What the home says about an app it knows: the crew that works in it, the job
 * in plain words, one sentence, and the button. Keyed by app id. An app not
 * listed here shows its own name and no crew, so the home never claims agents
 * work somewhere they do not.
 */
interface Tool {
  crew: readonly AgentKey[]
  job: string
  line: string
  go: string
}

const TOOLS: Readonly<Record<string, Tool>> = {
  'ie.napkin.campaign-research': {
    crew: ['extract', 'market_structure', 'positioning', 'culture', 'synthesis', 'drafter'],
    job: 'Research a market',
    line: 'Tell us the client and the market. The crew finds the facts, checks every one, and writes a report.',
    go: 'Start research',
  },
  'ie.napkin.brief-maker': {
    crew: ['extract', 'drafter', 'judge'],
    job: 'Write a brief',
    line: 'Drop in the client’s notes, email or deck. The crew turns them into a brief you can check line by line.',
    go: 'Start a brief',
  },
}

/** The home app is a document in the store too; it is not work to pick up. */
const HOME_APP_ID = 'ie.napkin.home'

/** The greeting, by the local time of day. */
function greeting(now = new Date()): string {
  const h = now.getHours()
  return h < 12 ? 'Good morning.' : h < 18 ? 'Good afternoon.' : 'Good evening.'
}

/** When a document was last worked on, as a person would say it. */
function lastTouched(iso: string, now = new Date()): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const day = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime()
  const days = Math.round((day(now) - day(d)) / 86_400_000)
  if (days <= 0) return 'today'
  if (days === 1) return 'yesterday'
  if (days < 7) return d.toLocaleDateString(undefined, { weekday: 'long' })
  return d.toLocaleDateString(undefined, {
    day: 'numeric', month: 'short', year: d.getFullYear() === now.getFullYear() ? undefined : 'numeric',
  })
}

/** "Ellis, Dara and Jude": the crew by the names they introduce themselves by. */
function names(crew: readonly AgentKey[]): string {
  const n = crew.map(k => AGENTS[k].given)
  return n.length < 2 ? n.join('') : `${n.slice(0, -1).join(', ')} and ${n[n.length - 1]}`
}

/** The OS home: a greeting, each installed tool with its crew, and the work left open. */
export default function Launcher({ installed, loading, onLaunchApp, onOpenFile, onOpenDocument }: Props) {
  // null while the list is on its way, so the section waits instead of flashing empty.
  const [recent, setRecent] = useState<RecentDoc[] | null>(null)

  useEffect(() => {
    let live = true
    host.listRecent()
      .then(list => { if (live) setRecent(list.filter(d => d.app_id !== HOME_APP_ID)) })
      // A host that cannot list its documents still has tools to start from.
      .catch(() => { if (live) setRecent([]) })
    return () => { live = false }
  }, [])

  const appName = (id?: string | null) => installed.find(a => a.app_id === id)?.name

  return (
    <div className="ln">
      <div className="ln-hello">
        <h1 className="ln-banner">{greeting()} What shall we work on?</h1>
        <p>Pick a tool. Its crew will ask you what they need.</p>
      </div>

      <div className="ln-apps">
        {installed.map(app => {
          const tool = TOOLS[app.app_id]
          return (
            <button key={app.app_id} className="ln-app" onClick={() => onLaunchApp(app.app_id)}>
              {tool ? (
                <>
                  <span className="ln-crew" aria-hidden>
                    {tool.crew.map(k => <AgentFigure key={k} agent={k} size={58} />)}
                  </span>
                  <span className="ln-crew-names">With {names(tool.crew)}</span>
                </>
              ) : (
                <span className="ch-monogram" aria-hidden>{app.name.slice(0, 1).toUpperCase()}</span>
              )}
              {/* The app keeps its own name; the title says the job. */}
              <span className="ln-app-name eyebrow">{app.name}</span>
              <span className="ln-app-job">{tool?.job ?? app.name}</span>
              <span className="ln-app-line">{tool?.line ?? 'Start a new document.'}</span>
              <span className="ln-app-go">{tool?.go ?? 'Start'} <span aria-hidden>→</span></span>
            </button>
          )
        })}
      </div>

      <button className="ln-open" onClick={onOpenFile}>
        <span aria-hidden>+</span> Open a .clan file
      </button>

      {installed.length === 0 && !loading && (
        <div className="ln-note">
          No tools installed yet. Open a <code>.clan</code> template, or run <code>clan app init</code>.
        </div>
      )}

      {recent && recent.length > 0 && (
        <div className="ln-recent">
          <span className="eyebrow">Pick up where you left off</span>
          {recent.map(d => {
            // Who has it: the app's first agent, idle. The shell is not told a
            // document's open checks, so it shows no state it cannot back.
            const who = TOOLS[d.app_id ?? '']?.crew[0]
            const app = appName(d.app_id)
            const meta = [app && app !== d.title ? app : '', lastTouched(d.updated_at)].filter(Boolean).join(' · ')
            return (
              <button key={d.path} className="ln-item" onClick={() => onOpenDocument(d.path)}>
                {who
                  ? <span className="ln-item-fig" aria-hidden><AgentFigure agent={who} size={42} /></span>
                  : <span className="ch-monogram ln-item-mono" aria-hidden>{(app || d.title || '?').slice(0, 1).toUpperCase()}</span>}
                <span className="ln-item-text">
                  <b>{d.title || 'Untitled'}</b>
                  {meta && <small>{meta}</small>}
                </span>
                <span className="ln-item-go" aria-hidden>→</span>
              </button>
            )
          })}
        </div>
      )}

      <div className="ln-footer"><PoweredByClan /></div>
    </div>
  )
}
