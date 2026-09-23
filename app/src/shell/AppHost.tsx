// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

import { useState } from 'react'
import Toolbar from '../components/Toolbar'
import Sidebar from '../components/Sidebar'
import AgentPanel from '../components/AgentPanel'
import AppRuntime from './AppRuntime'
import WorkspaceView from './WorkspaceView'
import { PoweredByClan } from '../brand/PoweredByClan'
import type { RunningApp } from './types'
import '../components/chrome.css'

interface Props {
  running: RunningApp
  onHome: () => void
  onOpenFile: () => void
  onSave: () => void
  onExport: (kind: 'html' | 'pdf') => void
  onSpinoff: (appId: string) => void
}

/** Chrome for one running app: toolbar + (collapsible) sidebar + render surface + panels. */
export default function AppHost({ running, onHome, onOpenFile, onSave, onExport, onSpinoff }: Props) {
  const [agentPanelOpen, setAgentPanelOpen] = useState(false)
  const [workspaceOpen, setWorkspaceOpen] = useState(false)
  const [sidebarOpen, setSidebarOpen] = useState(false) // collapsed by default
  const { open } = running

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100vh' }}>
      {/* Accent strip — recolors with a trusted app's theme (clan://set-theme). */}
      <div className="ch-strip" />
      <Toolbar
        title={open.manifest.title}
        isTemplate={open.is_template}
        trusted={open.trusted}
        onHome={onHome}
        onOpenFile={onOpenFile}
        onToggleAgent={() => setAgentPanelOpen(o => !o)}
        agentPanelOpen={agentPanelOpen}
        onToggleSidebar={() => setSidebarOpen(o => !o)}
        sidebarOpen={sidebarOpen}
        onWorkspace={() => setWorkspaceOpen(true)}
        onSave={onSave}
        onExport={onExport}
        docPath={open.path}
        onSpinoff={onSpinoff}
        loading={false}
        validation={open.validation}
      />
      <div style={{ display: 'flex', flex: 1, overflow: 'hidden' }}>
        {sidebarOpen && <Sidebar manifest={open.manifest} path={open.path} />}
        <main style={{ flex: 1, overflow: 'hidden', background: 'var(--paper)', display: 'flex', flexDirection: 'column' }}>
          <AppRuntime
            htmlContent={running.htmlContent}
            hasHumanView={open.has_human_view}
            manifest={open.manifest}
            renderModel={open.render_model === 'authored' ? 'authored' : 'legacy'}
            editMode={running.editMode}
          />
        </main>
        {agentPanelOpen && <AgentPanel onClose={() => setAgentPanelOpen(false)} />}
      </div>
      <footer className="ch-footer">
        <PoweredByClan />
      </footer>
      {workspaceOpen && <WorkspaceView manifest={open.manifest} onClose={() => setWorkspaceOpen(false)} />}
    </div>
  )
}
