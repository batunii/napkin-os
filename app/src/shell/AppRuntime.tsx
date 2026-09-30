// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { host } from '../host'
import type { ManifestInfo } from '../host'
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

  // With no server to serve it, the frame's clan:// requests arrive here and
  // go to the device host — `/api-proxy` included, which answers that no
  // middleware is configured. Only requests from our own frame are answered.
  useEffect(() => {
    if (host.frameLoad !== 'srcdoc') return
    const onMessage = async (e: MessageEvent) => {
      const frame = iframeRef.current?.contentWindow
      if (!frame || e.source !== frame) return
      const msg = e.data as {
        type?: string; id?: number; op?: string; path?: string; query?: string
        body?: string | Uint8Array
      }
      if (msg?.type !== 'clan:rpc') return
      const path = msg.path ?? (msg.op === 'api-proxy' ? '/api-proxy' : '')

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

  // Who is signed in, for an app that asks ("clan:me?"): read-only, and only
  // for our own frame. The host records a person's writes under this actor
  // whatever the app says, so an app uses it to write the same `by` and to say
  // "You" — never to prove who someone is.
  useEffect(() => {
    const onMessage = (e: MessageEvent) => {
      const frame = iframeRef.current?.contentWindow
      if (!frame || e.source !== frame || (e.data as { type?: string })?.type !== 'clan:me?') return
      host.whoAmI().then(
        me => frame.postMessage({ type: 'clan:me', actor: me.actor, id: me.id, name: me.name }, '*'),
        () => frame.postMessage({ type: 'clan:me', actor: null, id: null, name: null }, '*'),
      )
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
  <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Geist:wght@400..800&display=swap">
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    html { scroll-behavior: smooth; }
    /* A bare fragment gets the studio's paper, ink and type (the values of
       the shell's tokens, which do not cross into the frame). */
    :root { color-scheme: light; --paper: #FFFFFF; --ink: #14161B; --line: #D6DAE2; }
    html[data-color-scheme="dark"] { color-scheme: dark; --paper: #0F1114; --ink: #F2F3F5; --line: #2A2F38; }
    body {
      background: var(--paper);
      color: var(--ink);
      font-family: 'Geist', 'Helvetica Neue', Arial, sans-serif;
      font-size: 15px;
      line-height: 1.65;
      -webkit-font-smoothing: antialiased;
    }
    ::-webkit-scrollbar { width: 6px; }
    ::-webkit-scrollbar-thumb { background: var(--line); border-radius: 3px; }
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
      <div style={{ padding: '72px 24px', color: 'var(--ink2)', maxWidth: 640, width: '100%', margin: '0 auto' }}>
        <div className="eyebrow">No view yet</div>
        <h2 style={{ color: 'var(--ink)', margin: '10px 0 12px', fontSize: 32, fontWeight: 800, letterSpacing: '-0.04em', lineHeight: 1.05 }}>
          {manifest.title}
        </h2>
        <p style={{ fontSize: 16, lineHeight: 1.55 }}>This .clan file has no view yet — awaiting first agent pass.</p>
      </div>
    )
  }

  return (
    <iframe
      ref={iframeRef}
      {...(host.frameLoad === 'srcdoc' ? { srcDoc: prepared } : { src: iframeSrc })}
      style={{ width: '100%', flex: 1, border: 'none', background: 'var(--paper)' }}
      sandbox="allow-scripts allow-popups"
      title={manifest.title}
      onLoad={() => {
        postScheme(getTheme())
        setExportFrame(iframeRef.current?.contentWindow ?? null)
      }}
    />
  )
}
