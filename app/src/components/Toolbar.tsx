// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

import { useEffect, useRef, useState } from 'react'
import { StudioLogo } from '../brand/StudioMark'
import { LogoSpinner } from '../brand/LogoSpinner'
import { host } from '../host'
import { useSpinoffTargets } from './ContinueIn'
import ThemeToggle from './ThemeToggle'
import './chrome.css'

interface Props {
  title?: string
  isTemplate?: boolean
  trusted?: boolean
  onHome: () => void
  onOpenFile: () => void
  onToggleSidebar: () => void
  onWorkspace: () => void
  onSave: () => void
  /** Keep a copy on this device for presenting offline. Absent where it makes no sense. */
  onKeepOffline?: () => void
  onExport: (kind: 'html' | 'pdf') => void
  /** Identifies the open document, so "Continue in…" refetches when it changes. */
  docPath: string
  onSpinoff: (appId: string) => void
  sidebarOpen: boolean
  /** Edit mode, for every app: its fields become editable, each change a person's pinned decision. */
  editMode?: boolean
  onToggleEdit?: () => void
  loading: boolean
  validation?: string
  /**
   * The document lives in a store that keeps each change as it is made (the
   * studio, the desktop's files), so "Saved" is true. False on the device,
   * where changes stay in the tab.
   */
  saved?: boolean
}

/** One thing the More menu can do. */
interface MenuItem {
  label: string
  hint?: string
  run: () => void
  /** A rule above this item. */
  apart?: boolean
}

/**
 * "Saved", with the green dot. Only shown where it is true by construction
 * (see Props.saved); the time of the last change the host confirmed goes in
 * the tooltip. The shell is not told of a write that fails; the app is.
 */
function SaveStatus({ docPath }: { docPath: string }) {
  const [last, setLast] = useState<{ docPath: string; at: Date } | null>(null)
  useEffect(() => {
    const note = () => setLast({ docPath, at: new Date() })
    const subs = [host.on('clan-data-changed', note), host.on('clan-patch-saved', note)]
    subs.forEach(s => s.catch(() => {}))
    return () => { subs.forEach(s => s.then(f => f()).catch(() => {})) }
  }, [docPath])
  const at = last?.docPath === docPath ? last.at : null
  const time = at?.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
  return (
    <span className="ch-saved" title={`Every change is kept as you make it.${time ? ` Last change kept at ${time}.` : ''}`}>
      Saved
    </span>
  )
}

/** The menu's items, in order, and focus on the i-th (wrapping round). */
const entries = (menu: HTMLElement | null) =>
  Array.from(menu?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') ?? [])
function focusAt(menu: HTMLElement | null, i: number) {
  const els = entries(menu)
  if (els.length) els[(i + els.length) % els.length].focus()
}

/**
 * "More ▾": everything the bar does that is not Share, in plain words. A menu
 * button: arrows move through it, Home and End jump, Escape or Tab or a click
 * anywhere else closes it. Clicks inside the app frame never reach this page,
 * so leaving the window (a click into the frame) closes it too.
 */
function MoreMenu({ items }: { items: MenuItem[] }) {
  const [open, setOpen] = useState(false)
  const anchor = useRef<HTMLDivElement>(null)
  const button = useRef<HTMLButtonElement>(null)
  const menu = useRef<HTMLDivElement>(null)
  // Which item takes focus when the menu opens: the first, or the last (ArrowUp).
  const startAt = useRef(0)

  const close = (refocus: boolean) => {
    setOpen(false)
    if (refocus) button.current?.focus()
  }

  useEffect(() => {
    if (!open) return
    focusAt(menu.current, startAt.current < 0 ? entries(menu.current).length - 1 : startAt.current)
    const outside = (e: PointerEvent) => {
      if (!anchor.current?.contains(e.target as Node)) setOpen(false)
    }
    const left = () => setOpen(false)
    document.addEventListener('pointerdown', outside)
    window.addEventListener('blur', left)
    return () => {
      document.removeEventListener('pointerdown', outside)
      window.removeEventListener('blur', left)
    }
  }, [open])

  const openAt = (i: number) => { startAt.current = i; setOpen(true) }

  const onButtonKey = (e: React.KeyboardEvent) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); openAt(0) }
    if (e.key === 'ArrowUp') { e.preventDefault(); openAt(-1) }
  }

  const onMenuKey = (e: React.KeyboardEvent) => {
    const els = entries(menu.current)
    const i = els.indexOf(document.activeElement as HTMLButtonElement)
    switch (e.key) {
      case 'ArrowDown': e.preventDefault(); focusAt(menu.current, i + 1); break
      case 'ArrowUp': e.preventDefault(); focusAt(menu.current, i - 1); break
      case 'Home': e.preventDefault(); focusAt(menu.current, 0); break
      case 'End': e.preventDefault(); focusAt(menu.current, els.length - 1); break
      case 'Escape': e.preventDefault(); close(true); break
      case 'Tab': close(false); break
    }
  }

  return (
    <div className="ch-menu-anchor" ref={anchor}>
      <button
        ref={button}
        className="ch-btn"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? 'ch-more-menu' : undefined}
        onClick={() => (open ? close(false) : openAt(0))}
        onKeyDown={onButtonKey}
      >
        More <span aria-hidden>▾</span>
      </button>
      {open && (
        <div className="ch-menu" role="menu" id="ch-more-menu" aria-label="More" ref={menu} onKeyDown={onMenuKey}>
          {items.map(item => (
            <div key={item.label} role="none" className={item.apart ? 'ch-menu-apart' : undefined}>
              <button
                className="ch-menu-item"
                role="menuitem"
                tabIndex={-1}
                onClick={() => { close(true); item.run() }}
              >
                {item.label}
                {item.hint && <small>{item.hint}</small>}
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

/**
 * The document bar: studio home, the title, and "Saved · Share · More". A
 * badge only when something is wrong with the file; "Done editing" in the bar
 * itself whenever edit mode is on, so it is never hidden while it is.
 */
export default function Toolbar({
  title, isTemplate, trusted, onHome, onOpenFile, onToggleSidebar, onWorkspace, onSave, onKeepOffline, onExport, editMode, onToggleEdit,
  docPath, onSpinoff, sidebarOpen, loading, validation, saved,
}: Props) {
  const invalid = !!validation && validation !== 'OK'
  const targets = useSpinoffTargets(docPath)

  const items: MenuItem[] = [
    ...(onToggleEdit && !editMode
      ? [{ label: 'Edit', hint: 'Change it yourself. Every change is kept as yours.', run: onToggleEdit }]
      : []),
    { label: 'Open…', hint: 'Open another .clan file', run: onOpenFile },
    { label: 'Save as…', hint: 'Keep a copy of this file', run: onSave },
    ...(onKeepOffline
      ? [{ label: 'Present offline', hint: 'Keep it on this device, to show with no internet', run: onKeepOffline }]
      : []),
    { label: 'Export', hint: 'Make a PDF of it', run: () => onExport('pdf') },
    { label: 'Where it came from', hint: 'What it was made from, step by step', run: onWorkspace },
    ...targets.map((t, i) => ({
      label: `Continue in ${t.name}`,
      hint: t.map ? `Takes this along, into ${t.map}` : 'Takes its data and decisions along',
      run: () => onSpinoff(t.app_id),
      apart: i === 0,
    })),
    {
      label: sidebarOpen ? 'Hide the file’s details' : 'Show the file’s details',
      run: onToggleSidebar,
      apart: true,
    },
  ]

  return (
    <div className="ch-bar">
      <button className="ch-bar-home" onClick={onHome} title="Back to the studio">
        <StudioLogo size={15} compact />
      </button>
      <span className="ch-bar-title">{loading ? <LogoSpinner size="xs" label="Loading…" /> : (title ?? 'No file open')}</span>
      {isTemplate && <span className="ch-chip ch-chip-accent">Template</span>}
      {trusted && (
        <span className="ch-chip ch-chip-ok" title="Signed by Napkin — scoped host capabilities enabled">
          Trusted
        </span>
      )}
      {invalid && (
        <span className="ch-chip ch-chip-warn" title={validation}>
          <span className="ch-chip-dot" />Something’s wrong with this file
        </span>
      )}
      <span className="ch-bar-sep" />
      {onToggleEdit && editMode && (
        <button
          className="ch-btn ch-btn-editing"
          onClick={onToggleEdit}
          aria-pressed
          title="Stop editing"
        >
          Done editing
        </button>
      )}
      {saved && <SaveStatus docPath={docPath} />}
      <button className="ch-btn" onClick={onSave} title="Download a copy to send. It opens in Napkin with everything in it.">
        Share
      </button>
      <MoreMenu items={items} />
      <ThemeToggle />
    </div>
  )
}
