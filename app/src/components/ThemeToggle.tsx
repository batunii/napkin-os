// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

import { toggleTheme, useTheme } from '../theme'

interface Props {
  /** `floating` is for the home screen, which has no chrome to sit in. */
  variant?: 'toolbar' | 'floating'
}

const base: React.CSSProperties = {
  height: 30,
  width: 32,
  borderRadius: 6,
  border: '1px solid var(--border)',
  background: 'var(--surface)',
  color: 'var(--text)',
  cursor: 'pointer',
  fontSize: 13,
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  lineHeight: 1,
}

const floating: React.CSSProperties = {
  ...base,
  position: 'fixed',
  top: 14,
  right: 16,
  zIndex: 50,
  height: 32,
  width: 32,
  borderRadius: 999,
  opacity: 0.75,
}

export default function ThemeToggle({ variant = 'toolbar' }: Props) {
  const theme = useTheme()
  const next = theme === 'dark' ? 'light' : 'dark'
  return (
    <button
      style={variant === 'floating' ? floating : base}
      onClick={toggleTheme}
      title={`Switch to ${next} mode`}
      aria-label={`Switch to ${next} mode`}
    >
      {theme === 'dark' ? '☀' : '☾'}
    </button>
  )
}
