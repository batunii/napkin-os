// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

import { useEffect, useState } from 'react'

import { host } from '../host'
import type { SpinoffTarget } from '../host'

/**
 * "Continue in…": the apps that have declared they will take the open document
 * as a spin-off source, carrying its data and its decisions across. The bar
 * lists them in its More menu.
 *
 * Targets depend on the document's app, so they are held against the path they
 * were asked for: a different document shows none until its own answer comes.
 * An empty list is a normal state (nothing downstream is installed), not a
 * failure worth a disabled item in the chrome.
 */
export function useSpinoffTargets(docPath: string): SpinoffTarget[] {
  const [got, setGot] = useState<{ docPath: string; targets: SpinoffTarget[] } | null>(null)

  useEffect(() => {
    let live = true
    host.spinoffTargets()
      .then(targets => { if (live) setGot({ docPath, targets }) })
      // A host that cannot answer simply offers nothing; this is chrome, not
      // the document, and must never take the app down with it.
      .catch(() => { if (live) setGot({ docPath, targets: [] }) })
    return () => { live = false }
  }, [docPath])

  return got?.docPath === docPath ? got.targets : []
}
