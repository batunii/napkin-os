// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

import { useEffect, useMemo, useState } from 'react'
import type { CSSProperties } from 'react'
import { host } from '../host'
import type { AppHome, RecentDoc, RecentState } from '../host'
import { PoweredByClan } from '../brand/PoweredByClan'
import { AgentFigure } from '../studio/AgentFigure'
import { AGENTS, type AgentKey } from '../studio/model'
import type { InstalledApp } from './types'
import { docTitle } from './docTitle'
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

// Every word and colour on a card comes from the app's own manifest
// (`app.home`), so an app Napkin adds shows here without an OS change. An app
// without one shows its name and "Start", and no crew, so the home never
// claims agents work somewhere they do not.

/** The home app is a document in the store too; it is not work to pick up. */
const HOME_APP_ID = 'ie.napkin.home'

/** Recent work shown before "Show all". */
const RECENT_SHOWN = 6

/** A filter by tool once the list is long enough to need one. */
const FILTER_FROM = 5

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
  const time = d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
  if (days <= 0) return `Today, ${time}`
  if (days === 1) return `Yesterday, ${time}`
  if (days < 7) return d.toLocaleDateString(undefined, { weekday: 'long' })
  return d.toLocaleDateString(undefined, {
    day: 'numeric', month: 'short', year: d.getFullYear() === now.getFullYear() ? undefined : 'numeric',
  })
}

/** The crew the OS can draw: known figure keys only, at most six. */
function crewOf(home?: AppHome | null): AgentKey[] {
  return (home?.crew ?? []).filter((k): k is AgentKey => k in AGENTS).slice(0, 6)
}

/** "Ellis, Dara and Jude": the crew by the names they introduce themselves by. */
function names(crew: readonly AgentKey[]): string {
  const n = crew.map(k => AGENTS[k].given)
  return n.length < 2 ? n.join('') : `${n.slice(0, -1).join(', ')} and ${n[n.length - 1]}`
}

/** The app's colour as a style, when it is one; the OS mixes the rest. */
function tint(home?: AppHome | null): CSSProperties | undefined {
  const c = home?.colour
  return c && /^#[0-9a-f]{6}$/i.test(c) ? ({ '--tc': c } as CSSProperties) : undefined
}

/** The app's one-colour mark, painted in its colour; its initial without one. */
function Mark({ app, className }: { app: InstalledApp | undefined; className: string }) {
  if (app?.icon) {
    const style = { '--mark': `url("${app.icon.replace(/"/g, '%22')}")` } as CSSProperties
    return <i className={className} style={style} aria-hidden />
  }
  return <b className={`${className} ln-mono`} aria-hidden>{(app?.name ?? '?').slice(0, 1).toUpperCase()}</b>
}

function capital(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1)
}

// ── "New" ──────────────────────────────────────────────────────────────────
// Napkin adds apps; nobody installs them. One that arrives after this browser
// first saw the home is marked New until it is opened here.

const SEEN_KEY = 'napkin.home.seen-apps'

function readSeen(): string[] | null {
  try {
    const v = localStorage.getItem(SEEN_KEY)
    return v ? (JSON.parse(v) as string[]) : null
  } catch {
    return null
  }
}

function writeSeen(ids: string[]) {
  try { localStorage.setItem(SEEN_KEY, JSON.stringify(ids)) } catch { /* the mark just stays */ }
}

function useNewApps(installed: InstalledApp[], loading: boolean) {
  const [seen, setSeen] = useState<string[] | null>(readSeen)
  useEffect(() => {
    // The first visit sees everything as it is: nothing there is new to them.
    if (seen === null && !loading && installed.length) {
      const all = installed.map(a => a.app_id)
      writeSeen(all)
      setSeen(all)
    }
  }, [seen, loading, installed])
  const isNew = (id: string) => seen !== null && !seen.includes(id)
  const opened = (id: string) => {
    if (seen === null || seen.includes(id)) return
    const next = [...seen, id]
    writeSeen(next)
    setSeen(next)
  }
  return { isNew, opened }
}

// ── the chip on recent work ──────────────────────────────────────────────────

/** Only what the document's decisions back: no state, no chip. */
function Chip({ state }: { state?: RecentState | null }) {
  if (!state) return null
  if (state.locked) return <span className="ln-state ln-state-ok">Ready</span>
  if (state.needs_you > 0) {
    const n = state.needs_you
    return <span className="ln-state ln-state-need">{n} thing{n > 1 ? 's' : ''} to check</span>
  }
  return null
}

/** The OS home: a greeting, a card per tool, and the work left open. */
export default function Launcher({ installed, loading, onLaunchApp, onOpenFile, onOpenDocument }: Props) {
  // null while the list is on its way, so the section waits instead of flashing empty.
  const [recent, setRecent] = useState<RecentDoc[] | null>(null)
  const [recentFailed, setRecentFailed] = useState(false)
  const [filter, setFilter] = useState('all')
  const [showAll, setShowAll] = useState(false)
  const [opening, setOpening] = useState<string | null>(null)
  const { isNew, opened } = useNewApps(installed, loading)

  const loadRecent = () => {
    let live = true
    setRecentFailed(false)
    host.listRecent()
      .then(list => { if (live) setRecent(list.filter(d => d.app_id !== HOME_APP_ID)) })
      // A host that cannot list its documents still has tools to start from.
      .catch(() => { if (live) { setRecent([]); setRecentFailed(true) } })
    return () => { live = false }
  }
  useEffect(loadRecent, [])

  const appOf = (id?: string | null) => installed.find(a => a.app_id === id)

  const launch = (id: string) => {
    opened(id)
    setOpening(id)
    onLaunchApp(id)
  }

  // Tools with work in the list, in the order they first appear.
  const tools = useMemo(() => {
    const ids: string[] = []
    for (const d of recent ?? []) if (d.app_id && !ids.includes(d.app_id)) ids.push(d.app_id)
    return ids
  }, [recent])
  const filtering = (recent?.length ?? 0) > FILTER_FROM && tools.length > 1
  const listed = (recent ?? []).filter(d => !filtering || filter === 'all' || d.app_id === filter)
  const shown = showAll ? listed : listed.slice(0, RECENT_SHOWN)

  return (
    <div className="ln">
      <div className="ln-hello">
        <h1 className="ln-banner">{greeting()} What shall we work on?</h1>
      </div>

      {loading && installed.length === 0 ? (
        <div className="ln-apps" aria-busy="true" aria-label="Getting your tools ready">
          {[0, 1].map(i => (
            <div key={i} className="ln-app ln-skel" aria-hidden>
              <span className="ln-cover" />
              <span className="ln-body"><i /><i /></span>
            </div>
          ))}
        </div>
      ) : (
        <div className="ln-apps">
          {installed.map(app => {
            const home = app.home
            const crew = crewOf(home)
            const busy = opening === app.app_id
            return (
              <button
                key={app.app_id}
                className={['ln-app', isNew(app.app_id) && 'ln-app-new', busy && 'ln-app-busy'].filter(Boolean).join(' ')}
                style={tint(home)}
                title={home?.line}
                aria-busy={busy || undefined}
                onClick={() => launch(app.app_id)}
              >
                <span className="ln-cover" aria-hidden>
                  <Mark app={app} className="ln-big" />
                  {app.icon && <Mark app={app} className="ln-ghost" />}
                  {crew.length > 0 && (
                    <span className="ln-crew">
                      {crew.map(k => <AgentFigure key={k} agent={k} size={46} />)}
                    </span>
                  )}
                </span>
                <span className="ln-body">
                  {/* The app keeps its own name; the title says the job. */}
                  <span className="ln-app-name eyebrow">{app.name}</span>
                  <span className="ln-app-job">{home?.job ?? app.name}</span>
                  {(home?.line || crew.length > 0) && (
                    <span className="ln-sr">
                      {[home?.line, crew.length ? `With ${names(crew)}.` : ''].filter(Boolean).join(' ')}
                    </span>
                  )}
                  <span className="ln-app-go">
                    {busy ? 'Opening…' : <>{home?.start ?? 'Start'} <span aria-hidden>→</span></>}
                  </span>
                </span>
              </button>
            )
          })}
        </div>
      )}

      <button className="ln-open" onClick={onOpenFile}>
        <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" aria-hidden>
          <path d="M3 5.5h5l1.6 2H17v8.5H3z" />
        </svg>
        Open a file from your computer
      </button>

      {installed.length === 0 && !loading && (
        <div className="ln-note">
          <AgentFigure agent="extract" size={52} />
          <span><b>We couldn’t load your tools just now.</b> Nothing is lost. You can still open your recent work, or a file from your computer.</span>
        </div>
      )}

      <div className="ln-recent">
        <div className="ln-rhead">
          <h2>Pick up where you left off</h2>
          {filtering && (
            <div className="ln-filter" role="group" aria-label="Show work from">
              <button aria-pressed={filter === 'all'} onClick={() => setFilter('all')}>
                All<small>{recent?.length}</small>
              </button>
              {tools.map(id => {
                const app = appOf(id)
                const label = capital(app?.home?.noun?.[1] ?? app?.name ?? 'Other')
                return (
                  <button key={id} style={tint(app?.home)} aria-pressed={filter === id} onClick={() => setFilter(id)}>
                    <i aria-hidden />{label}<small>{recent?.filter(d => d.app_id === id).length}</small>
                  </button>
                )
              })}
            </div>
          )}
        </div>

        {recent === null ? (
          <>
            <div className="ln-item ln-skel" aria-hidden><i /><i /></div>
            <div className="ln-item ln-skel" aria-hidden><i /><i /></div>
          </>
        ) : recentFailed ? (
          <div className="ln-note">
            <AgentFigure agent="drafter" size={52} />
            <span><b>We couldn’t load your recent work.</b> It’s all still saved.</span>
            <button className="ln-retry" onClick={loadRecent}>Try again</button>
          </div>
        ) : recent.length === 0 ? (
          <div className="ln-empty">
            <AgentFigure agent="drafter" size={52} />
            <span><b>Nothing here yet.</b> Your work will wait for you here.</span>
          </div>
        ) : (
          shown.map(d => {
            const app = appOf(d.app_id)
            const title = docTitle(d.title, d.app_id, app?.name, app?.home?.untitled)
            const noun = capital(app?.home?.noun?.[0] ?? app?.name ?? '')
            return (
              <button key={d.path} className="ln-item" style={tint(app?.home)} onClick={() => onOpenDocument(d.path)}>
                <span className="ln-tile" aria-hidden><Mark app={app} className="ln-tile-mark" /></span>
                <span className="ln-item-text">
                  <b className={title.untitled ? 'ln-item-untitled' : undefined}>{title.text}</b>
                  <small>{noun && <span className="ln-sr">{noun}, </span>}{lastTouched(d.updated_at)}</small>
                </span>
                <Chip state={d.state} />
                <span className="ln-item-go" aria-hidden>→</span>
              </button>
            )
          })
        )}

        {!showAll && listed.length > RECENT_SHOWN && (
          <button className="ln-more" onClick={() => setShowAll(true)}>Show all {listed.length}</button>
        )}
      </div>

      <div className="ln-footer"><PoweredByClan /></div>
    </div>
  )
}
