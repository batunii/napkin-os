// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

import { useCallback, useMemo, useRef, useState, type ReactNode } from 'react'
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
}

/** Chrome for one running app: toolbar + (collapsible) sidebar + render surface + panels. */
export default function AppHost({ running, onHome, onOpenFile, onSave, onKeepOffline, banner, onExport, onSpinoff, saved }: Props) {
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

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100vh' }}>
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
      <div className={`ch-work ${clientOn ? 'ch-work-review' : ''}`}>
        {sidebarOpen && <Sidebar manifest={open.manifest} path={open.path} />}
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
      <footer className="ch-footer">
        <PoweredByClan />
      </footer>
      {workspaceOpen && <WorkspaceView manifest={open.manifest} onClose={() => setWorkspaceOpen(false)} />}
    </div>
  )
}
