// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

import type { CSSProperties } from 'react'
import { StudioMark } from './StudioMark'
import './LogoSpinner.css'

export type LogoSpinnerSize = 'xs' | 'sm' | 'md' | 'lg'

/** Pixel size of the mark for each named size. */
const LOGO_SPINNER_PX: Record<LogoSpinnerSize, number> = { xs: 14, sm: 18, md: 28, lg: 44 }

export interface LogoSpinnerProps {
  /** A named size, or pixels. Default `md`. */
  size?: LogoSpinnerSize | number
  /** Shown beside (or under, with `stack`) the mark and read out to screen
   *  readers. Without one, screen readers hear "Loading". */
  label?: string
  /** Label under the mark rather than beside it. */
  stack?: boolean
  className?: string
  style?: CSSProperties
}

/**
 * The shell's one loading sign: the studio mark turning a quarter at a time
 * (a gentle pulse under prefers-reduced-motion). Use it for anything the OS
 * layer waits on — opening a document, loading a list, an export.
 *
 * For a page with no React, use the markup in `logoSpinnerMarkup.ts`.
 */
export function LogoSpinner({ size = 'md', label, stack = false, className, style }: LogoSpinnerProps) {
  const px = typeof size === 'number' ? size : LOGO_SPINNER_PX[size]
  const cls = ['logo-spinner', stack && 'logo-spinner-stack', className].filter(Boolean).join(' ')
  return (
    <span className={cls} style={style} role="status" aria-live="polite">
      <span className="logo-spinner-mark" aria-hidden>
        <StudioMark size={px} />
      </span>
      {label
        ? <span className="logo-spinner-label">{label}</span>
        : <span className="logo-spinner-sr">Loading</span>}
    </span>
  )
}

/** A spinner centred in the space it is given — a panel, a pane, a screen. */
export function LogoSpinnerFill({ label, size = 'lg' }: Pick<LogoSpinnerProps, 'label' | 'size'>) {
  return (
    <div className="logo-spinner-fill">
      <LogoSpinner size={size} label={label} stack />
    </div>
  )
}

/** Over everything, while the shell opens or prepares something. */
export function LogoSpinnerVeil({ label }: { label?: string }) {
  return (
    <div className="logo-spinner-veil">
      <LogoSpinner size="md" label={label} />
    </div>
  )
}

export default LogoSpinner
