// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

import type { OpenResult } from '../App'

export type Screen = 'home' | 'app'

/** A template app installed in the local library, listed on the launcher. */
export interface InstalledApp {
  app_id: string
  name: string
  version: string
  path: string
  icon?: string | null
}

/** A document instance currently open and running in the viewer. */
export interface RunningApp {
  artifactPath: string
  open: OpenResult
  htmlContent: string
  editMode: boolean
}
