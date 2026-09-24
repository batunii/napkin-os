// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// A stand-in for the shared LogoSpinner (app/src/brand/) that is on its way;
// same name and props, so swapping it is an import change.

import { StudioMark } from '../../brand/StudioMark'

export function LogoSpinner({ size = 22, label = 'Loading' }: { size?: number; label?: string }) {
  return (
    <span className="dp-spinner" role="status" aria-label={label}>
      <StudioMark size={size} />
    </span>
  )
}
