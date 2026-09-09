// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// The serverless host: napkin-host compiled to WebAssembly, running here.
//
// Same contract as the desktop and the web service, and — importantly — the
// same code underneath. Documents are real `.clan` archives, created and
// patched and validated by the same SDK; they simply never leave the tab.
// Templates are fetched from wherever the site publishes them, so shipping a
// new version of an app is republishing one file.
//
// Nothing here persists. Work leaves as a download, and a refresh is a clean
// slate — which is the right default for a link anyone can open.

import init, { NapkinHost } from '../wasm/napkin_wasm'
import type { Host, HostEvents, InstalledApp, OpenResult, Unlisten } from './types'

/** Bytes out of wasm arrive as an Array unless they already are a view. */
function asBytes(value: Uint8Array | number[]): Uint8Array {
  return value instanceof Uint8Array ? value : Uint8Array.from(value)
}

/** What `NapkinHost.handle` gives back. */
interface RawResponse {
  status: number
  headers: [string, string][]
  body: Uint8Array | number[]
  events: { name: string; payload: unknown }[]
}

/** One published app, listed in the site's `apps.json`. */
interface AppEntry {
  app_id: string
  url: string
}

const base = import.meta.env.BASE_URL || '/'

let host: NapkinHost | null = null
let ready: Promise<NapkinHost> | null = null
let currentDoc: string | null = null

/** Templates published beside the site. Fetched once, installed into the store. */
async function loadApps(h: NapkinHost) {
  let listed: AppEntry[]
  try {
    const resp = await fetch(`${base}apps.json`, { cache: 'no-cache' })
    if (!resp.ok) return
    listed = ((await resp.json()) as { apps?: AppEntry[] }).apps ?? []
  } catch {
    // No manifest is not fatal: the launcher still opens, just empty.
    return
  }
  await Promise.all(
    listed.map(async app => {
      try {
        const bytes = await (await fetch(new URL(app.url, new URL(base, location.href)))).arrayBuffer()
        h.installApp(new Uint8Array(bytes))
      } catch (e) {
        console.error(`could not install ${app.app_id}`, e)
      }
    }),
  )
}

function boot(): Promise<NapkinHost> {
  ready ??= (async () => {
    await init()
    const h = new NapkinHost()
    await loadApps(h)
    host = h
    return h
  })()
  return ready
}

function required(): NapkinHost {
  if (!host) throw new Error('the host is still starting')
  return host
}

function adopt(open: OpenResult): OpenResult {
  currentDoc = open.path
  return open
}

// ── Events ───────────────────────────────────────────────────────────────────
//
// A route can ask the shell to do something it cannot do itself — open a
// document, raise a toast, recolor the chrome. On the desktop those are Tauri
// events and on the server they are SSE frames; here they come straight back
// from the call, and get dispatched to the same listeners.

type Handler = (payload: unknown) => void
const listeners = new Map<string, Set<Handler>>()

function emit(name: string, payload: unknown) {
  for (const fn of listeners.get(name) ?? []) fn(payload)
}

function dispatch(resp: RawResponse) {
  for (const e of resp.events) emit(e.name, e.payload)
}

function download(bytes: Uint8Array, filename: string) {
  const url = URL.createObjectURL(new Blob([bytes as unknown as BlobPart]))
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  // Give the click a tick before the URL is revoked.
  setTimeout(() => URL.revokeObjectURL(url), 10_000)
}

function stem(title: string): string {
  const t = title.trim() || 'document'
  return t.replace(/[^\w.-]+/g, '-')
}

/** Put a shim ahead of every script the app has, not after them.
 *
 * Apps fetch on parse, not on DOMContentLoaded — the launcher asks for its app
 * list from an inline script in the body. A shim spliced at `</body>` installs
 * itself after that has already run and quietly does nothing. */
function injectFirst(html: string, script: string): string {
  const head = /<head\b[^>]*>/i.exec(html)
  if (head) return html.slice(0, head.index + head[0].length) + script + html.slice(head.index + head[0].length)
  const tag = /<html\b[^>]*>/i.exec(html)
  if (tag) return html.slice(0, tag.index + tag[0].length) + script + html.slice(tag.index + tag[0].length)
  return script + html
}

/**
 * Hand a composed document to the user.
 *
 * There is no headless browser here, so a PDF is the browser's own print
 * dialog. It prints from a hidden iframe rather than a popup: `window.open`
 * must be called synchronously inside the click to survive a popup blocker,
 * and composing the document takes an `await` — the gesture is spent by the
 * time we would ask. An iframe needs no gesture at all.
 */
function printHtml(html: string) {
  const frame = document.createElement('iframe')
  frame.setAttribute('aria-hidden', 'true')
  // Off-screen, but laid out at a real page size. A zero-sized or
  // `visibility:hidden` frame has nothing to lay out, and some browsers print
  // it as a blank page.
  frame.style.cssText =
    'position:fixed;left:-10000px;top:0;width:210mm;height:297mm;border:0'
  frame.onload = () => {
    try {
      frame.contentWindow?.focus()
      frame.contentWindow?.print()
    } finally {
      // The dialog is modal and the frame must outlive it; a minute is plenty.
      setTimeout(() => frame.remove(), 60_000)
    }
  }
  document.body.appendChild(frame)
  // srcdoc, not document.write: it fires `load` reliably, after we are listening.
  frame.srcdoc = html
}

function deliverExport(kind: string, filename: string, html: string) {
  if (kind === 'pdf') printHtml(html)
  else download(new TextEncoder().encode(html), `${filename}.html`)
}

export const wasmHost: Host = {
  openClan: async path => adopt((await boot()).open(path) as OpenResult),
  openHome: async () => adopt((await boot()).openHome() as OpenResult),
  newDocumentFromApp: async (appId, title) =>
    adopt((await boot()).newDocument(appId, title) as OpenResult),

  // A shared link cannot carry a document, because there is no server holding
  // one. Nothing to take.
  takeLaunchFile: async () => null,

  getHumanHtml: async () => (await boot()).humanHtml(),
  getData: async () => (await boot()).entry('shared/data.yaml'),
  getChain: async () => (await boot()).entry('agent/decision-chain.yaml'),
  getAgentState: async () => (await boot()).entry('agent/state.yaml'),
  getContext: async () => (await boot()).entry('agent/context.md'),

  setEditMode: async active => { (await boot()).setEditMode(active) },
  // The frame is loaded from srcdoc, so there is no document slot to fill.
  updatePreviewHtml: async () => {},
  savePatch: async (id, content) => {
    const h = await boot()
    const body = new TextEncoder().encode(JSON.stringify({ id, content }))
    dispatch(h.handle('/patch', '', body) as RawResponse)
  },

  // Nothing is served over a URL here; the shim routes the app's calls to us.
  clanOrigin: () => 'clan://localhost',
  frameLoad: 'srcdoc',

  prepareAppHtml: html => {
    const shim = `<script>(function(){
  var f=window.fetch, seq=0, pending={};
  window.addEventListener('message',function(e){
    if(e.source!==window.parent) return;
    var m=e.data;
    if(m&&m.type==='clan:rpc-reply'&&pending[m.id]){pending[m.id](m);delete pending[m.id];}
  });
  function rpc(path,query,body){
    return new Promise(function(resolve){
      var id=++seq; pending[id]=resolve;
      window.parent.postMessage({type:'clan:rpc',id:id,path:path,query:query,body:body},'*');
    }).then(function(m){
      // 204 and 304 may not carry a body; nothing here returns them, but a
      // Response constructed with one would throw.
      var body=(m.status===204||m.status===304)?null:m.body;
      return new Response(body,{status:m.status,headers:m.headers||{}});
    });
  }
  // There is no server: every clan:// call is a function call in the parent.
  function split(u){
    var m=/^(?:clan:\\/\\/localhost|http:\\/\\/clan\\.localhost)(\\/[^?]*)(?:\\?(.*))?$/.exec(u);
    return m?{path:m[1],query:m[2]||''}:null;
  }
  window.fetch=function(input,init){
    if(typeof input==='string'){
      var t=split(input);
      if(t) return rpc(t.path,t.query,(init&&init.body)||'');
    }
    return f.call(this,input,init);
  };

  // An <img src="clan://…/assets/x.png"> never reaches fetch, so asset URLs in
  // the DOM are swapped for blobs as they appear. Mood boards depend on it.
  var blobs={};
  function resolveAsset(el){
    var src=el.getAttribute('src')||'';
    var t=split(src);
    if(!t||t.path.indexOf('/assets/')!==0) return;
    if(blobs[src]){el.src=blobs[src];return;}
    rpc(t.path,t.query,'').then(function(r){return r.ok?r.blob():null;}).then(function(b){
      if(b){blobs[src]=URL.createObjectURL(b);el.src=blobs[src];}
    });
  }
  function sweep(root){
    if(root.querySelectorAll) root.querySelectorAll('img[src]').forEach(resolveAsset);
    if(root.tagName==='IMG') resolveAsset(root);
  }
  new MutationObserver(function(muts){
    muts.forEach(function(m){
      m.addedNodes.forEach(function(n){ if(n.nodeType===1) sweep(n); });
      if(m.type==='attributes'&&m.target.tagName==='IMG') resolveAsset(m.target);
    });
  }).observe(document.documentElement,{childList:true,subtree:true,attributes:true,attributeFilter:['src']});
  document.addEventListener('DOMContentLoaded',function(){sweep(document);});
})();</script>`
    return injectFirst(html, shim)
  },

  handleFromFrame: async (path, query, body) => {
    const resp = (await boot()).handle(path, query, body) as RawResponse
    dispatch(resp)
    // An app that builds its own print layout pushes it here. On a server the
    // host stashes it and the shell fetches it back; here the shell is holding
    // it already, so deliver it and tell the app it is done.
    if (path === '/export' && resp.status === 200) {
      const out = JSON.parse(new TextDecoder().decode(asBytes(resp.body))) as {
        kind: string
        filename: string
        html: string
      }
      deliverExport(out.kind, out.filename, out.html)
      return {
        status: 200,
        headers: [['content-type', 'application/json']] as [string, string][],
        body: new TextEncoder().encode('{"ok":true}'),
      }
    }
    // serde-wasm-bindgen renders `Vec<u8>` as a plain Array of numbers, and a
    // Response built from one stringifies it — "91,34,110..." — so the app's
    // `.json()` throws and, because apps catch their own fetch errors, nothing
    // is reported. Normalise it here rather than let that be silent.
    return { ...resp, body: asBytes(resp.body) }
  },

  listApps: async () => (await boot()).listApps() as InstalledApp[],
  // Installing means taking an already-uploaded document into the app library.
  installApp: async docId => {
    const h = await boot()
    h.open(docId)
    const bytes = h.download()
    return h.installApp(bytes) as InstalledApp
  },

  saveClanTo: async () => {
    const h = await boot()
    download(asBytes(h.download()), `${stem(h.title())}.clan`)
  },
  exportCurrent: async (kind, provenance, noBrand) => {
    const h = await boot()
    // Composed by the SDK, same as everywhere else; only the delivery differs.
    const { html, filename } = h.composeExport(provenance, noBrand) as {
      html: string
      filename: string
    }
    deliverExport(kind, filename, html)
  },
  finishExport: async (_kind, _handle, dest) => dest,
  pickSaveDestination: async (defaultName, ext) => `${defaultName}.${ext}`,

  pickClanToOpen: () =>
    new Promise(resolve => {
      const input = document.createElement('input')
      input.type = 'file'
      input.accept = '.clan'
      input.onchange = async () => {
        const file = input.files?.[0]
        if (!file) return resolve(null)
        try {
          const h = await boot()
          const bytes = new Uint8Array(await file.arrayBuffer())
          const id = `upload-${Date.now().toString(36)}`
          const open = h.upload(bytes, id) as OpenResult
          resolve(adopt(open).path)
        } catch (e) {
          console.error('upload failed', e)
          resolve(null)
        }
      }
      input.oncancel = () => resolve(null)
      input.click()
    }),

  agentEndpoint: async () => 'your browser',
  agentPrompt: async () => ({ ok: false, error: 'not available in the browser build' }),
  inference: 'page',
  buildAgentPrompt: async payload => {
    const h = await boot()
    const body = new TextEncoder().encode(JSON.stringify({ payload }))
    const resp = h.handle('/agent-prompt', '', body) as RawResponse
    return JSON.parse(new TextDecoder().decode(asBytes(resp.body))) as {
      system: string
      user: string
    }
  },

  on: async <K extends keyof HostEvents>(
    event: K,
    handler: (payload: HostEvents[K]) => void,
  ): Promise<Unlisten> => {
    const set = listeners.get(event) ?? new Set()
    listeners.set(event, set)
    set.add(handler as Handler)
    return () => set.delete(handler as Handler)
  },
}

export { currentDoc as wasmCurrentDoc, required as wasmHostInstance }

/// The backend this build talks to — see the alias in vite.config.ts.
export { wasmHost as backendHost }
