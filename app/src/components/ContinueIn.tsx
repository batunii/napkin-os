// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

import { useEffect, useState } from 'react'

import { host } from '../host'
import type { SpinoffTarget } from '../host'

interface Props {
  /**
   * The open document. Targets depend on its app, so the toolbar mounts this
   * keyed by it — a different document gets a fresh fetch and a closed menu
   * without any of that having to be synchronised by hand.
   */
  docPath: string
  onSpinoff: (appId: string) => void
}

const s: Record<string, React.CSSProperties> = {
  btn: {
    height: 30, padding: '0 12px', borderRadius: 6, border: '1px solid var(--border)',
    background: 'var(--surface)', color: 'var(--text)', cursor: 'pointer', fontSize: 12,
    display: 'flex', alignItems: 'center', gap: 6,
  },
  backdrop: { position: 'fixed', inset: 0, zIndex: 40 },
  menu: {
    position: 'absolute', top: 36, right: 0, zIndex: 41, minWidth: 220,
    background: 'var(--surface)', border: '1px solid var(--border)', borderRadius: 8,
    padding: 4, boxShadow: '0 8px 24px rgba(0,0,0,0.28)',
  },
  item: {
    display: 'block', width: '100%', textAlign: 'left', padding: '7px 10px', borderRadius: 6,
    background: 'none', border: 'none', color: 'var(--text)', cursor: 'pointer', fontSize: 12,
  },
  where: { display: 'block', fontSize: 10, color: 'var(--muted)', marginTop: 2 },
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
    <div style={{ position: 'relative' }}>
      <button
        style={s.btn}
        onClick={() => setOpen(o => !o)}
        title="Branch this document into another app, keeping its data and decisions"
      >
        ↗ Continue in
      </button>
      {open && (
        <>
          <div style={s.backdrop} onClick={() => setOpen(false)} />
          <div style={s.menu} role="menu">
            {targets.map(t => (
              <button
                key={t.app_id}
                style={s.item}
                role="menuitem"
                onClick={() => { setOpen(false); onSpinoff(t.app_id) }}
              >
                {t.name}
                <span style={s.where}>
                  {t.map ? `this document lands at ${t.map}` : 'carries data and decisions'}
                </span>
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  )
}
