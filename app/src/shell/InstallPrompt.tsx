// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

import { PoweredByClan } from '../brand/PoweredByClan'
import type { OpenResult } from '../host'
import '../components/chrome.css'

interface Props {
  result: OpenResult
  onInstall: () => void
  onRunNew: () => void
  onView: () => void
  onCancel: () => void
}

export default function InstallPrompt({ result, onInstall, onRunNew, onView, onCancel }: Props) {
  const app = result.manifest.app
  const name = app?.name ?? result.manifest.title
  return (
    <div className="ch-scrim" style={{ zIndex: 100 }} onClick={onCancel}>
      <div
        className="ch-dialog"
        style={{ maxWidth: 440 }}
        role="dialog"
        aria-modal="true"
        aria-labelledby="install-title"
        onClick={e => e.stopPropagation()}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
          <div className="ch-monogram" style={{ width: 52, height: 52, fontSize: 22 }}>{name.slice(0, 1).toUpperCase()}</div>
          <div>
            <div className="eyebrow">Template app{app ? ` · v${app.version}` : ''}</div>
            <h2 id="install-title" style={{ marginTop: 4 }}>{name}</h2>
          </div>
        </div>
        <p>
          This is a Napkin app, packaged as a <code>.clan</code> template. Install it to your library,
          or start a new document from it right now.
        </p>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          <button className="ch-choice ch-choice-primary" onClick={onRunNew}>
            New document from this app
            <small>Creates a working copy, linked to the template</small>
          </button>
          <button className="ch-choice" onClick={onInstall}>
            Install to my apps
            <small>Adds it to the launcher so you can reuse it</small>
          </button>
          <button className="ch-choice" onClick={onView}>
            Just view the template
          </button>
        </div>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: 2 }}>
          <PoweredByClan />
          <button className="ch-btn ch-btn-quiet" onClick={onCancel}>Cancel</button>
        </div>
      </div>
    </div>
  )
}
