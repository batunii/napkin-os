// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// Dev-only: every agent in every state, to eyeball figures side by side.
// Open the dev server with ?figures. Never shipped (see main.tsx).

import ThemeToggle from '../components/ThemeToggle'
import { AgentAvatar, AgentFigure } from './AgentFigure'
import { AGENTS, LOOK_OF, STAGES } from './model'

const COLUMNS = [
  { label: 'idle', state: 'idle', walking: false },
  { label: 'working', state: 'working', walking: false },
  { label: 'needs-you', state: 'needs-you', walking: false },
  { label: 'walking', state: 'idle', walking: true },
] as const

const cell: React.CSSProperties = { padding: '10px 18px', borderBottom: '1px solid var(--line-soft)', verticalAlign: 'middle' }

export default function FigureGallery() {
  return (
    <div style={{ flex: 1, overflow: 'auto', padding: 32, background: 'var(--paper)', color: 'var(--ink)' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 16, marginBottom: 24 }}>
        <div>
          <div className="eyebrow">Dev · ?figures</div>
          <h1 style={{ fontSize: 32, fontWeight: 800, letterSpacing: '-0.03em' }}>Agent figures</h1>
        </div>
        <div style={{ marginLeft: 'auto' }}><ThemeToggle /></div>
      </div>
      <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap', alignItems: 'flex-end', marginBottom: 28 }}>
        {STAGES.flatMap(s => s.agents).map(k => (
          <div key={k} style={{ textAlign: 'center', width: 64 }}>
            <AgentFigure agent={k} size={72} />
            <div style={{ fontFamily: 'var(--f-mono)', fontSize: 9.5, color: 'var(--ink3)', marginTop: 4 }}>{AGENTS[k].name}</div>
          </div>
        ))}
      </div>
      <table style={{ borderCollapse: 'collapse' }}>
        <thead>
          <tr>
            {['agent', 'avatar', ...COLUMNS.map(c => c.label), 'large'].map(h => (
              <th key={h} className="eyebrow" style={{ ...cell, textAlign: 'left', fontWeight: 400 }}>{h}</th>
            ))}
          </tr>
        </thead>
        {STAGES.map(s => (
          <tbody key={s.id}>
            <tr><td colSpan={7} className="eyebrow" style={{ ...cell, paddingTop: 22 }}>{s.label}</td></tr>
            {s.agents.map(k => {
              const look = LOOK_OF[k]
              return (
                <tr key={k}>
                  <td style={cell}>
                    <b>{AGENTS[k].name}</b>
                    <div style={{ fontFamily: 'var(--f-mono)', fontSize: 11, color: 'var(--ink3)' }}>
                      {look.tone} · {look.shape}{look.emblem !== 'none' ? ` · ${look.emblem}` : ''} · {AGENTS[k].run}
                    </div>
                  </td>
                  <td style={cell}><AgentAvatar agent={k} size={34} /></td>
                  {COLUMNS.map(c => (
                    <td key={c.label} style={cell}><AgentFigure agent={k} size={64} state={c.state} walking={c.walking} /></td>
                  ))}
                  <td style={cell}><AgentFigure agent={k} size={140} state="working" /></td>
                </tr>
              )
            })}
          </tbody>
        ))}
      </table>
    </div>
  )
}
