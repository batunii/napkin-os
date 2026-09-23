// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// Pure reads over the Floor's data. Whatever feeds the Floor, example or
// live, goes through these, so swapping the source changes nothing here.

import { AGENTS, AGENT_KEYS, DOC_STATES } from '../model'
import type { AgentKey, AgentState, DocProgress, DocState, OpenItem } from '../model'
import type { RecentDoc } from '../../host'

export interface AgentSummary {
  state: AgentState
  /** Instances running, across documents. */
  running: number
  /** Documents it is working on. */
  docs: number
  /** Open items that came from its work. */
  open: number
}

/**
 * One line per agent across every document: needs-you when an open item came
 * from its work (a person has to settle something it proposed), working when
 * any instance is running, else idle.
 */
export function summariseAgents(docs: readonly DocProgress[], open: readonly OpenItem[]): Record<AgentKey, AgentSummary> {
  const out = Object.fromEntries(
    AGENT_KEYS.map(k => [k, { state: 'idle', running: 0, docs: 0, open: 0 } as AgentSummary]),
  ) as Record<AgentKey, AgentSummary>
  for (const d of docs) {
    for (const a of d.agents) {
      if (a.state !== 'working') continue
      out[a.agent].running += a.count ?? 1
      out[a.agent].docs += 1
    }
  }
  for (const i of open) out[i.from].open += 1
  for (const k of AGENT_KEYS) {
    const s = out[k]
    s.state = s.open ? 'needs-you' : s.running ? 'working' : 'idle'
  }
  return out
}

/** "× 2 markets", "× 5 fields", or nothing for a serial agent or a single run. */
export function fanOutLabel(agent: AgentKey, n: number): string {
  const f = AGENTS[agent].fanOut
  if (!f || n < 1) return ''
  const unit = f === 'market' ? 'market' : 'field'
  return `× ${n} ${unit}${n === 1 ? '' : 's'}`
}

/** Open items grouped by document, in the order documents first appear. */
export function groupByDoc(items: readonly OpenItem[]): { docId: string; title: string; items: OpenItem[] }[] {
  const groups = new Map<string, { docId: string; title: string; items: OpenItem[] }>()
  for (const i of items) {
    let g = groups.get(i.doc.id)
    if (!g) groups.set(i.doc.id, (g = { docId: i.doc.id, title: i.doc.title, items: [] }))
    g.items.push(i)
  }
  return [...groups.values()]
}

/** How far along the gate bar a state is: 0 created … 3 locked. */
export function stateIndex(state: DocState): number {
  return DOC_STATES.indexOf(state)
}

/**
 * The store path for a document, found among the host's recent documents by
 * title. Example documents are usually not in the store, so this is often
 * undefined; with live open items the host will hand the path over itself.
 */
export function pathFor(title: string, recent: readonly RecentDoc[], known?: string): string | undefined {
  if (known) return known
  return recent.find(r => r.title === title || r.title.startsWith(`${title} [`))?.path
}

const DATE = new Intl.DateTimeFormat('en-IE', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })

/** "23 Sept, 09:40", or the raw string if it does not parse. */
export function shortTime(iso: string): string {
  const t = Date.parse(iso)
  return Number.isNaN(t) ? iso : DATE.format(t)
}
