# napkin-host

Everything Napkin Studio OS does with a `.clan` file that is not drawing pixels.

```
                 ┌──────────────────────┐
  Tauri commands │                      │  HostResponse (status, headers, body)
  + clan:// ────►│     napkin-host      │──► HostEvent (what the shell must do)
                 │                      │
                 └───────┬──────────────┘
                         │  DocStore (bytes)   HostConfig (endpoints, secrets)
                         ▼
                    FsStore / …
```

A handler here takes a `Session` and a `HostConfig`, reaches storage only
through a `DocStore`, and returns bytes plus the list of `HostEvent`s the shell
must act on. It never opens a file dialog, emits a Tauri event, or touches
`std::fs` — which is what lets the same routing table serve a custom URI scheme
on the desktop and plain HTTP on the web.

| module | what lives there |
|---|---|
| `routes` | the `clan://` API surface — exact-path dispatch to everything below |
| `session` | one open document: open, read, every attributed write, export composition |
| `library` | installed template apps, document instances, the home CLAN app |
| `store` | `DocStore` — where `.clan` bytes live (`FsStore` is the desktop's) |
| `config` | workspace config + host-side secrets; resolving a `request_kind` |
| `proxy` | the single outbound network route (`/api-proxy`) |
| `export` | temp HTML, headless-browser PDF |
| `html` | pure transforms of the human view (bindings, `data-adf-id`, patches) |
| `event` | `HostEvent` — the shell-side requests a handler can raise |

## Shape of a shell

```rust
let session = Session::new(Arc::new(FsStore::new(data_dir)));
let config  = FsConfig::new(config_dir);

let resp = if napkin_host::is_async(&path) {
    napkin_host::handle_async(&session, &config, req).await
} else {
    napkin_host::handle(&session, &config, req)
};
for e in &resp.events { shell.emit(e.name(), e.payload()); }
```

`/api-proxy` is the one route that must be awaited; the desktop shell answers
everything else inline so it never blocks the WebView loop.

## Adding a route

Add the arm in `routes::handle`, put the work on `Session` (or `library`), and
if the shell has to do something about it, add a `HostEvent` variant rather
than reaching for the shell. `tests/routes.rs` covers the table; `session`'s
own tests cover the operations.
