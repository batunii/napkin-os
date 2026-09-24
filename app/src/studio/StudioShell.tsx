// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

import type { ReactNode } from 'react'
import { StudioLogo } from '../brand/StudioMark'
import ThemeToggle from '../components/ThemeToggle'
import './StudioShell.css'

interface Props {
  /** The home screen: the apps launcher. */
  children: ReactNode
  /** Extra controls at the start of the tools, e.g. the Offline section. */
  tools?: ReactNode
}

/** The studio frame: frosted top bar with the brand and the tools. */
export default function StudioShell({ children, tools }: Props) {
  return (
    <div className="studio">
      <header className="studio-top">
        <StudioLogo size={15} />
        <div className="studio-tools">
          {tools}
          <ThemeToggle />
        </div>
      </header>
      <main className="studio-panel">
        {children}
      </main>
    </div>
  )
}
