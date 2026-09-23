// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

import type { ManifestInfo } from '../host'
import './chrome.css'

interface Props {
  manifest: ManifestInfo | null
  path: string | null
}

function Row({ k, v, mono }: { k: string; v: string; mono?: boolean }) {
  return (
    <div className="ch-row">
      <span className="ch-key">{k}</span>
      <span className={mono ? 'ch-mono' : 'ch-val'}>{v}</span>
    </div>
  )
}

export default function Sidebar({ manifest, path }: Props) {
  if (!manifest) return <div className="ch-side ch-side-left"><p className="ch-empty">Open a .clan file to begin.</p></div>

  return (
    <div className="ch-side ch-side-left ch-side-scroll">
      <div className="ch-section">
        <div className="eyebrow">Document</div>
        <Row k="Title" v={manifest.title} />
        {manifest.document_type && (
          <div className="ch-row"><span className="ch-chip" style={{ alignSelf: 'flex-start' }}>{manifest.document_type}</span></div>
        )}
        <Row k="Version" v={manifest.version} />
        <Row k="Created" v={new Date(manifest.created_at).toLocaleString()} />
        <Row k="Updated" v={new Date(manifest.updated_at).toLocaleString()} />
        <Row k="Files" v={String(manifest.file_count)} />
      </div>

      <div className="ch-section">
        <div className="eyebrow">Identity</div>
        <Row k="ID" v={manifest.id} mono />
        <Row k="SHA-256" v={manifest.sha256.slice(0, 20) + '…'} mono />
      </div>

      {manifest.lineage ? (
        <div className="ch-section">
          <div className="eyebrow">Lineage</div>
          <Row k="Parent ID" v={manifest.lineage.parent_id} mono />
          <Row k="Delta" v={manifest.lineage.delta} />
          {manifest.lineage.parent_sha256 && (
            <Row k="Parent SHA-256" v={manifest.lineage.parent_sha256.slice(0, 20) + '…'} mono />
          )}
        </div>
      ) : (
        <div className="ch-section">
          <div className="eyebrow">Lineage</div>
          <span className="ch-val">Root document — no parent.</span>
        </div>
      )}

      {path && (
        <div className="ch-section">
          <div className="eyebrow">File</div>
          <span className="ch-mono">{path}</span>
        </div>
      )}
    </div>
  )
}
