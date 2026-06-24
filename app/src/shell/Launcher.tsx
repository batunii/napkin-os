// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

import { useEffect, useRef, useState } from 'react'
import { invoke } from '@tauri-apps/api/core'
import { NapkinMark } from '../brand/NapkinMark'
import { PoweredByClan } from '../brand/PoweredByClan'
import type { InstalledApp } from './types'

interface Props {
  installed: InstalledApp[]
  loading: boolean
  onLaunchApp: (appId: string) => void
  onOpenFile: () => void
}

const s: Record<string, React.CSSProperties> = {
  root: {
    flex: 1, overflowY: 'auto', display: 'flex', flexDirection: 'column',
    alignItems: 'center', padding: '0 32px', gap: 8,
  },
  hero: { display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 18, marginTop: '12vh', marginBottom: 26 },
  banner: {
    fontFamily: '"Space Grotesk", system-ui, sans-serif', fontWeight: 600,
    fontSize: 44, letterSpacing: '-0.01em', color: '#eceefb', display: 'flex', alignItems: 'center', gap: 14,
  },
  bannerOs: { color: '#2dd4cf', fontWeight: 500 },
  sub: { color: 'var(--muted)', fontSize: 14 },

  composer: {
    width: '100%', maxWidth: 720, background: 'var(--surface)', border: '1px solid var(--border)',
    borderRadius: 16, padding: 14, display: 'flex', flexDirection: 'column', gap: 10,
    boxShadow: '0 8px 30px rgba(0,0,0,0.25)',
  },
  textarea: {
    width: '100%', minHeight: 56, maxHeight: 220, resize: 'none', border: 'none', outline: 'none',
    background: 'transparent', color: 'var(--text)', fontSize: 16, lineHeight: 1.5,
    fontFamily: 'inherit',
  },
  composerRow: { display: 'flex', alignItems: 'center', justifyContent: 'space-between' },
  endpoint: { fontSize: 11, color: 'var(--muted)', fontFamily: 'monospace' },
  send: {
    width: 36, height: 36, borderRadius: 10, border: 'none', cursor: 'pointer',
    background: 'linear-gradient(135deg, #6366f1, #2dd4cf)', color: '#fff', fontSize: 16,
    display: 'flex', alignItems: 'center', justifyContent: 'center',
  },
  sendDisabled: { opacity: 0.4, cursor: 'default' },
  result: {
    width: '100%', maxWidth: 720, marginTop: 4, padding: '12px 14px', borderRadius: 12,
    background: 'var(--surface)', border: '1px solid var(--border)', fontSize: 13,
    color: 'var(--text)', whiteSpace: 'pre-wrap', wordBreak: 'break-word', maxHeight: 200, overflowY: 'auto',
  },
  resultErr: { borderColor: '#92400e', color: 'var(--warn)' },

  divider: {
    width: '100%', maxWidth: 720, display: 'flex', alignItems: 'center', gap: 12,
    color: 'var(--muted)', fontSize: 11, letterSpacing: '0.12em', textTransform: 'uppercase', margin: '30px 0 4px',
  },
  line: { flex: 1, height: 1, background: 'var(--border)' },

  grid: {
    width: '100%', maxWidth: 720,
    display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(150px, 1fr))', gap: 14,
  },
  card: {
    background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 12,
    padding: 16, display: 'flex', flexDirection: 'column', gap: 8, cursor: 'pointer',
  },
  openCard: {
    background: 'transparent', border: '1px dashed var(--border)', borderRadius: 12,
    padding: 16, display: 'flex', flexDirection: 'column', gap: 8, cursor: 'pointer',
    justifyContent: 'center', alignItems: 'center', color: 'var(--muted)', minHeight: 96,
  },
  iconWrap: {
    width: 40, height: 40, borderRadius: 10,
    background: 'linear-gradient(135deg, #6366f1, #2dd4cf)',
    display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 20, color: '#fff', fontWeight: 700,
  },
  cardName: { fontSize: 13, fontWeight: 600, color: 'var(--text)' },
  cardVer: { fontSize: 11, color: 'var(--muted)' },
  footer: { marginTop: 'auto', padding: '32px 0 24px' },
}

export default function Launcher({ installed, loading, onLaunchApp, onOpenFile }: Props) {
  const [prompt, setPrompt] = useState('')
  const [sending, setSending] = useState(false)
  const [result, setResult] = useState<{ text: string; error: boolean } | null>(null)
  const [endpoint, setEndpoint] = useState('')
  const taRef = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    invoke<string>('agent_endpoint').then(setEndpoint).catch(() => {})
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
      const res = await invoke<{ ok: boolean; status: number; endpoint: string; data: unknown }>('agent_prompt', { text })
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
    <div style={s.root}>
      <div style={s.hero}>
        <div style={s.banner}>
          <NapkinMark size={40} />
          Napkin <span style={s.bannerOs}>Studio</span>
        </div>
        <div style={s.sub}>Let's start here.</div>
      </div>

      <div style={s.composer}>
        <textarea
          ref={taRef}
          style={s.textarea}
          placeholder="Describe what you want to make…"
          value={prompt}
          onChange={e => { setPrompt(e.target.value); autoGrow() }}
          onKeyDown={onKeyDown}
          rows={2}
        />
        <div style={s.composerRow}>
          <span style={s.endpoint}>{endpoint ? `→ ${endpoint}` : ''}</span>
          <button
            style={{ ...s.send, ...(canSend ? {} : s.sendDisabled) }}
            onClick={send}
            disabled={!canSend}
            title="Send to agent (Enter)"
          >
            {sending ? '…' : '↑'}
          </button>
        </div>
      </div>

      {result && (
        <div style={{ ...s.result, ...(result.error ? s.resultErr : {}) }}>{result.text}</div>
      )}

      <div style={s.divider}>
        <div style={s.line} /> or start from <div style={s.line} />
      </div>

      <div style={s.grid}>
        {installed.map(app => (
          <div
            key={app.app_id}
            style={s.card}
            onClick={() => onLaunchApp(app.app_id)}
            onMouseEnter={e => { (e.currentTarget as HTMLDivElement).style.borderColor = 'var(--accent)' }}
            onMouseLeave={e => { (e.currentTarget as HTMLDivElement).style.borderColor = 'var(--border)' }}
          >
            <div style={s.iconWrap}>{app.name.slice(0, 1).toUpperCase()}</div>
            <div style={s.cardName}>{app.name}</div>
            <div style={s.cardVer}>v{app.version} · new document</div>
          </div>
        ))}
        <div style={s.openCard} onClick={onOpenFile}>
          <div style={{ fontSize: 22 }}>📂</div>
          <div style={{ fontSize: 12 }}>Open a .clan file</div>
        </div>
      </div>

      {installed.length === 0 && !loading && (
        <div style={{ fontSize: 12, color: 'var(--muted)', marginTop: 12 }}>
          No template apps installed yet — open a <code>.clan</code> template or run <code>clan app init</code>.
        </div>
      )}

      <div style={s.footer}><PoweredByClan /></div>
    </div>
  )
}
