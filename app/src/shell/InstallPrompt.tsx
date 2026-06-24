// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

import { PoweredByClan } from '../brand/PoweredByClan'
import type { OpenResult } from '../App'

interface Props {
  result: OpenResult
  onInstall: () => void
  onRunNew: () => void
  onView: () => void
  onCancel: () => void
}

const s: Record<string, React.CSSProperties> = {
  overlay: {
    position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.6)',
    display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 100,
  },
  modal: {
    width: 440, background: 'var(--surface)', border: '1px solid var(--border)',
    borderRadius: 14, padding: 24, display: 'flex', flexDirection: 'column', gap: 14,
  },
  icon: {
    width: 52, height: 52, borderRadius: 13,
    background: 'linear-gradient(135deg, #6366f1, #2dd4cf)',
    display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 26, color: '#fff', fontWeight: 700,
  },
  title: { fontSize: 18, fontWeight: 700, color: 'var(--text)' },
  sub: { fontSize: 13, color: 'var(--muted)', lineHeight: 1.6 },
  actions: { display: 'flex', flexDirection: 'column', gap: 8, marginTop: 6 },
  primary: {
    padding: '11px 14px', borderRadius: 9, background: 'var(--accent)', border: 'none',
    color: '#fff', fontSize: 13, fontWeight: 600, cursor: 'pointer', textAlign: 'left',
  },
  secondary: {
    padding: '11px 14px', borderRadius: 9, background: 'transparent', border: '1px solid var(--border)',
    color: 'var(--text)', fontSize: 13, cursor: 'pointer', textAlign: 'left',
  },
  hint: { fontSize: 11, color: 'var(--muted)' },
  cancel: { alignSelf: 'flex-end', background: 'none', border: 'none', color: 'var(--muted)', fontSize: 12, cursor: 'pointer' },
}

export default function InstallPrompt({ result, onInstall, onRunNew, onView, onCancel }: Props) {
  const app = result.manifest.app
  const name = app?.name ?? result.manifest.title
  return (
    <div style={s.overlay} onClick={onCancel}>
      <div style={s.modal} onClick={e => e.stopPropagation()}>
        <div style={s.icon}>{name.slice(0, 1).toUpperCase()}</div>
        <div>
          <div style={s.title}>{name}</div>
          <div style={s.hint}>Template app{app ? ` · v${app.version}` : ''}</div>
        </div>
        <p style={s.sub}>
          This is a Napkin app, packaged as a <code>.clan</code> template. Install it to your library,
          or start a new document from it right now.
        </p>
        <div style={s.actions}>
          <button style={s.primary} onClick={onRunNew}>
            ✨ New document from this app
            <div style={{ ...s.hint, color: 'rgba(255,255,255,0.8)' }}>Creates a working copy, linked to the template</div>
          </button>
          <button style={s.secondary} onClick={onInstall}>
            ⬇ Install to my apps
            <div style={s.hint}>Adds it to the launcher so you can reuse it</div>
          </button>
          <button style={s.secondary} onClick={onView}>
            👁 Just view the template
          </button>
        </div>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <PoweredByClan />
          <button style={s.cancel} onClick={onCancel}>Cancel</button>
        </div>
      </div>
    </div>
  )
}
