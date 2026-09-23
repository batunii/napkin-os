// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

import { useState } from 'react'

import { clearKey, getKey, hasKey, looksLikeKey, setKey } from '../agent/key'
import './chrome.css'

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
    <div className="ch-scrim" style={{ zIndex: 300 }} onClick={onClose}>
      <div
        className="ch-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="api-key-title"
        onClick={e => e.stopPropagation()}
      >
        <div>
          <div className="eyebrow">Your key</div>
          <h2 id="api-key-title" style={{ marginTop: 6 }}>Anthropic API key</h2>
        </div>
        <p>
          Generating a brief calls Claude with <strong>your</strong> key. It is stored in this
          browser only — it never reaches a server of ours, because there isn&rsquo;t one.
          You are billed by Anthropic for what you generate.
        </p>
        <input
          className="ch-input"
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
        {suspect && <div className="ch-warn">That doesn&rsquo;t look like an Anthropic key — they start <code>sk-ant-</code>.</div>}
        <div className="ch-actions" style={{ marginTop: 4 }}>
          {existing && (
            <button className="ch-btn ch-btn-danger" style={{ marginRight: 'auto' }} onClick={() => { clearKey(); onClose() }}>
              Remove key
            </button>
          )}
          <button className="ch-btn" onClick={onClose}>Cancel</button>
          <button className="ch-btn ch-btn-primary" onClick={save} disabled={!value.trim()}>
            {existing ? 'Update' : 'Save key'}
          </button>
        </div>
      </div>
    </div>
  )
}
