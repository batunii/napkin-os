import { existsSync } from 'node:fs'
import { fileURLToPath, URL } from 'node:url'

import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// One bundle, every host. The web build talks to napkin-web and loads the
// WebAssembly host only when a file is opened on the device; the serverless
// build (`VITE_NAPKIN_HOST=wasm`, `npm run build:static`) is the same bundle
// with the device host as the only one. See src/host/index.ts.
//
// The WebAssembly module is build output (`npm run wasm`). A checkout without
// it — CI's lint-and-build job, the Tauri build, the Docker shell stage — still
// builds: the device host is swapped for a stub that says what is missing.
const wasmBuilt = existsSync(fileURLToPath(new URL('./src/wasm/napkin_wasm.js', import.meta.url)))

export default defineConfig({
  plugins: [react()],
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
