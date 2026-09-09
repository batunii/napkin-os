// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

import { useState } from 'react'

import { clearKey, getKey, hasKey, looksLikeKey, setKey } from '../agent/key'

const s: Record<string, React.CSSProperties> = {
  scrim: {
    position: 'fixed', inset: 0, zIndex: 300, background: 'rgba(0,0,0,0.55)',
    display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 20,
  },
  panel: {
    width: '100%', maxWidth: 460, background: 'var(--surface)',
    border: '1px solid var(--border)', borderRadius: 14, padding: 22,
    boxShadow: '0 20px 60px rgba(0,0,0,0.45)', color: 'var(--text)',
  },
  h: { fontSize: 15, fontWeight: 700, marginBottom: 6 },
  p: { fontSize: 12.5, color: 'var(--muted)', lineHeight: 1.6, marginBottom: 14 },
  input: {
    width: '100%', height: 36, padding: '0 10px', borderRadius: 8, fontSize: 13,
    border: '1px solid var(--border)', background: 'var(--bg)', color: 'var(--text)',
    fontFamily: 'ui-monospace, monospace',
  },
  row: { display: 'flex', gap: 8, marginTop: 16, justifyContent: 'flex-end' },
  btn: {
    height: 32, padding: '0 14px', borderRadius: 7, fontSize: 12.5, cursor: 'pointer',
    border: '1px solid var(--border)', background: 'var(--surface)', color: 'var(--text)',
  },
  primary: { background: 'var(--accent)', borderColor: 'var(--accent)', color: '#fff', fontWeight: 600 },
  danger: { color: 'var(--danger)', borderColor: 'var(--border)' },
  warn: { fontSize: 11.5, color: 'var(--warn)', marginTop: 10, lineHeight: 1.5 },
}

export default function ApiKeyDialog({ onClose }: { onClose: () => void }) {
  const existing = hasKey()
  const [value, setValue] = useState(getKey() ?? '')
  const [touched, setTouched] = useState(false)
  const suspect = touched && value.trim().length > 0 && !looksLikeKey(value)

  function save() {
    setKey(value)
    onClose()
  }

  return (
    <div style={s.scrim} onClick={onClose}>
      <div style={s.panel} onClick={e => e.stopPropagation()}>
        <div style={s.h}>Anthropic API key</div>
        <p style={s.p}>
          Generating a brief calls Claude with <strong>your</strong> key. It is stored in this
          browser only — it never reaches a server of ours, because there isn&rsquo;t one.
          You are billed by Anthropic for what you generate.
        </p>
        <input
          style={s.input}
          type="password"
          value={value}
          spellCheck={false}
          autoComplete="off"
          placeholder="sk-ant-..."
          onChange={e => { setValue(e.target.value); setTouched(true) }}
          onKeyDown={e => { if (e.key === 'Enter' && value.trim()) save() }}
          aria-label="Anthropic API key"
          autoFocus
        />
        {suspect && <div style={s.warn}>That doesn&rsquo;t look like an Anthropic key — they start <code>sk-ant-</code>.</div>}
        <div style={s.row}>
          {existing && (
            <button style={{ ...s.btn, ...s.danger }} onClick={() => { clearKey(); onClose() }}>
              Remove key
            </button>
          )}
          <button style={s.btn} onClick={onClose}>Cancel</button>
          <button style={{ ...s.btn, ...s.primary }} onClick={save} disabled={!value.trim()}>
            {existing ? 'Update' : 'Save key'}
          </button>
        </div>
      </div>
    </div>
  )
}
