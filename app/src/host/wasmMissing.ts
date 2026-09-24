// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// Stands in for ../wasm/napkin_wasm in a build made without it (see
// vite.config.ts). Everything that talks to the server still works; opening a
// file on the device says why it cannot, instead of failing on a missing chunk.

const MISSING = 'This build has no offline viewer. Build it with `npm run wasm`, then rebuild.'

export default async function init(): Promise<never> {
  throw new Error(MISSING)
}

export class NapkinHost {
  constructor() {
    throw new Error(MISSING)
  }
}
