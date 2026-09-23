// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// The Decisions queue's card model.
//
// A card is one step only a person takes (model.ts `HUMAN_STEPS`) on one
// thing that is open: an ask field to confirm, a contest, a proposed finding,
// an unanswered bad verdict, a document ready to lock. Every card names the
// item it settles (an `OpenItem`, or an ask field still waiting to be
// confirmed), the agent whose work it came from, the document and the
// address — so a later task can turn a `DecisionOutcome` into the host call
// (`/resolve`, `/verify`, `/verdict`, `/approve`, an `edit`) without the
// queue changing.
//
// Everything here is plain data, so the same objects can come off the wire.

import type { Address, AgentKey, DecisionKind, DocRef, HumanStepId, OpenItem } from '../model'

// ── what a card settles ─────────────────────────────────────────────────────

/**
 * An ask field an agent extracted or proposed that no person has confirmed.
 * It is not an `OpenItem` — an unconfirmed field does not stop a lock
 * (os-layer §7) — but it is a step only a person takes, so it gets a card.
 */
export interface Unconfirmed {
  /** The field path, e.g. `campaign.markets`. */
  id: string
  kind: 'unconfirmed'
  doc: DocRef
  address: Address
  from: AgentKey
  instance?: string
  label: string
}

/** What a card is about: an open item, or an ask field to confirm. The lock card has none. */
export type CardItem = OpenItem | Unconfirmed

// ── evidence ────────────────────────────────────────────────────────────────

export type Confidence = 'high' | 'medium' | 'low'

/** A pinned fact or finding a card rests on, as a person reads it. */
export interface Citation {
  /** `f_…` fact id or `fi_…` finding id. */
  id: string
  /** What it says, in one line, figure included. */
  text: string
  market?: string
  asOf?: string
  confidence?: Confidence
}

/** One side of a contest: the value, and who says so. */
export interface ContestedValue {
  /** The fact id: what a resolve picks. */
  id: string
  /** As shown: "0.0%". */
  value: string
  market: string
  /** The run that wrote it, e.g. `brands_positioning/IE`. */
  from: string
  /** Source ids the fact rests on. */
  sources: string[]
}

/** A field somewhere in a document that cites a finding. */
export interface Citer {
  address: Address
  /** "Audience". */
  label: string
  /** The field's value, as shown, for the card a rejection makes. */
  value?: string
}

export type Evidence =
  /** "You said this": read from the material, with the span. */
  | { step: 'confirm'; origin: 'extracted'; field: string; value: string; quote: string; material: string; locator?: string }
  /** "We inferred this": derived from layer facts. */
  | { step: 'confirm'; origin: 'proposed'; field: string; value: string; facts: Citation[] }
  /** Two writers, two values, one key. Pick one and say why. */
  | { step: 'resolve'; field: string; key: string; values: [ContestedValue, ContestedValue]; note?: string }
  /** Agent synthesis, labelled as such, with the facts it cites and the fields that cite it. */
  | { step: 'verify'; statement: string; confidence: Confidence; facts: Citation[]; citedBy: Citer[] }
  /**
   * An unanswered bad verdict on a field. `bad-verdict`: a reviewer (or the
   * Judge) marked it bad; it can be revised, or the verdict overridden with a
   * reason. `rejected-finding`: it cites a finding a person rejected; only a
   * revision that stops citing it answers it.
   */
  | {
      step: 'verdict'
      cause: 'bad-verdict' | 'rejected-finding'
      field: string
      value: string
      /** Who marked it, or who rejected the finding. */
      by: string
      reasonCode?: string
      rationale: string
      finding?: { id: string; statement: string }
    }
  /** Lock = accept = seal. Its blockers are computed live from the open items. */
  | { step: 'lock' }

// ── card ────────────────────────────────────────────────────────────────────

export interface DecisionCard {
  /** Unique in the queue: `<step>:<doc id>:<item id>`. */
  id: string
  step: HumanStepId
  /** What it settles. Absent only on a lock card. */
  item?: CardItem
  /** The agent whose work it came from, and which run of it. */
  from: AgentKey
  instance?: string
  doc: DocRef
  /** What the decision is about. A lock targets the whole document. */
  address: Address
  title: string
  evidence: Evidence
}

// ── outcome ─────────────────────────────────────────────────────────────────

/**
 * What the person decided on one card — enough to build the host call.
 * Nothing is sent this round; a later task turns these into calls.
 *
 * `writes` is the decision it writes, or null when nothing is written: the
 * item stays open (a contest left, a lock declined) or the person goes to the
 * document (a field to edit or revise).
 *
 * | step    | yes writes                                              | no writes                                   |
 * |---------|---------------------------------------------------------|---------------------------------------------|
 * | confirm | `edit`, origin becomes `confirmed`                      | nothing — edit it in the document           |
 * | resolve | `resolve` with `choice` and a rationale                 | nothing — only that field waits             |
 * | verify  | `verify`: a `human:` source; the finding is a brand fact | `verdict` bad with a rationale; flags citers |
 * | verdict | `edit` (revised) or `verdict` good with a rationale (override) | nothing — open the field             |
 * | lock    | `approve` with a rationale, a `verify` per `verifies`   | nothing                                     |
 */
export interface DecisionOutcome {
  card: DecisionCard
  /** Yes (right) or no (left). */
  accepted: boolean
  writes: DecisionKind | null
  polarity?: 'good' | 'bad'
  /** Resolve: the fact id picked. */
  choice?: string
  /** Verdict: how the bad verdict was answered. */
  answer?: 'revised' | 'override'
  rationale?: string
  /** Verify, no: the fields the rejection flags. */
  flags?: Address[]
  /** Lock: the findings verified with it. */
  verifies?: string[]
  /** ISO time the call was made. */
  at: string
}

/** One line in "What your calls set moving". */
export interface LogEntry {
  id: string
  at: string
  /** The agent that handed the call over. */
  agent: AgentKey
  instance?: string
  doc: DocRef
  /** `yes` settled something, `no` left it or sent it back, `blocked` a lock that could not happen. */
  tone: 'yes' | 'no' | 'blocked'
  /** Lead: "Verified", "Contest resolved", "Lock blocked". */
  head: string
  text: string
}
