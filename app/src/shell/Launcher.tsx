// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

import { useEffect, useRef, useState } from 'react'
import { host } from '../host'
import { PoweredByClan } from '../brand/PoweredByClan'
import { LogoSpinner } from '../brand/LogoSpinner'
import type { InstalledApp } from './types'
import '../components/chrome.css'
import './Launcher.css'

interface Props {
  installed: InstalledApp[]
  loading: boolean
  onLaunchApp: (appId: string) => void
  onOpenFile: () => void
}

export default function Launcher({ installed, loading, onLaunchApp, onOpenFile }: Props) {
  const [prompt, setPrompt] = useState('')
  const [sending, setSending] = useState(false)
  const [result, setResult] = useState<{ text: string; error: boolean } | null>(null)
  const [endpoint, setEndpoint] = useState('')
  const taRef = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    host.agentEndpoint().then(setEndpoint).catch(() => {})
  }, [])

  function autoGrow() {
    const ta = taRef.current
    if (!ta) return
    ta.style.height = 'auto'
    ta.style.height = Math.min(ta.scrollHeight, 220) + 'px'
  }

  async function send() {
    const text = prompt.trim()
    if (!text || sending) return
    setSending(true)
    setResult(null)
    try {
      const res = (await host.agentPrompt(text)) as { ok: boolean; status: number; endpoint: string; data: unknown }
      const body = typeof res.data === 'string' ? res.data : JSON.stringify(res.data, null, 2)
      setResult({ text: res.ok ? body : `agent returned ${res.status}\n${body}`, error: !res.ok })
    } catch (e) {
      setResult({ text: String(e), error: true })
    } finally {
      setSending(false)
    }
  }

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      send()
    }
  }

  const canSend = !!prompt.trim() && !sending

  return (
    <div className="ln">
      {/* The bar above carries the product name; the page does not repeat it. */}
      <div className="ln-hero">
        <h1 className="ln-banner">Let&rsquo;s start here.</h1>
      </div>

      <div className="ln-composer">
        <textarea
          ref={taRef}
          placeholder="Describe what you want to make…"
          value={prompt}
          onChange={e => { setPrompt(e.target.value); autoGrow() }}
          onKeyDown={onKeyDown}
          rows={2}
        />
        <div className="ln-row">
          <span className="ln-endpoint">{endpoint ? `→ ${endpoint}` : ''}</span>
          <button
            className="ln-send"
            onClick={send}
            disabled={!canSend}
            title="Send to agent (Enter)"
            aria-label="Send to agent"
          >
            ↑
          </button>
        </div>
      </div>

      {sending && <LogoSpinner size="sm" label="Asking the agent…" style={{ alignSelf: 'center', width: 'auto', marginTop: 6 }} />}

      {result && (
        <div className="ln-result" data-error={result.error || undefined}>{result.text}</div>
      )}

      <div className="ln-divider eyebrow">
        <i /> or start from <i />
      </div>

      <div className="ln-grid">
        {installed.map(app => (
          <button key={app.app_id} className="ln-card" onClick={() => onLaunchApp(app.app_id)}>
            <div className="ch-monogram">{app.name.slice(0, 1).toUpperCase()}</div>
            <b>{app.name}</b>
            <small>v{app.version} · new document</small>
          </button>
        ))}
        <button className="ln-card ln-card-open" onClick={onOpenFile}>
          <span aria-hidden>+</span>
          Open a .clan file
        </button>
      </div>

      {installed.length === 0 && !loading && (
        <div className="ln-note">
          No template apps installed yet — open a <code>.clan</code> template or run <code>clan app init</code>.
        </div>
      )}

      <div className="ln-footer"><PoweredByClan /></div>
    </div>
  )
}
