// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

// What a studio view can ask of the shell.
//
// Floor and Decisions take no props: they read this context instead, so the
// shell can grow new abilities without every view's signature changing. The
// shell (App.tsx) owns the state; views only ask.

import { createContext, useContext } from 'react'
import type { RecentDoc } from '../host'
import type { InstalledApp } from '../shell/types'

/** The top-level views behind the tab control. Documents are not a view —
 *  opening one leaves the studio for the document view (AppHost). */
export type StudioView = 'floor' | 'decisions' | 'apps'

export const STUDIO_VIEWS: readonly { id: StudioView; label: string }[] = [
  { id: 'floor', label: 'Floor' },
  { id: 'decisions', label: 'Decisions' },
  { id: 'apps', label: 'Apps' },
]

export interface Studio {
  view: StudioView
  /** Switch tabs. */
  go: (view: StudioView) => void
  /** Apps installed on this host, for views that want to offer one. */
  installed: InstalledApp[]
  /** Documents in the store, newest first. Refreshed with the installed apps. */
  recent: RecentDoc[]
  /** Start a new document in an installed app and open it. */
  launchApp: (appId: string) => void
  /** Open a .clan by path: templates prompt to install, documents open. */
  openPath: (path: string) => void
  /** Ask the host for a .clan to open. */
  openFile: () => void
}

export const StudioContext = createContext<Studio | null>(null)

export function useStudio(): Studio {
  const s = useContext(StudioContext)
  if (!s) throw new Error('useStudio() outside the studio shell')
  return s
}
