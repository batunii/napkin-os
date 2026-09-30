// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// Client review, while the mode is on: the whole document's answer first
// (the owner's prototype, version 4). Accepted is one tap and Save. Accepted
// with changes and Rejected open an optional evidence area — the email
// pasted, the email or PDF attached, or a note of a call — and, for a
// rejection, why. Parts are marked on the document itself, by the app's
// fields; this panel only counts them. Nothing here is stored until Save,
// and leaving the mode drops it.

import { useId, useState } from 'react'
import type { ClientPartRef, ClientWho } from '../../host'
import { AgentFigure } from '../../studio/AgentFigure'
import { AGENTS } from '../../studio/model'
import {
  ANSWERS, EVIDENCE_TYPES, REASONS, isEvidenceFile, modeLine, problemOf, strengthLine, evidenceOf,
} from './review'
import type { Draft, HeldFile, How, Say } from './review'

/** Larger than any client's email; a file this size is not what the record is for. */
const MAX_FILE = 20 * 1024 * 1024

interface Props {
  draft: Draft
  onDraft: (change: (d: Draft) => Draft) => void
  /** The parts the app declared, and how many are marked. */
  parts: readonly ClientPartRef[]
  marked: number
  /** Clients already on record, most recent first (knownClients). */
  known: readonly ClientWho[]
  noun: string
  /** The parts the words name are found by the host in the same reply: nothing else to wait for. */
  saving: 'no' | 'saving'
  error: string | null
  onSave: () => void
}

/** A crew member speaking, as the apps draw it: the figure, then the bubble. */
export function Speak({ say }: { say: Say }) {
  const a = AGENTS[say.agent]
  return (
    <div className="cr-speak" aria-live="polite">
      <AgentFigure agent={say.agent} size={44} state={say.state} />
      <p className="cr-say"><span className="cr-who">{a.given}</span>{say.text}</p>
    </div>
  )
}

const HOW: readonly { how: How; label: string }[] = [
  { how: 'email', label: 'Paste their email' },
  { how: 'file', label: 'Add the email or PDF file' },
  { how: 'call', label: 'Nothing: a call or a chat' },
]

export default function ClientReviewPanel({ draft, onDraft, parts, marked, known, noun, saving, error, onSave }: Props) {
  const id = useId()
  // "Someone else…": the name is typed. Otherwise a known client is picked.
  const [other, setOther] = useState(false)
  const picked = known.find(k => k.name.toLowerCase() === draft.name.trim().toLowerCase())
  const typing = !known.length || other || !picked
  // At phone width the panel folds to one line and Save, so the document gets
  // the screen while parts are marked on it (the fold only shows there).
  const [folded, setFolded] = useState(false)
  const picks = ANSWERS.find(a => a.kind === draft.answer)
  const [fileError, setFileError] = useState<string | null>(null)
  const [dragging, setDragging] = useState(false)
  const busy = saving !== 'no'
  const set = (patch: Partial<Draft>) => onDraft(d => ({ ...d, ...patch }))
  const problem = problemOf(draft)
  const open = draft.answer === 'accepted_with_changes' || draft.answer === 'rejected'
  const strong = evidenceOf(draft).strong

  // The file is read into memory here and stored only at Save.
  const take = (file: File | undefined) => {
    setFileError(null)
    if (!file) return
    if (!isEvidenceFile(file.name)) { setFileError('Add the email as an .eml file, or a PDF.'); return }
    if (file.size > MAX_FILE) { setFileError('That file is too big to keep in the document (over 20 MB).'); return }
    file.arrayBuffer().then(
      buf => set({ file: { name: file.name, bytes: new Uint8Array(buf) } satisfies HeldFile }),
      () => setFileError('That file could not be read. Try it again.'),
    )
  }

  const say = modeLine(draft, noun)

  return (
    <aside className={`cr-panel${folded ? ' cr-folded' : ''}`} aria-labelledby={`${id}-q`}>
      <div className="cr-scroll">
        <div className="cr-fold cr-keep">
          {folded && (
            <p className="cr-fold-sum">
              <b>{picks ? picks.title : 'No answer picked yet'}</b>
              {open && ` · ${marked} part${marked === 1 ? '' : 's'} marked`}
            </p>
          )}
          <button type="button" aria-expanded={!folded} aria-controls={`${id}-q`} onClick={() => setFolded(f => !f)}>
            {folded ? 'Show the answer' : open ? 'Fold this away to mark parts' : 'Fold this away'}
          </button>
        </div>
        <Speak say={say} />

        <h2 id={`${id}-q`} className="cr-q">What did the client say about the {noun}?</h2>
        <div className="cr-answers" role="group" aria-label="The client’s answer">
          {ANSWERS.map(a => (
            <button
              key={a.kind}
              type="button"
              className="cr-answer"
              data-kind={a.kind}
              aria-pressed={draft.answer === a.kind}
              disabled={busy}
              onClick={() => set({ answer: a.kind })}
            >
              <b>{a.title}</b>
              <span>{a.sub}</span>
            </button>
          ))}
        </div>

        {open && (
          <div className="cr-sub">
            <h3>{draft.answer === 'rejected' ? 'Why did they reject it? Add what they sent, if anything.' : 'What did they want changed? Add what they sent, if anything.'}</h3>
            <div className="cr-how" role="group" aria-label="What the client sent">
              {HOW.map(h => (
                <button key={h.how} type="button" aria-pressed={draft.how === h.how} disabled={busy} onClick={() => set({ how: h.how })}>
                  {h.label}
                </button>
              ))}
            </div>

            {draft.how === 'email' && (
              <div className="cr-field">
                <label htmlFor={`${id}-email`}>Their email, as they sent it</label>
                <textarea
                  id={`${id}-email`}
                  rows={5}
                  value={draft.pasted}
                  disabled={busy}
                  onChange={e => set({ pasted: e.target.value })}
                  placeholder={`Paste the client’s reply. It stays in the document, word for word, and ${AGENTS.extract.given} looks in it for the parts it names.`}
                />
              </div>
            )}

            {draft.how === 'file' && (
              <div className="cr-field">
                <input
                  id={`${id}-file`}
                  type="file"
                  accept={EVIDENCE_TYPES}
                  className="cr-file-input"
                  disabled={busy}
                  onChange={e => { take(e.target.files?.[0]); e.target.value = '' }}
                />
                {draft.file ? (
                  <div className="cr-drop cr-drop-got">
                    <span>{draft.file.name}<small>kept in the document when you save</small></span>
                    <button type="button" className="cr-link" disabled={busy} onClick={() => set({ file: null })}>Remove</button>
                  </div>
                ) : (
                  <label
                    htmlFor={`${id}-file`}
                    className={`cr-drop ${dragging ? 'cr-drop-over' : ''}`}
                    onDragOver={e => { e.preventDefault(); setDragging(true) }}
                    onDragLeave={() => setDragging(false)}
                    onDrop={e => { e.preventDefault(); setDragging(false); take(e.dataTransfer.files?.[0]) }}
                  >
                    Drop the email (.eml) or the marked-up PDF here, or tap to choose it
                  </label>
                )}
                {fileError && <p className="cr-err" role="alert">{fileError}</p>}
              </div>
            )}

            {draft.how === 'call' && (
              <div className="cr-field">
                <label htmlFor={`${id}-note`}>What they said, and who was there</label>
                <textarea
                  id={`${id}-note`}
                  rows={3}
                  value={draft.note}
                  disabled={busy}
                  onChange={e => set({ note: e.target.value })}
                  placeholder="For example: call with Mary, the proposition is flat and the budget should be 400k"
                />
              </div>
            )}

            {draft.answer === 'rejected' && (
              <div className="cr-field">
                <span className="cr-label" id={`${id}-why`}>Why, if you know</span>
                <div className="cr-reasons" role="group" aria-labelledby={`${id}-why`}>
                  {REASONS.map(r => {
                    const on = draft.reasons.includes(r.code)
                    return (
                      <button
                        key={r.code}
                        type="button"
                        aria-pressed={on}
                        disabled={busy}
                        onClick={() => set({ reasons: on ? draft.reasons.filter(x => x !== r.code) : [...draft.reasons, r.code] })}
                      >
                        {r.label}
                      </button>
                    )
                  })}
                </div>
              </div>
            )}

            <p className="cr-hint">
              {parts.length
                ? <>Know which parts? Mark them on the {noun}. If not, leave it: {AGENTS.extract.given} will look for the parts their words name.
                  {marked > 0 && <b> {marked} part{marked === 1 ? '' : 's'} marked.</b>}</>
                : <>This {noun} names no parts, so the answer is for the whole of it.</>}
            </p>
          </div>
        )}

        {known.length > 0 && (
          <div className="cr-field">
            <span className="cr-label" id={`${id}-who`}>Who answered</span>
            <div className="cr-reasons cr-who-pick" role="group" aria-labelledby={`${id}-who`}>
              {known.map(k => (
                <button
                  key={k.name}
                  type="button"
                  aria-pressed={!other && picked === k}
                  disabled={busy}
                  onClick={() => { setOther(false); set({ name: k.name, email: k.email ?? '' }) }}
                >
                  {k.name}
                </button>
              ))}
              <button
                type="button"
                aria-pressed={typing}
                disabled={busy}
                onClick={() => { setOther(true); if (picked) set({ name: '', email: '' }) }}
              >
                Someone else…
              </button>
            </div>
          </div>
        )}
        {typing && <div className="cr-row">
          <div className="cr-field">
            <label htmlFor={`${id}-name`}>{known.length ? 'Their name' : 'Who answered'}</label>
            <input
              id={`${id}-name`}
              type="text"
              autoComplete="off"
              maxLength={120}
              value={draft.name}
              disabled={busy}
              onChange={e => set({ name: e.target.value })}
              placeholder="Their name"
            />
          </div>
          <div className="cr-field">
            <label htmlFor={`${id}-mail`}>Their email <small>if you have it</small></label>
            <input
              id={`${id}-mail`}
              type="email"
              autoComplete="off"
              value={draft.email}
              disabled={busy}
              onChange={e => set({ email: e.target.value })}
              placeholder="name@client.com"
            />
          </div>
        </div>}
        <p className="cr-version">They answered the locked version.</p>

        <p className={`cr-strength ${strong ? '' : 'cr-strength-weak'}`}><i aria-hidden />{strengthLine(draft)}</p>

        {error && <p className="cr-err cr-keep" role="alert">{error}</p>}
        <div className="cr-save cr-keep">
          {problem && draft.answer && <span className="cr-hint">{problem}</span>}
          <button type="button" className="cr-btn-go" disabled={busy || !!problem} onClick={onSave}>
            {saving === 'saving' ? 'Saving…'
              : draft.answer === 'accepted' ? 'Save: the client accepted it' : draft.answer ? 'Save the client’s answer' : 'Save'}
          </button>
        </div>
      </div>
    </aside>
  )
}
