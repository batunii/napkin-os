# napkin-web

Napkin Studio OS served over HTTP. The same `napkin-host` the desktop shell
runs, behind two front doors.

```
browser ──► /api/t/{tenant}/…   cookie session, one workspace per tenant
            /api/t/{tenant}/events  ── SSE ──► the same HostEvents Tauri delivers
                     │
app frame ─► /s/{token}/…       the clan:// table verbatim, no cookie, CORS open
                     │
                     ▼
              napkin-host  ──►  TenantStore (opaque, tenant-scoped DocIds)
```

`/s/{token}/patch-data` **is** `clan://localhost/patch-data`: the path after the
token goes to `napkin_host::handle_async` unchanged. A route added for one shell
appears in the other.

## Running it

```bash
cd app && npm install && npm run build      # the shell
cargo run -p napkin-web                     # http://127.0.0.1:8080
```

| variable | default | |
|---|---|---|
| `NAPKIN_WEB_ADDR` | `127.0.0.1:8080` | listen address |
| `NAPKIN_WEB_DATA` | `./napkin-web-data` | one directory per tenant beneath it |
| `NAPKIN_WEB_STATIC` | found automatically | the built shell (`app/dist`) |
| `NAPKIN_CONFIG_DIR` | `<data>/config` | `workspace.yaml` + `secrets.yaml` |
| `NAPKIN_AGENT_URL` | `http://localhost:8787` | the briefing engine |
| `NAPKIN_AGENT_CAP` | `40` | agent calls per tenant, per process |
| `NAPKIN_SANDBOX_ORIGIN` | unset (same origin) | where app frames load from |
| `NAPKIN_WEB_SECURE_COOKIE` | unset | set behind TLS |

## The API

```
GET  /api/session                              who am I, sandbox origin, quota
GET  /api/t/{t}/apps                           installed templates
POST /api/t/{t}/apps/from/{doc}                install one already uploaded
GET  /api/t/{t}/recent                         recent documents
GET  /api/t/{t}/home                           the launcher, itself a CLAN app
POST /api/t/{t}/documents                      {app_id, title} → a new instance
POST /api/t/{t}/documents/upload               raw .clan bytes → opened
GET  /api/t/{t}/d/{doc}                        open it
GET  /api/t/{t}/d/{doc}/human-html             the rendered human view
GET  /api/t/{t}/d/{doc}/entry/{data|chain|state|context}
POST /api/t/{t}/d/{doc}/edit-mode              {active}
POST /api/t/{t}/d/{doc}/preview-html           the composed page for the frame
POST /api/t/{t}/d/{doc}/patch                  {id, content}
GET  /api/t/{t}/d/{doc}/download               the archive, byte for byte
POST /api/t/{t}/d/{doc}/export                 → {handle}, and an event
GET  /api/t/{t}/export/{handle}?kind=pdf|html  claim it (single use)
GET  /api/t/{t}/agent/endpoint
POST /api/t/{t}/agent/prompt                   {text} — metered
GET  /api/t/{t}/events                         SSE
ANY  /s/{token}/…                              the clan:// surface
```

## Three things that are different from the desktop, and why

**A document id is opaque.** `FsStore` makes a `DocId` a path, which is right on
a desktop where the user picks files. Here ids travel in URLs and arrive from
browsers, so `store::TenantStore` resolves `doc-…` / `app-…` / `home-…` and
refuses everything else. Nothing outside that module knows the layout.

**A tenant is a cookie, for now.** No accounts. But every route is
tenant-addressed and the `Tenant` extractor — not the path — decides identity,
checking that the two agree. Real auth replaces one function; no URL moves.

**The sandbox has a token, not a session.** App HTML is third-party code. It
sends no credentials and reaches exactly the one document its token names, which
is what lets it move to its own origin without anything else changing: set
`NAPKIN_SANDBOX_ORIGIN`.

## How an app in a .clan reaches the API

Apps compute their base once, at parse time:

```js
var clanScheme = navigator.userAgent.includes('Windows')
  ? 'http://clan.localhost' : 'clan://localhost';
```

Neither form exists in a browser. `app/src/host/http.ts` rewrites both to the
frame's own `/s/{token}` URL as the page is composed, and patches `fetch` for
anything built later. Unmodified `.clan` apps — including ones already shipped —
run here as they are.

## Demo

```bash
cd app && npm run build
NAPKIN_WEB_SEED=/path/to/templates cargo run -p napkin-web
```

`NAPKIN_WEB_SEED` installs every template in a directory into each new
workspace, so a first-time visitor lands on a launcher with something in it.
