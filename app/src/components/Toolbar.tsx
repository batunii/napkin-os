// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

import { NapkinLogo } from '../brand/NapkinMark'

interface Props {
  title?: string
  isTemplate?: boolean
  trusted?: boolean
  onHome: () => void
  onOpenFile: () => void
  onToggleAgent: () => void
  onToggleSidebar: () => void
  onWorkspace: () => void
  onSave: () => void
  agentPanelOpen: boolean
  sidebarOpen: boolean
  loading: boolean
  validation?: string
}

const s: Record<string, React.CSSProperties> = {
  bar: {
    height: 48, background: 'var(--bg)', borderBottom: '1px solid var(--border)',
    display: 'flex', alignItems: 'center', padding: '0 12px', gap: 10, flexShrink: 0,
    userSelect: 'none',
  },
  home: {
    display: 'flex', alignItems: 'center', gap: 6, marginRight: 2, cursor: 'pointer',
    background: 'none', border: 'none', padding: '4px 6px', borderRadius: 6,
  },
  title: { flex: 1, fontSize: 13, color: 'var(--text)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
  badge: { fontSize: 11, padding: '2px 8px', borderRadius: 999, background: '#1e3a2f', color: '#4ade80', border: '1px solid #166534', letterSpacing: '0.05em' },
  badgeWarn: { background: '#3a2a1e', color: 'var(--warn)', border: '1px solid #92400e' },
  templateBadge: { fontSize: 10, padding: '2px 8px', borderRadius: 999, background: '#1e2d45', color: 'var(--accent)', border: '1px solid var(--accent)', letterSpacing: '0.05em' },
  btn: {
    height: 30, padding: '0 12px', borderRadius: 6, border: '1px solid var(--border)',
    background: 'var(--surface)', color: 'var(--text)', cursor: 'pointer', fontSize: 12,
    display: 'flex', alignItems: 'center', gap: 6,
  },
  btnActive: { background: 'var(--accent)', borderColor: 'var(--accent)', color: '#fff' },
  btnEdit: { background: '#1e3a2f', borderColor: '#166534', color: '#4ade80' },
  editBadge: {
    fontSize: 10, padding: '2px 8px', borderRadius: 999,
    background: 'rgba(74,222,128,0.15)', color: '#4ade80',
    border: '1px solid rgba(74,222,128,0.4)', letterSpacing: '0.05em',
    animation: 'pulse 2s infinite',
  },
}

export default function Toolbar({
  title, isTemplate, trusted, onHome, onOpenFile, onToggleAgent, onToggleSidebar, onWorkspace, onSave,
  agentPanelOpen, sidebarOpen, loading, validation,
}: Props) {
  const valid = validation === 'OK'
  return (
    <div style={s.bar}>
      <button
        style={{ ...s.btn, ...(sidebarOpen ? s.btnActive : {}), padding: '0 9px' }}
        onClick={onToggleSidebar}
        title={sidebarOpen ? 'Hide details' : 'Show details'}
      >
        ☰
      </button>
      <button style={s.home} onClick={onHome} title="Back to apps">
        <NapkinLogo size={16} compact />
      </button>
      <span style={s.title}>{loading ? 'Loading…' : (title ?? 'No file open')}</span>
      {isTemplate && <span style={s.templateBadge}>TEMPLATE</span>}
      {trusted && (
        <span
          style={{ ...s.templateBadge, background: '#10241b', color: '#4ade80', borderColor: '#166534' }}
          title="Signed by Napkin — scoped host capabilities enabled"
        >
          🛡 trusted
        </span>
      )}
      {validation && (
        <span
          style={{ ...s.badge, ...(valid ? {} : s.badgeWarn), cursor: valid ? 'default' : 'help' }}
          title={valid ? 'No validation issues' : validation}
        >
          {valid ? '✓ valid' : '⚠ issues'}
        </span>
      )}
      <button style={s.btn} onClick={onOpenFile}>📂 Open</button>
      <button style={s.btn} onClick={onSave} title="Save a copy of this .clan to share">💾 Save As</button>
      <button style={s.btn} onClick={onWorkspace} title="Lineage & provenance">🧬 Lineage</button>
      <button
        style={{ ...s.btn, ...(agentPanelOpen ? s.btnActive : {}) }}
        onClick={onToggleAgent}
      >
        🤖 Agent
      </button>
    </div>
  )
}
