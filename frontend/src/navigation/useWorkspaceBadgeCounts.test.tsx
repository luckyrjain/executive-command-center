// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { useWorkspaceBadgeCounts } from './useWorkspaceBadgeCounts'

afterEach(() => vi.unstubAllGlobals())

function wrapper({ children }: { children: React.ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>
}

function response(body: unknown) {
  return Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } }))
}

describe('useWorkspaceBadgeCounts', () => {
  it('reads count-shaped responses directly and items.length off list-shaped ones', async () => {
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/api/v1/attention/count')) return response({ count: 4 })
      if (url.includes('/api/v1/tasks/count')) return response({ count: 7 })
      if (url.includes('/api/v1/risks/review-queue')) return response({ items: [{}, {}] })
      if (url.includes('/api/v1/knowledge/resolution/candidates/count')) return response({ count: 0 })
      if (url.includes('/api/v1/automations/approvals')) return response({ approvals: [{}, {}, {}] })
      if (url.includes('/api/v1/recommendations/count')) return response({ count: 1 })
      return response({ error: { code: 'NOT_FOUND', message: 'no fixture route' } })
    }))

    const { result } = renderHook(() => useWorkspaceBadgeCounts(), { wrapper })

    await waitFor(() => {
      expect(result.current).toEqual({
        attention: 4,
        work: 7,
        risks: 2,
        automation: 3,
        recommendations: 1,
        // knowledge omitted: a badge of 0 renders no slot at all (spec:
        // "never a badge showing 0 -- no slot"), so a 0 count is dropped
        // from the returned map rather than included as 0.
      })
    })
  })

  it('omits a workspace entirely while its count is still loading or failed', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response('', { status: 500 }))))
    const { result } = renderHook(() => useWorkspaceBadgeCounts(), { wrapper })
    expect(result.current).toEqual({})
  })
})
