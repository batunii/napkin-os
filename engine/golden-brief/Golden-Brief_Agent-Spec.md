# The Golden Brief — Agent Build Spec

**Purpose:** the canonical specification for the brief schema that AI agents fill in, a critic loop scores, and we render to a human-facing form. One source of truth, many faces.

**Audience:** the developer building the Briefing System / Brief Parser (NAPKIN OS, node 2).

**Status:** v1.0 — hand-off. Built on the IPA / BetterBriefs best-practice guide ("The best way for a client to brief an agency", with Mark Ritson) and the brief structures of BBH, TBWA, Wieden+Kennedy, Mother and AMV BBDO.

---

## 0. The one principle that drives the whole build

> **The JSON schema is the source of truth. The HTML form and the Word doc are *renders* of it. Never hand-edit the renders.**

A template a human fills in by hand and a schema an agent is trained to fill in are different artifacts. HTML/DOCX are *presentation surfaces*; they are good for human sign-off and for a browser/DOM agent to populate, but the rules, types, examples and tests must live in structured data so that **every agent and every critic loop reads the identical contract**. If the rules live in HTML styling, an agent can't reliably read them.

```
client mess (emails / PDF / call notes / scraps)
        │
        ▼
   [ PARSER agent ]  ── extracts, loses nothing, tags provenance
        │
        ▼
   brief.json  ◄── conforms to golden_brief.schema.json
        │
        ▼
   [ CRITIC agent ]  ── scores each field + whole brief against the rubric
        │            ── returns pass/fail + fix notes + open questions
        ▼
   loop: FILL → REVIEW → REPEAT  until score ≥ target OR human gate
        │
        ▼
   render → HTML form  +  Word doc   (for human sign-off)
```

This maps 1:1 onto PLAN → EXECUTE → REVIEW → REPEAT and onto the 7-loop Briefing System (Parse → First-round → Research → Insight → SMP → Substantiation/mandatories → Assembly/IPA QA).

---

## 1. The canonical model — 12 fields, 4 zones, 1 gate, 3 rules

The 10 craft fields are fixed. We add **2 fields that exist specifically because an agent (not a human) is filling this in**: `open_questions` and `competitor_context`.

| # | Field | Zone | Why it's here |
|---|-------|------|---------------|
| 1 | `background` | 1 · Why | The case for the work |
| 2 | `objectives` | 1 · Why | Commercial → behavioural → attitudinal, linked |
| 3 | `audience` | 2 · Who & what we have | One human, not a demographic |
| 4 | `budget_scope` | 2 · Who & what we have | The backbone constraint |
| 5 | `insight` | 3 · The spark | **Hero.** The human tension |
| 6 | `smp` | 3 · The spark | **Hero.** The single-minded proposition |
| 7 | `reasons_to_believe` | 3 · The spark | Proof for the SMP |
| 8 | `desired_response` | 3 · The spark | Think / feel / do |
| 9 | `tone_world_assets` | 4 · How & guardrails | Personality + distinctive assets |
| 10 | `mandatories` | 4 · How & guardrails | Deliverables, dates, legal, avoid-list |
| 11 | `competitor_context` | 1 · Why (light) | **New.** Can't judge "ownable" without it |
| 12 | `open_questions` | cross-cutting | **New, critical for agents.** What's missing / assumed |
| — | `evaluation_and_signoff` | The Gate | How we'll judge + who accepted it |

**The 3 rules (enforced as hard checks, see §6):** one brief = one strategy · keep it brief · it's thinking not box-ticking (no fixed order; never prescribe the solution).

---

## 2. What every field object carries

This is the structure each field is described by in the schema. It's what makes the brief *trainable* and *tractable*.

```jsonc
{
  "id": "smp",
  "label": "Single-Minded Proposition",
  "zone": 3,
  "hero": true,
  "type": "string",
  "required": true,
  "max_words": 20,
  "min_words": 3,
  "prompt": "ONE sentence — the single most compelling, true and ownable thing we can say…",
  "good_example": "The lager that earns its place in your round.",
  "bad_example": "Great taste, fewer carbs, brewed with passion for modern drinkers.",
  "bad_reason": "Three messages, not one; describes features; could be any beer (not ownable); reads like a tagline.",
  "rubric": [
    {"check": "single_sentence", "test": "Is it exactly one sentence?"},
    {"check": "single_minded", "test": "Does it carry exactly one idea, not a list?"},
    {"check": "ownable", "test": "Could a direct competitor say the same thing? If yes, fail."},
    {"check": "not_a_tagline", "test": "Is it strategy that points to the idea, not finished ad copy?"},
    {"check": "derives_from", "test": "Is it traceable to insight + problem + a benefit?"}
  ],
  "depends_on": ["insight", "background", "reasons_to_believe"],
  "provenance_required": true
}
```

Key properties explained:

- **`max_words` / `min_words`** — "keep it brief" becomes machine-enforceable. Agents respect explicit limits.
- **`good_example` + `bad_example` + `bad_reason`** — agents learn the boundary from *contrastive* pairs. (Same logic as the "Cautionary" cases in the Effie dataset.)
- **`rubric`** — the REVIEW step. The critic agent scores each `check` pass/fail and returns the failing tests as fix instructions.
- **`depends_on`** — the dependency graph (see §4). Drives consistency checks and fill order.
- **`provenance_required`** — see §3.

---

## 3. Provenance model (the no-loss ledger)

Every filled field is not just a value — it's a value **plus where it came from and how sure we are**. This is the difference between an agent that hallucinates and an agent that's auditable.

```jsonc
"smp": {
  "value": "The lager that earns its place in your round.",
  "source": "inferred",              // enum: client_stated | inferred | missing
  "evidence": ["brief_email §2", "discovery call 03:14"],  // pointer back to raw input
  "confidence": 0.78,                // 0–1
  "assumptions": ["Assumes 'round culture' is the key social context"],
  "filled_by": "agent",             // agent | human
  "version": 3
}
```

Rules:
- An agent may **never** silently invent a `client_stated` value. If it isn't in the input, it is `inferred` (and must log an assumption) or `missing` (and must raise an `open_question`).
- `confidence` below a configurable floor (e.g. 0.6) auto-creates an open question.
- `evidence` must point back to the raw client input so a human can verify nothing was lost or fabricated.

---

## 4. The dependency graph (the "tractable" part)

The brief is deliberately **non-linear** (IPA: "there's no order"). So we don't enforce order — we enforce **relationships** as cross-field consistency checks. These are the failures that actually waste money.

| Check | Rule | Fails when |
|-------|------|-----------|
| `backbone_balance` | budget ↔ objectives ↔ audience must be mutually realistic | National fame ambition on a regional budget; audience too big for the money |
| `smp_derivation` | `smp` must trace to `insight` + `background` + a benefit | SMP introduces an idea absent from insight/problem |
| `rtb_supports_smp` | every `reasons_to_believe` item must support the `smp` | A proof point that backs a different message |
| `response_ladders_to_objectives` | `desired_response` (think/feel/do) must ladder up to `objectives` | "Do" doesn't drive the behavioural objective |
| `objectives_linked` | commercial ← behavioural ← attitudinal must form a logical chain | Attitudinal shift wouldn't plausibly cause the behaviour |
| `ownable_needs_competitors` | `smp.ownable` check requires `competitor_context` to be filled | Judging "ownable" with no idea what rivals say |

Represent as a directed graph so the fill agent can topologically prefer (not require) a sensible order, and the critic can run all checks regardless of order.

---

## 5. Brief-type classifier + flex modules (the "expandable" part)

Before filling, an upstream **classifier** sets `brief_type`. That decides which optional sub-schemas attach. Core 12 always present; modules bolt on.

```jsonc
"brief_type": "launch",   // enum below
"modules": ["versioning_matrix"]
```

`brief_type` enum: `launch · turnaround · brand_building · activation · behaviour_change · repositioning · tactical`

| Module (sub-schema) | Auto-attaches for | Adds |
|---|---|---|
| `versioning_matrix` | launch, brand_building, activation | per-asset table: ratio, duration, channel, owner |
| `convention_to_break` | turnaround, repositioning (challenger) | category norm + the orthodoxy we overturn (TBWA) |
| `cultural_moment` | brand_building (fame/social) | the cultural moment + tension "Force A vs Force B" (W+K / Mother) |
| `effectiveness_split` | brand_building, activation | brand-building vs activation split (default 60/40) + KPIs per layer + distinctive assets (Binet–Field / AMV BBDO) |
| `behaviour_change` | behaviour_change | COM-B barriers + EAST interventions |
| `positioning_statement` | repositioning, launch (new brand) | FOR…WHO…IS THE…THAT…BECAUSE…; UNLIKE…, …DIFFERENTIATOR |

**Subtract rule:** for `tactical`, fields 5–8 may collapse into a tight `smp` + `reasons_to_believe`; `tone_world_assets` and any positioning module are dropped. Encode as `required` overrides keyed by `brief_type`.

---

## 6. Brief-level "definition of done"

The whole-brief gate the critic runs before a brief can be marked ready. All must pass.

```jsonc
"definition_of_done": [
  {"id": "one_strategy",        "rule": "Exactly one strategy. If two messages exist → split into two briefs.", "type": "binary"},
  {"id": "all_required_filled", "rule": "All required fields present for this brief_type."},
  {"id": "objectives_linked",   "rule": "Commercial/behavioural/attitudinal form a logical chain."},
  {"id": "smp_single",          "rule": "SMP is one ownable sentence within word limit."},
  {"id": "rtb_supports_smp",    "rule": "All RTBs support the SMP."},
  {"id": "evaluation_present",  "rule": "Evaluation criteria agreed before creative starts."},
  {"id": "open_questions_clear","rule": "All open questions resolved OR explicitly flagged for the client."},
  {"id": "within_limits",       "rule": "No field exceeds its max_words. Brief is brief."},
  {"id": "no_solution_prescribed","rule": "Brief sets the problem; it does not prescribe the creative idea."}
]
```

---

## 7. Controlled vocabulary (enums) — reduces agent drift

Use enums wherever a value can be constrained:

- `objective.level`: `commercial · behavioural · attitudinal`
- `brief_type`: see §5
- `source`: `client_stated · inferred · missing`
- `status`: `draft · in_review · ready · signed_off`
- `channel` (mandatories / versioning): controlled channel list (TV, OLV, OOH, social, audio, print, retail, owned, …)
- `effectiveness_ladder` (effectiveness_split): the IPA Effectiveness Ladder levels

Free-text stays free-text for the craft fields (insight, smp, audience) — never constrain creativity — but every *structural* attribute is an enum.

---

## 8. The full schema (all 12 fields + gate)

> Examples are abbreviated here for readability; ship the full good/bad pairs from the one-pager and editable doc. `rubric` shown for the two hero fields and summarised elsewhere.

```jsonc
{
  "$schema": "golden_brief.schema.json",
  "version": "1.0",
  "meta": {
    "brand": {"type": "string", "required": true},
    "project": {"type": "string", "required": true},
    "author_client_lead": {"type": "string", "required": true},
    "agency_lead": {"type": "string", "required": true},
    "date": {"type": "date", "required": true},
    "status": {"enum": ["draft","in_review","ready","signed_off"], "default": "draft"},
    "brief_type": {"enum": ["launch","turnaround","brand_building","activation","behaviour_change","repositioning","tactical"], "required": true},
    "modules": {"type": "array", "items": "string"}
  },

  "fields": {
    "background": {
      "zone": 1, "type": "string", "required": true, "max_words": 70,
      "prompt": "Why are we here? The business problem or opportunity that makes this work necessary. What's going on, and why now? Plain words, no jargon. If there's no marketing strategy behind it, there's no brief yet.",
      "good_example": "Sales have plateaued and under-35s see us as 'their dad's beer.' A new low-carb variant launches in Q3 — our one shot to feel relevant to a younger drinker.",
      "bad_example": "We want a big, disruptive campaign that goes viral and builds brand love.",
      "bad_reason": "States a wish for advertising, not the business problem; no 'why now'.",
      "depends_on": [], "provenance_required": true
    },

    "objectives": {
      "zone": 1, "type": "object", "required": true,
      "shape": {
        "commercial": {"type": "string", "required": true, "must_be_measurable": true, "must_be_timebound": true},
        "behavioural": {"type": "string", "required": true},
        "attitudinal": {"type": "string", "required": true}
      },
      "max_objectives": 3,
      "prompt": "A handful, linked and logical, each measurable and time-stamped. Commercial = the business result; behavioural = what people must DO; attitudinal = what they must think/feel first.",
      "good_example": {"commercial": "+8% volume share in 18–34s within 12 months", "behavioural": "100k under-35s trial the variant", "attitudinal": "from 'beer for older men' to 'beer for people like me'"},
      "bad_example": {"commercial": "Increase awareness, sales, loyalty and consideration"},
      "bad_reason": "Multiple unranked objectives, none measurable or time-bound; awareness is not a commercial outcome.",
      "depends_on": ["budget_scope","audience"], "provenance_required": true
    },

    "audience": {
      "zone": 2, "type": "string", "required": true, "max_words": 80,
      "prompt": "One real human, not a demographic cell. What they want, fear and do; how they decide. Avoid clichés like 'millennials.'",
      "good_example": "Conor, 29, Dublin. Drinks craft cans because they signal taste, not tradition. Wouldn't be seen holding a pint of the 'big' lager — that's his uncle's drink.",
      "bad_example": "ABC1 males 18–34, urban, social, brand-aware.",
      "bad_reason": "A data cell, not a human; tells us nothing about motivation.",
      "depends_on": ["budget_scope"], "provenance_required": true
    },

    "budget_scope": {
      "zone": 2, "type": "string", "required": true, "max_words": 50,
      "prompt": "The money, and the honest ambition it buys. Budget ↔ objectives ↔ audience are linked — move one and the others must move.",
      "good_example": "€X working media. National reach unrealistic — prioritise four cities + always-on social.",
      "bad_example": "TBC / as needed.",
      "bad_reason": "No constraint = the backbone can't be balanced.",
      "depends_on": [], "provenance_required": true
    },

    "insight": {
      "zone": 3, "hero": true, "type": "string", "required": true, "max_words": 50,
      "prompt": "A human tension, not a fact. Reveal WHY, not just WHAT. Shape: 'They [do/believe X] because [deeper motivation], which means [implication].' Survive the 'so what?' test ×3.",
      "good_example": "Young drinkers don't reject us on taste — choosing a drink is choosing a tribe, and ours signals the wrong one. So the job isn't taste, it's belonging.",
      "bad_example": "Younger consumers are drinking less mainstream lager.",
      "bad_reason": "An observation/stat with no motivation and no implication; it's a 'what', not a 'why'. Fails 'so what?'.",
      "rubric": [
        {"check": "is_tension", "test": "Is there an opposing force / tension, not just a fact?"},
        {"check": "reveals_why", "test": "Does it state a motivation, not only a behaviour?"},
        {"check": "so_what_x3", "test": "Does it survive three 'so what?' probes?"},
        {"check": "has_implication", "test": "Does it tell us what the job actually is?"},
        {"check": "not_brand_first", "test": "Is it about the human, not the product?"}
      ],
      "depends_on": ["audience","background"], "provenance_required": true
    },

    "smp": {
      "zone": 3, "hero": true, "type": "string", "required": true, "max_words": 20, "min_words": 3,
      "prompt": "ONE sentence — the single most compelling, true and ownable thing. Not a tagline, not 'creative', not a list. If you have two, choose. (Problem + Benefit + Insight → SMP.)",
      "good_example": "The lager that earns its place in your round.",
      "bad_example": "Great taste, fewer carbs, brewed with passion for modern drinkers.",
      "bad_reason": "Three messages; features not benefit; not ownable; reads like finished copy.",
      "rubric": [
        {"check": "single_sentence", "test": "Exactly one sentence?"},
        {"check": "single_minded", "test": "Exactly one idea, not a list?"},
        {"check": "ownable", "test": "Could a competitor say it? If yes, fail. (Needs competitor_context.)"},
        {"check": "not_a_tagline", "test": "Strategy that points to the idea, not finished ad copy?"},
        {"check": "derives_from", "test": "Traceable to insight + problem + benefit?"}
      ],
      "depends_on": ["insight","background","reasons_to_believe","competitor_context"], "provenance_required": true
    },

    "reasons_to_believe": {
      "zone": 3, "type": "array", "items": "string", "required": true, "max_items": 5,
      "prompt": "The few proof points that make the proposition credible — strongest first. Not a shopping list; number flexes by channel.",
      "good_example": ["Brewed in small batches", "3g carbs", "Chosen #1 in a blind taste test of under-30s"],
      "bad_example": ["Award-winning", "Trusted", "Premium", "Loved by thousands", "Refreshing", "Bold"],
      "bad_reason": "Generic, unprovable claims; a shopping list, not proof for the SMP.",
      "depends_on": ["smp"], "provenance_required": true
    },

    "desired_response": {
      "zone": 3, "type": "object", "required": true,
      "shape": {"think": "string", "feel": "string", "do": "string"},
      "prompt": "The bridge from proposition to behaviour. One line each.",
      "good_example": {"think": "this one's actually for me", "feel": "quietly proud to order it", "do": "ask for it by name at the bar"},
      "bad_example": {"do": "engage with the brand"},
      "bad_reason": "Vague; 'engage' is not an observable behaviour and doesn't ladder to the objective.",
      "depends_on": ["objectives","smp"], "provenance_required": true
    },

    "tone_world_assets": {
      "zone": 4, "type": "object", "required_unless_brief_type": ["tactical"],
      "shape": {"personality": "string (≈3 words)", "distinctive_assets": "array"},
      "prompt": "Personality in ~3 words + the distinctive assets that must carry through every execution (logo, colour, character, sonic, line). Set the world; don't prescribe the idea.",
      "good_example": {"personality": "Dry, confident, never try-hard", "distinctive_assets": ["the red flash","the 'O'","two-note sting"]},
      "bad_example": {"personality": "Make it feel like a cinematic hero film with a celebrity"},
      "bad_reason": "Prescribes the execution; that's the agency's job, not the brief's.",
      "depends_on": [], "provenance_required": true
    },

    "mandatories": {
      "zone": 4, "type": "object", "required": true,
      "shape": {
        "deliverables_channels": "array",
        "key_dates": "array",
        "budget_constraints": "string",
        "legal_regulatory": "array",
        "avoid": "array"
      },
      "prompt": "What's genuinely fixed — deliverables & channels, dates, budget constraints, legal must-haves, and anything to AVOID. Keep tight; detail → appendix.",
      "good_example": {"deliverables_channels": ["3×30s","6s cutdowns","OOH","social"], "key_dates": ["Live 1 Sept"], "legal_regulatory": ["Responsibility message required"], "avoid": ["Don't reference competitors"]},
      "depends_on": [], "provenance_required": true
    },

    "competitor_context": {
      "zone": 1, "type": "object", "required": true,
      "shape": {"main_rivals": "array", "what_everyone_says": "string", "white_space": "string"},
      "prompt": "NEW. Who are we up against, and what does everyone in the category say/do? Needed to judge whether the SMP is ownable.",
      "good_example": {"main_rivals": ["BigLager A","BigLager B"], "what_everyone_says": "All claim heritage, mateship and 'the perfect pint'.", "white_space": "Nobody owns 'the drink that signals your taste'."},
      "bad_example": {"main_rivals": ["Everyone"]},
      "bad_reason": "No real category read; can't locate white space.",
      "depends_on": [], "provenance_required": true
    },

    "open_questions": {
      "zone": "cross-cutting", "type": "array", "items": {"question": "string", "blocks_field": "string", "severity": "enum[low,medium,high]"}, "required": true,
      "prompt": "NEW & CRITICAL FOR AGENTS. What we don't know, what we assumed, and what to ask the client. Auto-populated whenever a field is 'missing' or confidence < floor.",
      "good_example": [{"question": "Is the Q3 launch date fixed or a target?", "blocks_field": "mandatories", "severity": "high"}],
      "depends_on": [], "provenance_required": false
    }
  },

  "evaluation_and_signoff": {
    "criteria": {"type": "string", "required": true, "prompt": "How will we judge the work? Agree the shared criteria BEFORE creative starts. (Only ~30% of brands do — cheapest way to avoid endless rounds.)"},
    "approved_by_client": {"type": "string"}, "approved_date": {"type": "date"},
    "accepted_by_agency": {"type": "string"}, "accepted_date": {"type": "date"},
    "note": "A brief becomes a contract only once the agency accepts it."
  }
}
```

---

## 9. Renders (generated from the schema — do not hand-edit)

- **HTML form** — for browser/DOM agents to fill and for on-screen human review. One field block per schema field: label, prompt, good example, the input, and an inline validation badge driven by the rubric.
- **Word .docx** — for offline human sign-off. Same content, print-friendly.
- **Critic report** — machine output: per-field score, failing checks, open questions, definition-of-done status.

A single renderer should read `golden_brief.schema.json` and emit all three. When the schema changes, every surface updates — there is no second place to edit.

---

## 10. Build order (recommended)

1. **Lock the schema** (`golden_brief.schema.json`) — §8. This is the contract; everything else depends on it.
2. **Provenance + open-questions plumbing** — §3, field 12. Get "never hallucinate, always trace" right early.
3. **Critic agent** — runs per-field `rubric` + §4 dependency checks + §6 definition-of-done. Returns structured fix notes.
4. **Parser agent** — maps messy client input → `brief.json` with provenance (your Node-1 funnel).
5. **Fill/repair loop** — FILL → REVIEW → REPEAT to target score or human gate.
6. **Renderer** — schema → HTML form + docx + critic report.
7. **Classifier + flex modules** — §5, attach by `brief_type`.

---

## 11. Definition of "we got this right"

- Two different agents, given the same messy input, produce briefs that **agree on the structured fields and flag the same open questions.**
- No field is ever `client_stated` unless it's genuinely in the input (audit by `evidence`).
- The critic can reject a real-world bad brief (e.g. a two-message SMP, an unmeasurable objective) and say *why*, citing the failing `check`.
- A human can sign off from the rendered doc without opening the JSON.

---

### Appendix — source authorities
IPA / BetterBriefs, *The best way for a client to brief an agency* (with Mark Ritson). BetterBriefs global report 2021. Agency brief structures: BBH (truth/SMP), TBWA (disruption), Wieden+Kennedy & Mother (cultural), AMV BBDO (Binet–Field effectiveness). IPA Effectiveness Ladder. Byron Sharp / Romaniuk (distinctive assets). COM-B + EAST (behaviour change).
