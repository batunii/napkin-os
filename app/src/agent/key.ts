// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// The visitor's own Anthropic key.
//
// It lives in this browser and nowhere else: not in the bundle, not on a
// server, not in a request to anything but Anthropic. That is the whole reason
// the app can be a static file — there is no backend holding anyone's
// credentials, because each visitor brings their own.
//
// It is deliberately never handed to app code. An app asks the shell for
// inference; the shell makes the call. A `.clan` is third-party HTML and has no
// business seeing a key.

import { useEffect, useState } from 'react'

const STORAGE_KEY = 'napkin.anthropic-key'

const listeners = new Set<(key: string | null) => void>()

function read(): string | null {
  try {
    return localStorage.getItem(STORAGE_KEY) || null
  } catch {
    return null
  }
}

let current: string | null = read()

export function getKey(): string | null {
  return current
}

export function hasKey(): boolean {
  return !!current
}

/** Anthropic keys start `sk-ant-`; catch a paste of the wrong thing early. */
export function looksLikeKey(value: string): boolean {
  return /^sk-ant-\S{20,}$/.test(value.trim())
}

export function setKey(key: string) {
  const trimmed = key.trim()
  current = trimmed || null
  try {
    if (current) localStorage.setItem(STORAGE_KEY, current)
    else localStorage.removeItem(STORAGE_KEY)
  } catch { /* the key just won't outlive the tab */ }
  for (const fn of listeners) fn(current)
}

export function clearKey() {
  setKey('')
}

export function onKeyChange(fn: (key: string | null) => void): () => void {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

export function useHasKey(): boolean {
  const [present, setPresent] = useState(hasKey)
  useEffect(() => onKeyChange(k => setPresent(!!k)), [])
  return present
}
