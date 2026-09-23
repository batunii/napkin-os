// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// Placeholder until the Floor is built on the model: the twelve agents.

import { AgentFigure } from '../AgentFigure'
import { AGENT_KEYS } from '../model'

export default function FloorView() {
  return (
    <section className="studio-empty">
      <div className="eyebrow">Floor</div>
      <h1>Research, then the brief.</h1>
      <p>The floor will show every agent at work, each document’s gates, and what is waiting for you.</p>
      <div className="studio-empty-art">
        {AGENT_KEYS.map(k => <AgentFigure key={k} agent={k} size={52} />)}
      </div>
    </section>
  )
}
