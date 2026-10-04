// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at https://mozilla.org/MPL/2.0/.

import { StrictMode, lazy, Suspense } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { isDesktop, isViewerRoute, serverless } from './host'
import { SignInGate } from './shell/SignIn'
import { registerServiceWorker } from './pwa/pwa'

// The desktop serves its own files; a service worker is for the web.
if (!isDesktop) registerServiceWorker()

// Dev-only figure gallery at ?figures. The DEV check is a build-time constant,
// so a production bundle drops the branch and the chunk with it.
const FigureGallery = import.meta.env.DEV && new URLSearchParams(location.search).has('figures')
  ? lazy(() => import('./studio/FigureGallery'))
  : null

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    {FigureGallery ? <Suspense><FigureGallery /></Suspense> : (
      // A server with accounts asks for a sign-in before the studio opens.
      <SignInGate check={!isDesktop && !serverless && !isViewerRoute}><App /></SignInGate>
    )}
  </StrictMode>,
)
