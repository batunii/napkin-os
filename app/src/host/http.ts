// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// The web host: the same contract, over `napkin-web`'s HTTP API.
//
// Two things the desktop gets for free and this has to arrange:
//
//  * Which document is open. Tauri commands act on the host's one open file;
//    the HTTP API addresses documents explicitly. So this module remembers what
//    the shell last opened, which is the same state, just held on this side.
//  * Reaching the clan:// API. An app inside a .clan asks for
//    `clan://localhost/patch-data`, a scheme no browser has. `prepareAppHtml`
//    rewrites that base to the frame's own token URL, so app HTML runs
//    unmodified.

import type { Host, HostEvents, InstalledApp, OpenResult, Unlisten } from './types'

interface SessionInfo {
  tenant: string
  sandbox_origin: string | null
  quota: { used: number; cap: number }
}

/** An opened document, plus what the frame needs to talk to the host. */
interface OpenView extends OpenResult {
  token: string
  sandbox_origin: string | null
}

let sessionOnce: Promise<SessionInfo> | null = null

function session(): Promise<SessionInfo> {
  sessionOnce ??= fetch('/api/session', { credentials: 'same-origin' }).then(r => {
    if (!r.ok) throw new Error(`no session: ${r.status}`)
    return r.json() as Promise<SessionInfo>
  })
  return sessionOnce
}

async function base(): Promise<string> {
  return `/api/t/${(await session()).tenant}`
}

// What the shell has open. The API is explicit about documents; the Host
// contract is not, so the current one lives here.
let openDoc: string | null = null
let openToken: string | null = null
let sandboxOrigin: string | null = null

function requireDoc(): string {
  if (!openDoc) throw new Error('no file open')
  return openDoc
}

async function request(path: string, init?: RequestInit): Promise<Response> {
  const resp = await fetch(`${await base()}${path}`, { credentials: 'same-origin', ...init })
  if (!resp.ok) {
    const body = await resp.text()
    let message = body
    try {
      message = (JSON.parse(body) as { error?: string }).error ?? body
    } catch { /* not JSON: the body is the message */ }
    throw new Error(message || `${resp.status} ${resp.statusText}`)
  }
  return resp
}

const asJson = <T,>(r: Response) => r.json() as Promise<T>
const asText = (r: Response) => r.text()

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  return asJson<T>(await request(path, init))
}

async function text(path: string, init?: RequestInit): Promise<string> {
  return asText(await request(path, init))
}

function postJson(body: unknown): RequestInit {
  return { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }
}

/** Remember what the shell opened, and hand back the plain OpenResult. */
function adopt(view: OpenView): OpenResult {
  openDoc = view.path
  openToken = view.token
  sandboxOrigin = view.sandbox_origin
  return view
}

/** Save bytes the server is offering, without leaving the page. */
function download(url: string) {
  const a = document.createElement('a')
  a.href = url
  a.rel = 'noopener'
  document.body.appendChild(a)
  a.click()
  a.remove()
}

// One stream per page, fanned out to the shell's listeners.
//
// The promise is what is memoised, not the EventSource. The shell registers all
// of its listeners in a single tick, so caching the value instead would let
// every one of them get past the check while the first was still awaiting its
// URL — and seven streams is not merely wasteful: each holds a connection, and
// six is all a browser will give one origin over HTTP/1.1, so the rest of the
// app stops loading.
let streamOnce: Promise<EventSource> | null = null

function events(): Promise<EventSource> {
  streamOnce ??= base().then(b => new EventSource(`${b}/events`, { withCredentials: true }))
  return streamOnce
}

export const httpHost: Host = {
  openClan: async path => adopt(await json<OpenView>(`/d/${encodeURIComponent(path)}`)),
  openHome: async () => adopt(await json<OpenView>('/home')),
  newDocumentFromApp: async (appId, title) =>
    adopt(await json<OpenView>('/documents', postJson({ app_id: appId, title }))),

  // A shared link — `?open=<document>` — is the web's answer to double-clicking
  // a .clan. Consumed once, then cleared so a reload does not re-open it.
  takeLaunchFile: async () => {
    const params = new URLSearchParams(window.location.search)
    const doc = params.get('open')
    if (!doc) return null
    params.delete('open')
    const query = params.toString()
    window.history.replaceState({}, '', window.location.pathname + (query ? `?${query}` : ''))
    return doc
  },

  getHumanHtml: () => text(`/d/${requireDoc()}/human-html`),
  getData: () => text(`/d/${requireDoc()}/entry/data`),
  getChain: () => text(`/d/${requireDoc()}/entry/chain`),
  getAgentState: () => text(`/d/${requireDoc()}/entry/state`),
  getContext: () => text(`/d/${requireDoc()}/entry/context`),

  setEditMode: async active => {
    await request(`/d/${requireDoc()}/edit-mode`, postJson({ active }))
  },
  updatePreviewHtml: async html => {
    await request(`/d/${requireDoc()}/preview-html`, { method: 'POST', body: html })
  },
  savePatch: async (id, content) => {
    await request(`/d/${requireDoc()}/patch`, postJson({ id, content }))
  },

  clanOrigin: () => `${sandboxOrigin ?? window.location.origin}/s/${openToken ?? 'no-token'}`,

  // Apps compute their API base once, at parse time, from a scheme the browser
  // does not have. Rewriting the two forms it can take — and patching `fetch`
  // for anything built later — is what lets an unmodified .clan run here.
  prepareAppHtml: html => {
    const origin = httpHost.clanOrigin()
    const rewritten = html
      .split('http://clan.localhost')
      .join(origin)
      .split('clan://localhost')
      .join(origin)
    const shim = `<script>(function(){
  var BASE=${JSON.stringify(origin)};
  var f=window.fetch;
  window.fetch=function(input,init){
    if(typeof input==='string'){
      input=input.replace(/^clan:\\/\\/localhost/,BASE).replace(/^http:\\/\\/clan\\.localhost/,BASE);
    }
    return f.call(this,input,init);
  };
  var c=window.__CLAN__;
  if(c&&c.assets){for(var k in c.assets){if(c.assets[k].charAt(0)==='/')c.assets[k]=BASE+c.assets[k];}}
})();</script>`
    const close = rewritten.toLowerCase().lastIndexOf('</body>')
    return close >= 0 ? rewritten.slice(0, close) + shim + rewritten.slice(close) : rewritten + shim
  },

  listApps: () => json<InstalledApp[]>('/apps'),
  installApp: srcPath =>
    json<InstalledApp>(`/apps/from/${encodeURIComponent(srcPath)}`, { method: 'POST' }),

  saveClanTo: async () => {
    download(`${await base()}/d/${requireDoc()}/download`)
  },
  exportCurrent: async (kind, provenance, noBrand) => {
    // The server answers with an event; `finishExport` collects the file.
    await request(`/d/${requireDoc()}/export`, postJson({ kind, provenance, no_brand: noBrand }))
  },
  finishExport: async (kind, tmpHtml, dest) => {
    download(`${await base()}/export/${encodeURIComponent(tmpHtml)}?kind=${encodeURIComponent(kind)}`)
    return dest
  },

  // There is no destination to pick in a browser — the file lands wherever
  // downloads go. The name is still worth having, for the toast.
  pickSaveDestination: async (defaultName, ext) => `${defaultName}.${ext}`,

  // "Pick a file" means upload one: the document has to exist server-side
  // before it can be opened, so the picker returns the id it was given.
  pickClanToOpen: () =>
    new Promise(resolve => {
      const input = document.createElement('input')
      input.type = 'file'
      input.accept = '.clan'
      input.onchange = async () => {
        const file = input.files?.[0]
        if (!file) return resolve(null)
        try {
          const view = await json<OpenView>('/documents/upload', {
            method: 'POST',
            body: await file.arrayBuffer(),
          })
          resolve(view.path)
        } catch (e) {
          console.error('upload failed', e)
          resolve(null)
        }
      }
      // A cancelled picker fires nothing in some browsers; `cancel` covers it.
      input.oncancel = () => resolve(null)
      input.click()
    }),

  agentEndpoint: async () => (await json<{ endpoint: string }>('/agent/endpoint')).endpoint,
  agentPrompt: text_ => json<unknown>('/agent/prompt', postJson({ text: text_ })),

  on: async <K extends keyof HostEvents>(
    event: K,
    handler: (payload: HostEvents[K]) => void,
  ): Promise<Unlisten> => {
    const source = await events()
    const listener = (e: MessageEvent<string>) => {
      try {
        handler(JSON.parse(e.data) as HostEvents[K])
      } catch {
        console.warn(`malformed ${event} event`, e.data)
      }
    }
    source.addEventListener(event, listener as EventListener)
    return () => source.removeEventListener(event, listener as EventListener)
  },
}
