// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// The host this shell is running against.
//
// One bundle serves every shell. Inside the desktop app Tauri has already
// installed its bridge on `window` by the time this module is evaluated. On the
// web the shell talks to `napkin-web` — until it opens a file that lives on
// this device (a drop, the picker, the OS, an offline copy, the public viewer),
// and then it talks to the host compiled to WebAssembly and running in the
// page, which is loaded only then: 5 MB has no business on the critical path
// of a signed-in page that may never need it.

import { httpHost } from './http'
import { tauriHost } from './tauri'
import { prepareFrameHtml } from './wasmFrame'
import type { Host, OpenResult } from './types'

export const isDesktop = '__TAURI_INTERNALS__' in window

/**
 * The serverless build (`npm run build:static`): the same bundle with no
 * server behind it, where the device host is the only host and is allowed to
 * do everything the page can, inference included.
 */
export const serverless = import.meta.env.VITE_NAPKIN_HOST === 'wasm'

const base = import.meta.env.BASE_URL || '/'

/** `/view`: the public viewer. No session, no `/api`, nothing uploaded. */
export const isViewerRoute = (() => {
  const path = location.pathname.startsWith(base) ? location.pathname.slice(base.length) : location.pathname
  return /^\/?view\/?$/.test(path)
})()

type WasmModule = typeof import('./wasm')
let wasm: Promise<WasmModule> | null = null

function loadWasm(): Promise<WasmModule> {
  wasm ??= import('./wasm')
  return wasm
}

// The members a component reads synchronously, answered without the module.
const deviceSync: Partial<Host> = {
  clanOrigin: () => 'clan://localhost',
  prepareAppHtml: prepareFrameHtml,
  frameLoad: 'srcdoc',
  inference: 'page',
}

/** The device host, standing in for itself until its module has loaded. */
const deviceHost = new Proxy({} as Host, {
  get(_, key: keyof Host) {
    if (key in deviceSync) return deviceSync[key]
    // Not a promise, whatever awaits it.
    if ((key as string) === 'then') return undefined
    return (...args: unknown[]) =>
      loadWasm().then(m => (m.wasmHost[key] as (...a: unknown[]) => unknown)(...args))
  },
})

let current: Host = isDesktop ? tauriHost : serverless || isViewerRoute ? deviceHost : httpHost

/** Every call goes to whichever host is current at the time it is made. */
export const host: Host = new Proxy({} as Host, {
  get: (_, key: keyof Host) => current[key],
})

/** True while the open document (or the home screen) is the device's. */
export function onDevice(): boolean {
  return current === deviceHost
}

/**
 * Whether the page may spend the visitor's key for the open app.
 *
 * Not for a file from this device in the web app: that is a recipient's
 * viewer, or an offline copy for presenting, and an app in a file someone sent
 * you should not be able to run up a bill on your key.
 */
export function inferenceAllowed(): boolean {
  return !onDevice() || serverless
}

/** Hand the shell to the device host. The server's session is left as it was. */
export function switchToDevice() {
  if (!isDesktop) current = deviceHost
}

/** Back to the server, when there is one to go back to. */
export function switchToServer() {
  if (!isDesktop && !serverless && !isViewerRoute) current = httpHost
}

/** Whether this page has a server to go back to. */
export const hasServer = !isDesktop && !serverless && !isViewerRoute

/** Open `.clan` bytes on the device, in a host of their own. */
export async function openOnDevice(bytes: Uint8Array, label: string): Promise<OpenResult> {
  switchToDevice()
  return (await loadWasm()).openBytes(bytes, label)
}

export type * from './types'
