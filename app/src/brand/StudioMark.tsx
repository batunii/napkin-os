// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

import { useId } from 'react'
import type { CSSProperties } from 'react'

/**
 * The studio mark: one disc, four departments, turned -45° so each quarter
 * points out — Create up, Produce right, Learn down, Plan left. Colours are
 * the department tokens, so it follows the theme.
 */
export function StudioMark({ size = 22, style, title }: { size?: number; style?: CSSProperties; title?: string }) {
  const clip = useId()
  const a11y = title ? { role: 'img', 'aria-label': title } : { 'aria-hidden': true as const }
  return (
    <svg width={size} height={size} viewBox="0 0 22 22" style={{ display: 'block', flexShrink: 0, ...style }} {...a11y}>
      <clipPath id={clip}><circle cx="11" cy="11" r="11" /></clipPath>
      <g clipPath={`url(#${clip})`} transform="rotate(-45 11 11)">
        <rect x="0" y="0" width="11" height="11" fill="var(--plan)" />
        <rect x="11" y="0" width="11" height="11" fill="var(--create)" />
        <rect x="0" y="11" width="11" height="11" fill="var(--learn)" />
        <rect x="11" y="11" width="11" height="11" fill="var(--produce)" />
      </g>
    </svg>
  )
}

/** Mark + "Napkin Studio OS". */
export function StudioLogo({ size = 17, compact = false }: { size?: number; compact?: boolean }) {
  return (
    <span style={{
      display: 'inline-flex', alignItems: 'center', gap: 10, fontFamily: 'var(--f-display)',
      fontWeight: 700, fontSize: size, letterSpacing: '-0.03em', color: 'var(--ink)', lineHeight: 1,
      whiteSpace: 'nowrap',
    }}>
      <StudioMark size={Math.round(size * 1.3)} />
      {compact ? 'Napkin' : 'Napkin Studio OS'}
    </span>
  )
}
