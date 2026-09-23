// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

import { useState } from 'react'

import { useHasKey } from '../agent/key'
import { host } from '../host'
import ApiKeyDialog from './ApiKeyDialog'
import './chrome.css'

function KeyGlyph() {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" aria-hidden>
      <circle cx="4.6" cy="7" r="2.6" />
      <path d="M7.2 7h5.3M10.6 7v2M12.5 7v1.6" />
    </svg>
  )
}

/**
 * Only shown when this page is the one making the inference call. A desktop
 * host holds its own credentials and has nothing to ask for.
 */
export default function ApiKeyButton({ variant = 'toolbar' }: { variant?: 'toolbar' | 'floating' }) {
  const [open, setOpen] = useState(false)
  const present = useHasKey()
  if (host.inference !== 'page') return null

  const cls = ['ch-btn', present ? 'ch-btn-icon' : 'ch-btn-attn', variant === 'floating' && 'ch-btn-floating']
    .filter(Boolean).join(' ')

  return (
    <>
      <button
        className={cls}
        style={variant === 'floating' ? { right: 56 } : undefined}
        onClick={() => setOpen(true)}
        title={present ? 'Your Anthropic key is set — click to change or remove it'
                       : 'Add an Anthropic API key to generate briefs'}
        aria-label={present ? 'Change or remove your Anthropic key' : undefined}
      >
        <KeyGlyph />{!present && 'Add key'}
      </button>
      {open && <ApiKeyDialog onClose={() => setOpen(false)} />}
    </>
  )
}
