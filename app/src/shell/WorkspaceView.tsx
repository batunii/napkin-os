// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

import { PoweredByClan } from '../brand/PoweredByClan'
import type { ManifestInfo } from '../host'
import '../components/chrome.css'

interface Props {
  manifest: ManifestInfo
  onClose: () => void
}

export default function WorkspaceView({ manifest, onClose }: Props) {
  const app = manifest.app
  return (
    <div className="ch-scrim ch-scrim-right" style={{ zIndex: 90 }} onClick={onClose}>
      <div
        className="ch-drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="lineage-title"
        onClick={e => e.stopPropagation()}
      >
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 4 }}>
          <div>
            <div className="eyebrow">Provenance</div>
            <h2 id="lineage-title" style={{ fontSize: 20, fontWeight: 700, letterSpacing: '-0.03em', marginTop: 4 }}>
              Workspace &amp; lineage
            </h2>
          </div>
          <button className="ch-btn ch-btn-icon ch-btn-quiet" onClick={onClose} aria-label="Close">✕</button>
        </div>

        {app && (
          <div className="ch-node">
            <span className="eyebrow">App</span>
            <span className="ch-val">{app.name} <span style={{ color: 'var(--ink2)' }}>v{app.version}</span></span>
            <span className="ch-mono">{app.app_id}</span>
          </div>
        )}

        {manifest.lineage ? (
          <>
            <div className="ch-node">
              <span className="eyebrow">Parent</span>
              <span className="ch-val">{manifest.lineage.delta}</span>
              <span className="ch-mono">{manifest.lineage.parent_id}</span>
              {manifest.lineage.parent_sha256 && (
                <span className="ch-mono">{manifest.lineage.parent_sha256.slice(0, 24)}…</span>
              )}
            </div>
            <div className="ch-connector" aria-hidden />
          </>
        ) : (
          <div className="ch-node">
            <span className="eyebrow">Lineage</span>
            <span className="ch-val">Root document — no parent.</span>
          </div>
        )}

        <div className="ch-node ch-node-current">
          <span className="eyebrow">This document</span>
          <span className="ch-val">{manifest.title}</span>
          <span className="ch-mono">{manifest.id}</span>
          {manifest.document_type && <span className="ch-mono">type: {manifest.document_type}</span>}
        </div>

        <div style={{ marginTop: 'auto' }}><PoweredByClan /></div>
      </div>
    </div>
  )
}
