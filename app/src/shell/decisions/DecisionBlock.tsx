// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// One decision, read the way the foundation spec's own decisions read: what
// was decided, because (each point with what it rests on), what lost and
// why, how sure, and what would change it. Older decisions carry one line of
// rationale, and show that.

import { useState } from 'react'
import type { ReactNode } from 'react'
import type { AttentionItem, AttentionReason, CiteInfo, DecisionBlock } from '../../host'

const CITE_KIND: Record<CiteInfo['kind'], string> = {
  fact: 'Fact',
  finding: 'Finding',
  source: 'Source',
  material: 'Material',
  decision: 'Decision',
  person: 'Person',
  address: 'In this document',
  unknown: 'Not found here',
}

const DATE = new Intl.DateTimeFormat(undefined, {
  day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit',
})

function when(ts: string): string {
  const t = Date.parse(ts)
  return Number.isNaN(t) ? ts : DATE.format(t)
}

function humanise(s: string): string {
  const w = s.replace(/[_-]+/g, ' ').trim()
  return w.charAt(0).toUpperCase() + w.slice(1)
}

export function Block({ block, cites, who }: {
  block: DecisionBlock
  cites: Record<string, CiteInfo>
  who: ReactNode
}) {
  const d = block.decision
  const r = d.reasoning
  const needs = block.attention.length > 0
  const what = r?.decided || d.rationale || humanise(d.action)

  return (
    <article className={`dp-block ${needs ? 'dp-block-needs' : ''} ${block.superseded ? 'dp-block-old' : ''}`}>
      <header className="dp-block-head">
        {who}
        <span className="dp-meta">
          <span className="dp-kind">{d.kind ?? 'decision'}</span>
          <time dateTime={d.timestamp}>{when(d.timestamp)}</time>
        </span>
      </header>

      {needs && <Reasons reasons={block.attention} />}

      <p className="dp-what">{what}</p>

      {block.targets.length > 0 && (
        <ul className="dp-targets" aria-label="About">
          {block.targets.map(t => (
            <li key={t.address} className="dp-target" title={t.address}>
              {t.label}
              {!t.here && <span className="dp-upstream">upstream</span>}
            </li>
          ))}
        </ul>
      )}

      {r ? <Reasoned reasoning={r} cites={cites} /> : (
        <>
          {d.rationale && what !== d.rationale && <p className="dp-rationale">{d.rationale}</p>}
          {(d.cites?.length ?? 0) > 0 && (
            <Cited ids={d.cites ?? []} cites={cites} label="Cites" />
          )}
        </>
      )}

      {d.superseded_by && <p className="dp-note">Replaced by a later decision ({d.superseded_by}).</p>}
    </article>
  )
}

/** Something that needs a person but has no decision of its own here. */
export function StandaloneBlock({ item }: { item: AttentionItem }) {
  return (
    <article className="dp-block dp-block-needs">
      <Reasons reasons={[item]} />
      {item.label && (
        <ul className="dp-targets" aria-label="About">
          <li className="dp-target" title={item.address}>{item.label}</li>
        </ul>
      )}
    </article>
  )
}

function Reasons({ reasons }: { reasons: AttentionReason[] }) {
  return (
    <ul className="dp-reasons" aria-label="Needs attention">
      {reasons.map((a, i) => (
        <li key={i}>
          {a.blocks_lock && <span className="dp-blocks">Blocks lock</span>}
          <span>{a.text}</span>
        </li>
      ))}
    </ul>
  )
}

function Reasoned({ reasoning: r, cites }: { reasoning: NonNullable<DecisionBlock['decision']['reasoning']>; cites: Record<string, CiteInfo> }) {
  const level = r.certainty?.level ?? ''
  return (
    <dl className="dp-why">
      {r.because?.length > 0 && (
        <>
          <dt>Because</dt>
          <dd>
            <ul className="dp-because">
              {r.because.map((p, i) => (
                <li key={i}>
                  <span>{p.point}</span>
                  {(p.cites?.length ?? 0) > 0 && <Cited ids={p.cites ?? []} cites={cites} />}
                </li>
              ))}
            </ul>
          </dd>
        </>
      )}
      {r.rejected?.length > 0 ? (
        <>
          <dt>Not chosen</dt>
          <dd>
            <ul className="dp-rejected">
              {r.rejected.map((x, i) => <li key={i}><b>{x.option}</b> — {x.why}</li>)}
            </ul>
          </dd>
        </>
      ) : r.only_option ? (
        <>
          <dt>Only option</dt>
          <dd>{r.only_option}</dd>
        </>
      ) : null}
      {level && (
        <>
          <dt>Certainty</dt>
          <dd>
            <span className={`dp-level dp-level-${level}`}>{level}</span>
            {r.certainty.why && <span> {r.certainty.why}</span>}
          </dd>
        </>
      )}
      {r.would_change_if && (
        <>
          <dt>Would change if</dt>
          <dd>{r.would_change_if}</dd>
        </>
      )}
    </dl>
  )
}

/** Cite chips; one opens at a time to show what it names. */
function Cited({ ids, cites, label }: { ids: string[]; cites: Record<string, CiteInfo>; label?: string }) {
  const [open, setOpen] = useState<string | null>(null)
  const shown = open ? cites[open] : null
  return (
    <div className="dp-cited">
      {label && <span className="dp-cited-label">{label}</span>}
      <span className="dp-chips">
        {ids.map(id => {
          const c = cites[id]
          return (
            <button
              key={id}
              type="button"
              className={`dp-chip dp-chip-${c?.kind ?? 'unknown'}`}
              aria-expanded={open === id}
              onClick={() => setOpen(o => (o === id ? null : id))}
              title={id}
            >
              {c ? clip(c.label, 38) : id}
            </button>
          )
        })}
      </span>
      {open && (
        <div className="dp-cite" role="region" aria-label={`What ${open} is`}>
          <div className="eyebrow">{CITE_KIND[shown?.kind ?? 'unknown']}</div>
          <div className="dp-cite-label">{shown?.label ?? open}</div>
          {shown?.quote && <blockquote>{shown.quote}</blockquote>}
          {shown?.detail && <p>{shown.detail}</p>}
          <code>{open}</code>
        </div>
      )}
    </div>
  )
}

function clip(s: string, n: number): string {
  return s.length > n ? `${s.slice(0, n - 1)}…` : s
}
