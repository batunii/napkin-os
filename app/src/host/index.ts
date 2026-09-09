// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// The host this shell is running against, decided at load.
//
// One bundle serves both: inside the desktop app Tauri has already installed
// its bridge on `window` by the time this module is evaluated, and its absence
// means we are a page served by `napkin-web`.

import { httpHost } from './http'
import { tauriHost } from './tauri'
import type { Host } from './types'

export const isDesktop = '__TAURI_INTERNALS__' in window

export const host: Host = isDesktop ? tauriHost : httpHost

export type * from './types'
