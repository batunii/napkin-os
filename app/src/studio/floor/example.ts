// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// EXAMPLE — the Floor's sample story, until the host serves it.
//
// What a document's gates, agents, coverage and open items are can only be
// read from an open document today. The host's `/open` route (W2-O2) will
// serve them for any document; when it lands, replace these four exports with
// what it returns — they are typed with the model's own shapes so nothing
// that renders them changes.
//
// The campaign is the Research Tool's filled example
// (app/templates/campaign-research/example/, doc id 7c1e9a42-…): its open
// contest ct_orchard_hill_abv, its three proposed findings, the rejected one
// that flags campaign.in_market, the bad verdict on campaign.objective, its
// coverage and its chain. The brief spun off it (a drafter per field, the
// Judge after them) and the other two campaigns are made up, to show states
// the example alone cannot. Every brand, person and figure is invented.

import { address } from '../model'
import type { ActivityEntry, DocProgress, DocRef, OpenItem } from '../model'

const LUNASA: DocRef = { id: '7c1e9a42-5b3d-4f8e-9a6c-2d1f0e8b4a17', title: 'Lúnasa 0.0 launch' }
const LUNASA_BRIEF: DocRef = { id: 'e5a0c3b8-2f71-4d96-8b1e-6c4f9a0d7e25', title: 'Lúnasa 0.0 brief' }
const BRIGHTWATER: DocRef = { id: '1f8b6d20-9c4e-4a3b-b7d5-3e2a0c9f8b14', title: 'Brightwater autumn' }
const HARBOUR: DocRef = { id: '9d3e7a51-6b2c-4f80-a1d9-8c5b4e3f2a60', title: 'Harbour Stout winter' }

export const EXAMPLE_DOCS: readonly DocProgress[] = [
  {
    doc: LUNASA_BRIEF, kind: 'brief', state: 'brief', from: LUNASA, lease: 'Ciarán',
    agents: [
      { agent: 'drafter', state: 'working', count: 5 },
    ],
  },
  {
    doc: LUNASA, kind: 'campaign', state: 'brief', markets: ['IE', 'GB'], lease: 'Aoife',
    agents: [],
    // selection.coverage in the example, merged across IE and GB.
    coverage: {
      market_structure: 'filled', brands_positioning: 'thin', consumer_culture: 'thin', category_codes: 'thin',
      rhythm_moments: 'thin', media_spend: 'thin', regulation_clearance: 'filled', effectiveness_evidence: 'empty',
    },
  },
  {
    doc: HARBOUR, kind: 'campaign', state: 'research', markets: ['IE', 'GB'], lease: 'Niamh',
    // Research is running: every lens, once per market. Coverage fills as branches merge.
    agents: (['market_structure', 'positioning', 'culture', 'codes', 'rhythm', 'media', 'regulation', 'effectiveness'] as const)
      .map(agent => ({ agent, state: 'working' as const, count: agent === 'market_structure' ? 1 : 2 })),
    coverage: { market_structure: 'filled', regulation_clearance: 'thin' },
  },
  {
    doc: BRIGHTWATER, kind: 'campaign', state: 'brief', markets: ['IE', 'GB'], lease: 'Aoife',
    agents: [],
    coverage: {
      market_structure: 'filled', brands_positioning: 'filled', consumer_culture: 'filled', category_codes: 'thin',
      rhythm_moments: 'filled', media_spend: 'filled', regulation_clearance: 'filled', effectiveness_evidence: 'thin',
    },
  },
]

export const EXAMPLE_OPEN: readonly OpenItem[] = [
  // Lúnasa 0.0 brief
  {
    id: 'd_judge_smp', kind: 'verdict', doc: LUNASA_BRIEF, from: 'judge',
    address: address(LUNASA_BRIEF.id, 'single_minded_proposition'),
    label: 'Proposition says two things; the rest of the brief backs only one.',
  },
  {
    id: 'agents/u_ciaran.drafter.single_minded_proposition', kind: 'branch', doc: LUNASA_BRIEF, from: 'drafter',
    instance: 'Proposition', address: address(LUNASA_BRIEF.id, 'single_minded_proposition'),
    label: 'A redraft of the proposition is waiting to be merged.',
  },
  {
    id: 'ct_orchard_hill_abv', kind: 'contest', doc: LUNASA_BRIEF, from: 'positioning', instance: 'IE · GB', carried: true,
    address: address(LUNASA.id, 'selection.contested[ct_orchard_hill_abv]'),
    label: 'Orchard Hill’s strength: 0.0% from the IE run, 0.5% from GB.',
  },
  // Lúnasa 0.0 launch — the Research Tool example
  {
    id: 'ct_orchard_hill_abv', kind: 'contest', doc: LUNASA, from: 'positioning', instance: 'IE · GB',
    address: address(LUNASA.id, 'selection.contested[ct_orchard_hill_abv]'),
    label: 'Orchard Hill’s strength: 0.0% from the IE run, 0.5% from GB.',
  },
  {
    id: 'd_01JA0D09VRB', kind: 'verdict', doc: LUNASA, from: 'extract',
    address: address(LUNASA.id, 'campaign.objective'),
    label: 'Objective is marked bad: two objectives in one sentence.',
  },
  {
    id: 'd_01JA0D07REJ', kind: 'verdict', doc: LUNASA, from: 'synthesis',
    address: address(LUNASA.id, 'campaign.in_market'),
    label: 'In-market dates cite a finding that was rejected.',
  },
  {
    id: 'fi_01JA0F2B', kind: 'finding', doc: LUNASA, from: 'synthesis',
    address: address(LUNASA.id, 'findings[fi_01JA0F2B]'),
    label: 'Lúnasa arrives in Ireland with permission and in GB as a stranger.',
  },
  {
    id: 'fi_01JA0F3C', kind: 'finding', doc: LUNASA, from: 'synthesis',
    address: address(LUNASA.id, 'findings[fi_01JA0F3C]'),
    label: 'No/low is growing twice as fast in Ireland, so Ireland should lead the spend.',
  },
  {
    id: 'fi_01JA0F4D', kind: 'finding', doc: LUNASA, from: 'synthesis',
    address: address(LUNASA.id, 'findings[fi_01JA0F4D]'),
    label: 'Core audience: adults 25–44 cutting back midweek, still on cider at weekends.',
  },
  // Harbour Stout winter
  {
    id: 'campaign.budget_band', kind: 'field', doc: HARBOUR, from: 'extract',
    address: address(HARBOUR.id, 'campaign.budget_band'),
    label: 'No budget band: the email gives no figure, and Extract left it out rather than guess.',
  },
]

export const EXAMPLE_ACTIVITY: readonly ActivityEntry[] = [
  { id: 'a13', at: '2026-09-23T09:44:00Z', by: { agent: 'market_structure', instance: 'IE' }, kind: 'pin', doc: HARBOUR, text: '14 facts pinned' },
  { id: 'a12', at: '2026-09-23T09:40:00Z', by: { agent: 'judge' }, kind: 'verdict', doc: LUNASA_BRIEF, text: 'flagged Proposition: not single-minded' },
  { id: 'a11', at: '2026-09-23T09:31:00Z', by: { agent: 'drafter', instance: 'Audience' }, kind: 'edit', doc: LUNASA_BRIEF, text: 'draft merged, citing 3 pins' },
  { id: 'a10', at: '2026-09-23T09:12:00Z', by: { person: 'Ciarán' }, kind: 'edit', doc: LUNASA_BRIEF, text: 'spun off a brief from Lúnasa 0.0 launch' },
  { id: 'a09', at: '2026-09-23T08:02:00Z', by: { person: 'Aoife' }, kind: 'classify', doc: LUNASA, text: 'marked the budget band confidential' },
  { id: 'a08', at: '2026-09-22T17:20:00Z', by: { person: 'Ciarán' }, kind: 'verdict', doc: LUNASA, text: 'marked Objective bad: two objectives in one sentence' },
  { id: 'a07', at: '2026-09-22T16:09:12Z', by: { person: 'Aoife' }, kind: 'verdict', doc: LUNASA, text: 'rejected a finding; In market is flagged' },
  { id: 'a06', at: '2026-09-22T16:05:31Z', by: { person: 'Aoife' }, kind: 'verify', doc: LUNASA, text: 'verified a finding; it is a fact now' },
  { id: 'a05', at: '2026-09-21T14:20:55Z', by: { person: 'Aoife' }, kind: 'resolve', doc: LUNASA, text: 'resolved cider top-3 share: 71%' },
  { id: 'a04', at: '2026-09-21T12:10:00Z', by: { agent: 'synthesis' }, kind: 'finding', doc: LUNASA, text: '5 findings, derived by the agent' },
  { id: 'a03', at: '2026-09-21T11:42:10Z', by: { agent: 'positioning', instance: 'IE · GB' }, kind: 'contest', doc: LUNASA, text: 'opened a contest on Orchard Hill’s strength' },
  { id: 'a02', at: '2026-09-21T11:42:10Z', by: { agent: 'market_structure', instance: 'IE' }, kind: 'pin', doc: LUNASA, text: '4 facts pinned' },
  { id: 'a01', at: '2026-09-21T09:00:05Z', by: { agent: 'extract' }, kind: 'edit', doc: LUNASA, text: 'proposed the ask: 17 of 19 fields' },
]

/** Shown wherever example data is on screen. */
export const EXAMPLE_NOTE = 'Example — open items become live with W2-O2.'
