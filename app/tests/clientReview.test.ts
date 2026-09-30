// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// Client review's rules, as the owner's prototype (version 4) and the
// OS-layer contract (§7.5, §8.2) give them: how strong a record the evidence
// makes, the body the host is sent (the client's words verbatim, reasons only
// for a rejection, no parts marked on an acceptance), which parts an app may
// declare, and what Jude and Ellis say, by their own names.

import assert from 'node:assert/strict'
import { test } from 'node:test'

import type { ClientAnswerView, DecisionBlock, DecisionsView } from '../src/host/types.ts'
import {
  afterLine, aliasesFrom, assetNameOf, bodyOf, canReview, channelOf, emptyDraft, evidenceOf, knownClients, markedOf, modeLine,
  partsFrom, problemOf, recordedFrom, settledSuggestions, strengthLine,
} from '../src/shell/clientReview/review.ts'
import type { Draft } from '../src/shell/clientReview/review.ts'
import { clientLineOf, didWhat, whoOf } from '../src/shell/decisions/words.ts'
import { AGENTS } from '../src/studio/model.ts'

const draft = (over: Partial<Draft>): Draft => ({ ...emptyDraft({ name: 'Mary Kelly' }), ...over })
const PARTS = [
  { address: 'single_minded_proposition', label: 'Single-minded proposition' },
  { address: 'audience', label: 'Audience' },
]

// ── the evidence strength line ─────────────────────────────────────────────

test('an attached file is a strong record; anything else is weaker', () => {
  const file = { name: 'Re brief.eml', bytes: new Uint8Array([1]) }
  assert.deepEqual(evidenceOf(draft({ answer: 'rejected', how: 'file', file })),
    { strong: true, line: 'From Mary’s email, file attached' })
  assert.equal(evidenceOf(draft({ answer: 'rejected', how: 'file', file: { name: 'markup.PDF', bytes: new Uint8Array() } })).line,
    'From the PDF Mary sent, file attached')
  assert.deepEqual(evidenceOf(draft({ answer: 'accepted_with_changes', how: 'email', pasted: 'Budget is 400k.' })),
    { strong: false, line: 'From Mary’s email, pasted by you' })
  assert.deepEqual(evidenceOf(draft({ answer: 'rejected', how: 'call', note: 'Call with Mary: flat.' })),
    { strong: false, line: 'Your note of a call. Nothing attached' })
  // Chosen but empty is nothing.
  assert.equal(evidenceOf(draft({ answer: 'rejected', how: 'email', pasted: '   \n ' })).line, 'Recorded by you. Nothing attached')
  assert.equal(strengthLine(draft({ answer: 'rejected', how: 'file', file })), 'Strong record: From Mary’s email, file attached.')
  assert.equal(strengthLine(draft({ answer: 'accepted' })), 'Weaker record: Recorded by you. Nothing attached.')
})

test('accepted is one tap: what was left in the evidence area does not count', () => {
  const d = draft({ answer: 'accepted', how: 'email', pasted: 'All good!' })
  assert.deepEqual(channelOf(d), { channel: 'none' })
  assert.equal(evidenceOf(d).strong, false)
})

test('with no name yet the line speaks of the client', () => {
  assert.equal(evidenceOf(draft({ name: '', answer: 'rejected', how: 'email', pasted: 'No.' })).line,
    'From the client’s email, pasted by you')
})

// ── the body the host is sent ──────────────────────────────────────────────

test('the client’s words go verbatim, and the channel says how they came', () => {
  const words = '  Honestly this isn’t the brief.\n\n  The summer line doesn’t feel like us.  '
  const body = bodyOf(draft({ answer: 'rejected', how: 'email', pasted: words, email: '  mary@lunasa.ie ' }), PARTS, {})
  assert.equal(body.said, words)
  assert.equal(body.channel, 'pasted_email')
  assert.deepEqual(body.client, { name: 'Mary Kelly', email: 'mary@lunasa.ie' })
  assert.deepEqual(body.parts, PARTS)
  assert.ok(!('asset' in body) && !('marked' in body) && !('reasons' in body))
  const call = bodyOf(draft({ answer: 'accepted_with_changes', how: 'call', note: 'Mary on the phone.' }), PARTS, {})
  assert.equal(call.channel, 'call')
  assert.equal(call.said, 'Mary on the phone.')
})

test('a file goes as the stored asset, with no words of its own', () => {
  const file = { name: 'a.eml', bytes: new Uint8Array() }
  const body = bodyOf(draft({ answer: 'rejected', how: 'file', file, pasted: 'left in the other tab' }), PARTS, {}, 'human/assets/a.eml')
  assert.equal(body.channel, 'file')
  assert.equal(body.asset, 'human/assets/a.eml')
  assert.ok(!('said' in body))
})

test('reasons go only with a rejection', () => {
  assert.deepEqual(bodyOf(draft({ answer: 'rejected', reasons: ['off_brief', 'tone'] }), [], {}).reasons, ['off_brief', 'tone'])
  assert.ok(!('reasons' in bodyOf(draft({ answer: 'accepted_with_changes', reasons: ['tone'] }), [], {})))
})

test('marked parts take the whole document’s answer; an acceptance marks none', () => {
  const marks = { audience: { marked: true, words: 'Too young.' }, single_minded_proposition: { marked: false, words: 'x' }, gone: { marked: true, words: '' } }
  assert.deepEqual(markedOf(draft({ answer: 'rejected' }), PARTS, marks), [{ address: 'audience', answer: 'rejected', said: 'Too young.' }])
  assert.deepEqual(markedOf(draft({ answer: 'accepted' }), PARTS, marks), [])
  assert.ok(!('marked' in bodyOf(draft({ answer: 'accepted' }), PARTS, marks)))
})

test('what the recorder typed under a part goes as that part’s said, verbatim', () => {
  const words = '  Too young,\n\n  and too urban.  '
  const marks = { audience: { marked: true, words }, single_minded_proposition: { marked: true, words: ' \n\t ' } }
  const body = bodyOf(draft({ answer: 'accepted_with_changes', how: 'email', pasted: 'See notes.' }), PARTS, marks)
  assert.deepEqual(body.marked, [
    { address: 'single_minded_proposition', answer: 'accepted_with_changes' },
    { address: 'audience', answer: 'accepted_with_changes', said: words },
  ])
  // Never under the old name: the host reads `said`.
  assert.ok(body.marked!.every(m => !('words' in m)))
  // The document's own words are still the document's.
  assert.equal(body.said, 'See notes.')
})

test('Save waits for an answer and a name, and a typed email must be one', () => {
  assert.match(problemOf(draft({}))!, /Choose what the client said/)
  assert.match(problemOf(draft({ answer: 'accepted', name: ' ' }))!, /who answered/)
  assert.match(problemOf(draft({ answer: 'accepted', email: 'mary at lunasa' }))!, /email address/)
  assert.equal(problemOf(draft({ answer: 'accepted', email: '' })), null)
  assert.equal(problemOf(draft({ answer: 'accepted', email: 'mary@lunasa.ie' })), null)
})

test('the client’s file is stored under a name of its own, in plain characters', () => {
  const at = new Date('2026-09-30T10:15:00.123Z')
  assert.equal(assetNameOf('Re: Lunasa brief v7.eml', at), 'client-20260930T101500Z-Re-Lunasa-brief-v7.eml')
  assert.equal(assetNameOf('marked up.PDF', at), 'client-20260930T101500Z-marked-up.pdf')
  assert.equal(assetNameOf('../../x.eml', at), 'client-20260930T101500Z-x.eml')
})

// ── the parts an app declares ──────────────────────────────────────────────

test('only parts the host takes are kept', () => {
  const warn = console.warn
  console.warn = () => {}
  try {
    const got = partsFrom([
      { address: 'insight', label: 'The insight' },
      { address: 'objectives.commercial', label: 'Commercial' },
      { address: 'objectives', label: 'Objectives' }, // holds the part before it
      { address: 'sections[s_2]', label: 'Entity-keyed' },
      { address: 'upstream.abc.insight', label: 'Carried' },
      { address: 'projection', label: 'Host-written' },
      { address: 'audience', label: '' },
      { address: 'budget', label: 'x'.repeat(90) },
      'nonsense',
    ])
    assert.deepEqual(got.map(p => p.address), ['insight', 'objectives.commercial', 'budget'])
    assert.equal(got[2].label.length, 80)
    assert.deepEqual(partsFrom(undefined), [])
    // A part with no aliases carries no key for them.
    assert.ok(!('aliases' in got[0]))
  } finally {
    console.warn = warn
  }
})

test('a part’s aliases are words: trimmed, no repeats, the app’s order, sent with the part', () => {
  assert.deepEqual(aliasesFrom(' budget,  Cost , spend,, BUDGET ,scope '), ['budget', 'Cost', 'spend', 'scope'])
  assert.deepEqual(aliasesFrom(['proposition', ' the   line ', 7, '', 'idea']), ['proposition', 'the line', 'idea'])
  assert.deepEqual(aliasesFrom(undefined), [])
  assert.deepEqual(aliasesFrom('x'.repeat(61)), [])
  assert.equal(aliasesFrom(Array.from({ length: 20 }, (_, i) => `w${i}`)).length, 12)
  const warn = console.warn
  console.warn = () => {}
  try {
    const parts = partsFrom([
      { address: 'audience', label: 'Audience', aliases: ['audience', 'target', 'who'] },
      { address: 'budget_and_scope', label: 'Budget & scope', aliases: 'budget, cost, spend, scope' },
      { address: 'insight', label: 'Insight', aliases: '  ' },
    ])
    assert.deepEqual(parts, [
      { address: 'audience', label: 'Audience', aliases: ['audience', 'target', 'who'] },
      { address: 'budget_and_scope', label: 'Budget & scope', aliases: ['budget', 'cost', 'spend', 'scope'] },
      { address: 'insight', label: 'Insight' },
    ])
    const body = bodyOf(draft({ answer: 'rejected', how: 'email', pasted: 'The budget is wrong.' }), parts, {})
    assert.deepEqual(body.parts, parts)
    // The body's parts are copies: the host's list is not the shell's.
    assert.notEqual(body.parts[0].aliases, parts[0].aliases)
  } finally {
    console.warn = warn
  }
})

// ── what the crew says ─────────────────────────────────────────────────────

const answerView = (over: Partial<ClientAnswerView>): ClientAnswerView => ({
  decision: 'd_A', answer: 'rejected', client: { name: 'Mary Kelly' }, channel: 'pasted_email',
  evidence: { strength: 'weaker' }, recorded_by: { id: 'aoife', name: 'aoife' }, at: '2026-09-30T10:20:03Z',
  seen: { version: 'v', doc_hash: 'h', parts: [] }, current: true, parts_known: false, ...over,
})

function viewWith(client: DecisionsView['client'], lock: Partial<DecisionsView['lock']> = {}, decisions: DecisionBlock[] = []): DecisionsView {
  return {
    document_id: 'doc', version: 'v', decisions, attention: [], cites: {},
    lock: { can_lock: false, blockers: 0, locked: true, reopened: [], ...lock }, client,
  }
}

test('Jude asks for the whole answer, and Ellis is named from the crew', () => {
  assert.equal(modeLine(draft({}), 'brief').agent, 'judge')
  assert.match(modeLine(draft({}), 'brief').text, /about the brief as a whole/)
  assert.match(modeLine(draft({ answer: 'rejected' }), 'brief').text, new RegExp(`${AGENTS.extract.given} will look for the parts it names`))
  // Nothing to wait for: the host's matcher answers in Save's own reply.
  assert.equal(afterLine(null, 'brief'), null)
})

test('after Save, Jude says what is left to do', () => {
  const noParts = viewWith({ available: true, answer: answerView({}), answers: [], parts: [], suggestions: [] })
  assert.match(afterLine(noParts, 'brief')!.text, /don’t know which parts Mary meant yet\. Mark them before it can be locked again/)
  // The host looked in her words and no part was named: the answer is for the whole brief.
  assert.equal(afterLine(noParts, 'brief', true)!.text,
    `${AGENTS.extract.given} found no part named in Mary’s words, so the answer stands for the whole brief. If you know which parts they meant, mark them before it can be locked again.`)
  const changesNoParts = viewWith({ available: true, answer: answerView({ answer: 'accepted_with_changes' }), answers: [], parts: [], suggestions: [] })
  assert.match(afterLine(changesNoParts, 'brief', true)!.text, /stands for the whole brief\. If you know which parts they meant, mark them\.$/)
  const suggested = viewWith({ available: true, answer: answerView({}), answers: [], parts: [],
    suggestions: [{ decision: 'd_S', review: 'd_A', address: 'doc#audience', label: 'Audience', answer: 'rejected', quote: 'q' }] })
  assert.match(afterLine(suggested, 'brief')!.text, new RegExp(`^${AGENTS.extract.given} found the parts`))
  const part = { address: 'doc#audience', label: 'Audience', state: 'rejected' as const, decision: 'd_P', review: 'd_A',
    found_by: 'person' as const, client: { name: 'Mary Kelly' }, at: '', stale: false, answered: false, reopened: false }
  const open = viewWith({ available: true, answer: answerView({ parts_known: true }), answers: [], parts: [part], suggestions: [] })
  assert.match(afterLine(open, 'brief')!.text, /Mary rejected 1 part\. /)
  const changes = viewWith({ available: true, answer: answerView({ parts_known: true, answer: 'accepted_with_changes' }), answers: [], parts: [{ ...part, state: 'accepted_with_changes' as const }], suggestions: [] })
  assert.match(afterLine(changes, 'brief')!.text, /Mary asked for changes on 1 part\. /)
  const done = viewWith({ available: true, answer: answerView({ parts_known: true }), answers: [], parts: [{ ...part, answered: true }], suggestions: [] })
  assert.match(afterLine(done, 'brief')!.text, /Every change Mary asked for is made/)
  const accepted = viewWith({ available: true, answer: answerView({ answer: 'accepted' }), answers: [], parts: [], suggestions: [] })
  assert.match(afterLine(accepted, 'brief')!.text, /Mary accepted the whole brief/)
  assert.equal(afterLine(viewWith({ available: true, answer: null, answers: [], parts: [], suggestions: [] }), 'brief'), null)
})

test('the saved record says where its evidence came from, and who recorded it', () => {
  assert.equal(recordedFrom(answerView({}), true), 'From Mary’s email, pasted by you')
  assert.equal(recordedFrom(answerView({}), false), 'From Mary’s email, pasted by aoife')
  assert.equal(recordedFrom(answerView({ channel: 'file', evidence: { asset: 'human/assets/x.pdf', strength: 'strong' } }), true), 'From the PDF Mary sent, file attached')
  assert.equal(recordedFrom(answerView({ channel: 'call' }), true), 'Your note of a call. Nothing attached')
  assert.equal(recordedFrom(answerView({ channel: 'none' }), false), 'Recorded by aoife. Nothing attached')
})

test('Client review is offered only on a locked document with nothing reopened', () => {
  const client = { available: true, answer: null, answers: [], parts: [], suggestions: [] }
  assert.equal(canReview(viewWith(client)), true)
  assert.equal(canReview(viewWith(client, { locked: false })), false)
  assert.equal(canReview(viewWith(client, { reopened: [{ address: 'doc#audience', label: 'Audience', decision: 'd_U', answers: 'd_P' }] })), false)
  // A host without client review offers none.
  assert.equal(canReview(viewWith(undefined)), false)
  assert.equal(canReview(null), false)
})

// ── in "What happened" ─────────────────────────────────────────────────────

function block(over: Partial<DecisionBlock['decision']>, who: DecisionBlock['who']): DecisionBlock {
  return {
    decision: { agent: who.id, action: 'edit', rationale: '', timestamp: '2026-09-30T10:00:00Z', ...over },
    who, targets: [], attention: [], superseded: false,
  }
}
const you: DecisionBlock['who'] = { kind: 'person', id: 'local', name: 'You', you: true }

test('a client’s answer reads as the client’s, recorded by you from what', () => {
  const a = block({
    id: 'd_A', kind: 'client_review', action: 'client_answer', answer: 'accepted_with_changes',
    client: { name: 'Mary Kelly' }, channel: 'pasted_email', evidence: { strength: 'weaker' },
  }, you)
  const view = viewWith(undefined, {}, [a])
  assert.deepEqual(clientLineOf(a, view), {
    subject: 'Mary Kelly (client)',
    rest: 'accepted it with changes · recorded by you from Mary’s email, pasted',
  })
  const r = block({ id: 'd_B', kind: 'client_review', action: 'client_answer', answer: 'rejected', reasons: ['off_brief', 'tone'],
    client: { name: 'Mary Kelly' }, channel: 'file', evidence: { asset: 'human/assets/re.eml', strength: 'strong' } }, you)
  assert.equal(clientLineOf(r, viewWith(undefined, {}, [r]))!.rest, 'rejected it (off brief, tone) · recorded by you from Mary’s email, attached')
})

test('Ellis’s suggestion is signed Ellis, and a confirmation names the client from the answer', () => {
  const a = block({ id: 'd_A', kind: 'client_review', action: 'client_answer', answer: 'rejected', client: { name: 'Mary Kelly' } }, you)
  const s = block({ id: 'd_S', kind: 'client_review', action: 'suggest_part', handler: 'find_client_parts@1.0', review: 'd_A',
    label: 'Audience', answer: 'rejected', actor: 'process:middleware' }, { kind: 'agent', id: 'find_client_parts', name: 'Find client parts' })
  const p = block({ id: 'd_P', kind: 'client_review', action: 'client_answer_part', review: 'd_A', found_by: 'agent',
    label: 'Audience', answer: 'rejected', client: { name: 'Mary Kelly' } }, you)
  const u = block({ id: 'd_U', kind: 'unlock', action: 'reopen_part', answers: 'd_P', cites: ['d_P'], label: 'Audience',
    rationale: 'Mary Kelly asked: The audience is wrong.' }, you)
  const e = block({ id: 'd_E', kind: 'edit', action: 'edit_field', answers: 'd_P' }, you)
  const view = viewWith(undefined, {}, [e, u, p, s, a])
  assert.equal(whoOf(s).name, AGENTS.extract.given)
  // The host's matcher, which replaced the middleware call: Ellis too.
  const h = block({ id: 'd_S2', kind: 'client_review', action: 'suggest_part', handler: 'client_parts_match@1', review: 'd_A',
    label: 'Audience', answer: 'rejected', actor: 'process:host' }, { kind: 'agent', id: 'client_parts_match', name: 'Client parts match' })
  assert.equal(whoOf(h).name, AGENTS.extract.given)
  assert.match(didWhat(h, view), /^thinks Mary Kelly’s answer is about Audience/)
  assert.match(didWhat(s, view), /^thinks Mary Kelly’s answer is about Audience/)
  assert.equal(didWhat(p, view), `confirmed ${AGENTS.extract.given}’s suggestion: Mary Kelly rejected Audience`)
  assert.equal(didWhat(u, view), 'reopened Audience to make the change: Mary Kelly asked: The audience is wrong.')
  assert.match(didWhat(e, view), /, as Mary Kelly asked$/)
})

test('a lock after a reopen is locking it again', () => {
  const first = block({ id: 'd_L1', kind: 'approve', action: 'lock', targets: ['doc'] }, you)
  const again = block({ id: 'd_L2', kind: 'approve', action: 'lock', targets: ['doc'] }, you)
  const view = viewWith(undefined, {}, [again, first])
  assert.equal(didWhat(again, view), 'locked it again')
  assert.equal(didWhat(first, view), 'locked the document')
})

test('the clients on record are offered most recent first, the carried one too, one per name', () => {
  const carried = block({ id: 'd_R', kind: 'client_review', action: 'client_answer', answer: 'accepted',
    client: { name: 'Aoife Byrne', email: 'aoife@lunasa.ie' } }, you)
  const view = viewWith({ available: true, answer: null, answers: [answerView({ client: { name: 'Mary Kelly' } })], parts: [], suggestions: [] }, {}, [carried])
  assert.deepEqual(knownClients(view, [{ name: 'mary kelly' }, { name: 'Tom Ryan' }]),
    [{ name: 'Mary Kelly' }, { name: 'Aoife Byrne', email: 'aoife@lunasa.ie' }, { name: 'Tom Ryan' }])
  assert.deepEqual(knownClients(null), [])
})

test('Ellis’s answered suggestions stay listed, in his order, and one a lock closed does not', () => {
  const agent: DecisionBlock['who'] = { kind: 'agent', id: 'find_client_parts', name: 'Find client parts' }
  const s1 = block({ id: 'd_S1', kind: 'client_review', action: 'suggest_part', review: 'd_A', label: 'Audience', answer: 'rejected', quote: 'Wrong people.' }, agent)
  const s2 = block({ id: 'd_S2', kind: 'client_review', action: 'suggest_part', review: 'd_A', label: 'Tone & world', answer: 'rejected', quote: 'Flat.' }, agent)
  const s3 = block({ id: 'd_S3', kind: 'client_review', action: 'suggest_part', review: 'd_A', label: 'Budget', answer: 'rejected', quote: '' }, agent)
  const yes = block({ id: 'd_P', kind: 'client_review', action: 'client_answer_part', review: 'd_A', suggestion: 'd_S2', found_by: 'agent', label: 'Tone & world', answer: 'rejected' }, you)
  const no = block({ id: 'd_X', kind: 'client_review', action: 'dismiss_part', review: 'd_A', suggestion: 'd_S1' }, you)
  const shut = block({ id: 'd_Y', kind: 'client_review', action: 'dismiss_part', review: 'd_A', suggestion: 'd_S3', closed_by: 'd_L' }, you)
  const view = viewWith(undefined, {}, [shut, no, yes, s3, s2, s1])
  assert.deepEqual(settledSuggestions(view, 'd_A').map(x => [x.label, x.confirmed, x.quote]),
    [['Audience', false, 'Wrong people.'], ['Tone & world', true, 'Flat.']])
  assert.match(didWhat(shut, view), new RegExp(`^locked it again; ${AGENTS.extract.given}’s suggestion about Budget was left unanswered and is closed$`))
})
