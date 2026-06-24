// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

import { useEffect, useRef, useCallback, useState } from 'react'
import { invoke } from '@tauri-apps/api/core'
import { listen } from '@tauri-apps/api/event'
import type { ManifestInfo } from '../App'
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
    invoke('set_edit_mode', { active }).catch(console.error)
  }, [])

  useEffect(() => { sendEditMode(editMode) }, [editMode, sendEditMode])

  // Legacy views save HTML-fragment patches; the backend echoes a
  // clan-patch-saved event. The edit is already in the DOM — don't reload.
  useEffect(() => {
    const unlisten = listen('clan-patch-saved', () => {})
    return () => { unlisten.then(f => f()) }
  }, [])

  const [iframeSrc, setIframeSrc] = useState<string>('')

  useEffect(() => {
    if (!hasHumanView) return

    const isFullDoc = /^\s*<!doctype\s+html/i.test(htmlContent) || /^\s*<html/i.test(htmlContent)
    const bridgeScript = renderModel === 'authored' ? STRUCTURED_EDIT_BRIDGE : LEGACY_EDIT_BRIDGE
    const bridge = `<script>${bridgeScript}</script>`
    let fullHtml: string

    if (isFullDoc) {
      fullHtml = /<\/body>/i.test(htmlContent)
        ? htmlContent.replace(/<\/body>/i, `${bridge}</body>`)
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
    body {
      background: #0f1117;
      color: #e2e8f0;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
      font-size: 15px;
      line-height: 1.65;
      -webkit-font-smoothing: antialiased;
    }
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

    const clanScheme = window.navigator.userAgent.includes('Windows') ? 'http://clan.localhost' : 'clan://localhost'
    invoke('update_preview_html', { html: fullHtml }).then(() => {
      setIframeSrc(clanScheme + '/document?t=' + Date.now())
    }).catch(console.error)
  }, [htmlContent, hasHumanView, renderModel])

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
      src={iframeSrc}
      style={{ width: '100%', flex: 1, border: 'none', background: '#0f1117' }}
      sandbox="allow-scripts allow-popups"
      title={manifest.title}
    />
  )
}
