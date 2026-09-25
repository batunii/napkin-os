// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// The agents, drawn from the owner's character sheet ("01 / Graphic
// characters", 2026-09-24): flat bodies in bold colour, thin ink legs with
// feet, a face, and one mark each. The sheet's four states are here too: idle,
// working (leaning in, with motion lines), needs you (a speech bubble with a
// "!"), and walking. Colour is by token (model.ts TONE_VARS), so ink bodies
// follow the theme; motion is plain CSS (AgentFigure.css), so
// prefers-reduced-motion stops it without any JS.

import type { CSSProperties, ReactNode } from 'react'
import { AGENTS, LOOK_OF, TONE_VARS } from './model'
import type { AgentKey, AgentLook, AgentState, Emblem, ShapeName } from './model'
import './AgentFigure.css'

// ── geometry ────────────────────────────────────────────────────────────────
// One 48×64 box per figure, ground at y=58.

const W = 48
const H = 64
const CX = 24
const GROUND = 58
const LEG_DX = 4.5

interface Body {
  /** Filled in the agent's colour. */
  fill: string[]
  /** Filled in the accent colour, over the body. */
  accents?: string[]
  /** A stroked arc instead of a fill (the crescent): [path, width]. */
  stroke?: [string, number]
  /** Turns the whole body, face and all (the Judge's card sits a little askew). */
  turn?: string
  /** Where the legs start. Inside the body, so the body hides the join. */
  hipY: number
  eyeY: number
  eyeX: number
  /** Centre of the mark. */
  markX?: number
  markY: number
}

const r1 = (n: number) => Math.round(n * 100) / 100
const pt = (x: number, y: number) => `${r1(x)} ${r1(y)}`
const diamond = (x: number, y: number, hw: number, hh = hw) =>
  `M${pt(x, y - hh)} L${pt(x + hw, y)} L${pt(x, y + hh)} L${pt(x - hw, y)}Z`

function starBody(): Body {
  const cy = 27, outer = 17, inner = 7.2
  const p = Array.from({ length: 10 }, (_, k) => {
    const r = k % 2 ? inner : outer
    const a = (k / 10) * Math.PI * 2 + Math.PI / 2
    return pt(CX + Math.cos(a) * r, cy - Math.sin(a) * r)
  })
  return { fill: ['M' + p.join(' L') + 'Z'], hipY: 36, eyeY: 25, eyeX: 3, markY: 31 }
}

function crescentBody(): Body {
  // A bold C, open to the right, drawn as one thick arc with round ends.
  const cy = 26, R = 10
  const at = (deg: number) => {
    const a = (deg * Math.PI) / 180
    return pt(CX + Math.cos(a) * R, cy - Math.sin(a) * R)
  }
  return {
    fill: [],
    stroke: [`M${at(40)} A${R} ${R} 0 1 0 ${at(320)}`, 10],
    hipY: 38, eyeY: 16.2, eyeX: 3, markX: CX + 1, markY: cy,
  }
}

function stackBody(): Body {
  // Two diamonds stacked, and a small one in the accent colour off to the side.
  return {
    fill: [diamond(CX, 16, 8.5), diamond(CX, 32, 9.5)],
    accents: [diamond(CX + 8.5, 24, 4.6)],
    hipY: 36, eyeY: 15.5, eyeX: 2.8, markY: 32,
  }
}

function triangleBody(): Body {
  return { fill: [`M${pt(CX, 8)} L${pt(41.5, 40)} L${pt(6.5, 40)}Z`], hipY: 38, eyeY: 24, eyeX: 3, markY: 32.5 }
}

function columnBody(): Body {
  return { fill: ['M14 8.5 H34 V38 H14Z'], hipY: 36, eyeY: 15.5, eyeX: 3, markY: 29 }
}

function cardBody(): Body {
  return { fill: ['M11.5 11 H36.5 V36 H11.5Z'], turn: 'rotate(-5 24 23.5)', hipY: 34, eyeY: 19, eyeX: 3.2, markY: 27.5 }
}

function diamondBody(): Body {
  return { fill: [diamond(CX, 23, 14.5, 19)], hipY: 37, eyeY: 18.5, eyeX: 2.8, markY: 27.5 }
}

function domeBody(): Body {
  // An arch standing on its flat edge.
  const L = 9.5, R = 38.5, top = 12, base = 40, r = (R - L) / 2, sy = top + r
  return { fill: [`M${L} ${base} V${sy} A${r} ${r} 0 0 1 ${R} ${sy} V${base}Z`], hipY: 38, eyeY: 22, eyeX: 3.2, markY: 30 }
}

function hexBody(): Body {
  // Flat top and bottom, points to either side.
  const cy = 26, r = 16
  const P = [0, 60, 120, 180, 240, 300].map(d => {
    const a = (d * Math.PI) / 180
    return pt(CX + Math.cos(a) * r, cy + Math.sin(a) * r)
  })
  return { fill: ['M' + P.join(' L') + 'Z'], hipY: 37, eyeY: 22, eyeX: 3.2, markY: 30 }
}

const BODIES: Record<ShapeName, Body> = {
  star: starBody(), crescent: crescentBody(), stack: stackBody(),
  triangle: triangleBody(), column: columnBody(), card: cardBody(),
  diamond: diamondBody(), dome: domeBody(), hex: hexBody(),
}

/** The mark, centred on (x, y): drawn in `c`, with `ground` for what sits under it. */
function emblemMarks(emblem: Emblem, x: number, y: number, c: string, ground: string): ReactNode {
  const line = { stroke: c, strokeWidth: 1.5, strokeLinecap: 'round' as const, strokeLinejoin: 'round' as const, fill: 'none' }
  switch (emblem) {
    case 'none': return null
    case 'bars': // market share, three rising bars
      return <g fill={c}>{[0, 1, 2].map(i => {
        const hgt = 3.2 + i * 2.4
        return <rect key={i} x={x - 4.8 + i * 3.5} y={y + 3.5 - hgt} width={2.3} height={hgt} rx={0.4} />
      })}</g>
    case 'target': // where the brand sits
      return <g><circle cx={x} cy={y} r={3.6} {...line} strokeWidth={1.3} /><circle cx={x} cy={y} r={1.2} fill={c} /></g>
    case 'dots': // people
      return <g fill={c}>{[-3.6, 0, 3.6].map(dx => <circle key={dx} cx={x + dx} cy={y} r={1.3} />)}</g>
    case 'mouth': // a flat, knowing line: the category's codes
      return <path d={`M${x - 4.5} ${y} h9`} {...line} strokeWidth={1.7} />
    case 'wave': // broadcast
      return <path d={`M${x - 6} ${y} q1.5 -2.4 3 0 t3 0 t3 0 t3 0`} {...line} strokeWidth={1.4} />
    case 'page': // a page being read
      return <g>
        <rect x={x - 4.6} y={y - 3.2} width={9.2} height={6.4} rx={0.8} fill={c} opacity={0.88} />
        <path d={`M${x - 2.8} ${y - 1.1} h5.6 M${x - 2.8} ${y + 1.1} h4`} stroke={ground} strokeWidth={0.9} strokeLinecap="round" />
      </g>
    case 'spark': // a point made: a four-point star
      return <path d={`M${x} ${y - 4} Q${x + 0.6} ${y - 0.6} ${x + 4} ${y} Q${x + 0.6} ${y + 0.6} ${x} ${y + 4} Q${x - 0.6} ${y + 0.6} ${x - 4} ${y} Q${x - 0.6} ${y - 0.6} ${x} ${y - 4}Z`} fill={c} />
    case 'tick': // checked
      return <path d={`M${x - 3.8} ${y} l2.6 2.6 l5 -5`} {...line} strokeWidth={1.8} />
  }
}

/** The body, its face and its mark: what the figure and the avatar share. */
function bodyParts(look: AgentLook): ReactNode {
  const body = BODIES[look.shape]
  const { body: fill, mark: eye } = TONE_VARS[look.tone]
  const accent = look.accent ? TONE_VARS[look.accent].body : fill
  const markColour = look.lightMark ? '#FFFFFF' : eye
  return (
    <g transform={body.turn}>
      {body.fill.map((d, i) => <path key={i} d={d} fill={fill} />)}
      {body.stroke && <path d={body.stroke[0]} fill="none" stroke={fill} strokeWidth={body.stroke[1]} strokeLinecap="round" />}
      {body.accents?.map((d, i) => <path key={`a${i}`} d={d} fill={accent} />)}
      <g className="af-emblem">{emblemMarks(look.emblem, body.markX ?? CX, body.markY, markColour, fill)}</g>
      <g className="af-eyes" fill={eye}>
        <rect x={CX - body.eyeX - 1.1} y={body.eyeY - 1.8} width={2.2} height={3.6} rx={1.1} />
        <rect x={CX + body.eyeX - 1.1} y={body.eyeY - 1.8} width={2.2} height={3.6} rx={1.1} />
      </g>
    </g>
  )
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
  /** idle: gentle bob · working: leans in, steps, motion lines · needs-you: a "!" bubble. */
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
  const label = `${a.name}, ${a.role}${state === 'needs-you' ? ' — needs you' : state === 'working' ? ' — working' : ''}`
  const delay = { '--af-phase': phaseOf(agent) } as CSSProperties

  const leg = (side: 'l' | 'r') => {
    const x = CX + (side === 'l' ? -LEG_DX : LEG_DX)
    return (
      <g className={`af-leg af-leg-${side}`}>
        <line x1={x} y1={body.hipY} x2={x} y2={GROUND} />
        {/* Feet point forward (right). */}
        <rect x={x - 1.4} y={GROUND - 1} width={4.6} height={2.2} rx={1.1} />
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
      <g className="af-body">{bodyParts(look)}</g>
      {/* Shown only while working (AgentFigure.css). */}
      <g className="af-motion">
        <path d="M39.5 9.5 l3.2 -2.6" />
        <path d="M41 14 l4 -0.9" />
        <path d="M36.5 6.5 l1 -3.6" />
      </g>
      {state === 'needs-you' && (
        <g className="af-badge">
          <circle className="af-badge-ring" cx={40} cy={8} r={6} />
          <path className="af-badge-bubble" d="M40 2 a6 6 0 1 1 -3.9 10.55 l-2.6 1.95 l1.3 -3.5 A6 6 0 0 1 40 2Z" />
          <path className="af-badge-mark" d="M40 5 v3.6" />
          <circle className="af-badge-dot" cx={40} cy={11} r={0.95} />
        </g>
      )}
    </svg>
  )
}

// ── AgentAvatar ─────────────────────────────────────────────────────────────
// The sheet's avatar: the agent's body on a rounded tile, for dense lists. The
// tile is the page's soft grey; ink bodies invert with the theme, the way the
// sheet's light and dark avatars do.

export interface AgentAvatarProps {
  agent: AgentKey
  /** Side in px. Default 38. */
  size?: number
  /** Hide from assistive tech when the name is printed right beside it. */
  decorative?: boolean
  className?: string
  style?: CSSProperties
}

export function AgentAvatar({ agent, size = 38, decorative = false, className, style }: AgentAvatarProps) {
  const a = AGENTS[agent]
  const a11y = decorative
    ? { 'aria-hidden': true as const }
    : { role: 'img', 'aria-label': `${a.name}, ${a.role}` }
  return (
    <svg viewBox="0 0 40 40" width={size} height={size} className={['agent-avatar', className].filter(Boolean).join(' ')} style={style} {...a11y}>
      <rect className="aa-tile" width="40" height="40" rx="10" />
      <g transform="translate(20 20.5) scale(0.72) translate(-24 -24)">{bodyParts(LOOK_OF[agent])}</g>
    </svg>
  )
}
