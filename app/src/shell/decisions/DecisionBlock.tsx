// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// The pieces of the decision panel: a "Needs you" card, a "Worth a look"
// item, and one line of history that opens to the decision itself.

import type { AttentionItem, DecisionBlock, DecisionsView } from '../../host'
import { AgentFigure } from '../../studio/AgentFigure'
import { openInApp } from '../appExport'
import {
  chipOf, citesOf, didWhat, hhmm, mainTarget, opensInApp, plain, refOfAddress, sourcesOf, sureWord, whoOf,
} from './words'

/** The agent's figure, or a person's initial. */
export function Face({ block, size = 22, needs = false }: { block?: DecisionBlock; size?: number; needs?: boolean }) {
  const w = block ? whoOf(block) : null
  if (w?.agent) return <AgentFigure agent={w.agent} size={size} state={needs ? 'needs-you' : 'idle'} />
  return <span className="dp-av" aria-hidden>{(w?.name[0] ?? '?').toUpperCase()}</span>
}

const TITLE: Record<string, string> = {
  unverified_finding: 'A finding is waiting for your check',
  open_contest: 'Two sources disagree',
  flagged_field: 'This relies on a finding that was rejected',
  bad_verdict: 'Marked wrong, and not answered yet',
  unmerged_branch: 'Work not merged yet',
}

const WHERE = (
  <svg width="11" height="11" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden>
    <path d="M2 6h7M6.5 3 9.5 6l-3 3" />
  </svg>
)

/** One thing that blocks the lock, and where it is settled. */
export function NeedsCard({ item, block }: { item: AttentionItem; block?: DecisionBlock }) {
  const ref = refOfAddress(item.address)
  const path = String(item.address ?? '').split('#')[1]
  const who = block ? whoOf(block) : null
  const statement = item.code === 'unverified_finding' || item.code === 'flagged_field'
    ? (item.label ?? '').replace(/^(Finding|Fact|Contest) · /, '') : (item.label ?? '')
  const raisedBy = block
    ? `${who!.name} ${block.decision.kind === 'finding' ? 'proposed it' : 'raised it'}${sureWord(block.decision.reasoning?.certainty?.level) === 'Unsure' ? ', unsure' : ''}`
    : null
  return (
    <div className="dp-card">
      <div className="dp-card-top">
        <Face block={block} size={30} needs />
        <div>
          <div className="dp-card-what">{TITLE[item.code] ?? 'Needs you'}</div>
          {raisedBy && <div className="dp-card-who">{raisedBy}</div>}
        </div>
      </div>
      {statement && <div className="dp-stmt"><span className="dp-clip">{statement}</span></div>}
      {item.code === 'flagged_field' && <p className="dp-why">{item.text}</p>}
      <div className="dp-acts">
        {item.address && <button className="dp-btn dp-btn-primary" onClick={() => openInApp(ref, path)}>Open it</button>}
        <span className="dp-hint">{item.code === 'open_contest' ? 'you’ll pick one against its sources' : item.code === 'unverified_finding' ? 'you’ll check it against its sources' : 'settle it there'}</span>
      </div>
    </div>
  )
}

/** Something an agent flagged. Accepting the call is recorded; the value opens in the app. */
export function QuietItem({ block, text, busy, onLooksRight }: {
  block: DecisionBlock; text: string; busy: boolean; onLooksRight: (id: string) => void
}) {
  const w = whoOf(block)
  const t = mainTarget(block)
  const valued = t && (t.kind === 'fact' || t.kind === 'finding' || t.kind === 'contest')
  return (
    <div className="dp-qi">
      <Face block={block} />
      <div>
        <div className="dp-qi-t"><b>{w.name}</b>: {text}</div>
        <div className="dp-acts">
          {block.decision.id && (
            <button className="dp-btn" disabled={busy} onClick={() => onLooksRight(block.decision.id!)}>{busy ? 'Recording…' : 'Looks right'}</button>
          )}
          {t && (valued
            ? <button className="dp-btn" onClick={() => openInApp(refOfAddress(t.address), t.path)}>Open it</button>
            : <button className="dp-link" onClick={() => openInApp(refOfAddress(t.address), t.path)}>Show in document</button>)}
        </div>
      </div>
    </div>
  )
}

/** One line of history: who · did what · where · when; opens to why. */
export function HistoryLine({ block, view }: { block: DecisionBlock; view: DecisionsView }) {
  const d = block.decision
  const r = d.reasoning
  const w = whoOf(block)
  const t = mainTarget(block)
  const sure = sureWord(r?.certainty?.level)
  const cites = citesOf(block)
  const sources = sourcesOf(block, view)
  const facts = cites.filter(c => view.cites[c]?.kind === 'fact').length
  const findings = cites.filter(c => view.cites[c]?.kind === 'finding').length
  const materials = cites.filter(c => view.cites[c]?.kind === 'material')
  const hasMore = !!(r || d.rationale || cites.length)
  return (
    <details className={`dp-ev ${block.superseded ? 'dp-ev-old' : ''}`}>
      <summary>
        <Face block={block} />
        <div>
          <div className="dp-line"><b>{w.name}</b> {didWhat(block, view)}</div>
          <div className="dp-meta">
            {t && (
              <button className="dp-where" onClick={e => { e.preventDefault(); openInApp(refOfAddress(t.address), t.path) }}>
                {WHERE}{t.label.replace(/^(Finding|Fact) · /, (_, k: string) => `${k} · `).slice(0, 60)}
              </button>
            )}
            {sure === 'Unsure' && <span className="dp-pill-unsure">unsure</span>}
            {block.superseded && <span className="dp-pill-old">replaced since</span>}
          </div>
        </div>
        <span className="dp-time">{hhmm(d.timestamp)}{hasMore && <span className="dp-more">Why</span>}</span>
      </summary>
      {hasMore && (
        <div className="dp-ex">
          <p className="dp-decided">{plain(r?.decided || d.rationale.split(/\. Because/)[0])}</p>
          {r?.because?.length ? (
            <div>
              <h4>Why</h4>
              <ul>
                {r.because.map((p, i) => (
                  <li key={i}>
                    {plain(p.point)}
                    <Chips ids={p.cites ?? []} view={view} />
                  </li>
                ))}
              </ul>
            </div>
          ) : d.rationale.includes('. Because') ? (
            <div><h4>Why</h4><p>{plain(d.rationale.split('. Because: ')[1] ?? '')}</p></div>
          ) : null}
          {(facts + findings + sources.length + materials.length) > 0 && (
            <div>
              <h4>From</h4>
              {materials.map(m => (
                <div className="dp-row dp-row-static" key={m}>
                  <span>{/prompt|request/i.test(view.cites[m].label) ? 'Your request' : view.cites[m].label}
                    {view.cites[m].quote && <small>“{view.cites[m].quote}”</small>}</span>
                </div>
              ))}
              {(facts + findings) > 0 && (
                <details className="dp-from">
                  <summary className="dp-row">
                    <span>{[facts && `${facts} fact${facts === 1 ? '' : 's'}`, findings && `${findings} finding${findings === 1 ? '' : 's'}`].filter(Boolean).join(' and ')}
                      <small>{sources.length ? `from ${sources.length} source${sources.length === 1 ? '' : 's'}` : 'none of their sources are recorded in this document'}</small></span>
                    <b className="dp-show">Show</b>
                  </summary>
                  {sources.map(s => (
                    <button className="dp-row" key={s.id} onClick={() => openInApp(s.id)}>
                      <span>{s.cite.title ?? s.cite.label}
                        <small>{[s.cite.publisher, s.cite.tier, s.cite.published_at && `published ${s.cite.published_at.slice(0, 4)}`, s.facts && `${s.facts} fact${s.facts === 1 ? '' : 's'}`].filter(Boolean).join(' · ')}</small></span>
                      <b>›</b>
                    </button>
                  ))}
                  {cites.filter(c => view.cites[c]?.kind === 'finding').map(c => (
                    <button className="dp-row" key={c} onClick={() => openInApp(c)}>
                      <span>{view.cites[c].label}<small>a finding · {view.cites[c].detail}</small></span><b>›</b>
                    </button>
                  ))}
                </details>
              )}
            </div>
          )}
          {r?.rejected?.length ? (
            <div><h4>Considered and set aside</h4><ul>{r.rejected.map((x, i) => <li key={i}>{plain(x.option)}{x.why && <span className="dp-muted"> — {plain(x.why)}</span>}</li>)}</ul></div>
          ) : null}
          {sure && <p><b>{sure}</b>{r?.certainty?.why ? ` · ${plain(r.certainty.why)}` : ''}</p>}
          {t && <div className="dp-acts"><button className="dp-link" onClick={() => openInApp(refOfAddress(t.address), t.path)}>Show in document</button></div>}
          <details className="dp-tech">
            <summary>Technical details</summary>
            <dl>
              <dt>step</dt><dd>{d.kind ?? 'edit'} · {d.action}</dd>
              {d.handler && <><dt>handler</dt><dd>{d.handler}</dd></>}
              {d.backend && <><dt>backend</dt><dd>{d.backend}</dd></>}
              {d.actor && <><dt>actor</dt><dd>{d.actor}</dd></>}
              {d.id && <><dt>decision</dt><dd>{d.id}</dd></>}
              <dt>time</dt><dd>{d.timestamp}</dd>
              {d.targets?.length ? <><dt>targets</dt><dd>{d.targets.join(', ')}</dd></> : null}
            </dl>
          </details>
        </div>
      )}
    </details>
  )
}

/** What a reason rests on, as chips — at most three, then how many more. */
function Chips({ ids, view }: { ids: string[]; view: DecisionsView }) {
  const named = ids.map(id => ({ id, text: chipOf(id, view) })).filter(x => x.text)
  if (!named.length) return null
  const shown = named.slice(0, 3)
  return (
    <span className="dp-chips">
      {shown.map(x => opensInApp(x.id, view)
        ? <button key={x.id} className="dp-chip" onClick={() => openInApp(x.id)}>{x.text}</button>
        : <span key={x.id} className="dp-chip dp-chip-static">{x.text}</span>)}
      {named.length > 3 && <span className="dp-chip dp-chip-static">+{named.length - 3} more</span>}
    </span>
  )
}
