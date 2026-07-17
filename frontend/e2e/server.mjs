import { spawn } from 'node:child_process'

const HOST = '127.0.0.1'
const PORT = 4173
export const BASE_URL = `http://${HOST}:${PORT}`

async function waitForServer() {
  for (let attempt = 0; attempt < 60; attempt += 1) {
    try {
      const response = await fetch(BASE_URL)
      if (response.ok) return
    } catch {
      // Preview is still starting.
    }
    await new Promise((resolve) => setTimeout(resolve, 500))
  }
  throw new Error('Vite preview did not start')
}

/**
 * Spawns `vite preview` and waits until it accepts connections.
 * Returns a handle whose `stop()` terminates the child process.
 */
export async function startPreviewServer() {
  const preview = spawn('pnpm', ['exec', 'vite', 'preview', '--host', HOST, '--port', String(PORT)], {
    stdio: 'inherit',
  })

  try {
    await waitForServer()
  } catch (error) {
    preview.kill('SIGTERM')
    throw error
  }

  return {
    baseURL: BASE_URL,
    stop: () => preview.kill('SIGTERM'),
  }
}
