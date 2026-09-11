// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import SidebarNavigation from './SidebarNavigation'

afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks() })

function renderAt(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response('', { status: 500 }))))
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
})
