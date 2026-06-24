// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

import { PoweredByClan } from '../brand/PoweredByClan'
import type { ManifestInfo } from '../App'

interface Props {
  manifest: ManifestInfo
  onClose: () => void
}

const s: Record<string, React.CSSProperties> = {
  overlay: {
    position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.55)',
    display: 'flex', justifyContent: 'flex-end', zIndex: 90,
  },
  panel: {
    width: 360, height: '100%', background: 'var(--surface)', borderLeft: '1px solid var(--border)',
    padding: 22, display: 'flex', flexDirection: 'column', gap: 16, overflowY: 'auto',
  },
  head: { display: 'flex', alignItems: 'center', justifyContent: 'space-between' },
  title: { fontSize: 15, fontWeight: 700, color: 'var(--text)' },
  close: { background: 'none', border: 'none', color: 'var(--muted)', fontSize: 18, cursor: 'pointer' },
  node: {
    border: '1px solid var(--border)', borderRadius: 10, padding: '12px 14px',
    display: 'flex', flexDirection: 'column', gap: 4,
  },
  nodeCurrent: { borderColor: 'var(--accent)', background: 'rgba(99,102,241,0.08)' },
  label: { fontSize: 10, fontWeight: 700, letterSpacing: '0.1em', color: 'var(--muted)', textTransform: 'uppercase' },
  val: { fontSize: 12, color: 'var(--text)' },
  mono: { fontSize: 10, color: 'var(--muted)', fontFamily: 'monospace', wordBreak: 'break-all' },
  connector: { alignSelf: 'center', color: 'var(--muted)', fontSize: 16 },
}

export default function WorkspaceView({ manifest, onClose }: Props) {
  const app = manifest.app
  return (
    <div style={s.overlay} onClick={onClose}>
      <div style={s.panel} onClick={e => e.stopPropagation()}>
        <div style={s.head}>
          <span style={s.title}>Workspace &amp; lineage</span>
          <button style={s.close} onClick={onClose}>×</button>
        </div>

        {app && (
          <div style={s.node}>
            <span style={s.label}>App</span>
            <span style={s.val}>{app.name} <span style={{ color: 'var(--muted)' }}>v{app.version}</span></span>
            <span style={s.mono}>{app.app_id}</span>
          </div>
        )}

        {manifest.lineage ? (
          <>
            <div style={s.node}>
              <span style={s.label}>Parent</span>
              <span style={s.val}>{manifest.lineage.delta}</span>
              <span style={s.mono}>{manifest.lineage.parent_id}</span>
              {manifest.lineage.parent_sha256 && (
                <span style={s.mono}>{manifest.lineage.parent_sha256.slice(0, 24)}…</span>
              )}
            </div>
            <div style={s.connector}>↓</div>
          </>
        ) : (
          <div style={s.node}>
            <span style={s.label}>Lineage</span>
            <span style={s.val}>Root document — no parent.</span>
          </div>
        )}

        <div style={{ ...s.node, ...s.nodeCurrent }}>
          <span style={s.label}>This document</span>
          <span style={s.val}>{manifest.title}</span>
          <span style={s.mono}>{manifest.id}</span>
          {manifest.document_type && <span style={s.mono}>type: {manifest.document_type}</span>}
        </div>

        <div style={{ marginTop: 'auto' }}><PoweredByClan /></div>
      </div>
    </div>
  )
}
