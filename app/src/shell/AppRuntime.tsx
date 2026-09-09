// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { host } from '../host'
import type { ManifestInfo } from '../host'
import { runInference } from '../agent/inference'
import { getTheme, onThemeChange } from '../theme'
import { setExportFrame } from './appExport'
import { LEGACY_EDIT_BRIDGE } from '../bridge/legacyEditBridge'
import { STRUCTURED_EDIT_BRIDGE } from '../bridge/structuredEditBridge'

interface Props {
  htmlContent: string
  hasHumanView: boolean
  manifest: ManifestInfo
  /** "authored" → structured (data-layer) bridge; "legacy" → contenteditable. */
  renderModel: 'authored' | 'legacy'
  editMode: boolean
}

/**
 * The render surface for one running app. Renders the human view inside a
 * sandboxed iframe served from the backend's clan://document slot, and injects
 * the appropriate edit bridge: the structured (data-layer) bridge for authored
 * Napkin apps, or the legacy contenteditable bridge for AI-generated HTML.
 */
export default function AppRuntime({ htmlContent, hasHumanView, manifest, renderModel, editMode }: Props) {
  const iframeRef = useRef<HTMLIFrameElement>(null)
  const editModeRef = useRef(editMode)

  useEffect(() => { editModeRef.current = editMode }, [editMode])

  const sendEditMode = useCallback((active: boolean) => {
    host.setEditMode(active).catch(console.error)
  }, [])

  useEffect(() => { sendEditMode(editMode) }, [editMode, sendEditMode])

  // Legacy views save HTML-fragment patches; the backend echoes a
  // clan-patch-saved event. The edit is already in the DOM — don't reload.
  useEffect(() => {
    const unlisten = host.on('clan-patch-saved', () => {})
    return () => { unlisten.then(f => f()) }
  }, [])

  // The app runs in its own document, so the colour scheme has to be sent to
  // it. Apps opt in by styling `html[data-color-scheme="light"]`, or by
  // listening for the `clan:colorscheme` event the bridge dispatches; one that
  // does neither simply keeps its own palette.
  const postScheme = useCallback((scheme: string) => {
    iframeRef.current?.contentWindow?.postMessage(
      { type: 'clan:colorscheme', scheme }, '*',
    )
  }, [])

  useEffect(() => onThemeChange(postScheme), [postScheme])

  // The app asks for inference; the shell performs it. Only this side ever
  // touches the key, and only requests from our own frame are answered.
  useEffect(() => {
    if (host.inference !== 'page' && host.frameLoad !== 'srcdoc') return
    const onMessage = async (e: MessageEvent) => {
      const frame = iframeRef.current?.contentWindow
      if (!frame || e.source !== frame) return
      const msg = e.data as {
        type?: string; id?: number; op?: string; path?: string; query?: string
        body?: string | Uint8Array
      }
      if (msg?.type !== 'clan:rpc') return

      // Inference is the shell's to perform — it holds the key, the app must
      // not. Everything else is a host call, which only the serverless build
      // routes through here.
      const path = msg.path ?? (msg.op === 'api-proxy' ? '/api-proxy' : '')
      if (path === '/api-proxy') {
        let body: string
        try {
          const raw = typeof msg.body === 'string' ? msg.body : '{}'
          const request = JSON.parse(raw || '{}') as { payload?: unknown }
          body = JSON.stringify(await runInference(request.payload ?? request))
        } catch (err) {
          body = JSON.stringify({ ok: false, status: 500, data: null, error: String(err) })
        }
        // The two shims differ: one wants a plain JSON string back, the other
        // a full response. Sending both fields satisfies each.
        frame.postMessage(
          {
            type: 'clan:rpc-reply', id: msg.id, body,
            status: 200, headers: { 'content-type': 'application/json' },
          },
          '*',
        )
        return
      }

      try {
        const raw = typeof msg.body === 'string' ? new TextEncoder().encode(msg.body)
                                                 : (msg.body ?? new Uint8Array())
        const r = await host.handleFromFrame(path, msg.query ?? '', raw)
        frame.postMessage(
          { type: 'clan:rpc-reply', id: msg.id, status: r.status,
            headers: Object.fromEntries(r.headers), body: r.body },
          '*',
        )
      } catch (err) {
        frame.postMessage(
          { type: 'clan:rpc-reply', id: msg.id, status: 500, headers: {},
            body: new TextEncoder().encode(String(err)) },
          '*',
        )
      }
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [])

  const [iframeSrc, setIframeSrc] = useState<string>('')

  // Composing the page is a pure derivation of the view, the render model and
  // the theme — not an effect. The effect below only has to publish it.
  const prepared = useMemo(() => {
    if (!hasHumanView) return ''

    const isFullDoc = /^\s*<!doctype\s+html/i.test(htmlContent) || /^\s*<html/i.test(htmlContent)
    const bridgeScript = renderModel === 'authored' ? STRUCTURED_EDIT_BRIDGE : LEGACY_EDIT_BRIDGE
    const bridge = `<script>${bridgeScript}</script>`
    let fullHtml: string

    if (isFullDoc) {
      // Inject at the LAST </body>: an app's inline script may legitimately
      // contain '</body>' inside a string literal (e.g. an HTML-export
      // builder), and splicing the bridge there is a syntax error that kills
      // the app's whole script.
      const closeIdx = htmlContent.toLowerCase().lastIndexOf('</body>')
      fullHtml = closeIdx >= 0
        ? htmlContent.slice(0, closeIdx) + bridge + htmlContent.slice(closeIdx)
        : htmlContent + bridge
    } else {
      fullHtml = `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    html { scroll-behavior: smooth; }
    :root { color-scheme: dark; }
    html[data-color-scheme="light"] { color-scheme: light; }
    body {
      background: #0f1117;
      color: #e2e8f0;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
      font-size: 15px;
      line-height: 1.65;
      -webkit-font-smoothing: antialiased;
    }
    html[data-color-scheme="light"] body { background: #f7f8fb; color: #171c2b; }
    ::-webkit-scrollbar { width: 6px; }
    ::-webkit-scrollbar-thumb { background: #1e2d45; border-radius: 3px; }
  </style>
</head>
<body>
  ${htmlContent}
  ${bridge}
</body>
</html>`
    }

    // Stamp the scheme into the markup as well as posting it: the message
    // arrives after load, and a light user should not see a dark flash first.
    const themed = fullHtml.replace(
      /<html\b([^>]*)>/i,
      (m, attrs: string) =>
        /data-color-scheme=/i.test(attrs) ? m : `<html${attrs} data-color-scheme="${getTheme()}">`,
    )

    return host.prepareAppHtml(themed)
  }, [htmlContent, hasHumanView, renderModel])

  // With a host behind a URL, hand it the page and point the frame at it.
  // With no server there is nothing to hand it to: the page is inlined below.
  useEffect(() => {
    if (!prepared || host.frameLoad === 'srcdoc') return
    host.updatePreviewHtml(prepared).then(() => {
      setIframeSrc(host.clanOrigin() + '/document?t=' + Date.now())
    }).catch(console.error)
  }, [prepared])

  if (!hasHumanView) {
    return (
      <div style={{ padding: 40, color: 'var(--muted)', maxWidth: 600, margin: '0 auto' }}>
        <h2 style={{ color: 'var(--text)', marginBottom: 12 }}>{manifest.title}</h2>
        <p>This .clan file has no view yet — awaiting first agent pass.</p>
      </div>
    )
  }

  return (
    <iframe
      ref={iframeRef}
      {...(host.frameLoad === 'srcdoc' ? { srcDoc: prepared } : { src: iframeSrc })}
      style={{ width: '100%', flex: 1, border: 'none', background: 'var(--bg)' }}
      sandbox="allow-scripts allow-popups"
      title={manifest.title}
      onLoad={() => {
        postScheme(getTheme())
        setExportFrame(iframeRef.current?.contentWindow ?? null)
      }}
    />
  )
}
