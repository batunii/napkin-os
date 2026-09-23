// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

import { StudioLogo } from '../brand/StudioMark'
import ApiKeyButton from './ApiKeyButton'
import ContinueIn from './ContinueIn'
import ThemeToggle from './ThemeToggle'
import './chrome.css'

interface Props {
  title?: string
  isTemplate?: boolean
  trusted?: boolean
  onHome: () => void
  onOpenFile: () => void
  onToggleAgent: () => void
  onToggleSidebar: () => void
  onWorkspace: () => void
  onSave: () => void
  onExport: (kind: 'html' | 'pdf') => void
  /** Identifies the open document, so "Continue in…" refetches when it changes. */
  docPath: string
  onSpinoff: (appId: string) => void
  agentPanelOpen: boolean
  sidebarOpen: boolean
  loading: boolean
  validation?: string
}

/** Two stacked panes: the details sidebar toggle. */
function SidebarGlyph() {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.4" aria-hidden>
      <rect x="1.5" y="2" width="11" height="10" rx="2.2" />
      <line x1="5.5" y1="2" x2="5.5" y2="12" />
    </svg>
  )
}

/** The document bar: studio home, title and status, file actions, panels. */
export default function Toolbar({
  title, isTemplate, trusted, onHome, onOpenFile, onToggleAgent, onToggleSidebar, onWorkspace, onSave, onExport,
  docPath, onSpinoff, agentPanelOpen, sidebarOpen, loading, validation,
}: Props) {
  const valid = validation === 'OK'
  return (
    <div className="ch-bar">
      <button
        className="ch-btn ch-btn-icon"
        aria-pressed={sidebarOpen}
        onClick={onToggleSidebar}
        title={sidebarOpen ? 'Hide details' : 'Show details'}
        aria-label={sidebarOpen ? 'Hide details' : 'Show details'}
      >
        <SidebarGlyph />
      </button>
      <button className="ch-bar-home" onClick={onHome} title="Back to the studio">
        <StudioLogo size={15} compact />
      </button>
      <span className="ch-bar-title">{loading ? 'Loading…' : (title ?? 'No file open')}</span>
      {isTemplate && <span className="ch-chip ch-chip-accent">Template</span>}
      {trusted && (
        <span className="ch-chip ch-chip-ok" title="Signed by Napkin — scoped host capabilities enabled">
          Trusted
        </span>
      )}
      {validation && (
        <span
          className={`ch-chip ${valid ? 'ch-chip-ok' : 'ch-chip-warn'}`}
          title={valid ? 'No validation issues' : validation}
        >
          <span className="ch-chip-dot" />{valid ? 'Valid' : 'Issues'}
        </span>
      )}
      <span className="ch-bar-sep" />
      <button className="ch-btn" onClick={onOpenFile}>Open</button>
      <button className="ch-btn" onClick={onSave} title="Save a copy of this .clan to share">Save as</button>
      <button className="ch-btn" onClick={() => onExport('pdf')} title="Export a standalone PDF (composed from this document's data)">Export</button>
      <button className="ch-btn" onClick={onWorkspace} title="Lineage & provenance">Lineage</button>
      <ContinueIn key={docPath} docPath={docPath} onSpinoff={onSpinoff} />
      <button className="ch-btn" aria-pressed={agentPanelOpen} onClick={onToggleAgent}>
        Agent
      </button>
      <ApiKeyButton />
      <ThemeToggle />
    </div>
  )
}
