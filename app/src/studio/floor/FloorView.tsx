// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// Placeholder. The Floor — the agents at work across the four departments —
// is built in this folder; the shell only guarantees it a flex-column panel
// under the top bar and the useStudio() context.

import { AgentFigure } from '../AgentFigure'
import { AGENT_KEYS } from '../model'

export default function FloorView() {
  return (
    <section className="studio-empty">
      <div className="eyebrow">Floor</div>
      <h1>One agency. Four departments. One loop.</h1>
      <p>The floor will show every agent at work, what each department is making, and what is waiting for you.</p>
      <div className="studio-empty-art">
        {AGENT_KEYS.map(k => <AgentFigure key={k} agent={k} size={52} />)}
      </div>
    </section>
  )
}
