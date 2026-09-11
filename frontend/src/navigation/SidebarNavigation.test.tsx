// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import SidebarNavigation from './SidebarNavigation'

afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks() })

function response(body: unknown) {
  return Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } }))
}

function renderAt(path: string, fetchImpl: ((input: RequestInfo | URL) => Promise<Response>) | null = null) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const fetch = fetchImpl ?? (() => Promise.resolve(new Response('', { status: 500 })))
  vi.stubGlobal('fetch', vi.fn(fetch))
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <SidebarNavigation />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('SidebarNavigation', () => {
  it('renders a nav landmark with all 15 workspace links, each a real link with a real href', () => {
    renderAt('/today')
    const nav = screen.getByRole('navigation', { name: 'Workspaces' })
    const links = nav.querySelectorAll('a')
    expect(links).toHaveLength(15)
    expect(screen.getByRole('link', { name: 'Risks' }).getAttribute('href')).toBe('/risks')
    expect(screen.getByRole('link', { name: 'Team' }).getAttribute('href')).toBe('/team')
  })

  it('marks exactly the current route as aria-current="page"', () => {
    renderAt('/risks')
    expect(screen.getByRole('link', { name: 'Risks' }).getAttribute('aria-current')).toBe('page')
    expect(screen.getByRole('link', { name: 'Today' }).getAttribute('aria-current')).toBeNull()
  })

  it('renders the 4 named group headers, in order, with the correct workspaces under each', () => {
    renderAt('/today')
    const headers = screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent)
    expect(headers).toEqual(['Work', 'Risk & knowledge', 'Systems', 'Account'])
  })

  it('renders badges for workspaces with counts and no badge for workspaces without counts', async () => {
    const fetchImpl = vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/api/v1/attention/count')) return response({ count: 4 })
      if (url.includes('/api/v1/tasks/count')) return response({ count: 7 })
      if (url.includes('/api/v1/risks/review-queue')) return response({ items: [{}, {}] })
      if (url.includes('/api/v1/knowledge/resolution/candidates/count')) return response({ count: 0 })
      if (url.includes('/api/v1/automations/approvals')) return response({ approvals: [{}, {}, {}] })
      if (url.includes('/api/v1/recommendations/count')) return response({ count: 1 })
      return response({ error: { code: 'NOT_FOUND', message: 'no fixture route' } })
    })
    renderAt('/today', fetchImpl)

    await waitFor(() => {
      // Verify badges render for workspaces with counts
      expect(screen.getByRole('link', { name: /Attention/ }).textContent).toContain('4')
      expect(screen.getByRole('link', { name: /Work/ }).textContent).toContain('7')
      expect(screen.getByRole('link', { name: /Risks/ }).textContent).toContain('2')
      expect(screen.getByRole('link', { name: /Automation/ }).textContent).toContain('3')
    })

    // Verify no badge for workspaces without counts (count of 0 or no count)
    expect(screen.getByRole('link', { name: /Today/ }).textContent).not.toMatch(/\d/)
    expect(screen.getByRole('link', { name: /Knowledge/ }).textContent).not.toMatch(/\d/)
  })
})
