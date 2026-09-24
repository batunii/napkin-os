// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// Placeholder: the studio mark, turning — the loading sign. task/os-restyle
// ships the real shared one at this path; this stands in until that merges.

import { StudioMark } from './StudioMark'

export function LogoSpinner({ size = 18, label = 'Loading' }: { size?: number; label?: string }) {
  return (
    <span role="progressbar" aria-label={label} style={{ display: 'inline-flex' }}>
      <StudioMark size={size} style={{ animation: 'logo-spin 1.1s linear infinite' }} />
      <style>{'@keyframes logo-spin { to { transform: rotate(360deg) } }'}</style>
    </span>
  )
}

export default LogoSpinner
