// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// Placeholder. Decisions — the calls the agents hand to a person, gate by
// gate — is built in this folder; the shell only guarantees it a flex-column
// panel under the top bar and the useStudio() context.

import { GATES } from '../model'

export default function DecisionsView() {
  return (
    <section className="studio-empty">
      <div className="eyebrow">Decisions</div>
      <h1>Nothing is waiting on you.</h1>
      <p>
        When an agent needs a person — at any of the {GATES.length} gates, or anywhere else — the call lands here,
        with its reasons.
      </p>
    </section>
  )
}
