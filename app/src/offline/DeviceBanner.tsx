// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

import { when, type DeviceSource } from './actions'
import './offline.css'

interface Props {
  source: DeviceSource
  onDownload: () => void
}

/**
 * Under the toolbar of a document open on the device. Says, every time, that
 * nothing done here reaches the studio — the server is authoritative, and an
 * offline copy is a snapshot (N3).
 */
export default function DeviceBanner({ source, onDownload }: Props) {
  return (
    <div className="dv-banner" role="status">
      <span className="ch-chip dv-banner-chip"><span className="ch-chip-dot" aria-hidden />{source.kind === 'offline' ? 'Offline copy' : 'On this device'}</span>
      <span className="dv-banner-text">
        {source.kind === 'offline'
          ? `Saved ${when(source.copy.savedAt)}. Changes you make here are not sent to the studio and are lost when you close it.`
          : 'Opened from this device and never uploaded. Changes stay in this tab.'}
      </span>
      <button className="ch-btn" onClick={onDownload}>Download a copy</button>
    </div>
  )
}
