// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// The service worker: the shell, the WebAssembly host and the published
// templates, kept so the app opens with no network.
//
// The build fills in the two placeholders (vite.config.ts). Four rules:
//
//  * The server's routes are never touched. `/api/*` is client work, and the
//    server is authoritative for it (N3); `/s/*` is an app frame's capability
//    URL. Neither is answered from, or written to, a cache.
//  * The shell is precached whole and served from the cache. A new build is a
//    new cache, installed beside the old one and swapped in when the page says
//    so — never half of one build's files and half of another's.
//  * `apps.json` and the templates try the network first, because republishing
//    one of them is how an app ships; the precached copy is the fallback.
//  * Everything else goes to the network untouched.
//
// Offline copies are not here. The shell keeps them in IndexedDB, where an
// app frame — sandboxed onto an opaque origin — cannot reach them.

const VERSION = __NAPKIN_VERSION__
const PRECACHE = __NAPKIN_PRECACHE__

const SHELL = `napkin-shell-${VERSION}`
const FONTS = 'napkin-fonts'
const SCOPE = new URL('./', self.location.href)
const INDEX = new URL('index.html', SCOPE).href

/** Paths the server owns, whatever base the shell is served under. */
function serverOwned(url) {
  if (url.origin !== SCOPE.origin) return false
  const p = url.pathname
  return ['api/', 's/'].some(r => p.startsWith(`/${r}`) || p.startsWith(SCOPE.pathname + r))
}

/** Published separately from the shell: apps.json and apps/*.clan. */
function published(url) {
  return url.origin === SCOPE.origin && url.pathname.startsWith(SCOPE.pathname)
    && /^(apps\.json|apps\/[^/]+\.clan)$/.test(url.pathname.slice(SCOPE.pathname.length))
}

/** A navigation the shell answers: a route, not a file. */
function shellRoute(url) {
  if (url.origin !== SCOPE.origin || !url.pathname.startsWith(SCOPE.pathname)) return false
  const last = url.pathname.split('/').pop() ?? ''
  return !last.includes('.') || last === 'index.html'
}

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(SHELL).then(cache =>
      // `reload`: the HTTP cache may hold the previous build's copy of a file
      // whose name did not change (index.html, apps.json).
      cache.addAll(PRECACHE.map(p => new Request(new URL(p, SCOPE).href, { cache: 'reload' }))),
    ),
  )
})

self.addEventListener('activate', event => {
  event.waitUntil(
    (async () => {
      for (const key of await caches.keys()) {
        if (key.startsWith('napkin-shell-') && key !== SHELL) await caches.delete(key)
      }
      await self.clients.claim()
    })(),
  )
})

// The page decides when an update takes over, so an open document is never
// swapped out from under the person presenting it.
self.addEventListener('message', event => {
  if (event.data === 'napkin:skip-waiting') self.skipWaiting()
})

async function fromShell(request) {
  const cache = await caches.open(SHELL)
  return (await cache.match(request)) ?? fetch(request)
}

/** Network first, but not for long: a pitch room's Wi-Fi that is up and
 * passing nothing should fall back as quickly as no Wi-Fi at all. */
async function networkFirst(request) {
  const cache = await caches.open(SHELL)
  try {
    const response = await Promise.race([
      fetch(request),
      new Promise((_, reject) => setTimeout(() => reject(new Error('slow network')), 4000)),
    ])
    if (response.ok) await cache.put(request.url, response.clone())
    return response
  } catch (e) {
    const cached = await cache.match(request.url)
    if (cached) return cached
    throw e
  }
}

/** Typefaces: whatever we last saw, refreshed in the background. */
async function staleWhileRevalidate(request) {
  const cache = await caches.open(FONTS)
  const cached = await cache.match(request)
  const fresh = fetch(request)
    .then(r => {
      // The stylesheet is fetched no-cors, so its response is opaque; keep it anyway.
      if (r.ok || r.type === 'opaque') cache.put(request, r.clone())
      return r
    })
    .catch(() => cached)
  return cached ?? fresh
}

self.addEventListener('fetch', event => {
  const request = event.request
  if (request.method !== 'GET') return
  const url = new URL(request.url)

  if (serverOwned(url)) return

  if (url.hostname === 'fonts.googleapis.com' || url.hostname === 'fonts.gstatic.com') {
    event.respondWith(staleWhileRevalidate(request))
    return
  }
  if (url.origin !== SCOPE.origin) return

  if (request.mode === 'navigate' && shellRoute(url)) {
    event.respondWith(caches.open(SHELL).then(c => c.match(INDEX)).then(r => r ?? fetch(request)))
    return
  }
  if (published(url)) {
    event.respondWith(networkFirst(request))
    return
  }
  if (PRECACHE.includes(url.pathname.slice(SCOPE.pathname.length))) {
    event.respondWith(fromShell(request))
  }
})
