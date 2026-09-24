import { createHash } from 'node:crypto'
import { existsSync, readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import { join, relative, resolve, sep } from 'node:path'
import { fileURLToPath, URL } from 'node:url'

import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'

// One bundle, every host. The web build talks to napkin-web and loads the
// WebAssembly host only when a file is opened on the device; the serverless
// build (`VITE_NAPKIN_HOST=wasm`, `npm run build:static`) is the same bundle
// with the device host as the only one. See src/host/index.ts.
//
// The WebAssembly module is build output (`npm run wasm`). A checkout without
// it — CI's lint-and-build job, the Tauri build, the Docker shell stage before
// it was taught to build one — still builds: the device host is swapped for a
// stub that says what is missing, and the service worker does not precache
// what is not there.
const wasmBuilt = existsSync(fileURLToPath(new URL('./src/wasm/napkin_wasm.js', import.meta.url)))

/** Files under dist/ that the service worker must not precache. */
function skipPrecache(path: string): boolean {
  return (
    path === 'sw.js' ||
    path === '404.html' ||
    path.endsWith('.map') ||
    // A dev fixture, and a sample document: neither is part of the shell.
    path.startsWith('mock-clan/') ||
    path.endsWith('.example.clan')
  )
}

function walk(dir: string, root = dir): string[] {
  return readdirSync(dir).flatMap(name => {
    const full = join(dir, name)
    return statSync(full).isDirectory() ? walk(full, root) : [relative(root, full).split(sep).join('/')]
  })
}

/**
 * Write the service worker once the build is on disk.
 *
 * Hand-written (pwa/sw.js) rather than a plugin dependency: it has four rules,
 * and the only part that needs the build is the list of files to precache and
 * a version to name the cache by. The version is a hash of every precached
 * file, so any change to the shell, the wasm or a template is a new cache that
 * replaces the old one whole, and an unchanged build is not an update at all.
 */
function serviceWorker(): Plugin {
  let outDir = 'dist'
  return {
    name: 'napkin-service-worker',
    apply: 'build',
    configResolved(config) {
      outDir = resolve(config.root, config.build.outDir)
    },
    closeBundle() {
      const root = outDir
      const files = walk(root).filter(p => !skipPrecache(p)).sort()
      const hash = createHash('sha256')
      for (const f of files) hash.update(f).update('\0').update(readFileSync(join(root, f)))
      const template = readFileSync(fileURLToPath(new URL('./pwa/sw.js', import.meta.url)), 'utf8')
      hash.update(template)
      const version = hash.digest('hex').slice(0, 12)
      const sw = template
        .replace('__NAPKIN_VERSION__', () => JSON.stringify(version))
        .replace('__NAPKIN_PRECACHE__', () => JSON.stringify(files, null, 2))
      writeFileSync(join(root, 'sw.js'), sw)
    },
  }
}

export default defineConfig({
  plugins: [react(), serviceWorker()],
  // Set for a project page, whose site lives under /<repo>/.
  base: process.env.VITE_BASE || '/',
  resolve: {
    alias: wasmBuilt
      ? []
      : [{
          find: /^\.\.\/wasm\/napkin_wasm$/,
          replacement: fileURLToPath(new URL('./src/host/wasmMissing.ts', import.meta.url)),
        }],
  },
  clearScreen: false,
  server: {
    port: 1420,
    strictPort: true,
    host: process.env.TAURI_DEV_HOST || false,
    watch: { ignored: ['**/src-tauri/**'] },
  },
  envPrefix: ['VITE_', 'TAURI_ENV_*'],
  build: {
    target: 'es2022',
    minify: 'esbuild',
    sourcemap: false,
  },
})
