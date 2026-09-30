// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// The host's decision view of the open document, read once for the whole
// shell: the decision panel renders it, and the top bar and client review
// read the lock and the client's answers from it.

import { useCallback, useEffect, useState } from 'react'
import { host } from '../../host'
import type { DecisionsView } from '../../host'

export interface Decisions {
  view: DecisionsView | null
  error: string | null
  reload: () => void
}

export function useDecisions(docPath: string): Decisions {
  // Keyed by the document it was read for, so a new document never shows the
  // old one's view while its own is on the way.
  const [got, setGot] = useState<{ docPath: string; view: DecisionsView | null; error: string | null }>(
    { docPath, view: null, error: null },
  )

  const reload = useCallback(() => {
    host.getDecisions().then(
      v => setGot({ docPath, view: v, error: null }),
      e => setGot(g => ({ docPath, view: g.docPath === docPath ? g.view : null, error: String(e instanceof Error ? e.message : e) })),
    )
  }, [docPath])

  useEffect(() => { reload() }, [reload])

  // Every write the host fans out can change the chain.
  useEffect(() => {
    const offs = [
      host.on('clan-data-changed', reload),
      host.on('clan-patch-saved', reload),
      host.on('clan-title-changed', reload),
    ]
    return () => { for (const off of offs) off.then(f => f()).catch(() => {}) }
  }, [reload])

  const mine = got.docPath === docPath
  return { view: mine ? got.view : null, error: mine ? got.error : null, reload }
}
