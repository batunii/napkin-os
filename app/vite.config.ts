import { fileURLToPath, URL } from 'node:url'

import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Which host the shell talks to, decided at build time rather than sniffed —
// it determines what gets bundled, and a 5 MB WebAssembly host has no business
// in a build that is going to talk to a server anyway.
//
//   npm run build              -> talks to napkin-web over HTTP
//   VITE_NAPKIN_HOST=wasm ...  -> no server at all; the host runs in the page
const backend = process.env.VITE_NAPKIN_HOST === 'wasm' ? 'wasm' : 'http'

export default defineConfig({
  plugins: [react()],
  // Set for a project page, whose site lives under /<repo>/.
  base: process.env.VITE_BASE || '/',
  resolve: {
    alias: {
      '@backend': fileURLToPath(new URL(`./src/host/${backend}.ts`, import.meta.url)),
    },
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
