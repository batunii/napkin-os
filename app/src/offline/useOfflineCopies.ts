// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

import { useEffect, useState } from 'react'
import { listCopies, onCopiesChanged, type OfflineCopy } from './store'

/**
 * The copies on this device, read straight from IndexedDB — so it works with
 * no network. `null` until the first read; empty if storage is unavailable.
 */
export function useOfflineCopies(): OfflineCopy[] | null {
  const [copies, setCopies] = useState<OfflineCopy[] | null>(null)
  useEffect(() => {
    let live = true
    const load = () => listCopies().then(c => { if (live) setCopies(c) }).catch(e => {
      console.error('offline copies unavailable', e)
      if (live) setCopies([])
    })
    load()
    const off = onCopiesChanged(load)
    return () => { live = false; off() }
  }, [])
  return copies
}
