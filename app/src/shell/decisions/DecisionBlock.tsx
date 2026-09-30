// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// The pieces of the decision panel: a "Needs you" card, a "Worth a look"
// item, and one line of history that opens to the decision itself. Every
// line is signed the way the crew signs in the apps: the figure, then the
// name and the job ("Jude · checks"); a person's line is "You" or their name.

import type { AttentionItem, DecisionBlock, DecisionsView } from '../../host'
import { AgentFigure } from '../../studio/AgentFigure'
import { AGENTS } from '../../studio/model'
import { canRun, openInApp, runInApp } from '../appExport'
import {
  changeOf, chipOf, citesOf, clientLineOf, didWhat, hhmm, mainTarget, opensInApp, plain, redoOf, refOfAddress, signOf, skippedOf,
  sourcesOf, sureWord, upper, whereOf, whoOf, withCountry,
} from './words'
import type { WhoIs } from './words'

/** The agent's figure, or a person's initial. */
export function Face({ block, size = 30, needs = false }: { block?: DecisionBlock; size?: number; needs?: boolean }) {
  const w = block ? whoOf(block) : null
  if (w?.agent) return <span className="dp-fig" aria-hidden><AgentFigure agent={w.agent} size={size} state={needs ? 'needs-you' : 'idle'} /></span>
  return <span className="dp-av" aria-hidden>{(w?.name[0] ?? '?').toUpperCase()}</span>
}

/**
 * Who says it, as the crew signs its bubbles in the apps: "Jude · checks".
 * A person has no job to add, and their line already starts "You…".
 */
function Sign({ who }: { who: WhoIs }) {
  if (who.person) return null
  return <span className="dp-sign">{signOf(who)}</span>
}

/** Each "Needs you" item as a question a person can answer. */
const TITLE: Record<string, string> = {
  unverified_finding: 'Is this right?',
  open_contest: 'The sources disagree. Which is right?',
  flagged_field: 'This rests on a point you turned down',
  bad_verdict: 'Marked wrong, and not answered yet',
  unmerged_branch: 'Some work is not in the document yet',
  client_rejected: 'The client turned this part down',
  client_rejected_parts_unknown: 'The client turned it down. Which parts?',
}

/** The button that takes a person to where they answer it. */
const GO: Record<string, string> = {
  unverified_finding: 'Check it',
  open_contest: 'Pick one',
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
    ? withCountry((item.label ?? '').replace(/^(Finding|Fact|Contest) · /, '')) : withCountry(item.label ?? '')
  // A client's answer is not something anyone "raised": the host's sentence says whose it is.
  const raisedBy = block && !item.code.startsWith('client_')
    ? `${who!.name} ${block.decision.kind === 'finding' ? 'found this' : 'raised it'}${sureWord(block.decision.reasoning?.certainty?.level) === 'Unsure' ? ', and is unsure' : ''}`
    : null
  return (
    <div className="dp-card">
      <div className="dp-card-top">
        <Face block={block} size={40} needs />
        <div>
          {who && <Sign who={who} />}
          <div className="dp-card-what">{TITLE[item.code] ?? 'Needs you'}</div>
          {raisedBy && <div className="dp-card-who">{raisedBy}</div>}
        </div>
      </div>
      {statement && <div className="dp-stmt"><span className="dp-clip">{statement}</span></div>}
      {(item.code === 'flagged_field' || item.code === 'client_rejected' || item.code === 'client_rejected_parts_unknown') && <p className="dp-why">{item.text}</p>}
      <div className="dp-acts">
        {item.address && <button className="dp-btn dp-btn-primary" onClick={() => openInApp(ref, path)}>{GO[item.code] ?? 'Open it'}</button>}
        {item.code === 'flagged_field' && /campaign\.audience/.test(path ?? '') && canRun('synthesise_findings') && (
          <button className="dp-btn" onClick={() => runInApp('synthesise_findings', { redo: 'audience' })}>Ask {AGENTS.synthesis.given} to redo it</button>
        )}
        <span className="dp-hint">{item.code === 'open_contest' ? 'you’ll pick one against its sources'
          : item.code === 'unverified_finding' ? 'you’ll check it against its sources'
            : item.code === 'client_rejected' ? 'change it there, saying why'
              : item.code === 'client_rejected_parts_unknown' ? 'mark the parts above the document' : 'settle it there'}</span>
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
        <Sign who={w} />
        <div className="dp-qi-t">{text}</div>
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

/** One line of history: who, signed · did what · where · when; opens to why. */
export function HistoryLine({ block, view }: { block: DecisionBlock; view: DecisionsView }) {
  const d = block.decision
  const r = d.reasoning
  const w = whoOf(block)
  const t = mainTarget(block)
  const where = t ? whereOf(t.label) : null
  const sure = sureWord(r?.certainty?.level)
  const cites = citesOf(block)
  const sources = sourcesOf(block, view)
  const facts = cites.filter(c => view.cites[c]?.kind === 'fact').length
  const findings = cites.filter(c => view.cites[c]?.kind === 'finding').length
  const materials = cites.filter(c => view.cites[c]?.kind === 'material')
  const hasMore = !!(r || d.rationale || cites.length)
  // A client's answer leads with the client, not the person who recorded it.
  const client = clientLineOf(block, view)
  return (
    <details className={`dp-ev ${block.superseded ? 'dp-ev-old' : ''}`}>
      <summary>
        <Face block={block} />
        <div>
          <Sign who={w} />
          {/* An agent's line sits under its signature, so it does not name them again. */}
          <div className="dp-line">
            {client?.subject ? <><b>{client.subject}</b> {client.rest}</>
              : w.person ? <><b>{w.name}</b> {didWhat(block, view)}</> : upper(didWhat(block, view))}
          </div>
          {typeof d.said === 'string' && d.action === 'client_answer' && (
            <q className="dp-said">{clipSaid(d.said)}</q>
          )}
          {typeof d.was === 'string' && typeof d.now === 'string' && <Change was={d.was} now={d.now} why={d.rationale} />}
          <div className="dp-meta">
            {t && where && (
              <button className="dp-where" onClick={e => { e.preventDefault(); openInApp(refOfAddress(t.address), t.path) }}>
                {WHERE}{where}
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
          {typeof d.now === 'string' || typeof d.was === 'string' ? (
            <div className="dp-diff">
              {typeof d.was === 'string' && d.was && <div><h4>Was</h4><p className="dp-was">{d.was}</p></div>}
              {typeof d.now === 'string' && d.now && <div><h4>Now</h4><p>{d.now}</p></div>}
              <div><h4>Why</h4><p>{d.rationale}</p></div>
            </div>
          ) : (
            <p className="dp-decided">{plain(r?.decided || d.rationale.split(/\. Because/)[0])}</p>
          )}
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
              <h4>Where this came from</h4>
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
          <Again block={block} />
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

/** The client's words on the line, verbatim; cut only for the line, with the whole in the record. */
function clipSaid(said: string): string {
  const one = said.replace(/\s+/g, ' ').trim()
  return one.length > 160 ? `${one.slice(0, 159).trimEnd()}…` : one
}

/** The change itself, on the line: what went, what came, and why. */
function Change({ was, now, why }: { was: string; now: string; why?: string }) {
  const c = changeOf(was, now)
  return (
    <div className="dp-change">
      <span>
        {c.before && <>{c.before} </>}
        {c.cut && <del>{c.cut}</del>}
        {c.cut && c.put && ' '}
        {c.put && <ins>{c.put}</ins>}
        {c.after && <> {c.after}</>}
      </span>
      {why && <span className="dp-change-why">Why: {why}</span>}
    </div>
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

/** The step again, when the app can run it: never a button that does nothing. */
function Again({ block }: { block: DecisionBlock }) {
  const redo = redoOf(block)
  const skipped = canRun('research_lens') ? skippedOf(block) : []
  if (!(redo && canRun(redo.task)) && !skipped.length) return null
  return (
    <div className="dp-acts">
      {redo && canRun(redo.task) && <button className="dp-btn" onClick={() => runInApp(redo.task, redo.input)}>{redo.label}</button>}
      {skipped.length > 0 && (
        <details className="dp-skip">
          <summary className="dp-btn">Look into something that was left out…</summary>
          <div className="dp-acts">
            {skipped.map(x => (
              <button key={x.lens} className="dp-btn" onClick={() => runInApp('research_lens', { lenses: [x.lens] })}>{x.name}</button>
            ))}
          </div>
        </details>
      )}
    </div>
  )
}
