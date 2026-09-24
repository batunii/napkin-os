// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// Which of the twelve agents made a decision, so its block can show the
// agent's figure. The host says who acted (a handler's name, or a person);
// this only picks the figure.

import { AGENT_OF_LENS, LENS_IDS } from '../../studio/model'
import type { AgentKey, LensId } from '../../studio/model'
import type { DecisionBlock } from '../../host'

/** Handlers that are one agent, whatever stage they are in. */
const BY_HANDLER: Readonly<Record<string, AgentKey>> = {
  extract_ask: 'extract',
  synthesise_findings: 'synthesis',
}

/** `start_campaign` runs several agents in turn; its action says which one decided. */
const BY_ACTION: Readonly<Record<string, AgentKey>> = {
  extract: 'extract',
  extract_ask: 'extract',
  identify: 'extract',
  synthesise: 'synthesis',
  synthesise_findings: 'synthesis',
}

function lensIn(texts: string[]): LensId | null {
  for (const t of texts) {
    const hit = LENS_IDS.find(l => t.includes(l))
    if (hit) return hit
  }
  return null
}

/** The agent behind a block, or null for a person or a process with no figure. */
export function agentOf(block: DecisionBlock): AgentKey | null {
  if (block.who.kind !== 'agent') return null
  const d = block.decision
  const handler = block.who.id
  if (BY_HANDLER[handler]) return BY_HANDLER[handler]
  if (handler === 'research_lens') {
    const lens = lensIn([String(d.lens ?? ''), d.action, ...(d.targets ?? [])])
    return lens ? AGENT_OF_LENS[lens] : null
  }
  if (BY_ACTION[d.action]) return BY_ACTION[d.action]
  if (d.kind === 'finding') return 'synthesis'
  if (handler.includes('draft')) return 'drafter'
  if (handler.includes('judge')) return 'judge'
  return null
}
