// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

import { useRef } from 'react'
import type { KeyboardEvent, ReactNode } from 'react'
import { StudioLogo } from '../brand/StudioMark'
import ApiKeyButton from '../components/ApiKeyButton'
import ThemeToggle from '../components/ThemeToggle'
import { STUDIO_VIEWS } from './nav'
import type { StudioView } from './nav'
import './StudioShell.css'

interface Props {
  view: StudioView
  onView: (view: StudioView) => void
  /** The current view's content, rendered as the tab panel. */
  children: ReactNode
}

/** The studio frame: frosted top bar with the brand and the view tabs. */
export default function StudioShell({ view, onView, children }: Props) {
  const tabs = useRef<(HTMLButtonElement | null)[]>([])

  // Arrow keys move between tabs (and select, as the tabs are cheap to show).
  function onKeyDown(e: KeyboardEvent) {
    const i = STUDIO_VIEWS.findIndex(v => v.id === view)
    const step = e.key === 'ArrowRight' ? 1 : e.key === 'ArrowLeft' ? -1 : 0
    const to = e.key === 'Home' ? 0 : e.key === 'End' ? STUDIO_VIEWS.length - 1
      : step ? (i + step + STUDIO_VIEWS.length) % STUDIO_VIEWS.length : -1
    if (to < 0) return
    e.preventDefault()
    onView(STUDIO_VIEWS[to].id)
    tabs.current[to]?.focus()
  }

  return (
    <div className="studio">
      <header className="studio-top">
        <StudioLogo />
        <nav className="studio-tabs" role="tablist" aria-label="Studio views" onKeyDown={onKeyDown}>
          {STUDIO_VIEWS.map((v, i) => (
            <button
              key={v.id}
              ref={el => { tabs.current[i] = el }}
              role="tab"
              id={`studio-tab-${v.id}`}
              aria-selected={v.id === view}
              aria-controls="studio-panel"
              tabIndex={v.id === view ? 0 : -1}
              onClick={() => onView(v.id)}
            >
              <span>{String(i + 1).padStart(2, '0')}</span>{v.label}
            </button>
          ))}
        </nav>
        <div className="studio-tools">
          <ApiKeyButton />
          <ThemeToggle />
        </div>
      </header>
      <main className="studio-panel" id="studio-panel" role="tabpanel" aria-labelledby={`studio-tab-${view}`}>
        {children}
      </main>
    </div>
  )
}
