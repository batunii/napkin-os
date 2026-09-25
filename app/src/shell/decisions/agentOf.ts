// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// Which of the twelve agents made a decision, so its block can show the
// agent's figure. The host says who acted (a handler's name, or a person);
// the rule is the shared crew's (model.ts agentOfDecision), the same one the
// apps use.

import { agentOfDecision } from '../../studio/model'
import type { AgentKey } from '../../studio/model'
import type { DecisionBlock } from '../../host'

/** The agent behind a block, or null for a person or a process with no figure. */
export function agentOf(block: DecisionBlock): AgentKey | null {
  if (block.who.kind !== 'agent') return null
  const d = block.decision
  return agentOfDecision({
    agent: block.who.id, action: d.action, kind: d.kind, polarity: d.polarity,
    lens: d.lens == null ? undefined : String(d.lens), targets: d.targets,
  })
}
