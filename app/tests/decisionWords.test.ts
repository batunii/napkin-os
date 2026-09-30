// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// The decision panel names the crew the way the apps do: by the name each
// agent introduces itself by, read from model.ts, never a role label. These
// pin that, so a renamed agent is renamed in the panel too.

import assert from 'node:assert/strict'
import { test } from 'node:test'

import type { DecisionBlock, DecisionsView } from '../src/host/types.ts'
import { didWhat, plain, redoOf, runsOf, signOf, whereOf, whoOf } from '../src/shell/decisions/words.ts'
import { AGENTS } from '../src/studio/model.ts'

function block(over: Partial<DecisionBlock['decision']>, who: DecisionBlock['who']): DecisionBlock {
  return {
    decision: { agent: who.id, action: 'edit', rationale: '', timestamp: '2026-09-30T10:00:00Z', ...over },
    who, targets: [], attention: [], superseded: false,
  }
}

const agent = (id: string): DecisionBlock['who'] => ({ kind: 'agent', id, name: id })

test('an agent is signed with its own name and job', () => {
  const w = whoOf(block({ kind: 'verdict', polarity: 'bad' }, agent('napkin/brief/judge')))
  assert.equal(w.agent, 'judge')
  assert.equal(w.name, AGENTS.judge.given)
  assert.equal(signOf(w), `${AGENTS.judge.given} · ${AGENTS.judge.job}`)
})

test('a lens researcher is named, not called by its lens', () => {
  const w = whoOf(block({ action: 'research_run', targets: ['lenses_run[media_spend/IE]'] }, agent('research_lens')))
  assert.equal(w.agent, 'media')
  assert.equal(w.name, AGENTS.media.given)
})

test('a person is "You", a name, or "Someone" for an opaque id', () => {
  const you = whoOf(block({}, { kind: 'person', id: 'x', name: 'x', you: true }))
  assert.equal(you.name, 'You')
  assert.equal(signOf(you), 'You')
  assert.equal(whoOf(block({}, { kind: 'person', id: 'ana', name: 'ana' })).name, 'ana')
  const tenant = '9ba18b76-0a48-42fc-a689-71f858645ce0'
  assert.equal(whoOf(block({}, { kind: 'person', id: tenant, name: tenant })).name, 'Someone')
})

test('asking again asks the agent who does that work, by name', () => {
  const r = redoOf(block({ action: 'synthesise_finding', kind: 'finding' }, agent('synthesise_findings')))
  assert.equal(r?.label, `Ask ${AGENTS.synthesis.given} again`)
  const lens = redoOf(block({ action: 'research_run', targets: ['lenses_run[rhythm_moments/IE]'] }, agent('research_lens')))
  assert.equal(lens?.label, `Ask ${AGENTS.rhythm.given} to look again`)
})

test('a run is titled by who worked in it, in the order they started', () => {
  const view: DecisionsView = {
    document_id: 'd', version: 'v', attention: [], cites: {}, lock: { can_lock: true, blockers: 0 },
    // Newest first, as the host sends them.
    decisions: [
      block({ action: 'synthesise_finding', kind: 'finding', handler: 'campaign', timestamp: '2026-09-30T10:03:00Z' }, agent('campaign')),
      block({ action: 'extract', handler: 'campaign', timestamp: '2026-09-30T10:00:00Z' }, agent('campaign')),
    ],
  }
  const [run] = runsOf(view)
  assert.equal(run.title, `${AGENTS.extract.given} and ${AGENTS.synthesis.given}`)
})

test('where a decision landed reads in words, never an id', () => {
  assert.equal(whereOf('Intake › Messages · msg_01M3MAWD3BMYKN002'), 'Your request')
  assert.equal(whereOf('Selection › Lenses run · consumer_culture/IE'), `${AGENTS.culture.given} · ${AGENTS.culture.job}`)
  assert.equal(whereOf('Fact · Private label share · IE'), 'Fact · Private label share · Ireland')
  const long = whereOf(`Finding · ${'word '.repeat(30)}`)!
  assert.ok(long.length <= 60 && long.endsWith('…'))
})

test('a person’s typed name reads as the name again', () => {
  assert.equal(whoOf(block({}, { kind: 'person', id: 'Sean-OBrien', name: 'Sean-OBrien' })).name, 'Sean OBrien')
  assert.equal(whoOf(block({}, { kind: 'person', id: '.5f20.4f1f', name: '.5f20.4f1f' })).name, '张伟')
})

test('the crew’s working words are said plainly', () => {
  assert.equal(plain('Proposed a consumer_culture finding for a person to verify'), 'Proposed a consumer culture point for you to check')
  const view: DecisionsView = { document_id: 'd', version: 'v', attention: [], cites: {}, lock: { can_lock: true, blockers: 0 }, decisions: [] }
  const b = block({ action: 'edit_field' }, { kind: 'person', id: 'x', name: 'x', you: true })
  b.targets = [{ address: 'd#campaign.name', path: 'campaign.name', label: 'Campaign › Name', kind: 'field', here: true }]
  assert.equal(didWhat(b, view), 'changed the campaign’s name')
})

test('a run of look changes reads as one line', () => {
  const view: DecisionsView = { document_id: 'd', version: 'v', attention: [], cites: {}, lock: { can_lock: true, blockers: 0 }, decisions: [] }
  const b = block({ action: 'look', rationale: 'Changed the look: Studio brief; Dark with orange palette' }, { kind: 'person', id: 'x', name: 'x', you: true })
  assert.equal(didWhat(b, view), 'changed the look: Studio brief, Dark with orange palette')
})
