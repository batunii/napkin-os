// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// The agents, drawn as small folded-paper characters with legs.
//
// A flat 2D port of the prototype's 3D origami figures: its six bodies plus a
// dome and a hex, the same thin ink legs and feet, the same two eyes. Twelve
// agents share eight bodies, so each also wears a small emblem and a tone
// (model.ts LOOK_OF). Everything is colour-by-token, so a figure follows the
// theme; motion is plain CSS (AgentFigure.css), so prefers-reduced-motion
// stops it without any JS.

import type { CSSProperties } from 'react'
import { AGENTS, LOOK_OF, TONE_VARS } from './model'
import type { AgentKey, AgentState, Emblem, ShapeName } from './model'
import './AgentFigure.css'

// ── geometry ────────────────────────────────────────────────────────────────
// One 48×64 box per figure, ground at y=58. Scale: 1 prototype unit = 32.

const W = 48
const H = 64
const CX = 24
const GROUND = 58
const LEG_DX = 4.5

interface Body {
  /** Filled in the department colour. */
  fill: string[]
  /** Darker fold facets over the fill — what makes it read as paper. */
  folds?: string[]
  /** Lighter facets (a top face catching light). */
  lights?: string[]
  /** Stroked arcs instead of fills (the tube). [path, width, isFold] */
  strokes?: [string, number, boolean][]
  /** Where the legs start. Inside the body, so the body hides the join. */
  hipY: number
  eyeY: number
  eyeX: number
  /** Centre of the emblem, on the body below the eyes. */
  markY: number
}

const r1 = (n: number) => Math.round(n * 100) / 100
const pt = (x: number, y: number) => `${r1(x)} ${r1(y)}`

function starBody(): Body {
  const cy = 27, outer = 16, inner = 6.8
  const p = Array.from({ length: 10 }, (_, k) => {
    const r = k % 2 ? inner : outer
    const a = (k / 10) * Math.PI * 2 + Math.PI / 2
    return [CX + Math.cos(a) * r, cy - Math.sin(a) * r] as const
  })
  const star = 'M' + p.map(([x, y]) => pt(x, y)).join(' L') + 'Z'
  // Shade one half of every point: centre → tip → next valley.
  const folds = [0, 2, 4, 6, 8].map(k => {
    const [tx, ty] = p[k], [vx, vy] = p[(k + 1) % 10]
    return `M${pt(CX, cy)} L${pt(tx, ty)} L${pt(vx, vy)}Z`
  })
  return { fill: [star], folds, hipY: 36, eyeY: 25, eyeX: 3.2, markY: 30.5 }
}

function tubeBody(): Body {
  // A C of rolled paper, open at the upper right so the legs meet solid body.
  const cy = 28, R = 12
  const at = (deg: number, r: number) => {
    const a = (deg * Math.PI) / 180
    return pt(CX + Math.cos(a) * r, cy - Math.sin(a) * r)
  }
  const arc = (r: number) => `M${at(50, r)} A${r} ${r} 0 1 0 ${at(290, r)}`
  return {
    fill: [],
    strokes: [[arc(R), 9.6, false], [arc(R - 2.4), 4.8, true]],
    hipY: 40, eyeY: 16, eyeX: 3.2, markY: 28,
  }
}

function zigBody(): Body {
  // Three stacked paper diamonds, zig-zagging up.
  const d = (x: number, y: number, s = 9) => `M${pt(x, y - s)} L${pt(x + s, y)} L${pt(x, y + s)} L${pt(x - s, y)}Z`
  const f = (x: number, y: number, s = 9) => `M${pt(x, y - s)} L${pt(x + s, y)} L${pt(x, y + s)}Z`
  const stack: [number, number][] = [[CX - 3.2, 35.4], [CX + 3.2, 25.8], [CX - 3.2, 16.2]]
  return {
    fill: stack.map(([x, y]) => d(x, y)),
    folds: stack.map(([x, y]) => f(x, y)),
    hipY: 37, eyeY: 23, eyeX: 3.2, markY: 30,
  }
}

function pyramidBody(): Body {
  return {
    fill: [`M${pt(CX, 9)} L${pt(40, 41)} L${pt(8, 41)}Z`],
    folds: [`M${pt(CX, 9)} L${pt(40, 41)} L${pt(CX, 41)}Z`],
    hipY: 38, eyeY: 25, eyeX: 3.2, markY: 33.5,
  }
}

function blockBody(): Body {
  // A box turned a little: front face, a narrow side, a sliver of top.
  return {
    fill: [`M13 14 H31 V36 H13Z`, `M31 14 L36 11.5 V33.5 L31 36Z`, `M13 14 L18 11.5 H36 L31 14Z`],
    folds: [`M31 14 L36 11.5 V33.5 L31 36Z`],
    lights: [`M13 14 L18 11.5 H36 L31 14Z`],
    hipY: 33, eyeY: 20.5, eyeX: 3.2, markY: 28.5,
  }
}

function gemBody(): Body {
  const cy = 23.2, hw = 14.4, hh = 20.2
  const T = pt(CX, cy - hh), B = pt(CX, cy + hh), L = pt(CX - hw, cy), R = pt(CX + hw, cy)
  return {
    fill: [`M${T} L${R} L${B} L${L}Z`],
    folds: [`M${T} L${R} L${B}Z`, `M${L} L${pt(CX, cy)} L${B}Z`],
    hipY: 37, eyeY: 20, eyeX: 3.2, markY: 28,
  }
}

function domeBody(): Body {
  // An arch of folded card standing on its flat edge.
  const L = 10, R = 38, top = 13, base = 40, r = (R - L) / 2, sy = top + r
  return {
    fill: [`M${L} ${base} V${sy} A${r} ${r} 0 0 1 ${R} ${sy} V${base}Z`],
    folds: [`M${CX} ${top} A${r} ${r} 0 0 1 ${R} ${sy} V${base} H${CX}Z`],
    hipY: 38, eyeY: 23, eyeX: 3.2, markY: 31,
  }
}

function hexBody(): Body {
  // A flat-topped hexagon: two lower facets in shadow, the top one lit.
  const cy = 26, r = 14.5
  const v = [0, 60, 120, 180, 240, 300].map(d => {
    const a = (d * Math.PI) / 180
    return [CX + Math.cos(a) * r, cy + Math.sin(a) * r] as const
  })
  // v: 0 right, 1 lower-right, 2 lower-left, 3 left, 4 upper-left, 5 upper-right
  const P = v.map(([x, y]) => pt(x, y))
  return {
    fill: ['M' + P.join(' L') + 'Z'],
    folds: [`M${pt(CX, cy)} L${P[0]} L${P[1]} L${P[2]}Z`],
    lights: [`M${pt(CX, cy)} L${P[4]} L${P[5]}Z`],
    hipY: 36, eyeY: 21.5, eyeX: 3.2, markY: 29.5,
  }
}

const BODIES: Record<ShapeName, Body> = {
  star: starBody(), tube: tubeBody(), zig: zigBody(),
  pyramid: pyramidBody(), block: blockBody(), gem: gemBody(),
  dome: domeBody(), hex: hexBody(),
}

/** The emblem, centred on (CX, y), drawn in the tone's mark colour. */
function emblemMarks(emblem: Emblem, y: number, c: string) {
  const line = { stroke: c, strokeWidth: 1.5, strokeLinecap: 'round' as const, fill: 'none' }
  switch (emblem) {
    case 'none': return null
    case 'bars': // market share, three rising bars
      return <g fill={c}>{[0, 1, 2].map(i => {
        const hgt = 3 + i * 2.2
        return <rect key={i} x={CX - 4.6 + i * 3.4} y={y + 3 - hgt} width={2.2} height={hgt} rx={0.5} />
      })}</g>
    case 'target': // where the brand sits
      return <g><circle cx={CX} cy={y + 0.6} r={4.2} {...line} strokeWidth={1.2} /><circle cx={CX} cy={y + 0.6} r={1.3} fill={c} /></g>
    case 'dots': // people
      return <g fill={c}>{[-3.6, 0, 3.6].map(dx => <circle key={dx} cx={CX + dx} cy={y} r={1.25} />)}</g>
    case 'stripe': // a code, a band across the body
      return <rect x={CX - 7} y={y - 1} width={14} height={2} rx={1} fill={c} />
    case 'wave': // broadcast
      return <path d={`M${CX - 6} ${y} q1.5 -2.6 3 0 t3 0 t3 0 t3 0`} {...line} />
    case 'lines': // a page being read
      return <g {...line}><path d={`M${CX - 4.5} ${y - 1.6} h9`} /><path d={`M${CX - 4.5} ${y + 1.6} h5.5`} /></g>
    case 'seal': // a stamp
      return <rect x={CX - 2} y={y - 2} width={4} height={4} rx={0.6} fill={c} transform={`rotate(45 ${CX} ${y})`} />
    case 'spark': // a point made: a four-point star
      return <path d={`M${CX} ${y - 4} Q${CX + 0.6} ${y - 0.6} ${CX + 4} ${y} Q${CX + 0.6} ${y + 0.6} ${CX} ${y + 4} Q${CX - 0.6} ${y + 0.6} ${CX - 4} ${y} Q${CX - 0.6} ${y - 0.6} ${CX} ${y - 4}Z`} fill={c} />
    case 'tick': // checked
      return <path d={`M${CX - 3.4} ${y} l2.3 2.3 l4.5 -4.6`} {...line} strokeWidth={1.7} />
  }
}

/** Stable per-agent phase so a row of figures doesn't bob in lockstep. */
function phaseOf(key: string): number {
  let h = 0
  for (const ch of key) h = (h * 31 + ch.charCodeAt(0)) >>> 0
  return (h % 1000) / 1000
}

// ── AgentFigure ─────────────────────────────────────────────────────────────

export interface AgentFigureProps {
  agent: AgentKey
  /** Rendered height in px; width is 3/4 of it. Default 64. */
  size?: number
  /** idle: gentle bob · working: steps in place · needs-you: --create badge. */
  state?: AgentState
  /** A full stride, for when the parent is moving the figure somewhere. */
  walking?: boolean
  className?: string
  style?: CSSProperties
}

export function AgentFigure({ agent, size = 64, state = 'idle', walking = false, className, style }: AgentFigureProps) {
  const a = AGENTS[agent]
  const look = LOOK_OF[agent]
  const body = BODIES[look.shape]
  const { body: fill, mark: eye } = TONE_VARS[look.tone]
  const label = `${a.name}, ${a.role}${state === 'needs-you' ? ' — needs you' : state === 'working' ? ' — working' : ''}`
  const delay = { '--af-phase': phaseOf(agent) } as CSSProperties

  const leg = (side: 'l' | 'r') => {
    const x = CX + (side === 'l' ? -LEG_DX : LEG_DX)
    return (
      <g className={`af-leg af-leg-${side}`}>
        <line x1={x} y1={body.hipY} x2={x} y2={GROUND} />
        {/* Feet point forward (right), like the prototype's. */}
        <rect x={x - 1.5} y={GROUND - 1} width={4.8} height={2.2} rx={1.1} />
      </g>
    )
  }

  return (
    <svg
      className={['agent-figure', className].filter(Boolean).join(' ')}
      data-state={state}
      data-walking={walking || undefined}
      viewBox={`0 0 ${W} ${H}`}
      width={size * (W / H)}
      height={size}
      role="img"
      aria-label={label}
      style={{ ...delay, ...style }}
    >
      <ellipse className="af-shadow" cx={CX} cy={GROUND + 2} rx={10} ry={1.6} />
      {leg('l')}
      {leg('r')}
      <g className="af-body">
        {body.fill.map((d, i) => <path key={i} d={d} fill={fill} />)}
        {body.strokes?.map(([d, w, fold], i) => (
          <path key={i} d={d} fill="none" stroke={fold ? 'rgba(0,0,0,.14)' : fill} strokeWidth={w} />
        ))}
        {body.folds?.map((d, i) => <path key={i} className="af-fold" d={d} />)}
        {body.lights?.map((d, i) => <path key={i} className="af-light" d={d} />)}
        <g className="af-emblem">{emblemMarks(look.emblem, body.markY, eye)}</g>
        <g className="af-eyes" fill={eye}>
          <rect x={CX - body.eyeX - 1.2} y={body.eyeY - 2} width={2.4} height={4} rx={1.2} />
          <rect x={CX + body.eyeX - 1.2} y={body.eyeY - 2} width={2.4} height={4} rx={1.2} />
        </g>
      </g>
      {state === 'needs-you' && (
        <g className="af-badge">
          <circle className="af-badge-ring" cx={40} cy={8} r={4.5} />
          <circle className="af-badge-dot" cx={40} cy={8} r={4.5} />
        </g>
      )}
    </svg>
  )
}

// ── AgentAvatar ─────────────────────────────────────────────────────────────
// The prototype's flat circular face, for dense lists: a disc in the agent's
// tone with a geometric face derived from the agent's key.

export interface AgentAvatarProps {
  agent: AgentKey
  /** Diameter in px. Default 38. */
  size?: number
  /** Hide from assistive tech when the name is printed right beside it. */
  decorative?: boolean
  className?: string
  style?: CSSProperties
}

export function AgentAvatar({ agent, size = 38, decorative = false, className, style }: AgentAvatarProps) {
  const a = AGENTS[agent]
  const { body: c, mark: fg } = TONE_VARS[LOOK_OF[agent].tone]
  let h = 0
  for (const ch of agent) h = (h * 31 + ch.charCodeAt(0)) >>> 0
  // `>>>` where the prototype had `>>`: for large hashes `>>` went negative and
  // the face lost its eyes.
  const shape = h % 4, rot = (h % 8) * 45, eyes = (h >>> 3) % 3
  const turn = `rotate(${rot} 20 20)`
  const inner = [
    <rect key="i" x="11" y="11" width="18" height="18" rx="3" fill={fg} transform={turn} />,
    <circle key="i" cx="20" cy="20" r="9" fill={fg} />,
    <path key="i" d="M20 9 L31 29 L9 29Z" fill={fg} transform={turn} />,
    <path key="i" d="M9 20a11 11 0 0 1 22 0Z" fill={fg} transform={turn} />,
  ][shape]
  const face = [
    <g key="e"><circle cx="17" cy="20" r="1.8" fill={c} /><circle cx="23" cy="20" r="1.8" fill={c} /></g>,
    <rect key="e" x="15" y="19" width="10" height="2.4" rx="1.2" fill={c} />,
    <circle key="e" cx="20" cy="20" r="2.6" fill={c} />,
  ][eyes]
  const a11y = decorative
    ? { 'aria-hidden': true as const }
    : { role: 'img', 'aria-label': `${a.name}, ${a.role}` }
  return (
    <svg viewBox="0 0 40 40" width={size} height={size} className={className} style={style} {...a11y}>
      <circle cx="20" cy="20" r="20" fill={c} />
      {inner}
      {face}
    </svg>
  )
}
