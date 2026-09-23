// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

import { useEffect, useState } from 'react'
import { host } from '../host'
import './chrome.css'

interface Props { onClose: () => void }

type Tab = 'chain' | 'state' | 'context'

export default function AgentPanel({ onClose }: Props) {
  const [tab, setTab] = useState<Tab>('chain')
  const [content, setContent] = useState('')
  const [loading, setLoading] = useState(false)

  async function load(t: Tab) {
    setLoading(true)
    try {
      setContent(
        await (t === 'chain' ? host.getChain() : t === 'state' ? host.getAgentState() : host.getContext()),
      )
    } catch (e) {
      setContent(`Error: ${e}`)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    load(tab)
  }, [tab])

  const tabs: { id: Tab; label: string }[] = [
    { id: 'chain', label: 'Decisions' },
    { id: 'state', label: 'State' },
    { id: 'context', label: 'Context' },
  ]

  return (
    <div className="ch-side ch-side-right">
      <div className="ch-side-head">
        <span className="eyebrow">Agent</span>
        <button className="ch-btn ch-btn-icon ch-btn-quiet" onClick={onClose} aria-label="Close the agent panel">✕</button>
      </div>
      <div className="ch-seg" role="tablist" aria-label="Agent panel">
        {tabs.map(t => (
          <button
            key={t.id}
            role="tab"
            aria-selected={tab === t.id}
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div style={{ flex: 1, overflow: 'auto', padding: '4px 16px 16px' }}>
        {loading ? (
          <span className="ch-key">Loading…</span>
        ) : (
          <pre className="ch-pre">{content || '(empty)'}</pre>
        )}
      </div>
    </div>
  )
}
