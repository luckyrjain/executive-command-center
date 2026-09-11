// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import App from './App'
import { WORKSPACES } from './navigation/workspaces'

// Nothing today checks that every path in workspaces.ts (which drives
// SidebarNavigation's links) actually resolves to real routed content in
// App.tsx's own <Route> table -- the two happen to agree on all 15 paths,
// but nothing would fail if someone added/renamed a workspace in one and
// forgot the other, shipping a sidebar link that 404s. This renders the
// real <App /> (not a synthetic harness) at each of WORKSPACES's paths and
// asserts none of them fall through to the "*" catch-all's not-found page.
//
// App.tsx owns its own <BrowserRouter>, so this can't be wrapped in a
// <MemoryRouter> the way SidebarNavigation.test.tsx wraps that component --
// instead each case pushes the target path onto real browser history before
// mounting a fresh <App />, the same thing a real navigation would do.

function renderAppAt(path: string) {
  window.history.pushState({}, '', path)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><App /></QueryClientProvider>)
}

beforeEach(() => {
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'app-routing-test-uuid') })
  // A generic failing fetch is enough here -- every feature workspace
  // already renders its own loading/error UI on a failed request (see
  // TodayPage.test.tsx), and this test only cares whether the route hits
  // real page content or the "*" not-found fallback, not whether any one
  // workspace's data loaded successfully.
  vi.stubGlobal(
    'fetch',
    vi.fn(() =>
      Promise.resolve(
        new Response(JSON.stringify({ error: { code: 'INTERNAL', message: 'no fixture in this test' } }), {
          status: 500,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    ),
  )
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('App routing', () => {
  it.each(WORKSPACES.map((workspace) => [workspace.view, workspace.path] as const))(
    "workspaces.ts's %s workspace (%s) resolves to a real route, not the not-found page",
    (_view, path) => {
      renderAppAt(path)
      expect(screen.queryByText(/page not found/i)).toBeNull()
    },
  )
})
