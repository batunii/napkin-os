// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// The host this build talks to. One import for the whole shell; swapping in an
// HTTP implementation for the web build is a change to this file alone.

import { tauriHost } from './tauri'
import type { Host } from './types'

export const host: Host = tauriHost

export type * from './types'
