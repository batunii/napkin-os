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

import type {
  ClientConfirmReply,
  ClientReopenReply,
  ClientReviewReply,
  DecisionsView,
  Host,
  HostEvents,
  InstalledApp,
  OpenResult,
  RecentDoc,
  SpinoffTarget,
  Unlisten,
  UploadedAsset,
  UpstreamView,
} from './types'

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
  // A page that started offline must be able to try again once it is back.
  sessionOnce.catch(() => { sessionOnce = null })
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

/**
 * A `clan://` route, reached as the frame reaches it: on the frame's token
 * URL, so it acts on the document the frame shows. No content type, as the
 * app's own calls send none — a plain request, which the sandbox origin
 * answers across origins. A refusal is thrown as the host's own words.
 */
async function clanCall<T>(route: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${httpHost.clanOrigin()}${route}`, init)
  const body = await resp.text()
  let parsed: unknown = null
  try { parsed = JSON.parse(body) } catch { /* not JSON: the body is the message */ }
  if (!resp.ok) {
    const err = (parsed as { error?: unknown } | null)?.error
    const message = typeof err === 'string' ? err : (err as { message?: string } | undefined)?.message
    throw new Error(message || body || `${route.slice(1)}: ${resp.status}`)
  }
  return parsed as T
}

const clanPost = <T,>(route: string, body: unknown) =>
  clanCall<T>(route, { method: 'POST', body: JSON.stringify(body) })

/** Opens asked for so far. Only the latest one may become the open document. */
let opens = 0

/**
 * Open something, and remember it as what the shell has open — unless another
 * open was asked for while this one was in flight.
 *
 * Responses do not come back in the order they were asked for. The shell asks
 * for home and for a `?open=` document at the same moment on load, and when
 * home answered last it became "the open document" underneath a frame showing
 * the other one: that frame's token, its /chain and its writes all went to
 * home. The last open *asked for* is the one the shell is showing, so it is the
 * one that wins.
 */
async function adopt(request: Promise<OpenView>): Promise<OpenResult> {
  const mine = ++opens
  const view = await request
  if (mine === opens) {
    openDoc = view.path
    openToken = view.token
    sandboxOrigin = view.sandbox_origin
  }
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

/** Put a shim ahead of every script the app has, not after them.
 *
 * Apps fetch on parse, not on DOMContentLoaded — the launcher asks for its app
 * list from an inline script in the body. A shim spliced at `</body>` installs
 * itself after that has already run, and quietly does nothing. */
function injectFirst(html: string, script: string): string {
  const head = /<head\b[^>]*>/i.exec(html)
  if (head) {
    const at = head.index + head[0].length
    return html.slice(0, at) + script + html.slice(at)
  }
  const tag = /<html\b[^>]*>/i.exec(html)
  if (tag) {
    const at = tag.index + tag[0].length
    return html.slice(0, at) + script + html.slice(at)
  }
  return script + html
}

/**
 * The open document as the server holds it now: its packed `.clan` and the
 * manifest that describes it. What "Present offline" keeps.
 *
 * The same `/download` route "Save as" uses — the single-file handoff, which
 * is exactly what N3 says an offline copy is: a snapshot, never a working
 * copy. The manifest is read first, so a revision is never newer than the
 * bytes stored with it.
 */
export async function fetchOpenDocument(): Promise<{ manifest: OpenResult['manifest']; bytes: ArrayBuffer }> {
  const doc = requireDoc()
  const { manifest } = await json<OpenView>(`/d/${encodeURIComponent(doc)}`)
  const bytes = await (await request(`/d/${doc}/download`, { cache: 'no-store' })).arrayBuffer()
  return { manifest, bytes }
}

export const httpHost: Host = {
  // The tenant is the person: napkin-web's TenantId::ctx (tenant.rs) acts as
  // `human:<tenant>`, and /session is where the page learns the tenant.
  whoAmI: async () => {
    const { tenant } = await session()
    return { actor: `human:${tenant}`, id: tenant, name: null }
  },

  openClan: path => adopt(json<OpenView>(`/d/${encodeURIComponent(path)}`)),
  openHome: () => adopt(json<OpenView>('/home')),
  newDocumentFromApp: async (appId, title) =>
    adopt(json<OpenView>('/documents', postJson({ app_id: appId, title }))),

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
  // A clan:// route, so it is the frame's token that reaches it.
  getDecisions: async () => {
    const resp = await fetch(`${httpHost.clanOrigin()}/decisions`)
    if (!resp.ok) throw new Error(`decisions: ${resp.status}`)
    return resp.json() as Promise<DecisionsView>
  },
  acknowledge: async decision => {
    const resp = await fetch(`${httpHost.clanOrigin()}/acknowledge`, { method: 'POST', body: JSON.stringify({ decision }) })
    if (!resp.ok) throw new Error(((await resp.json().catch(() => null)) as { error?: string } | null)?.error ?? `acknowledge: ${resp.status}`)
  },

  upstream: () => clanCall<UpstreamView>('/upstream'),

  clientReview: body => clanPost<ClientReviewReply>('/client-review', body),
  clientReviewConfirm: body => clanPost<ClientConfirmReply>('/client-review/confirm', body),
  clientReviewReopen: answer => clanPost<ClientReopenReply>('/client-review/reopen', { answer }),
  uploadAsset: (name, bytes) =>
    clanCall<UploadedAsset>(`/upload-asset?name=${encodeURIComponent(name)}`, {
      method: 'POST',
      body: bytes as unknown as BodyInit,
    }),

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
  frameLoad: 'url',
  handleFromFrame: () =>
    Promise.reject(new Error('the frame reaches the host directly in this build')),
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
    return injectFirst(rewritten, shim)
  },

  listApps: () => json<InstalledApp[]>('/apps'),
  installApp: srcPath =>
    json<InstalledApp>(`/apps/from/${encodeURIComponent(srcPath)}`, { method: 'POST' }),

  // The tenant's own /recent (napkin-web api.rs), not the frame's clan://
  // origin: that one needs an open document's token, and home has none.
  listRecent: () => json<RecentDoc[]>('/recent'),

  spinoffTargets: () => json<SpinoffTarget[]>(`/d/${requireDoc()}/spinoff-targets`),
  spinoffDocument: (appId, title, map) =>
    adopt(
      json<OpenView>(
        `/d/${requireDoc()}/spinoff`,
        postJson({ app_id: appId, title, map }),
      ),
    ),

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
