// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import TeamSuggestionsPanel from './TeamSuggestionsPanel'
import type { TeamSuggestionGroup } from './types'

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

function group(overrides: Partial<TeamSuggestionGroup> = {}): TeamSuggestionGroup {
  return {
    suggested_team_name: 'acme',
    repository_count: 2,
    work_item_count: 1,
    sample_items: [
      { id: 'repo-1', resource_type: 'repository', name: 'acme/widgets' },
      { id: 'repo-2', resource_type: 'repository', name: 'acme/gadgets' },
      { id: 'wi-1', resource_type: 'work_item', name: 'ACME-1' },
    ],
    ...overrides,
  }
}

function stubFetch({
  groups,
  teams = [],
}: {
  groups: TeamSuggestionGroup[]
  teams?: { id: string; canonical_name: string }[]
}) {
  // Stateful, not a fixed response -- proves confirm/dismiss remove the
  // group from the next refetch, mirroring RepositoriesPanel.test.tsx's
  // own identical pattern for the same reason.
  const state = groups.map((g) => ({ ...g }))
  let nextTeamId = 1
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/knowledge/entities')) {
        if (init?.method === 'POST') {
          const body = JSON.parse(String(init.body)) as { kind: string; canonical_name: string }
          return response({ id: `new-team-${nextTeamId++}`, kind: body.kind, canonical_name: body.canonical_name })
        }
        return response({ items: teams })
      }
      if (init?.method === 'POST' && url.includes('/team-suggestions/confirm')) {
        const body = JSON.parse(String(init.body)) as { suggested_team_name: string }
        const index = state.findIndex((g) => g.suggested_team_name === body.suggested_team_name)
        const removed = index >= 0 ? state.splice(index, 1)[0] : undefined
        const count = removed ? removed.repository_count + removed.work_item_count : 0
        return response({
          updated: Array.from({ length: count }, (_, i) => `id-${i}`),
          skipped_unauthorized: [],
        })
      }
      if (init?.method === 'POST' && url.includes('/team-suggestions/dismiss')) {
        const body = JSON.parse(String(init.body)) as { suggested_team_name: string }
        const index = state.findIndex((g) => g.suggested_team_name === body.suggested_team_name)
        if (index >= 0) state.splice(index, 1)
        return response({ updated: [], skipped_unauthorized: [] })
      }
      return response({ items: state })
    }),
  )
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><TeamSuggestionsPanel /></QueryClientProvider>)
}

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('TeamSuggestionsPanel', () => {
  it('shows an empty state when there are no pending suggestions', async () => {
    stubFetch({ groups: [] })
    renderPanel()
    expect(await screen.findByText('No pending team suggestions.')).toBeTruthy()
  })

  it('lists a suggestion group with its repository/work-item counts', async () => {
    stubFetch({ groups: [group()] })
    renderPanel()
    expect(await screen.findByText('acme')).toBeTruthy()
    expect(screen.getByText('2 repositories · 1 work items (3 total)')).toBeTruthy()
  })

  it('shows the sample items for a group', async () => {
    stubFetch({ groups: [group()] })
    renderPanel()
    expect(await screen.findByText('acme/widgets (repository)')).toBeTruthy()
    expect(screen.getByText('ACME-1 (work_item)')).toBeTruthy()
  })

  it('disables Confirm until a team is picked', async () => {
    stubFetch({ groups: [group()], teams: [{ id: 'team-1', canonical_name: 'Platform' }] })
    renderPanel()
    await screen.findByText('acme')
    const confirmButton = screen.getByRole('button', { name: 'Confirm' })
    expect(confirmButton.hasAttribute('disabled')).toBe(true)
    fireEvent.change(screen.getByLabelText('Assign team for acme'), { target: { value: 'team-1' } })
    expect(confirmButton.hasAttribute('disabled')).toBe(false)
  })

  it('disables Create & confirm when the team name is empty', async () => {
    stubFetch({ groups: [group()], teams: [] })
    renderPanel()
    await screen.findByText('acme')
    const createButton = screen.getByRole('button', { name: 'Create & confirm' })
    expect(createButton.hasAttribute('disabled')).toBe(false)

    fireEvent.change(screen.getByLabelText('New team name for acme'), { target: { value: '   ' } })
    expect(createButton.hasAttribute('disabled')).toBe(true)

    fireEvent.change(screen.getByLabelText('New team name for acme'), { target: { value: 'Acme Platform' } })
    expect(createButton.hasAttribute('disabled')).toBe(false)
  })

  it('confirming posts the suggested name and chosen team, then removes the group', async () => {
    stubFetch({ groups: [group()], teams: [{ id: 'team-1', canonical_name: 'Platform' }] })
    renderPanel()
    await screen.findByText('acme')
    fireEvent.change(screen.getByLabelText('Assign team for acme'), { target: { value: 'team-1' } })
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))

    await waitFor(() => expect(screen.queryByText('acme')).toBeNull())
    const calls = (fetch as unknown as { mock: { calls: [RequestInfo | URL, RequestInit?][] } }).mock.calls
    const postCall = calls.find(([, init]) => init?.method === 'POST')
    expect(String(postCall?.[0])).toContain('/api/v1/engineering/team-suggestions/confirm')
    expect(JSON.parse(String(postCall?.[1]?.body))).toEqual({ suggested_team_name: 'acme', team_entity_id: 'team-1' })
  })

  it('creating a team from the suggestion posts create then confirm, then removes the group', async () => {
    stubFetch({ groups: [group()], teams: [] })
    renderPanel()
    await screen.findByText('acme')

    fireEvent.click(screen.getByRole('button', { name: 'Create & confirm' }))

    await waitFor(() => expect(screen.queryByText('acme')).toBeNull())
    const calls = (fetch as unknown as { mock: { calls: [RequestInfo | URL, RequestInit?][] } }).mock.calls
    const createCall = calls.find(([url, init]) => init?.method === 'POST' && String(url).includes('/knowledge/entities'))
    expect(JSON.parse(String(createCall?.[1]?.body))).toEqual({ kind: 'team', canonical_name: 'acme' })

    const confirmCall = calls.find(([url, init]) => init?.method === 'POST' && String(url).includes('/team-suggestions/confirm'))
    expect(JSON.parse(String(confirmCall?.[1]?.body))).toEqual({ suggested_team_name: 'acme', team_entity_id: 'new-team-1' })
  })

  it('keeps the created team usable when confirm fails after create succeeds', async () => {
    let createdTeam = false
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input)
        if (url.includes('/knowledge/entities')) {
          if (init?.method === 'POST') {
            const body = JSON.parse(String(init.body)) as { kind: string; canonical_name: string }
            createdTeam = true
            return response({ id: 'new-team-1', kind: body.kind, canonical_name: body.canonical_name })
          }
          return response({ items: createdTeam ? [{ id: 'new-team-1', canonical_name: 'acme' }] : [] })
        }
        if (init?.method === 'POST' && url.includes('/team-suggestions/confirm')) {
          return Promise.reject(new TypeError('fetch failed'))
        }
        return response({ items: [group()] })
      }),
    )
    renderPanel()
    await screen.findByText('acme')

    fireEvent.click(screen.getByRole('button', { name: 'Create & confirm' }))

    expect(await screen.findByRole('alert', {}, { timeout: 3000 })).toBeTruthy()
    expect(screen.getByText('2 repositories · 1 work items (3 total)')).toBeTruthy()
    expect(screen.getByRole('option', { name: 'acme' })).toBeTruthy()
  })

  it('dismissing removes the group without assigning a team', async () => {
    stubFetch({ groups: [group()] })
    renderPanel()
    await screen.findByText('acme')
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }))

    await waitFor(() => expect(screen.queryByText('acme')).toBeNull())
    const calls = (fetch as unknown as { mock: { calls: [RequestInfo | URL, RequestInit?][] } }).mock.calls
    const postCall = calls.find(([, init]) => init?.method === 'POST')
    expect(String(postCall?.[0])).toContain('/api/v1/engineering/team-suggestions/dismiss')
    expect(JSON.parse(String(postCall?.[1]?.body))).toEqual({ suggested_team_name: 'acme' })
  })

  it('shows a partial-authorization message when some items are skipped', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input)
        if (url.includes('/knowledge/entities')) return response({ items: [{ id: 'team-1', canonical_name: 'Platform' }] })
        if (init?.method === 'POST' && url.includes('/confirm')) {
          return response({ updated: ['repo-1', 'repo-2'], skipped_unauthorized: ['wi-1'] })
        }
        return response({ items: [group()] })
      }),
    )
    renderPanel()
    await screen.findByText('acme')
    fireEvent.change(screen.getByLabelText('Assign team for acme'), { target: { value: 'team-1' } })
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))

    expect(
      await screen.findByText('Applied to 2 of 3 — 1 skipped: insufficient permission.'),
    ).toBeTruthy()
  })

  it('surfaces a load failure as an alert, not a silent empty list', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input)
        if (url.includes('/knowledge/entities')) return response({ items: [] })
        return Promise.reject(new TypeError('fetch failed'))
      }),
    )
    renderPanel()
    expect(await screen.findByRole('alert', {}, { timeout: 3000 })).toBeTruthy()
  })

  it('shows an alert when the team picker fails to load, without blocking the group list', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input)
        if (url.includes('/knowledge/entities')) return Promise.reject(new TypeError('fetch failed'))
        return response({ items: [group()] })
      }),
    )
    renderPanel()
    await screen.findByText('acme')
    expect(await screen.findByRole('alert', {}, { timeout: 3000 })).toBeTruthy()
    expect(screen.getByText(/Could not load teams to assign/)).toBeTruthy()
  })
})
