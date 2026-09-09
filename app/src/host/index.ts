// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// The host this shell is running against, decided at load.
//
// One bundle serves both: inside the desktop app Tauri has already installed
// its bridge on `window` by the time this module is evaluated, and its absence
// means we are a page served by `napkin-web`.

import { backendHost } from '@backend'
import { tauriHost } from './tauri'
import type { Host } from './types'

export const isDesktop = '__TAURI_INTERNALS__' in window

/**
 * The serverless build is chosen at compile time (`VITE_NAPKIN_HOST=wasm`),
 * not sniffed: it decides what gets bundled. A 5 MB WebAssembly host has no
 * business in a build that is going to talk to a server anyway.
 */
export const host: Host = isDesktop ? tauriHost : backendHost

export type * from './types'
