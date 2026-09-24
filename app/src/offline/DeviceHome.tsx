// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

import { useInstall } from '../pwa/pwa'
import { LogoSpinner } from '../brand/LogoSpinner'
import OfflineList from './OfflineList'
import { useOfflineCopies } from './useOfflineCopies'
import type { OfflineCopy } from './store'
import '../components/chrome.css'
import './offline.css'

interface Props {
  onChooseFile: () => void
  onOpenCopy: (copy: OfflineCopy) => void
  /** Set when there is a studio to go back to (a signed-in page, not /view). */
  onBackToStudio?: () => void
  /** The studio could not be reached, which is why we are here. */
  offline: boolean
}

/**
 * Home when the shell is on the device: the public viewer at /view, and the
 * signed-in app with no network. Opens a `.clan` in this tab — dropped,
 * chosen, handed over by the OS, or kept as an offline copy.
 */
export default function DeviceHome({ onChooseFile, onOpenCopy, onBackToStudio, offline }: Props) {
  const copies = useOfflineCopies()
  const install = useInstall()

  return (
    <div className="dv">
      <section className="dv-open">
        <div className="eyebrow">{offline ? 'No connection' : 'On this device'}</div>
        <h1>Open a .clan file.</h1>
        <p>
          Drop it anywhere on this window, or choose one. It opens here in this tab and is never
          uploaded: the file stays on this device.
        </p>
        {offline && (
          <p className="dv-offline"><span className="dv-dot" aria-hidden />The studio can’t be reached right now. Offline copies and files on this device still open.</p>
        )}
        <div className="dv-actions">
          <button className="ch-btn ch-btn-primary" onClick={onChooseFile}>Choose a file</button>
          {install && <button className="ch-btn" onClick={install}>Install app</button>}
          {onBackToStudio && (
            <button className="ch-btn ch-btn-quiet" onClick={onBackToStudio}>Back to the studio</button>
          )}
        </div>
      </section>

      {!copies && <LogoSpinner size="sm" label="Reading offline copies…" />}

      {copies && (copies.length > 0 || onBackToStudio) && (
        <section className="dv-copies">
          <div className="dv-sec">
            <span className="eyebrow">Offline copies</span><i />
            {copies.length > 0 && <span className="dv-sec-n">{copies.length}</span>}
          </div>
          <p className="dv-hint">
            Snapshots saved from the studio. Changes made to one here are not sent back.
          </p>
          <OfflineList copies={copies} onOpen={onOpenCopy} />
        </section>
      )}
    </div>
  )
}
