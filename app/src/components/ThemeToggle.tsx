// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

import { toggleTheme, useTheme } from '../theme'
import './chrome.css'

interface Props {
  /** `floating` is for a view with no chrome to sit in. */
  variant?: 'toolbar' | 'floating'
}

/** Sun for "go light", crescent for "go dark" — drawn, so they match the type. */
function Glyph({ sun }: { sun: boolean }) {
  return sun ? (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" aria-hidden>
      <circle cx="7" cy="7" r="2.6" />
      {[0, 45, 90, 135, 180, 225, 270, 315].map(a => (
        <line key={a} x1="7" y1="1.2" x2="7" y2="2.6" transform={`rotate(${a} 7 7)`} />
      ))}
    </svg>
  ) : (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="currentColor" aria-hidden>
      <path d="M11.8 8.9A5.2 5.2 0 0 1 5.1 2.2a5.2 5.2 0 1 0 6.7 6.7Z" />
    </svg>
  )
}

export default function ThemeToggle({ variant = 'toolbar' }: Props) {
  const theme = useTheme()
  const next = theme === 'dark' ? 'light' : 'dark'
  return (
    <button
      className={`ch-btn ch-btn-icon${variant === 'floating' ? ' ch-btn-floating' : ''}`}
      style={variant === 'floating' ? { right: 16, width: 32 } : undefined}
      onClick={toggleTheme}
      title={`Switch to ${next} mode`}
      aria-label={`Switch to ${next} mode`}
    >
      <Glyph sun={theme === 'dark'} />
    </button>
  )
}
