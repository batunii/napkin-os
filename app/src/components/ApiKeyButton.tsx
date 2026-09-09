// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

import { useState } from 'react'

import { useHasKey } from '../agent/key'
import { host } from '../host'
import ApiKeyDialog from './ApiKeyDialog'

/**
 * Only shown when this page is the one making the inference call. A desktop
 * host holds its own credentials and has nothing to ask for.
 */
export default function ApiKeyButton({ variant = 'toolbar' }: { variant?: 'toolbar' | 'floating' }) {
  const [open, setOpen] = useState(false)
  const present = useHasKey()
  if (host.inference !== 'page') return null

  const base: React.CSSProperties = {
    height: 30, padding: '0 10px', borderRadius: 6, fontSize: 12, cursor: 'pointer',
    display: 'flex', alignItems: 'center', gap: 6, color: 'var(--text)',
    border: `1px solid ${present ? 'var(--border)' : 'var(--warn)'}`,
    background: 'var(--surface)',
  }
  const floating: React.CSSProperties = {
    ...base, position: 'fixed', top: 14, right: 56, zIndex: 50, borderRadius: 999, opacity: 0.85,
  }

  return (
    <>
      <button
        style={variant === 'floating' ? floating : base}
        onClick={() => setOpen(true)}
        title={present ? 'Your Anthropic key is set — click to change or remove it'
                       : 'Add an Anthropic API key to generate briefs'}
      >
        {present ? '🔑' : '🔑 Add key'}
      </button>
      {open && <ApiKeyDialog onClose={() => setOpen(false)} />}
    </>
  )
}
