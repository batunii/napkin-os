// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// Asking the running app to export itself.
//
// The host can compose an export from a document's data, and for a view whose
// content is in the markup that is exactly right. But an authored app renders
// client-side from `window.__CLAN__.data`, so composing from its markup — with
// the scripts stripped, as an export must — yields the empty shell it starts
// as. Brief Maker's intake form instead of the brief.
//
// So the shell asks first, and only composes if nothing answers. An app claims
// the request by calling `preventDefault()` on the `clan:export` event, which
// is also how it says "I have a better idea of what this should look like".

const TIMEOUT_MS = 800

let frame: Window | null = null

/** Called by the render surface as the app frame loads. */
export function setExportFrame(win: Window | null) {
  frame = win
}

/**
 * Ask the app to export itself. Resolves `true` if it took the job — in which
 * case it will deliver through `clan://export` and the shell should not
 * compose its own.
 */
export function askAppToExport(kind: 'html' | 'pdf'): Promise<boolean> {
  const target = frame
  if (!target) return Promise.resolve(false)

  return new Promise(resolve => {
    const id = `x${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`
    const finish = (handled: boolean) => {
      clearTimeout(timer)
      window.removeEventListener('message', onMessage)
      resolve(handled)
    }
    // An app that does not implement this never answers, so the fallback must
    // not wait on it forever.
    const timer = setTimeout(() => finish(false), TIMEOUT_MS)
    function onMessage(e: MessageEvent) {
      const msg = e.data as { type?: string; id?: string; handled?: boolean }
      if (msg?.type === 'clan:export-ack' && msg.id === id) finish(!!msg.handled)
    }
    window.addEventListener('message', onMessage)
    target.postMessage({ type: 'clan:export-request', id, kind }, '*')
  })
}
