// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// Light / dark for the shell.
//
// The palette is CSS custom properties on `:root`, so switching is one
// attribute flip and nothing re-renders to change colour. Until someone picks
// a side we follow the OS and keep following it; the moment they do pick, that
// choice is theirs and the OS stops overriding it.

import { useEffect, useState } from 'react'

export type Theme = 'light' | 'dark'

const STORAGE_KEY = 'napkin.theme'
const QUERY = '(prefers-color-scheme: light)'

const listeners = new Set<(theme: Theme) => void>()

function systemTheme(): Theme {
  return window.matchMedia?.(QUERY).matches ? 'light' : 'dark'
}

/** The user's own choice, if they have made one. */
function storedTheme(): Theme | null {
  try {
    const v = localStorage.getItem(STORAGE_KEY)
    return v === 'light' || v === 'dark' ? v : null
  } catch {
    // Private windows and locked-down browsers throw on access, not on read.
    return null
  }
}

let current: Theme = storedTheme() ?? systemTheme()

function apply(theme: Theme) {
  const root = document.documentElement
  root.dataset.theme = theme
  // Native widgets — scrollbars, form controls, the canvas behind the page —
  // follow this rather than our variables.
  root.style.colorScheme = theme
}

function set(theme: Theme, remember: boolean) {
  if (remember) {
    try {
      localStorage.setItem(STORAGE_KEY, theme)
    } catch { /* the preference just won't survive the tab */ }
  }
  if (theme === current) return
  current = theme
  apply(theme)
  for (const fn of listeners) fn(theme)
}

apply(current)

// Keep following the OS, but only for as long as there is nothing to override.
window.matchMedia?.(QUERY).addEventListener?.('change', e => {
  if (storedTheme()) return
  set(e.matches ? 'light' : 'dark', false)
})

export function getTheme(): Theme {
  return current
}

export function setTheme(theme: Theme) {
  set(theme, true)
}

export function toggleTheme() {
  setTheme(current === 'dark' ? 'light' : 'dark')
}

export function onThemeChange(fn: (theme: Theme) => void): () => void {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

export function useTheme(): Theme {
  const [theme, setLocal] = useState(getTheme)
  useEffect(() => onThemeChange(setLocal), [])
  return theme
}
