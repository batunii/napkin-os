// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

import { useState } from 'react'
import Toolbar from '../components/Toolbar'
import Sidebar from '../components/Sidebar'
import AgentPanel from '../components/AgentPanel'
import AppRuntime from './AppRuntime'
import WorkspaceView from './WorkspaceView'
import type { RunningApp } from './types'

interface Props {
  running: RunningApp
  onHome: () => void
  onOpenFile: () => void
  onToggleEdit: () => void
}

/** Chrome for one running app: toolbar + sidebar + render surface + panels. */
export default function AppHost({ running, onHome, onOpenFile, onToggleEdit }: Props) {
  const [agentPanelOpen, setAgentPanelOpen] = useState(false)
  const [workspaceOpen, setWorkspaceOpen] = useState(false)
  const { open } = running

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100vh' }}>
      <Toolbar
        title={open.manifest.title}
        isTemplate={open.is_template}
        trusted={open.trusted}
        onHome={onHome}
        onOpenFile={onOpenFile}
        onToggleAgent={() => setAgentPanelOpen(o => !o)}
        agentPanelOpen={agentPanelOpen}
        editMode={running.editMode}
        onToggleEdit={onToggleEdit}
        onWorkspace={() => setWorkspaceOpen(true)}
        loading={false}
        validation={open.validation}
      />
      <div style={{ display: 'flex', flex: 1, overflow: 'hidden' }}>
        <Sidebar manifest={open.manifest} path={open.path} />
        <main style={{ flex: 1, overflow: 'hidden', background: 'var(--bg)', display: 'flex', flexDirection: 'column' }}>
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
      {workspaceOpen && <WorkspaceView manifest={open.manifest} onClose={() => setWorkspaceOpen(false)} />}
    </div>
  )
}
