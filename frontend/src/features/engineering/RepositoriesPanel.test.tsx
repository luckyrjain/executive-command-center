// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import RepositoriesPanel from './RepositoriesPanel'
import type { Repository } from './types'

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

function repository(overrides: Partial<Repository> = {}): Repository {
  return {
    id: 'repo-1',
    connector_account_id: 'connector-1',
    provider: 'github',
    external_id: 'gh-repo-1',
    name: 'acme/widgets',
    source_url: 'https://github.com/acme/widgets',
    default_branch: 'main',
    permission_state: 'active',
    freshness_state: 'fresh',
    provider_updated_at: '2026-07-20T00:00:00Z',
    observed_at: '2026-07-26T00:00:00Z',
    created_at: '2026-07-01T00:00:00Z',
    updated_at: '2026-07-26T00:00:00Z',
    team_entity_id: null,
    suggested_team_name: null,
    team_assignment_version: 1,
    team_assignment_updated_by: null,
    ...overrides,
  }
}

function stubFetch({
  repositories,
  teams = [],
}: {
  repositories: Repository[]
  teams?: { id: string; canonical_name: string }[]
}) {
  // Stateful, not a fixed response -- `assigning a team...` below asserts
  // the confirmed link survives the post-mutation refetch (`onSuccess`'s
  // `invalidateQueries`), which a fixed-response stub can't prove.
  const state = repositories.map((repo) => ({ ...repo }))
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/knowledge/entities')) return response({ items: teams })
      if (init?.method === 'POST' && url.includes('/team')) {
        const teamEntityId = (JSON.parse(String(init.body)) as { team_entity_id: string | null }).team_entity_id
        const repositoryId = url.split('/repositories/')[1]?.split('/team')[0]
        const repo = state.find((candidate) => candidate.id === repositoryId)
        if (repo) {
          repo.team_entity_id = teamEntityId
          repo.team_assignment_version += 1
        }
        return response(repo ?? {})
      }
      // Mirrors the real backend's `?team_entity_id=` filter
      // (`list_repositories_endpoint`) so the "filter by team" tests below
      // can prove the panel actually sends and reacts to it, not just that
      // the select exists.
      const teamEntityId = new URL(url, 'http://localhost').searchParams.get('team_entity_id')
      const filtered = teamEntityId ? state.filter((repo) => repo.team_entity_id === teamEntityId) : state
      return response({ repositories: filtered })
    }),
  )
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><RepositoriesPanel /></QueryClientProvider>)
}

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('RepositoriesPanel', () => {
  it('shows an empty state ("first sync") when nothing has synced yet', async () => {
    stubFetch({ repositories: [] })
    renderPanel()
    expect(await screen.findByText(/No repositories have synced yet/)).toBeTruthy()
  })

  it('lists a repository with its provider and a link to view it', async () => {
    stubFetch({ repositories: [repository()] })
    renderPanel()
    expect(await screen.findByText('acme/widgets')).toBeTruthy()
    const link = screen.getByRole('link', { name: 'View on github' })
    expect(link.getAttribute('href')).toBe('https://github.com/acme/widgets')
  })

  it('shows the partial-permissions state for a repository that lost permission', async () => {
    stubFetch({ repositories: [repository({ permission_state: 'permission_lost' })] })
    renderPanel()
    expect(await screen.findByText('permission lost')).toBeTruthy()
  })

  it('shows the stale-connector state for a repository whose content has gone stale', async () => {
    stubFetch({ repositories: [repository({ freshness_state: 'stale' })] })
    renderPanel()
    expect(await screen.findByText('stale')).toBeTruthy()
  })

  it('surfaces a load failure as an alert, not a silent empty list', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new TypeError('fetch failed'))))
    renderPanel()
    expect(await screen.findByRole('alert', {}, { timeout: 3000 })).toBeTruthy()
  })

  // --- team linkage (migration `0050_phase6_team_linkage.py`) -------------

  it('excludes archived/redirected teams from the team picker', async () => {
    // Without `status=active`, a team merged away via the Knowledge
    // Platform's own resolution flow stayed selectable forever, and
    // confirming it 404'd server-side with no explanation in the UI.
    stubFetch({ repositories: [repository()] })
    renderPanel()
    await screen.findByText('acme/widgets')
    const entitiesCall = (fetch as unknown as { mock: { calls: [RequestInfo | URL][] } }).mock.calls.find(([input]) =>
      String(input).includes('/knowledge/entities'),
    )
    expect(entitiesCall).toBeTruthy()
    expect(String(entitiesCall?.[0])).toContain('status=active')
  })

  it('shows the suggested team name as a hint when unassigned', async () => {
    stubFetch({ repositories: [repository({ suggested_team_name: 'acme' })] })
    renderPanel()
    expect(await screen.findByText('suggested: acme')).toBeTruthy()
  })

  it('does not show the suggestion hint once a team is confirmed', async () => {
    stubFetch({
      repositories: [repository({ team_entity_id: 'team-1', suggested_team_name: 'acme' })],
      teams: [{ id: 'team-1', canonical_name: 'Platform Engineering' }],
    })
    renderPanel()
    await screen.findByText('acme/widgets')
    expect(screen.queryByText(/suggested:/)).toBeNull()
    expect((screen.getByLabelText('Team for acme/widgets') as HTMLSelectElement).value).toBe('team-1')
  })

  it('still shows the correct assignment when the confirmed team is outside the first 100 fetched', async () => {
    // `listTeams` fetches at most 100 teams; a repository confirmed to a
    // team outside that page must not silently render as "Unassigned" --
    // the `<select>`'s controlled `value` needs an `<option>` to match.
    stubFetch({
      repositories: [repository({ team_entity_id: 'team-not-in-page' })],
      teams: [{ id: 'team-1', canonical_name: 'Platform Engineering' }],
    })
    renderPanel()
    const select = (await screen.findByLabelText('Team for acme/widgets')) as HTMLSelectElement
    expect(select.value).toBe('team-not-in-page')
    expect(screen.getByText('Assigned team (not in first 100)')).toBeTruthy()
  })

  it('assigning a team from the dropdown posts the confirmed link and refetches', async () => {
    stubFetch({
      repositories: [repository()],
      teams: [{ id: 'team-1', canonical_name: 'Platform Engineering' }],
    })
    renderPanel()
    const select = await screen.findByLabelText('Team for acme/widgets')
    fireEvent.change(select, { target: { value: 'team-1' } })

    await waitFor(() => {
      expect((screen.getByLabelText('Team for acme/widgets') as HTMLSelectElement).value).toBe('team-1')
    })
    const calls = (fetch as unknown as { mock: { calls: [RequestInfo | URL, RequestInit?][] } }).mock.calls
    const postCall = calls.find(([, init]) => init?.method === 'POST')
    expect(String(postCall?.[0])).toContain('/api/v1/engineering/repositories/repo-1/team')
    expect(JSON.parse(String(postCall?.[1]?.body))).toEqual({ expected_version: 1, team_entity_id: 'team-1' })
  })

  it('surfaces a failed team assignment as an alert next to the dropdown, and refetches so the stale expected_version doesn\'t linger', async () => {
    // Round-trip fixture, not just an error-message assertion: an earlier
    // draft of this handler had no onError refetch at all, so this row's
    // team_assignment_version stayed stale after a 409 -- a retry click
    // would submit the same now-wrong expected_version and just 409 again
    // in a loop. The GET after the 409 returns a row someone else already
    // bumped to version 2, proving the panel actually re-reads current
    // state rather than only displaying an error string.
    let getCount = 0
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input)
        if (url.includes('/knowledge/entities')) return response({ items: [{ id: 'team-1', canonical_name: 'Platform Engineering' }] })
        if (init?.method === 'POST' && url.includes('/team')) {
          return response({ error: { code: 'VERSION_CONFLICT', message: 'Someone else changed this first.' } }, 409)
        }
        getCount += 1
        if (getCount === 1) return response({ repositories: [repository()] })
        return response({ repositories: [repository({ team_assignment_version: 2, suggested_team_name: 'acme' })] })
      }),
    )
    renderPanel()
    const select = await screen.findByLabelText('Team for acme/widgets')
    fireEvent.change(select, { target: { value: 'team-1' } })

    expect(await screen.findByRole('alert')).toBeTruthy()
    await waitFor(() => expect(getCount).toBe(2))
    expect(await screen.findByText('suggested: acme')).toBeTruthy()
  })

  // --- team filter (read-only view filter, distinct from TeamAssignment) --

  it('filtering by team only shows repositories assigned to that team', async () => {
    stubFetch({
      repositories: [
        repository({ id: 'repo-1', name: 'acme/widgets', team_entity_id: 'team-1' }),
        repository({ id: 'repo-2', name: 'acme/gadgets', team_entity_id: null }),
      ],
      teams: [{ id: 'team-1', canonical_name: 'Platform Engineering' }],
    })
    renderPanel()
    await screen.findByText('acme/widgets')
    expect(await screen.findByText('acme/gadgets')).toBeTruthy()

    fireEvent.change(screen.getByLabelText('Filter repositories by team'), { target: { value: 'team-1' } })

    expect(await screen.findByText('acme/widgets')).toBeTruthy()
    await waitFor(() => expect(screen.queryByText('acme/gadgets')).toBeNull())
    const calls = (fetch as unknown as { mock: { calls: [RequestInfo | URL, RequestInit?][] } }).mock.calls
    expect(calls.some(([url]) => String(url).includes('team_entity_id=team-1'))).toBe(true)
  })

  it('shows a team-specific empty state when the filtered team has no repositories', async () => {
    stubFetch({
      repositories: [repository({ team_entity_id: null })],
      teams: [{ id: 'team-1', canonical_name: 'Platform Engineering' }],
    })
    renderPanel()
    await screen.findByText('acme/widgets')

    fireEvent.change(screen.getByLabelText('Filter repositories by team'), { target: { value: 'team-1' } })

    expect(await screen.findByText('No repositories are assigned to this team yet.')).toBeTruthy()
  })
})
