// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

import { useEffect, useState, useCallback } from 'react'
import { invoke } from '@tauri-apps/api/core'
import { listen } from '@tauri-apps/api/event'
import { open as openDialog } from '@tauri-apps/plugin-dialog'
import Launcher from './shell/Launcher'
import AppHost from './shell/AppHost'
import AppRuntime from './shell/AppRuntime'
import InstallPrompt from './shell/InstallPrompt'
import type { InstalledApp, RunningApp, Screen } from './shell/types'
import './index.css'

export interface AppMeta {
  name: string
  app_id: string
  version: string
  icon?: string | null
}

export interface ManifestInfo {
  title: string
  id: string
  version: string
  created_at: string
  updated_at: string
  document_type?: string
  sha256: string
  file_count: number
  lineage?: {
    parent_id: string
    parent_uri: string
    parent_sha256?: string
    delta: string
  }
  app?: AppMeta
}

export interface OpenResult {
  path: string
  manifest: ManifestInfo
  validation: string
  has_human_view: boolean
  render_model: 'authored' | 'legacy'
  is_template: boolean
  trusted: boolean
}

export default function App() {
  const [screen, setScreen] = useState<Screen>('home')
  const [running, setRunning] = useState<RunningApp | null>(null)
  // The home page is itself a CLAN app, rendered full-bleed.
  const [home, setHome] = useState<{ open: OpenResult; html: string } | null>(null)
  const [installed, setInstalled] = useState<InstalledApp[]>([])
  const [pendingLaunch, setPendingLaunch] = useState<OpenResult | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Toast raised by a trusted app via window.napkin.notify → clan://notify.
  const [toast, setToast] = useState<{ title: string; body: string } | null>(null)

  const refreshApps = useCallback(async () => {
    try { setInstalled(await invoke<InstalledApp[]>('list_apps')) } catch (e) { console.error(e) }
  }, [])

  // Open the home CLAN app as the current document and render it.
  const openHome = useCallback(async () => {
    try {
      const open = await invoke<OpenResult>('open_home')
      const html = open.has_human_view ? await invoke<string>('get_human_html') : ''
      setHome({ open, html })
    } catch (e) {
      console.error('open_home failed', e)
      setHome(null) // fall back to the native launcher
      refreshApps()
    }
    setScreen('home')
  }, [refreshApps])

  const runArtifact = useCallback(async (open: OpenResult) => {
    const html = open.has_human_view ? await invoke<string>('get_human_html') : ''
    setRunning({ artifactPath: open.path, open, htmlContent: html, editMode: false })
    setScreen('app')
  }, [])

  // Open a .clan path: templates → install prompt; documents → run.
  const openPath = useCallback(async (path: string) => {
    setLoading(true); setError(null)
    try {
      const result = await invoke<OpenResult>('open_clan', { path })
      if (result.is_template) setPendingLaunch(result)
      else await runArtifact(result)
    } catch (e) { setError(String(e)) } finally { setLoading(false) }
  }, [runArtifact])

  const handleOpenFile = useCallback(async () => {
    const selected = await openDialog({ multiple: false, filters: [{ name: 'CLAN Files', extensions: ['clan'] }] })
    if (selected) await openPath(selected as string)
  }, [openPath])

  const launchApp = useCallback(async (appId: string) => {
    setLoading(true); setError(null)
    try {
      const result = await invoke<OpenResult>('new_document_from_app', { appId, title: null })
      await runArtifact(result)
    } catch (e) { setError(String(e)) } finally { setLoading(false) }
  }, [runArtifact])

  useEffect(() => {
    openHome()
    invoke<string | null>('take_launch_file').then(p => { if (p) openPath(p) }).catch(() => {})
    // The host forwards launch/open requests that originate INSIDE a clan file
    // (e.g. a click in the home CLAN app), plus OS "open with" events.
    const subs = [
      listen<string>('open-file', e => { if (e.payload) openPath(e.payload) }),
      listen<string>('clan-open-document', e => { if (e.payload) openPath(e.payload) }),
      listen('clan-open-file-request', () => { handleOpenFile() }),
      listen<{ title: string; body: string }>('napkin-notify', e => {
        if (e.payload) { setToast(e.payload); setTimeout(() => setToast(null), 4000) }
      }),
    ]
    return () => { subs.forEach(s => s.then(f => f())) }
  }, [openHome, openPath, handleOpenFile])

  const goHome = useCallback(() => { openHome() }, [openHome])

  const toggleEdit = useCallback(() => {
    setRunning(r => (r ? { ...r, editMode: !r.editMode } : r))
  }, [])

  const onInstall = useCallback(async () => {
    if (!pendingLaunch) return
    try { await invoke('install_app', { srcPath: pendingLaunch.path }); await refreshApps() } catch (e) { setError(String(e)) }
    setPendingLaunch(null); openHome()
  }, [pendingLaunch, refreshApps, openHome])

  const onRunNew = useCallback(async () => {
    if (!pendingLaunch?.manifest.app) return
    const appId = pendingLaunch.manifest.app.app_id
    try { await invoke('install_app', { srcPath: pendingLaunch.path }) } catch { /* maybe already installed */ }
    setPendingLaunch(null)
    await launchApp(appId)
  }, [pendingLaunch, launchApp])

  const onViewTemplate = useCallback(async () => {
    if (!pendingLaunch) return
    const result = pendingLaunch
    setPendingLaunch(null)
    await runArtifact(result)
  }, [pendingLaunch, runArtifact])

  return (
    <>
      {error && (
        <div style={{ padding: 16, color: 'var(--danger)', fontFamily: 'monospace', background: '#0a0d14', borderBottom: '1px solid var(--border)' }}>
          <strong>Error:</strong> {error}{' '}
          <button onClick={() => setError(null)} style={{ marginLeft: 8, background: 'none', border: '1px solid var(--border)', color: 'var(--muted)', borderRadius: 4, cursor: 'pointer' }}>dismiss</button>
        </div>
      )}

      {screen === 'app' && running ? (
        <AppHost running={running} onHome={goHome} onOpenFile={handleOpenFile} onToggleEdit={toggleEdit} />
      ) : home ? (
        // The home page is a CLAN file, rendered full-bleed with no doc chrome.
        <div style={{ display: 'flex', flexDirection: 'column', height: '100vh' }}>
          <AppRuntime
            htmlContent={home.html}
            hasHumanView={home.open.has_human_view}
            manifest={home.open.manifest}
            renderModel="authored"
            editMode={false}
          />
        </div>
      ) : (
        // Fallback: native launcher if the home CLAN app couldn't load.
        <Launcher installed={installed} loading={loading} onLaunchApp={launchApp} onOpenFile={handleOpenFile} />
      )}

      {pendingLaunch && (
        <InstallPrompt
          result={pendingLaunch}
          onInstall={onInstall}
          onRunNew={onRunNew}
          onView={onViewTemplate}
          onCancel={() => setPendingLaunch(null)}
        />
      )}

      {toast && (
        <div style={{
          position: 'fixed', bottom: 20, right: 20, zIndex: 200, maxWidth: 320,
          background: 'var(--surface)', border: '1px solid var(--accent)', borderRadius: 12,
          padding: '12px 16px', boxShadow: '0 8px 30px rgba(0,0,0,0.4)',
        }}>
          <div style={{ fontSize: 13, fontWeight: 700, color: 'var(--text)', marginBottom: 2 }}>🛡 {toast.title}</div>
          <div style={{ fontSize: 12, color: 'var(--muted)' }}>{toast.body}</div>
        </div>
      )}
    </>
  )
}
