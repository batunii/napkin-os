// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import Toolbar from '../components/Toolbar'
import Sidebar from '../components/Sidebar'
import AppRuntime from './AppRuntime'
import WorkspaceView from './WorkspaceView'
import DecisionPanel from './decisions/DecisionPanel'
import { useDecisions } from './decisions/useDecisions'
import ClientReviewPanel from './clientReview/ClientReviewPanel'
import ClientReviewBand from './clientReview/ClientReviewBand'
import { assetNameOf, bodyOf, canReview, emptyDraft, knownClients, markedOf, nounOf, problemOf } from './clientReview/review'
import type { Draft, Mark } from './clientReview/review'
import { refreshApp } from './appExport'
import { host } from '../host'
import type { ClientPartRef, ClientWho } from '../host'
import { PoweredByClan } from '../brand/PoweredByClan'
import type { RunningApp } from './types'
import { docTitle } from './docTitle'
import '../components/chrome.css'
import './clientReview/ClientReview.css'

interface Props {
  running: RunningApp
  onHome: () => void
  onOpenFile: () => void
  onSave: () => void
  onKeepOffline?: () => void
  /** Shown under the toolbar — what a document open on the device says about itself. */
  banner?: ReactNode
  onExport: (kind: 'html' | 'pdf') => void
  onSpinoff: (appId: string) => void
  /** Each change is kept as it is made, so the bar may say "Saved" (Toolbar). */
  saved?: boolean
  /** Open another document of the store (the sidebar's "Made from" link). */
  onOpenDocument?: (path: string) => void
}

/** Chrome for one running app: toolbar + (collapsible) sidebar + render surface + panels. */
export default function AppHost({ running, onHome, onOpenFile, onSave, onKeepOffline, banner, onExport, onSpinoff, saved, onOpenDocument }: Props) {
  const [workspaceOpen, setWorkspaceOpen] = useState(false)
  const [sidebarOpen, setSidebarOpen] = useState(false) // collapsed by default
  // Edit mode is the OS's: the app's fields become editable while it is on.
  const [editMode, setEditMode] = useState(running.editMode)
  const { open } = running
  const docPath = open.path
  // The host's view of the document, read once: the panel renders it, and
  // the bar and client review read the lock and the client's answers from it.
  const decisions = useDecisions(docPath)
  const noun = nounOf(open.manifest.app?.app_id)

  // ── client review (OS-layer contract §7.5) ──────────────────────────────
  // The shell's second mode, like edit mode: on only while the document is
  // locked with nothing reopened; the app's fields mark the parts. What was
  // not saved is dropped when it ends. Each piece is kept against the
  // document it was for, so another document never sees it.
  const [review, setReview] = useState<{ docPath: string; draft: Draft; marks: Record<string, Mark>; known: ClientWho[] } | null>(null)
  const [declared, setDeclared] = useState<{ docPath: string; parts: ClientPartRef[] }>({ docPath, parts: [] })
  const [saving, setSaving] = useState<'no' | 'saving'>('no')
  const [saveError, setSaveError] = useState<string | null>(null)
  // What the host's matcher said on the last Save here: why it could not
  // look, or that it looked and no part was named.
  const [matched, setMatched] = useState<{ docPath: string; review: string; reason: string | null; none: boolean } | null>(null)
  // Who answered last, per document, for this session: the next review starts
  // from them, or from the client the document last recorded. Memory only.
  const lastWho = useRef(new Map<string, ClientWho>())

  const clientOn = review?.docPath === docPath
  const parts = useMemo(() => (declared.docPath === docPath ? declared.parts : []), [declared, docPath])
  const reviewable = canReview(decisions.view)

  const startReview = useCallback(() => {
    setEditMode(false)
    setSaveError(null)
    // The clients on record, offered as picks; the one the draft starts from is preselected.
    const known = knownClients(decisions.view, [...lastWho.current.values()])
    const who = lastWho.current.get(docPath) ?? decisions.view?.client?.answer?.client ?? known[0] ?? null
    setReview({ docPath, draft: emptyDraft(who), marks: {}, known })
  }, [docPath, decisions.view])

  const endReview = useCallback(() => {
    setReview(null)
    setSaveError(null)
  }, [])

  const onDraft = useCallback((change: (d: Draft) => Draft) => {
    setReview(r => (r ? { ...r, draft: change(r.draft) } : r))
  }, [])

  // Marks count only while the mode is on; one posted after it ended is dropped.
  const onPartMark = useCallback((m: { address: string; marked: boolean; words: string }) => {
    setReview(r => (r ? { ...r, marks: { ...r.marks, [m.address]: { marked: m.marked, words: m.words } } } : r))
  }, [])

  const onParts = useCallback((list: ClientPartRef[]) => setDeclared({ docPath, parts: list }), [docPath])

  // Something the host wrote changed the chain: read it again here, and have
  // the app's fields read it again too.
  const { reload } = decisions
  const changed = useCallback(() => {
    reload()
    refreshApp()
  }, [reload])

  const save = useCallback(async () => {
    if (!review || review.docPath !== docPath || problemOf(review.draft)) return
    const { draft, marks } = review
    setSaving('saving')
    setSaveError(null)
    try {
      // The client's file is stored only now, under a name of its own.
      let asset: string | undefined
      if (draft.answer !== 'accepted' && draft.how === 'file' && draft.file) {
        asset = (await host.uploadAsset(assetNameOf(draft.file.name, new Date()), draft.file.bytes)).internal_path
      }
      const reply = await host.clientReview(bodyOf(draft, parts, marks, asset))
      const email = draft.email.trim()
      lastWho.current.set(docPath, { name: draft.name.trim(), ...(email ? { email } : {}) })
      const sg = reply.suggestions
      setMatched(sg ? {
        docPath, review: reply.decision,
        reason: sg.status === 'unavailable' && sg.reason ? sg.reason : null,
        none: sg.status === 'found' && !sg.decisions?.length,
      } : null)
      setReview(null)
      changed()
    } catch (e) {
      setSaveError(String(e instanceof Error ? e.message : e))
    } finally {
      setSaving('no')
    }
  }, [review, docPath, parts, changed])

  // "Make this change" in the app reopened a part: it opens in edit mode.
  const onEditRequest = useCallback(() => {
    setReview(null)
    setEditMode(true)
  }, [])

  const marked = review ? markedOf(review.draft, parts, review.marks).length : 0
  const authored = open.render_model === 'authored'

  // ── glass chrome ──────────────────────────────────────────────────────────
  // An app may ask to be shown under glass (`clan:chrome`): the bar, footer
  // and rail lie over it, frosted, so the document shows through them, and it
  // is told what they cover. Client review keeps the solid layout (its panel
  // and band are part of the page's flow).
  const [chromeMode, setChromeMode] = useState<'glass' | 'solid'>('solid')
  // The glass takes the document's own ground as its tint, when the app gives
  // one the chrome's ink still reads on (4.5:1); never any other colour.
  const [glassTint, setGlassTint] = useState<string | null>(null)
  useEffect(() => { setChromeMode('solid'); setGlassTint(null) }, [docPath])
  const onChrome = useCallback((mode: 'glass' | 'solid', tint: string | null) => {
    setChromeMode(mode)
    setGlassTint(tint && readsWithChromeInk(tint) ? tint : null)
  }, [])
  const glass = chromeMode === 'glass' && !clientOn
  const topRef = useRef<HTMLDivElement>(null)
  const workRef = useRef<HTMLDivElement>(null)
  const footRef = useRef<HTMLElement>(null)
  const [insets, setInsets] = useState<{ top: number; right: number; bottom: number } | null>(null)
  useEffect(() => {
    if (!glass) { setInsets(null); return }
    const measure = () => {
      const top = topRef.current?.getBoundingClientRect().height ?? 0
      const bottom = footRef.current?.getBoundingClientRect().height ?? 0
      // The rail (or the open panel) on the right; on a narrow screen it floats
      // in the footer instead, and covers nothing.
      const side = workRef.current?.querySelector(':scope > .dp-rail, :scope > .dp-panel') as HTMLElement | null
      const right = side && getComputedStyle(side).position === 'absolute' ? side.getBoundingClientRect().width : 0
      setInsets(i => (i && i.top === Math.round(top) && i.right === Math.round(right) && i.bottom === Math.round(bottom)
        ? i : { top: Math.round(top), right: Math.round(right), bottom: Math.round(bottom) }))
    }
    measure()
    const ro = new ResizeObserver(measure)
    if (topRef.current) ro.observe(topRef.current)
    if (footRef.current) ro.observe(footRef.current)
    if (workRef.current) ro.observe(workRef.current)
    const mo = new MutationObserver(() => {
      measure()
      workRef.current?.querySelectorAll(':scope > .dp-rail, :scope > .dp-panel').forEach(el => ro.observe(el))
    })
    if (workRef.current) mo.observe(workRef.current, { childList: true })
    workRef.current?.querySelectorAll(':scope > .dp-rail, :scope > .dp-panel').forEach(el => ro.observe(el))
    window.addEventListener('resize', measure)
    return () => { ro.disconnect(); mo.disconnect(); window.removeEventListener('resize', measure) }
  }, [glass])

  return (
    <div className={glass ? 'ch-doc ch-doc-glass' : 'ch-doc'} style={{ display: 'flex', flexDirection: 'column', height: '100vh',
      ...(insets ? { ['--glass-top' as string]: insets.top + 'px', ['--glass-bottom' as string]: insets.bottom + 'px' } : {}),
      ...(glass && glassTint ? { ['--glass-tint' as string]: glassTint } : {}) }}>
      <div className="ch-top" ref={topRef}>
      {/* Accent strip — recolors with a trusted app's theme (clan://set-theme). */}
      <div className="ch-strip" />
      <Toolbar
        title={open.is_template ? open.manifest.title : docTitle(open.manifest.title, open.manifest.app?.app_id, open.manifest.app?.name).text}
        isTemplate={open.is_template}
        trusted={open.trusted}
        onHome={onHome}
        onOpenFile={onOpenFile}
        onToggleSidebar={() => setSidebarOpen(o => !o)}
        sidebarOpen={sidebarOpen}
        onWorkspace={() => setWorkspaceOpen(true)}
        onSave={onSave}
        onKeepOffline={onKeepOffline}
        onExport={onExport}
        docPath={open.path}
        onSpinoff={onSpinoff}
        loading={false}
        validation={open.validation}
        saved={saved}
        editMode={editMode}
        // A locked document with nothing reopened takes no edit: Client
        // review stands where Edit was.
        onToggleEdit={authored && (editMode || !reviewable) ? () => setEditMode(e => !e) : undefined}
        onClientReview={reviewable && !open.is_template ? startReview : undefined}
        clientAnswered={!!decisions.view?.client?.answer?.current}
        clientMode={clientOn ? { onCancel: endReview, busy: saving !== 'no' } : undefined}
      />
      {banner}
      </div>
      <div className={`ch-work ${clientOn ? 'ch-work-review' : ''}`} ref={workRef}>
        {sidebarOpen && <Sidebar manifest={open.manifest} path={open.path} onOpenDocument={onOpenDocument} />}
        <main className="ch-work-main">
          {!clientOn && decisions.view && (
            <ClientReviewBand
              view={decisions.view}
              noun={noun}
              matched={matched?.docPath === docPath ? matched : null}
              onChanged={changed}
            />
          )}
          <AppRuntime
            htmlContent={running.htmlContent}
            hasHumanView={open.has_human_view}
            manifest={open.manifest}
            renderModel={authored ? 'authored' : 'legacy'}
            editMode={editMode}
            clientMode={{ on: clientOn, answer: clientOn ? review!.draft.answer : null }}
            onParts={onParts}
            onPartMark={onPartMark}
            onEditRequest={onEditRequest}
            onChrome={onChrome}
            insets={insets}
          />
        </main>
        {clientOn ? (
          <ClientReviewPanel
            draft={review!.draft}
            onDraft={onDraft}
            parts={parts}
            marked={marked}
            known={review!.known}
            noun={noun}
            saving={saving}
            error={saveError}
            onSave={save}
          />
        ) : (
          <DecisionPanel decisions={decisions} />
        )}
      </div>
      <footer className="ch-footer" ref={footRef}>
        <PoweredByClan />
      </footer>
      {workspaceOpen && <WorkspaceView manifest={open.manifest} onClose={() => setWorkspaceOpen(false)} />}
    </div>
  )
}

/** Whether the chrome's own ink reads on `hex` (WCAG contrast 4.5:1 or more). */
function readsWithChromeInk(hex: string): boolean {
  const ink = getComputedStyle(document.documentElement).getPropertyValue('--ink').trim()
  const lum = (h: string) => {
    const m = /^#?([0-9a-f]{6})$/i.exec(h)
    if (!m) return null
    const [r, g, b] = [0, 2, 4].map(i => {
      const v = parseInt(m[1].slice(i, i + 2), 16) / 255
      return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4
    })
    return 0.2126 * r + 0.7152 * g + 0.0722 * b
  }
  const a = lum(hex), b = lum(ink)
  if (a == null || b == null) return false
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05) >= 4.5
}
