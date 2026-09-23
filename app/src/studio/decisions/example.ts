// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// EXAMPLE — the Decisions queue's sample story, until the host serves it.
//
// The host's `/open` route (W2-O2) will serve every unresolved item for the
// documents a person holds; when it lands, `EXAMPLE_ITEMS` becomes what it
// returns (typed as the model's `OpenItem`), and `EXAMPLE_EVIDENCE` what the
// document's parts say about each (the field envelope, the contest's values,
// the finding's cites). Nothing that renders them changes.
//
// The campaign is the Research Tool's filled example
// (app/templates/campaign-research/example/, doc id 7c1e9a42-…): two ask
// fields not yet confirmed, the open contest ct_orchard_hill_abv, the three
// proposed findings, the bad verdict on campaign.objective and the field the
// rejected finding flags. The open items themselves are floor/example.ts's;
// this file adds the ask fields still to confirm and what each document says
// about each item. Every brand, person and figure is invented.

import { EXAMPLE_OPEN } from '../floor/example'
import { address } from '../model'
import type { DocRef } from '../model'
import { evidenceKey } from './rules'
import type { EvidenceIndex } from './rules'
import type { CardItem, Unconfirmed } from './types'

const LUNASA: DocRef = { id: '7c1e9a42-5b3d-4f8e-9a6c-2d1f0e8b4a17', title: 'Lúnasa 0.0 launch' }
const LUNASA_BRIEF: DocRef = { id: 'e5a0c3b8-2f71-4d96-8b1e-6c4f9a0d7e25', title: 'Lúnasa 0.0 brief' }

const at = (doc: DocRef, path: string) => address(doc.id, path)

/**
 * What is open, as `/open` will serve it: the Floor's own list, so the two
 * views tell one story. An item the queue has no evidence for, or that only
 * the document can settle, is listed under the phone instead of carded.
 */
export { EXAMPLE_OPEN }

/** Ask fields the Extract agent filled that nobody has confirmed yet. */
const UNCONFIRMED: readonly Unconfirmed[] = [
  {
    id: 'campaign.markets', kind: 'unconfirmed', doc: LUNASA, from: 'extract',
    address: at(LUNASA, 'campaign.markets'), label: 'Is this for Ireland and GB?',
  },
  {
    id: 'campaign.client_org', kind: 'unconfirmed', doc: LUNASA, from: 'extract',
    address: at(LUNASA, 'campaign.client_org'), label: 'Is Glenmore Drinks the client?',
  },
]

/** The queue's input, in the order the cards come: the ask, then research, then the brief. */
export const EXAMPLE_ITEMS: readonly CardItem[] = [
  ...UNCONFIRMED,
  ...EXAMPLE_OPEN.filter(i => i.doc.id === LUNASA.id),
  ...EXAMPLE_OPEN.filter(i => i.doc.id !== LUNASA.id),
]

/** Documents the person holds the lease on and may lock from here. */
export const EXAMPLE_LOCK_DOCS: readonly DocRef[] = [LUNASA]

const k = evidenceKey

/** What each item's document says about it: shared/data.yaml, facts.yaml, findings.yaml, the chain. */
export const EXAMPLE_EVIDENCE: EvidenceIndex = {
  [k(LUNASA.id, 'campaign.markets')]: {
    step: 'confirm', origin: 'extracted', field: 'Markets', value: 'IE, GB',
    quote: 'in Ireland and Northern Ireland/GB', material: 'Client email', locator: '¶1',
  },
  [k(LUNASA.id, 'campaign.client_org')]: {
    step: 'confirm', origin: 'proposed', field: 'Client', value: 'Glenmore Drinks',
    facts: [{ id: 'f_01JA0B1C4F', text: 'Brand roster: Lúnasa is held by Glenmore Drinks', asOf: '2026-01-15', confidence: 'high' }],
  },
  [k(LUNASA.id, 'ct_orchard_hill_abv')]: {
    step: 'resolve', field: 'Orchard Hill ABV', key: 'brand/orchard-hill:product.abv',
    values: [
      { id: 'f_01JA0B5C6M', value: '0.0%', market: 'IE', from: 'brands_positioning/IE', sources: ['src_tr4d'] },
      { id: 'f_01JA0B5C7N', value: '0.5%', market: 'GB', from: 'brands_positioning/GB', sources: ['src_rt9l'] },
    ],
    note: 'A product’s strength does not change at the border, so this is a contest, not a market difference.',
  },
  [k(LUNASA.id, 'fi_01JA0F2B')]: {
    step: 'verify', confidence: 'medium',
    statement: 'Lúnasa arrives in Ireland with permission and in GB as a stranger: the IE launch can lean on the parent brand, the GB launch has to introduce it.',
    facts: [
      { id: 'f_01JA0B2K7M', text: 'Prompted awareness of Lúnasa: 47%', market: 'IE', asOf: '2026-06-30', confidence: 'high' },
      { id: 'f_01JA0B2K8N', text: 'Prompted awareness of Lúnasa: 12%', market: 'GB', asOf: '2026-06-30', confidence: 'medium' },
    ],
    citedBy: [],
  },
  [k(LUNASA.id, 'fi_01JA0F3C')]: {
    step: 'verify', confidence: 'high',
    statement: 'No/low value is growing about twice as fast in Ireland as in GB, so Ireland should lead the spend.',
    facts: [
      { id: 'f_01JA0B4A1B', text: 'No/low value growth, year on year: +24%', market: 'IE', asOf: '2026-06-30', confidence: 'high' },
      { id: 'f_01JA0B4A2C', text: 'No/low value growth, year on year: +11%', market: 'GB', asOf: '2026-06-30', confidence: 'high' },
    ],
    citedBy: [],
  },
  [k(LUNASA.id, 'fi_01JA0F4D')]: {
    step: 'verify', confidence: 'medium',
    statement: 'Core audience: adults 25–44 who are cutting back midweek but still drink cider at weekends.',
    facts: [
      { id: 'f_01JA0B6D6G', text: 'Adults who say they are moderating: 31%', market: 'IE', asOf: '2026-05-31', confidence: 'medium' },
      { id: 'f_01JA0B3R5S', text: 'Consideration of Lúnasa among no/low drinkers: 18%', market: 'IE', asOf: '2026-06-30', confidence: 'medium' },
    ],
    citedBy: [{
      address: at(LUNASA, 'campaign.audience'), label: 'Audience',
      value: 'Irish adults who are cutting back on alcohol midweek but have not given up cider, and still want a drink that looks like one when friends are round.',
    }],
  },
  [k(LUNASA.id, 'd_01JA0D09VRB')]: {
    step: 'verdict', cause: 'bad-verdict', field: 'Objective',
    value: 'Lúnasa 0.0 to be the alcohol-free cider people ask for by name, winning back the midweek occasions we’re losing.',
    by: 'Ciarán, planner', reasonCode: 'other',
    rationale: 'Two objectives in one sentence — ask-by-name and winning back midweek. The brief needs one; ask Niamh which leads.',
  },
  [k(LUNASA.id, 'd_01JA0D07REJ')]: {
    step: 'verdict', cause: 'rejected-finding', field: 'In market', value: '1 May – 31 Aug 2027',
    by: 'Aoife',
    rationale: 'A launch date is not evidence the occasion was taken — nothing here measures Orchard Hill’s sales.',
    finding: { id: 'fi_01JA0F5E', statement: 'Orchard Hill’s March launch has taken the spring occasion; a Lúnasa launch before May would be second in.' },
  },
  [k(LUNASA_BRIEF.id, 'd_judge_smp')]: {
    step: 'verdict', cause: 'bad-verdict', field: 'Proposition',
    value: 'The alcohol-free cider you ask for by name — and the one that gets you through midweek.',
    by: 'the Judge',
    rationale: 'It says two things. The audience and the support back only midweek; ask-by-name has nothing under it.',
  },
}
