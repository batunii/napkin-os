// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// The studio skeleton: one agency, four departments, one loop.
//
// Every view (Floor, Decisions, the brand mark) renders from this, so the
// shape of the studio is decided once. Two kinds of data live here:
//
//   STRUCTURE — departments, which agents sit where, their shapes, the three
//     gates. These are design decisions and are meant to hold.
//   EXAMPLE   — agent `status` lines and `mode`, and every tool without an
//     `appId`. Nothing produces these yet; they are the prototype's sample
//     story (a summer cider launch) so the views have something to show.
//     Anything reading them should treat `EXAMPLE` as "not live".

/** True while agent statuses, modes and most tools are sample data. */
export const EXAMPLE = true

export type DeptId = 'plan' | 'create' | 'produce' | 'learn'
/** Desk is not a department: it sits in the middle and routes between them. */
export type AgentDept = DeptId | 'desk'

export type AgentKey =
  | 'desk'
  | 'scout' | 'brief' | 'mirror'
  | 'spark' | 'pen' | 'frame'
  | 'reel' | 'stage' | 'forma'
  | 'judge' | 'archive' | 'loop'

/** How much an agent acts on its own: watches, suggests, or just does it. */
export type AgentMode = 'watch' | 'coach' | 'do'

/** What an agent figure is doing right now. */
export type AgentState = 'idle' | 'working' | 'needs-you'

export type ShapeName = 'star' | 'tube' | 'zig' | 'pyramid' | 'block' | 'gem'

export interface Tool {
  name: string
  description: string
  /** The agent that runs it. */
  agent: AgentKey
  /** Icon hint from the prototype (doc, tree, lens …). No icon set yet. */
  icon: string
  /** Installed CLAN app that implements this tool. Absent = not built yet. */
  appId?: string
}

export interface Department {
  id: DeptId
  name: string
  /** Its step in the loop: Brief → Create → Scale → Judge + Learn. */
  loop: string
  /** Light-theme colour, for places that cannot take a CSS variable. */
  color: string
  /** Light-theme text/marks on `color`. */
  fg: string
  /** Light-theme colour for text *in* the department's colour on paper. */
  ink: string
  /** CSS variable for the colour; follows the theme. Prefer this. */
  colorVar: string
  /** CSS variable for text/marks on the colour. */
  fgVar: string
  job: string
  bottleneck: string
  handIn: string
  handOut: string
  agents: AgentKey[]
  tools: Tool[]
}

export interface Agent {
  key: AgentKey
  name: string
  role: string
  dept: AgentDept
  /** EXAMPLE: one-line status from the sample story. */
  status: string
  /** EXAMPLE: the autonomy setting shown on the dial. */
  mode: AgentMode
}

export interface Gate {
  /** 1, 2, 3 — the order a job passes them. */
  n: number
  title: string
  description: string
  /** The department whose work this gate signs off. */
  after: DeptId
}

const deptVars = (id: DeptId) => ({ colorVar: `var(--${id})`, fgVar: `var(--${id}-fg)` })

export const DEPARTMENTS: readonly Department[] = [
  {
    id: 'plan', name: 'Plan', loop: 'Brief', color: '#14161B', fg: '#FFFFFF', ink: '#14161B', ...deptVars('plan'),
    job: 'Turn a client ask into a sharp, evidenced brief.',
    bottleneck: 'Briefs arrive vague. Category knowledge lives in a few heads and walks out the door.',
    handIn: 'Client ask, past work, category signals',
    handOut: 'Signed brief with a single-minded proposition',
    agents: ['scout', 'brief', 'mirror'],
    tools: [
      { name: 'Brief Maker', description: 'Talk through the ask. It writes the brief, flags the gaps and asks the questions a planner would.', agent: 'brief', icon: 'doc', appId: 'ie.napkin.brief-maker' },
      { name: 'Category Tree', description: 'Live research on every vertical. Telco, alcohol, FMCG and more, kept fresh by a research agent.', agent: 'scout', icon: 'tree' },
      { name: 'Audience Lens', description: 'Segments the market and writes a real person for each segment, with sources.', agent: 'mirror', icon: 'lens' },
      { name: 'Agency MRI', description: 'Maps how a client or agency works today and where time is lost.', agent: 'scout', icon: 'scan' },
    ],
  },
  {
    id: 'create', name: 'Create', loop: 'Create', color: '#FF4F2E', fg: '#FFFFFF', ink: '#FF4F2E', ...deptVars('create'),
    job: 'Turn the brief into ideas worth making.',
    bottleneck: 'Too few routes explored before the deadline. Good ideas die in the deck because nobody can see them.',
    handIn: 'Signed brief',
    handOut: 'Three territories with scripts, key art and a pick',
    agents: ['spark', 'pen', 'frame'],
    tools: [
      { name: 'Territory Forge', description: 'Opens ten strategic routes from one brief, then helps you cut to three.', agent: 'spark', icon: 'burst' },
      { name: 'Script Room', description: 'Scripts, lines and cutdown copy in the brand voice. Every draft is versioned.', agent: 'pen', icon: 'lines' },
      { name: 'Key Art Board', description: 'Frames, boards and mood from a sketch or a sentence. See the idea before you sell it.', agent: 'frame', icon: 'frame' },
      { name: 'Think, Don’t Prompt', description: 'Sketch on paper. It turns the drawing into a brief and first images.', agent: 'frame', icon: 'pen' },
    ],
  },
  {
    id: 'produce', name: 'Produce', loop: 'Scale', color: '#8B919E', fg: '#FFFFFF', ink: '#6E7482', ...deptVars('produce'),
    job: 'Make it for real, then make every version.',
    bottleneck: 'Versioning eats weeks. One film becomes sixty formats by hand, and brand drifts every time.',
    handIn: 'Approved idea and assets',
    handOut: 'Master film plus every format, brand checked',
    agents: ['reel', 'stage', 'forma'],
    tools: [
      { name: 'Shot Designer', description: 'Breaks a script into shots, lenses and moves. Hands a clean list to the render models.', agent: 'reel', icon: 'cam' },
      { name: 'Render Bay', description: 'Film and stills through Seedance, Runway and friends. Picks the right model for each shot.', agent: 'reel', icon: 'play' },
      { name: 'Director3D', description: 'Speak to Blender. Set up scenes, cameras and light by voice.', agent: 'stage', icon: 'cube' },
      { name: 'Versioning Machine', description: 'One master in, every size and cutdown out. Static, animated and film.', agent: 'forma', icon: 'grid' },
      { name: 'Brand Guard', description: 'Checks every version against the brand. Type, colour, logo, product.', agent: 'forma', icon: 'shield' },
    ],
  },
  {
    id: 'learn', name: 'Learn', loop: 'Judge + Learn', color: '#DADDE3', fg: '#14161B', ink: '#9AA0AC', ...deptVars('learn'),
    job: 'Score the work, keep what worked, feed it back in.',
    bottleneck: 'Nobody writes down why work won or lost. Every job starts from zero.',
    handIn: 'Ideas to score, results from market',
    handOut: 'Scores, lessons and a smarter next brief',
    agents: ['judge', 'archive', 'loop'],
    tools: [
      { name: 'Judge', description: 'Scores ideas against effectiveness evidence before money is spent. Shows its reasons.', agent: 'judge', icon: 'scale' },
      { name: 'Awards Library', description: 'Own-words records of winning work: problem, insight, mechanic, evidence, lesson.', agent: 'archive', icon: 'books' },
      { name: 'Results Loop', description: 'Pulls live results back in and writes the lesson into the next brief.', agent: 'loop', icon: 'loop' },
      { name: 'Client Vault', description: 'Each client’s memory, sealed. Judges learn from patterns, vaults never leak.', agent: 'loop', icon: 'lock' },
    ],
  },
]

/** Departments by id. */
export const DEPT: Readonly<Record<DeptId, Department>> =
  Object.fromEntries(DEPARTMENTS.map(d => [d.id, d])) as Record<DeptId, Department>

export const AGENTS: Readonly<Record<AgentKey, Agent>> = {
  desk:    { key: 'desk',    name: 'Desk',    role: 'Traffic and approvals', dept: 'desk',    status: 'Holding 3 decisions for you', mode: 'coach' },
  scout:   { key: 'scout',   name: 'Scout',   role: 'Category researcher',   dept: 'plan',    status: 'Watching 14 cider signals', mode: 'do' },
  brief:   { key: 'brief',   name: 'Brief',   role: 'Brief writer',          dept: 'plan',    status: 'Brief v3 signed', mode: 'coach' },
  mirror:  { key: 'mirror',  name: 'Mirror',  role: 'Audience planner',      dept: 'plan',    status: 'Idle', mode: 'coach' },
  spark:   { key: 'spark',   name: 'Spark',   role: 'Idea generator',        dept: 'create',  status: 'Cutting 10 routes to 3', mode: 'coach' },
  pen:     { key: 'pen',     name: 'Pen',     role: 'Copywriter',            dept: 'create',  status: 'Scripting route B', mode: 'coach' },
  frame:   { key: 'frame',   name: 'Frame',   role: 'Art director',          dept: 'create',  status: 'Key art for route A', mode: 'coach' },
  reel:    { key: 'reel',    name: 'Reel',    role: 'Film producer',         dept: 'produce', status: 'Waiting on idea pick', mode: 'watch' },
  stage:   { key: 'stage',   name: 'Stage',   role: '3D director',           dept: 'produce', status: 'Idle', mode: 'watch' },
  forma:   { key: 'forma',   name: 'Forma',   role: 'Versioning',            dept: 'produce', status: 'Preparing 60 format specs', mode: 'do' },
  judge:   { key: 'judge',   name: 'Judge',   role: 'Effectiveness scorer',  dept: 'learn',   status: 'Scored 3 routes', mode: 'do' },
  archive: { key: 'archive', name: 'Archive', role: 'Librarian',             dept: 'learn',   status: 'Found 6 precedents', mode: 'do' },
  loop:    { key: 'loop',    name: 'Loop',    role: 'Learning writer',       dept: 'learn',   status: 'Idle', mode: 'do' },
}

/** All thirteen, Desk first, then department by department in loop order. */
export const AGENT_KEYS: readonly AgentKey[] = ['desk', ...DEPARTMENTS.flatMap(d => d.agents)]

export const GATES: readonly Gate[] = [
  { n: 1, title: 'Sign the brief', description: 'A person agrees the problem before anyone makes anything. Cheap to change here, expensive later.', after: 'plan' },
  { n: 2, title: 'Pick the idea', description: 'Judge scores the routes and shows its reasons. A person makes the call.', after: 'create' },
  { n: 3, title: 'Approve the master', description: 'A person signs off the hero film. Then Forma makes every version without asking.', after: 'produce' },
]

/** Each agent's folded-paper body. Shared by AgentFigure and anything 3D later. */
export const SHAPE_OF: Readonly<Record<AgentKey, ShapeName>> = {
  desk: 'block',
  scout: 'zig', brief: 'tube', mirror: 'gem',
  spark: 'star', pen: 'tube', frame: 'pyramid',
  reel: 'pyramid', stage: 'block', forma: 'star',
  judge: 'gem', archive: 'block', loop: 'zig',
}

/** The theme-following CSS colour for an agent's body. Desk is ink. */
export function agentColorVar(key: AgentKey): string {
  return `var(--${AGENTS[key].dept})`
}

/**
 * EXAMPLE: the figure state implied by the sample status line, until agents
 * report real state. "Idle" → idle, Desk (holding decisions) → needs-you,
 * anything else → working.
 */
export function exampleStateOf(key: AgentKey): AgentState {
  if (key === 'desk') return 'needs-you'
  return AGENTS[key].status === 'Idle' ? 'idle' : 'working'
}
