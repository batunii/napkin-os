// Write app/templates/shared/agent-figures.html: the approved agent figures
// (src/studio/AgentFigure.tsx + AgentFigure.css) as a plain-HTML snippet the
// template apps inline at build (see src/studio/figureSnippet.tsx).
//
//     npm run figures        (also the first step of `npm run apps`)
//
// Bundled with esbuild first, the way tests/run.mjs runs the TypeScript tests.

import { build } from 'esbuild'
import { mkdtempSync, readFileSync, rmSync, writeFileSync, mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const app = fileURLToPath(new URL('..', import.meta.url))
// Inside app/, so the bundle's react-dom import resolves against app/node_modules.
const out = mkdtempSync(join(app, 'scripts/.tmp-'))

try {
  await build({
    entryPoints: [join(app, 'src/studio/figureSnippet.tsx')],
    outfile: join(out, 'snippet.mjs'),
    bundle: true,
    platform: 'node',
    format: 'esm',
    target: 'node20',
    jsx: 'automatic',
    packages: 'external',
    loader: { '.css': 'empty' },
    logLevel: 'warning',
  })
  const { figureSnippet } = await import(pathToFileURL(join(out, 'snippet.mjs')).href)
  const css = readFileSync(join(app, 'src/studio/AgentFigure.css'), 'utf8')
  const dest = join(app, 'templates/shared/agent-figures.html')
  mkdirSync(join(app, 'templates/shared'), { recursive: true })
  writeFileSync(dest, figureSnippet(css))
  console.log(`wrote ${dest}`)
} finally {
  rmSync(out, { recursive: true, force: true })
}
