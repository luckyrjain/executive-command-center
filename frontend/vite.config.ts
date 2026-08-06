import { fileURLToPath } from 'node:url'

import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

const repoRoot = fileURLToPath(new URL('..', import.meta.url))

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, repoRoot, '')
  // `loadEnv` always loads the repo's plain `.env` regardless of mode (Vite's
  // own documented behavior), so a contributor's local dev `.env` (set for
  // `pnpm dev` against a real backend) otherwise leaks into `vitest` runs and
  // breaks any test asserting an exact relative fetch URL. Tests must never
  // depend on a developer's local dev environment -- force relative URLs
  // under Vitest (`process.env.VITEST` is set by Vitest itself) regardless of
  // what `.env` contains.
  const apiBaseUrl = process.env.VITEST ? '' : (env.VITE_API_BASE_URL ?? '')

  return {
    define: {
      'import.meta.env.VITE_API_BASE_URL': JSON.stringify(apiBaseUrl),
    },
    plugins: [react()],
    server: { port: 5173, strictPort: true },
  }
})
