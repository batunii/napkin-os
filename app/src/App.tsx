// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

import { useEffect, useState, useCallback, useRef } from 'react'
import { hasServer, host, onDevice, openOnDevice, serverless, switchToDevice, switchToServer } from './host'
import type { InstalledApp, OpenResult } from './host'
import DeviceBanner from './offline/DeviceBanner'
import DeviceHome from './offline/DeviceHome'
import OfflineDialog from './offline/OfflineDialog'
import { isClanFile, keepOffline, pickFile, type DeviceSource } from './offline/actions'
import { copyBytes, type OfflineCopy } from './offline/store'
import { useFileDrop } from './offline/useFileDrop'
import { applyUpdate, onLaunchFiles, useUpdateReady } from './pwa/pwa'
import { LogoSpinner } from './brand/LogoSpinner'
import { askAppToExport } from './shell/appExport'
import Launcher from './shell/Launcher'
import AppHost from './shell/AppHost'
import AppRuntime from './shell/AppRuntime'
import InstallPrompt from './shell/InstallPrompt'
import type { RunningApp, Screen } from './shell/types'
import StudioShell from './studio/StudioShell'
import './index.css'

// Theme keys an immersive app may recolor → CSS variables on the shell root.
// These are the design tokens; the legacy names (--bg, --surface, --text,
// --border, --muted) alias onto them in index.css, so both follow. The accent
// is --accent, not --create: an app may recolour the chrome's accent but not
// the Create department.
const THEME_VARS: Record<string, string> = {
  accent: '--accent', bg: '--paper', surface: '--card',
  text: '--ink', border: '--line', muted: '--ink2',
}
function applyTheme(colors: Record<string, string> | null | undefined) {
  if (!colors) return
  const root = document.documentElement
  for (const [k, v] of Object.entries(colors)) {
    if (THEME_VARS[k] && typeof v === 'string') root.style.setProperty(THEME_VARS[k], v)
  }
}
function resetTheme() {
  const root = document.documentElement
  for (const v of Object.values(THEME_VARS)) root.style.removeProperty(v)
}

export default function App() {
  const [screen, setScreen] = useState<Screen>('home')
  const [running, setRunning] = useState<RunningApp | null>(null)
  // The home CLAN app, shown under the Apps tab.
  const [home, setHome] = useState<{ open: OpenResult; html: string } | null>(null)
  const [installed, setInstalled] = useState<InstalledApp[]>([])
  const [pendingLaunch, setPendingLaunch] = useState<OpenResult | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Toast raised by a trusted app via window.napkin.notify → clan://notify.
  const [toast, setToast] = useState<{ title: string; body: string } | null>(null)
  // On the device: the public viewer, a file opened here, an offline copy, or
  // a signed-in page that could not reach the studio.
  const [device, setDevice] = useState(onDevice)
  const [unreachable, setUnreachable] = useState(false)
  const [offlineOpen, setOfflineOpen] = useState(false)
  // Work that takes a moment and has no screen of its own: starting the device
  // host and opening a file in it, or saving an offline copy.
  const [busy, setBusy] = useState<string | null>(null)
  const updateReady = useUpdateReady()

  const notify = useCallback((title: string, body: string, ms = 5000) => {
    setToast({ title, body })
    setTimeout(() => setToast(null), ms)
  }, [])

  // Every open — home, a document, a new or spun-off one, a file on the
  // device — takes a number, and only the latest may change what is on screen.
  // Opens overlap: on load the shell asks for home and for a `?open=` document
  // at once, and whichever answered last used to win, sometimes painting one
  // document's view over the other's host session.
  const openSeq = useRef(0)
  const nextOpen = () => ++openSeq.current
  const isLatest = (n: number) => n === openSeq.current

  const refreshApps = useCallback(async () => {
    try { setInstalled(await host.listApps()) } catch (e) { console.error(e) }
  }, [])

  // Open the home CLAN app as the current document and render it.
  const openHome = useCallback(async () => {
    const n = nextOpen()
    resetTheme() // home and other apps use the default Napkin theme
    // The web app on the device has its own home: open a file, or a copy.
    if (onDevice() && !serverless) {
      setScreen('home')
      return
    }
    try {
      const open = await host.openHome()
      const html = open.has_human_view ? await host.getHumanHtml() : ''
      if (!isLatest(n)) return
      setHome({ open, html })
      refreshApps() // views other than Apps may offer installed apps too
    } catch (e) {
      if (!isLatest(n)) return
      console.error('open_home failed', e)
      setHome(null)
      if (hasServer && (e instanceof TypeError || !navigator.onLine)) {
        // The studio is out of reach, not broken: carry on as the viewer, so
        // offline copies and files on this device still open.
        switchToDevice()
        setDevice(true)
        setUnreachable(true)
      } else {
        refreshApps() // fall back to the native launcher
      }
    }
    setScreen('home')
  }, [refreshApps])

  const runArtifact = useCallback(async (n: number, open: OpenResult, source?: DeviceSource) => {
    const html = open.has_human_view ? await host.getHumanHtml() : ''
    if (!isLatest(n)) return
    resetTheme() // clear any prior app's theme before this one (re)applies its own
    setRunning({ artifactPath: open.path, open, htmlContent: html, editMode: false, source })
    setScreen('app')
  }, [])

  // A .clan from this device — dropped, chosen, handed over by the OS, or an
  // offline copy. It opens in the tab and is never uploaded, whichever host
  // the page was using before.
  const openDeviceBytes = useCallback(async (bytes: Uint8Array, label: string, source: DeviceSource) => {
    const n = nextOpen()
    setLoading(true); setError(null); setBusy('Opening on this device…')
    try {
      const result = await openOnDevice(bytes, label)
      setDevice(true)
      if (!isLatest(n)) return
      if (result.is_template) setPendingLaunch(result)
      else await runArtifact(n, result, source)
    } catch (e) { setError(String(e)) } finally { setLoading(false); setBusy(null) }
  }, [runArtifact])

  const openLocalFile = useCallback(async (file: File) => {
    if (!isClanFile(file)) {
      setError(`${file.name} is not a .clan file.`)
      return
    }
    await openDeviceBytes(new Uint8Array(await file.arrayBuffer()), file.name, { kind: 'file', name: file.name })
  }, [openDeviceBytes])

  const openOfflineCopy = useCallback(async (copy: OfflineCopy) => {
    try {
      await openDeviceBytes(await copyBytes(copy.id), `offline-${copy.id}`, { kind: 'offline', copy })
    } catch (e) { setError(String(e)) }
  }, [openDeviceBytes])

  const chooseDeviceFile = useCallback(async () => {
    const file = await pickFile()
    if (file) await openLocalFile(file)
  }, [openLocalFile])

  const dragging = useFileDrop(openLocalFile)

  // Chrome and Edge, installed: a .clan double-clicked in the OS.
  const openLocalRef = useRef(openLocalFile)
  useEffect(() => { openLocalRef.current = openLocalFile }, [openLocalFile])
  useEffect(() => { onLaunchFiles(file => { openLocalRef.current(file) }) }, [])

  const backToStudio = useCallback(() => {
    switchToServer()
    setDevice(false)
    setUnreachable(false)
    setRunning(null)
    openHome()
  }, [openHome])

  // Open a .clan path: templates → install prompt; documents → run.
  const openPath = useCallback(async (path: string) => {
    const n = nextOpen()
    setLoading(true); setError(null)
    try {
      const result = await host.openClan(path)
      if (!isLatest(n)) return
      if (result.is_template) setPendingLaunch(result)
      else await runArtifact(n, result)
    } catch (e) { setError(String(e)) } finally { setLoading(false) }
  }, [runArtifact])

  const handleOpenFile = useCallback(async () => {
    // On the device, "Open" never means upload.
    if (onDevice() && !serverless) return chooseDeviceFile()
    const selected = await host.pickClanToOpen()
    if (selected) await openPath(selected)
  }, [openPath, chooseDeviceFile])

  const launchApp = useCallback(async (appId: string) => {
    const n = nextOpen()
    setLoading(true); setError(null)
    try {
      const result = await host.newDocumentFromApp(appId, null)
      await runArtifact(n, result)
    } catch (e) { setError(String(e)) } finally { setLoading(false) }
  }, [runArtifact])

  // Branch the open document into another app. The source is untouched — this
  // opens a new document that carries its data and its decisions.
  const spinOff = useCallback(async (appId: string) => {
    const n = nextOpen()
    setLoading(true); setError(null)
    try {
      const result = await host.spinoffDocument(appId, null, null)
      await runArtifact(n, result)
    } catch (e) { setError(String(e)) } finally { setLoading(false) }
  }, [runArtifact])

  // Save/export the open .clan. Shared by the toolbar Save As button and the
  // in-app "lock also saves" request. Reads the latest running app via a ref so
  // the once-registered event listener never goes stale.
  const runningRef = useRef<RunningApp | null>(null)
  useEffect(() => { runningRef.current = running }, [running])
  const saveCurrent = useCallback(async () => {
    const r = runningRef.current
    if (!r) return
    const base = (r.open.manifest.title || 'document').replace(/[^\w.-]+/g, '-')
    const path = await host.pickSaveDestination(base, 'clan')
    if (path) await host.saveClanTo(path).catch(console.error)
  }, [])

  // "Present offline": keep the server's current copy of this document here.
  const keepCurrentOffline = useCallback(async () => {
    const r = runningRef.current
    if (!r) return
    setBusy('Saving for offline…')
    try {
      const copy = await keepOffline(r.open.path)
      notify('Saved for offline', `“${copy.title}” now opens from Offline with no network. It is a snapshot: changes made to it there are not sent back.`, 7000)
    } catch (err) {
      notify('Could not save for offline', String(err))
    } finally {
      setBusy(null)
    }
  }, [notify])

  // OS-owned export: the host composes a standalone document from the open
  // file's data via the SDK, then the clan-export-request listener runs the
  // save dialog + finish_export. Works for every app, no in-view builder needed.
  const exportCurrent = useCallback(async (kind: 'html' | 'pdf' = 'pdf') => {
    if (!runningRef.current) return
    // An app that renders its own view knows how it should look on paper; the
    // host can only compose from the markup, which for such an app is empty.
    if (await askAppToExport(kind)) return
    await host.exportCurrent(kind, false, false).catch(err => {
      setToast({ title: 'Export failed', body: String(err) })
      setTimeout(() => setToast(null), 5000)
    })
  }, [])

  useEffect(() => {
    // openHome is async and sets no state synchronously: everything before its
    // first await is resetTheme(), which only clears CSS custom properties —
    // or, on the device, a setScreen to the screen we are already on. The
    // other setState calls all run after `await host.openHome()` — this is the
    // initial load from the host, i.e. the external-system synchronisation the
    // rule explicitly allows, not derived state.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    openHome()
    host.takeLaunchFile().then(p => { if (p) openPath(p) }).catch(() => {})
  }, [openHome, openPath])

  useEffect(() => {
    // Re-subscribed whenever the page changes host (`device`): events come
    // from whichever one the open document lives in.
    // The host forwards launch/open requests that originate INSIDE a clan file
    // (e.g. a click in the home CLAN app), plus OS "open with" events.
    const subs = [
      host.on('open-file', path => { if (path) openPath(path) }),
      host.on('clan-open-document', path => { if (path) openPath(path) }),
      host.on('clan-open-file-request', () => { handleOpenFile() }),
      host.on('clan-request-save', () => { saveCurrent() }),
      host.on('clan-export-request', async p => {
        if (!p) return
        const ext = p.kind === 'pdf' ? 'pdf' : 'html'
        const dest = await host.pickSaveDestination(p.filename, ext)
        if (!dest) return
        try {
          await host.finishExport(p.kind, p.tmpHtml, dest)
          setToast({ title: 'Exported', body: `Saved ${ext.toUpperCase()} to ${dest}` })
        } catch (err) {
          setToast({ title: 'Export failed', body: String(err) })
        }
        setTimeout(() => setToast(null), 5000)
      }),
      host.on('clan-title-changed', title => {
        if (title) setRunning(r => (r ? { ...r, open: { ...r.open, manifest: { ...r.open.manifest, title } } } : r))
      }),
      host.on('napkin-notify', n => {
        if (n) { setToast(n); setTimeout(() => setToast(null), 4000) }
      }),
      host.on('clan-theme-changed', colors => { applyTheme(colors) }),
    ]
    // A host that cannot be reached cannot subscribe either; that is not news.
    subs.forEach(s => s.catch(() => {}))
    return () => { subs.forEach(s => s.then(f => f()).catch(() => {})) }
  }, [device, openPath, handleOpenFile, saveCurrent])

  const goHome = useCallback(() => { openHome() }, [openHome])

  const onInstall = useCallback(async () => {
    if (!pendingLaunch) return
    try { await host.installApp(pendingLaunch.path); await refreshApps() } catch (e) { setError(String(e)) }
    setPendingLaunch(null); openHome()
  }, [pendingLaunch, refreshApps, openHome])

  const onRunNew = useCallback(async () => {
    if (!pendingLaunch?.manifest.app) return
    const appId = pendingLaunch.manifest.app.app_id
    try { await host.installApp(pendingLaunch.path) } catch { /* maybe already installed */ }
    setPendingLaunch(null)
    await launchApp(appId)
  }, [pendingLaunch, launchApp])

  const onViewTemplate = useCallback(async () => {
    if (!pendingLaunch) return
    const result = pendingLaunch
    setPendingLaunch(null)
    await runArtifact(nextOpen(), result)
  }, [pendingLaunch, runArtifact])

  // Apps: the home CLAN app when the host has one (as home always was), the
  // native launcher when it couldn't load.
  const apps = device && !serverless ? (
    <DeviceHome
      onChooseFile={chooseDeviceFile}
      onOpenCopy={openOfflineCopy}
      onBackToStudio={hasServer ? backToStudio : undefined}
      offline={unreachable}
    />
  ) : home ? (
    <AppRuntime
      htmlContent={home.html}
      hasHumanView={home.open.has_human_view}
      manifest={home.open.manifest}
      renderModel="authored"
      editMode={false}
    />
  ) : (
    <Launcher installed={installed} loading={loading} onLaunchApp={launchApp} onOpenFile={handleOpenFile} />
  )

  return (
    <>
      {error && (
        <div style={{ padding: 16, color: 'var(--danger)', fontFamily: 'monospace', background: '#0a0d14', borderBottom: '1px solid var(--border)' }}>
          <strong>Error:</strong> {error}{' '}
          <button onClick={() => setError(null)} style={{ marginLeft: 8, background: 'none', border: '1px solid var(--border)', color: 'var(--muted)', borderRadius: 4, cursor: 'pointer' }}>dismiss</button>
        </div>
      )}

      {screen === 'app' && running ? (
        <AppHost
          running={running}
          onHome={goHome}
          onOpenFile={handleOpenFile}
          onSave={saveCurrent}
          onKeepOffline={!device && hasServer && !running.open.is_template ? keepCurrentOffline : undefined}
          banner={device && !serverless ? (
            <DeviceBanner
              source={running.source ?? { kind: 'file', name: running.open.manifest.title }}
              onDownload={saveCurrent}
            />
          ) : undefined}
          onExport={exportCurrent}
          onSpinoff={spinOff}
        />
      ) : (
        <StudioShell
          tools={hasServer && !device ? (
            <button className="ch-btn" onClick={() => setOfflineOpen(true)} title="Documents kept on this device">
              Offline
            </button>
          ) : undefined}
        >
          {apps}
        </StudioShell>
      )}

      {offlineOpen && <OfflineDialog onOpen={openOfflineCopy} onClose={() => setOfflineOpen(false)} />}

      {dragging && (
        <div className="dv-drop" aria-hidden>
          <div>
            Drop a .clan to open it here
            <small>It stays on this device and is never uploaded.</small>
          </div>
        </div>
      )}

      {busy && (
        <div className="dv-update" role="status" style={{ left: '50%', transform: 'translateX(-50%)', paddingRight: 14 }}>
          <LogoSpinner size={16} label={busy} />
          {busy}
        </div>
      )}

      {updateReady && (
        <div className="dv-update" role="status">
          A new version is ready.
          <button className="ch-btn" onClick={applyUpdate}>Reload</button>
        </div>
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
