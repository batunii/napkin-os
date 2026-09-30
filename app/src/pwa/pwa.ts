// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// The page's side of the installable app: the service worker, the browser's
// install prompt, and files the OS hands us.
//
// Everything here is feature-detected. Safari and Firefox have no install
// prompt event and no launch queue; they get the service worker, and a file
// dropped on the window or chosen with the picker.

import { useEffect, useState } from 'react'

const base = import.meta.env.BASE_URL || '/'

type Listener = () => void

function signal() {
  const listeners = new Set<Listener>()
  return {
    fire: () => listeners.forEach(fn => fn()),
    on: (fn: Listener) => {
      listeners.add(fn)
      return () => { listeners.delete(fn) }
    },
  }
}

// ── The service worker ───────────────────────────────────────────────────────

let waiting: ServiceWorker | null = null
const updates = signal()

/**
 * Register the worker, and notice when a new build has finished installing.
 *
 * It waits rather than taking over: a new build replaces the shell's files,
 * and a page halfway through presenting keeps the build it started with until
 * someone chooses to reload.
 */
export function registerServiceWorker() {
  if (!('serviceWorker' in navigator) || import.meta.env.DEV) return
  // The local stack keeps no offline copy: a cached shell there shows last
  // build's screens after every rebuild. Drop any worker and its caches.
  if (/^(localhost|127\.0\.0\.1|\[::1\])$/.test(location.hostname)) {
    void navigator.serviceWorker.getRegistrations().then(rs => rs.forEach(r => void r.unregister()))
    if ('caches' in window) void caches.keys().then(ks => ks.forEach(k => void caches.delete(k)))
    return
  }
  window.addEventListener('load', async () => {
    try {
      const reg = await navigator.serviceWorker.register(`${base}sw.js`, { scope: base })
      const note = (sw: ServiceWorker | null) => {
        // A first install has no controller to replace; nothing to offer.
        if (sw && navigator.serviceWorker.controller) {
          waiting = sw
          updates.fire()
        }
      }
      note(reg.waiting)
      reg.addEventListener('updatefound', () => {
        const sw = reg.installing
        sw?.addEventListener('statechange', () => { if (sw.state === 'installed') note(sw) })
      })
    } catch (e) {
      console.warn('service worker not registered', e)
    }
  })
}

/** Take the waiting build and reload into it. */
export function applyUpdate() {
  if (!waiting) return
  navigator.serviceWorker.addEventListener('controllerchange', () => location.reload(), { once: true })
  waiting.postMessage('napkin:skip-waiting')
}

/** True once a new build is ready to take over. */
export function useUpdateReady(): boolean {
  const [ready, setReady] = useState(waiting !== null)
  useEffect(() => updates.on(() => setReady(true)), [])
  return ready
}

// ── Install ──────────────────────────────────────────────────────────────────

interface InstallPromptEvent extends Event {
  prompt(): Promise<void>
  userChoice: Promise<{ outcome: 'accepted' | 'dismissed' }>
}

let deferred: InstallPromptEvent | null = null
const installable = signal()

// Registered at import: the event fires once, early, and is gone if nobody is
// listening for it.
window.addEventListener('beforeinstallprompt', e => {
  e.preventDefault()
  deferred = e as InstallPromptEvent
  installable.fire()
})
window.addEventListener('appinstalled', () => {
  deferred = null
  installable.fire()
})

/** The browser's own install, when it offers one; `null` when it does not. */
export function useInstall(): (() => void) | null {
  const [can, setCan] = useState(deferred !== null)
  useEffect(() => installable.on(() => setCan(deferred !== null)), [])
  if (!can) return null
  return () => {
    const e = deferred
    if (!e) return
    deferred = null
    setCan(false)
    e.prompt().catch(() => {})
  }
}

// ── Files from the OS ────────────────────────────────────────────────────────

interface LaunchParams {
  files: FileSystemFileHandle[]
}

interface LaunchQueue {
  setConsumer(consumer: (params: LaunchParams) => void): void
}

/**
 * A `.clan` double-clicked, or opened with, in an installed app — Chrome and
 * Edge only, through the manifest's `file_handlers`. Launches queue until a
 * consumer is set, so it is fine to call this once the shell is ready.
 */
export function onLaunchFiles(open: (file: File) => void) {
  const queue = (window as unknown as { launchQueue?: LaunchQueue }).launchQueue
  queue?.setConsumer(async params => {
    const handle = params.files[0]
    if (handle) open(await handle.getFile())
  })
}

/** Ask the browser not to evict what we store. A no-op where unsupported. */
export async function persistStorage(): Promise<boolean> {
  try {
    return (await navigator.storage?.persist?.()) ?? false
  } catch {
    return false
  }
}
