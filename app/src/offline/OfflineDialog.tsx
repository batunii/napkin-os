// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

import OfflineList from './OfflineList'
import { LogoSpinner } from '../brand/LogoSpinner'
import { useOfflineCopies } from './useOfflineCopies'
import type { OfflineCopy } from './store'
import '../components/chrome.css'

interface Props {
  onOpen: (copy: OfflineCopy) => void
  onClose: () => void
}

/** The Offline section, from the studio's top bar. */
export default function OfflineDialog({ onOpen, onClose }: Props) {
  const copies = useOfflineCopies()
  return (
    <div className="ch-scrim" style={{ zIndex: 100 }} onClick={onClose}>
      <div
        className="ch-dialog"
        style={{ maxWidth: 620 }}
        role="dialog"
        aria-modal="true"
        aria-labelledby="offline-title"
        onClick={e => e.stopPropagation()}
      >
        <div>
          <div className="eyebrow">On this device</div>
          <h2 id="offline-title" style={{ marginTop: 4 }}>Offline</h2>
        </div>
        <p>
          Copies kept here open with no network. Each is a snapshot of the document when you saved
          it: changes made to an offline copy are not sent back to the studio.
        </p>
        {!copies && <LogoSpinner size="sm" label="Reading offline copies…" />}
        {copies && <OfflineList copies={copies} onOpen={copy => { onClose(); onOpen(copy) }} />}
        <div className="ch-actions">
          <button className="ch-btn ch-btn-quiet" onClick={onClose}>Close</button>
        </div>
      </div>
    </div>
  )
}
