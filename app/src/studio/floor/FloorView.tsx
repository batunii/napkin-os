// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// The Floor: who is working, where each document is, and what waits on you.
//
//   left    Agents — the twelve, by stage, each with its current state
//   centre  In progress — each document's gates, agents, coverage, lease;
//           then every recent document (live, from the host)
//   right   Needs you — open items by document, and the activity feed
//
// Documents in the store are live. Per-document progress, open items and the
// feed are EXAMPLE (floor/example.ts) until the host's `/open` route (W2-O2).

import { useMemo, useState } from 'react'
import { useStudio } from '../nav'
import { EXAMPLE, STAGES } from '../model'
import { EXAMPLE_ACTIVITY, EXAMPLE_DOCS, EXAMPLE_OPEN } from './example'
import { summariseAgents } from './derive'
import AgentsPanel from './AgentsPanel'
import DocsPanel from './DocsPanel'
import NeedsPanel from './NeedsPanel'
import './Floor.css'

export default function FloorView() {
  const { recent } = useStudio()
  const docs = EXAMPLE_DOCS
  const open = EXAMPLE_OPEN
  const agents = useMemo(() => summariseAgents(docs, open), [docs, open])
  // "Not now" hides an item for this visit only; it records no decision.
  const [parked, setParked] = useState<ReadonlySet<string>>(new Set())
  const showing = open.filter(i => !parked.has(`${i.doc.id}/${i.id}`))

  return (
    <div className="floor">
      <div className="floor-grid">
        <aside className="floor-col-l" aria-label="Agents">
          <AgentsPanel stages={STAGES} agents={agents} example={EXAMPLE} />
        </aside>
        <div className="floor-col-c">
          <DocsPanel docs={docs} open={open} recent={recent} example={EXAMPLE} />
        </div>
        <aside className="floor-col-r" aria-label="Needs you">
          <NeedsPanel
            items={showing}
            docs={docs}
            activity={EXAMPLE_ACTIVITY}
            recent={recent}
            example={EXAMPLE}
            onPark={key => setParked(p => new Set(p).add(key))}
          />
        </aside>
      </div>
    </div>
  )
}
