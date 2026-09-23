// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// Left column: the twelve agents, research then brief. A row opens to show
// what the agent does and how it runs.

import { useState } from 'react'
import { AgentFigure } from '../AgentFigure'
import { AGENTS } from '../model'
import type { AgentKey, Stage } from '../model'
import type { AgentSummary } from './derive'
import { fanOutLabel } from './derive'
import { EXAMPLE_NOTE } from './example'

interface Props {
  stages: readonly { id: Stage; label: string; agents: readonly AgentKey[] }[]
  agents: Record<AgentKey, AgentSummary>
  example: boolean
}

const STAGE_NOTE: Record<Stage, string> = {
  research: 'on the campaign',
  brief: 'on the brief',
}

function statusLine(key: AgentKey, s: AgentSummary): string {
  if (s.state === 'needs-you') {
    const fan = s.running ? fanOutLabel(key, s.running) || 'working' : ''
    return [`Needs you · ${s.open} open`, fan].filter(Boolean).join(' · ')
  }
  if (s.state === 'working') {
    const fan = fanOutLabel(key, s.running)
    return ['Working', fan, s.docs > 1 ? `on ${s.docs} documents` : ''].filter(Boolean).join(' ')
  }
  return 'Idle'
}

export default function AgentsPanel({ stages, agents, example }: Props) {
  const [openKey, setOpenKey] = useState<AgentKey | null>(null)
  const total = stages.reduce((n, s) => n + s.agents.length, 0)
  const busy = Object.values(agents).filter(a => a.state !== 'idle').length

  return (
    <section className="fl-pan">
      <header className="fl-ph">
        <h2>Agents</h2>
        <span>{total} agents · {busy} busy</span>
      </header>
      {stages.map(stage => (
        <div key={stage.id} className="fl-group">
          <div className="fl-gl">{stage.label}<span>{STAGE_NOTE[stage.id]}</span></div>
          {stage.agents.map(k => {
            const a = AGENTS[k], s = agents[k], isOpen = openKey === k
            return (
              <button
                key={k}
                type="button"
                className="fl-ag"
                data-open={isOpen || undefined}
                aria-expanded={isOpen}
                onClick={() => setOpenKey(isOpen ? null : k)}
              >
                <AgentFigure agent={k} size={40} state={s.state} />
                <span className="fl-ag-text">
                  <span className="fl-ag-nm">{a.name}</span>
                  <span className="fl-ag-st" data-state={s.state}>{statusLine(k, s)}</span>
                  {isOpen && (
                    <span className="fl-ag-more">
                      {a.role}
                      <em>{a.run === 'parallel' ? `parallel, one per ${a.fanOut}` : 'serial'}{a.handler ? ` · ${a.handler}` : ''}</em>
                    </span>
                  )}
                </span>
              </button>
            )
          })}
        </div>
      ))}
      <p className="fl-leap">
        Agents propose; they never write a document. A person confirms, resolves, verifies, marks and locks.
      </p>
      {example && <p className="fl-note">{EXAMPLE_NOTE}</p>}
    </section>
  )
}
