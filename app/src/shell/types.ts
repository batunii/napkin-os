// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

import type { OpenResult } from '../host'
import type { DeviceSource } from '../offline/actions'

export type Screen = 'home' | 'app'

// The launcher's app list comes straight off the host contract.
export type { InstalledApp } from '../host'

/** A document instance currently open and running in the viewer. */
export interface RunningApp {
  artifactPath: string
  open: OpenResult
  htmlContent: string
  editMode: boolean
  /** Set when the document is open on the device rather than on the server. */
  source?: DeviceSource
}
