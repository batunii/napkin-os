// Run the shell's unit tests with Node's own runner.
//
// The tests are TypeScript and import the shell's modules as they are; Node 20
// (CI's version) cannot load .ts, so each test is bundled with esbuild — already
// a dependency — into a temporary directory first. No test framework to add.

import { build } from 'esbuild'
import { spawnSync } from 'node:child_process'
import { mkdtempSync, readdirSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = fileURLToPath(new URL('.', import.meta.url))
const tests = readdirSync(here).filter(f => f.endsWith('.test.ts'))
// Inside app/, so a bundle's external imports (react-dom, for the figure
// test) resolve against app/node_modules.
const out = mkdtempSync(join(here, '.run-'))

try {
  await build({
    entryPoints: tests.map(f => join(here, f)),
    outdir: out,
    outExtension: { '.js': '.mjs' },
    bundle: true,
    platform: 'node',
    format: 'esm',
    target: 'node20',
    packages: 'external',
    // The figure test renders the shell's own React components.
    jsx: 'automatic',
    loader: { '.css': 'empty' },
    logLevel: 'warning',
  })
  const files = readdirSync(out).filter(f => f.endsWith('.mjs')).map(f => join(out, f))
  const run = spawnSync(process.execPath, ['--test', ...files], { stdio: 'inherit' })
  process.exitCode = run.status ?? 1
} finally {
  rmSync(out, { recursive: true, force: true })
}
