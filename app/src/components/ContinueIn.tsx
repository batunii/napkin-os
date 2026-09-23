// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

import { useEffect, useState } from 'react'

import { host } from '../host'
import type { SpinoffTarget } from '../host'
import './chrome.css'

interface Props {
  /**
   * The open document. Targets depend on its app, so the toolbar mounts this
   * keyed by it — a different document gets a fresh fetch and a closed menu
   * without any of that having to be synchronised by hand.
   */
  docPath: string
  onSpinoff: (appId: string) => void
}

/**
 * "Continue in…" — the apps that have declared they will take this document as
 * a spin-off source, carrying its data and its decisions across.
 *
 * Renders nothing when no installed app accepts it. An empty list is a normal
 * state (nothing downstream is installed), not a failure worth a disabled
 * button in the chrome.
 */
export default function ContinueIn({ docPath, onSpinoff }: Props) {
  const [targets, setTargets] = useState<SpinoffTarget[]>([])
  const [open, setOpen] = useState(false)

  useEffect(() => {
    let live = true
    host.spinoffTargets()
      .then(t => { if (live) setTargets(t) })
      // A host that cannot answer simply offers nothing; this is chrome, not
      // the document, and must never take the app down with it.
      .catch(() => { if (live) setTargets([]) })
    return () => { live = false }
  }, [docPath])

  if (targets.length === 0) return null

  return (
    <div className="ch-menu-anchor">
      <button
        className="ch-btn"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen(o => !o)}
        title="Branch this document into another app, keeping its data and decisions"
      >
        Continue in <span aria-hidden>↗</span>
      </button>
      {open && (
        <>
          <div className="ch-menu-backdrop" onClick={() => setOpen(false)} />
          <div className="ch-menu" role="menu">
            {targets.map(t => (
              <button
                key={t.app_id}
                className="ch-menu-item"
                role="menuitem"
                onClick={() => { setOpen(false); onSpinoff(t.app_id) }}
              >
                {t.name}
                <small>
                  {t.map ? `this document lands at ${t.map}` : 'carries data and decisions'}
                </small>
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  )
}
