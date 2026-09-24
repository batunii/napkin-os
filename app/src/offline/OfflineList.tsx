// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

import { useState } from 'react'
import { downloadCopy, size, when } from './actions'
import { removeCopy, type OfflineCopy } from './store'
import '../components/chrome.css'
import './offline.css'

interface Props {
  copies: OfflineCopy[]
  onOpen: (copy: OfflineCopy) => void
}

/** One row per offline copy: open it, save it as a file, or let it go. */
export default function OfflineList({ copies, onOpen }: Props) {
  const [error, setError] = useState<string | null>(null)

  if (copies.length === 0) {
    return (
      <p className="off-empty">
        Nothing yet. Open a document in the studio and choose <b>Present offline</b> to keep a copy
        on this device.
      </p>
    )
  }

  const remove = async (copy: OfflineCopy) => {
    if (!window.confirm(`Remove the offline copy of “${copy.title}” from this device?`)) return
    try { await removeCopy(copy.id) } catch (e) { setError(String(e)) }
  }

  return (
    <>
      <ul className="off-list">
        {copies.map(copy => (
          <li key={copy.id} className="off-row">
            <span className="off-icon" aria-hidden>
              <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round">
                <path d="M4 1.8h5.2L12.5 5v9.2H4z" /><path d="M9 1.8V5.2h3.4M6.2 8.4h4M6.2 11h4" />
              </svg>
            </span>
            <div className="off-meta">
              <b>{copy.title}</b>
              <small>
                {copy.appName && copy.appName !== copy.title ? `${copy.appName} · ` : ''}revision {copy.revision} · saved {when(copy.savedAt)} · {size(copy.size)}
              </small>
            </div>
            <div className="off-actions">
              <button className="ch-btn ch-btn-primary" onClick={() => onOpen(copy)}>Open</button>
              <button className="ch-btn" onClick={() => downloadCopy(copy).catch(e => setError(String(e)))}>
                Download a copy
              </button>
              <button className="ch-btn ch-btn-quiet ch-btn-danger" onClick={() => remove(copy)}>Remove</button>
            </div>
          </li>
        ))}
      </ul>
      {error && <p className="ch-warn">{error}</p>}
    </>
  )
}
