// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// After a client's answer is recorded: what they said, above the document.
// The banner (the answer, who, when, the evidence strength), Jude on what is
// left to do, and — when the answer did not name its parts — Ellis's
// suggestions, each confirmed or turned down by a person, or the parts
// marked by hand. Only a confirmed part counts (OS-layer contract §7.5.2).
// The suggestions are the host's own matcher (the parts whose names the
// client's sentences use), recorded as that process and shown as Ellis's;
// they arrive with the Save's reply, so there is nothing to wait for.
//
// Everything here reads the host's view (`/decisions`, `client`); each
// answer is one host route and the view is read again after it.

import { useState } from 'react'
import { host } from '../../host'
import type { ClientSuggestionView, DecisionsView } from '../../host'
import { AgentFigure } from '../../studio/AgentFigure'
import { AGENTS } from '../../studio/model'
import { Speak } from './ClientReviewPanel'
import { afterLine, bannerWords, firstName, reasonWords, recordedFrom, settledSuggestions } from './review'

interface Props {
  view: DecisionsView
  noun: string
  /**
   * What the host's matcher said on the last Save: why it could not look
   * (`reason`, in the host's words), or that it looked and no part was named
   * (`none`). For the answer `review` only.
   */
  matched: { review: string; reason: string | null; none: boolean } | null
  /** The host wrote something: read the view again, and tell the app. */
  onChanged: () => void
}

const DAY = new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'short' })
const dayOf = (ts: string) => { const t = Date.parse(ts); return Number.isNaN(t) ? '' : DAY.format(t) }

const PART_WORDS: Record<string, string> = {
  accepted: 'accepted', accepted_with_changes: 'needs a change', rejected: 'rejected',
}

export default function ClientReviewBand({ view, noun, matched, onChanged }: Props) {
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [folded, setFolded] = useState(false)
  const c = view.client
  const a = c?.answer ?? null
  const mine = matched && matched.review === a?.decision ? matched : null
  const say = afterLine(view, noun, !!mine?.none)
  // An answer to an earlier lock is history, in "What happened"; the band is
  // for the version locked now.
  if (!c || !a || !a.current) return null

  const act = (key: string, run: () => Promise<unknown>) => {
    setBusy(key); setError(null)
    run().then(onChanged, e => setError(String(e instanceof Error ? e.message : e))).finally(() => setBusy(null))
  }

  const you = !!view.decisions.find(b => b.decision.id === a.decision)?.who.you
  const who = firstName(a.client.name) || 'The client'
  const suggestions = c.suggestions.filter(s => s.review === a.decision)
  // Answered ones stay in the list, dimmed, with what was said to them.
  const done = settledSuggestions(view, a.decision)
  // Each in the order Ellis made them, answered or not (the list is a flex column).
  const orderOf = (at: number) => ({ order: view.decisions.length - at })
  const atOf = (id: string) => view.decisions.findIndex(b => b.decision.id === id)
  // Parts the client saw that this answer has not settled yet, for marking by hand.
  const settled = new Set([
    ...c.parts.filter(p => p.review === a.decision && p.found_by !== 'document').map(p => p.address),
    ...suggestions.map(s => s.address),
  ])
  const unmarked = a.answer === 'accepted' ? [] : a.seen.parts.filter(p => !settled.has(p.address))
  const tone = a.answer === 'accepted' ? 'ok' : a.answer === 'rejected' ? 'rj' : 'chg'

  return (
    <section className="cr-band" aria-label="The client’s answer">
      <div className={`cr-banner cr-banner-${tone}`}>
        <b>{bannerWords(a.answer, noun)}</b>
        <span>{a.client.name} · {dayOf(a.at)} · {a.current ? 'on the locked version' : 'on an earlier version'}</span>
        <span className={`cr-strength ${a.evidence.strength === 'strong' ? '' : 'cr-strength-weak'}`}>
          <i aria-hidden />{recordedFrom(a, you)}
        </span>
        {a.reasons?.length ? <span>Reason: {reasonWords(a.reasons)}</span> : null}
        <button type="button" className="cr-link cr-banner-fold" aria-expanded={!folded} onClick={() => setFolded(f => !f)}>
          {folded ? 'Show' : 'Hide'}
        </button>
      </div>

      {!folded && (
        <>
          {a.said && (
            <details className="cr-said">
              <summary>{a.channel === 'call' ? `${you ? 'Your' : 'The'} note of the call` : `What ${who} said`}</summary>
              {/* Verbatim: as it was recorded, line breaks and all. */}
              <blockquote>{a.said}</blockquote>
            </details>
          )}

          {say && <Speak say={say} />}
          {mine?.reason && !suggestions.length && !a.parts_known && a.answer !== 'accepted' && (
            <p className="cr-hint">{AGENTS.extract.given} could not look for the parts this time: {mine.reason}</p>
          )}

          {suggestions.length + done.length > 0 && (
            <div className="cr-ellis">
              <AgentFigure agent="extract" size={40} />
              <div>
                <h3>{AGENTS.extract.given}: I think {who} meant these parts</h3>
                <p>{suggestions.length ? 'Each is only a suggestion until you say yes.' : 'You’ve answered each suggestion.'}</p>
                <ul>
                  {done.map(s => (
                    <li key={s.suggestion} className="cr-done" style={orderOf(s.at)}>
                      <b>{s.label}</b>
                      <span>{PART_WORDS[s.answer] ?? s.answer}</span>
                      <span className="cr-sp" />
                      <span className="cr-done-tag">{s.confirmed ? '✓ Confirmed' : 'Not this part'}</span>
                      {s.quote && <q>{s.quote}</q>}
                    </li>
                  ))}
                  {suggestions.map((s: ClientSuggestionView) => (
                    <li key={s.decision} style={orderOf(atOf(s.decision))}>
                      <b>{s.label}</b>
                      <span>{PART_WORDS[s.answer] ?? s.answer}</span>
                      <span className="cr-sp" />
                      <button
                        type="button"
                        className="cr-btn"
                        disabled={!!busy}
                        onClick={() => act(s.decision, () => host.clientReviewConfirm({ suggestion: s.decision, confirm: true }))}
                      >
                        {busy === s.decision ? 'Saving…' : 'Yes, this part'}
                      </button>
                      <button
                        type="button"
                        className="cr-btn cr-btn-quiet"
                        disabled={!!busy}
                        onClick={() => act(`${s.decision}:no`, () => host.clientReviewConfirm({ suggestion: s.decision, confirm: false }))}
                      >
                        Not this part
                      </button>
                      <q>{s.quote}</q>
                    </li>
                  ))}
                </ul>
              </div>
            </div>
          )}

          {unmarked.length > 0 && (
            <details className="cr-mark" open={!a.parts_known && !suggestions.length}>
              <summary>{a.parts_known ? `Mark another part ${who} meant` : `Which parts did ${who} mean?`}</summary>
              <div className="cr-reasons">
                {unmarked.map(p => (
                  <button
                    key={p.address}
                    type="button"
                    disabled={!!busy}
                    onClick={() => act(p.address, () => host.clientReviewConfirm({ review: a.decision, address: p.address, answer: a.answer }))}
                  >
                    {busy === p.address ? 'Saving…' : `+ ${p.label}`}
                  </button>
                ))}
              </div>
            </details>
          )}
          {error && <p className="cr-err" role="alert">{error}</p>}
        </>
      )}
    </section>
  )
}
