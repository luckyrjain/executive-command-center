// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import WorkItemsPanel from './WorkItemsPanel'
import type { WorkItem } from './types'

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

function workItem(overrides: Partial<WorkItem> = {}): WorkItem {
  return {
    id: 'item-1',
    connector_account_id: 'connector-1',
    provider: 'jira',
    external_id: 'ACME-1',
    title: 'Fix the widget',
    source_url: 'https://acme.atlassian.net/browse/ACME-1',
    item_type: 'Bug',
    status: 'In Progress',
    reporter_external_id: 'reporter-1',
    assignee_external_id: 'assignee-1',
    permission_state: 'active',
    freshness_state: 'fresh',
    provider_created_at: '2026-07-15T00:00:00Z',
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
  workItems,
  teams = [],
}: {
  workItems: WorkItem[]
  teams?: { id: string; canonical_name: string }[]
}) {
  // Stateful, not a fixed response -- mirrors `RepositoriesPanel.test.tsx`'s
  // own identical reasoning: `assigning a team...` below asserts the
  // confirmed link survives the post-mutation refetch.
  const state = workItems.map((item) => ({ ...item }))
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/knowledge/entities')) return response({ items: teams })
      if (init?.method === 'POST' && url.includes('/team')) {
        const teamEntityId = (JSON.parse(String(init.body)) as { team_entity_id: string | null }).team_entity_id
        const workItemId = url.split('/work-items/')[1]?.split('/team')[0]
        const item = state.find((candidate) => candidate.id === workItemId)
        if (item) {
          item.team_entity_id = teamEntityId
          item.team_assignment_version += 1
        }
        return response(item ?? {})
      }
      const teamEntityId = new URL(url, 'http://localhost').searchParams.get('team_entity_id')
      const filtered = teamEntityId ? state.filter((item) => item.team_entity_id === teamEntityId) : state
      return response({ work_items: filtered })
    }),
  )
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><WorkItemsPanel /></QueryClientProvider>)
}

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('WorkItemsPanel', () => {
  it('shows an empty state ("first sync") when nothing has synced yet', async () => {
    stubFetch({ workItems: [] })
    renderPanel()
    expect(await screen.findByText(/No work items have synced yet/)).toBeTruthy()
  })

  it('lists a work item with its provider, status and a link to view it', async () => {
    stubFetch({ workItems: [workItem()] })
    renderPanel()
    expect(await screen.findByText('Fix the widget')).toBeTruthy()
    expect(screen.getByText('In Progress')).toBeTruthy()
    const link = screen.getByRole('link', { name: 'View on jira' })
    expect(link.getAttribute('href')).toBe('https://acme.atlassian.net/browse/ACME-1')
  })

  it('shows the partial-permissions state for a work item that lost permission', async () => {
    stubFetch({ workItems: [workItem({ permission_state: 'permission_lost' })] })
    renderPanel()
    expect(await screen.findByText('permission lost')).toBeTruthy()
  })

  it('shows the stale-connector state for a work item whose content has gone stale', async () => {
    stubFetch({ workItems: [workItem({ freshness_state: 'stale' })] })
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
    stubFetch({ workItems: [workItem()] })
    renderPanel()
    await screen.findByText('Fix the widget')
    const entitiesCall = (fetch as unknown as { mock: { calls: [RequestInfo | URL][] } }).mock.calls.find(([input]) =>
      String(input).includes('/knowledge/entities'),
    )
    expect(entitiesCall).toBeTruthy()
    expect(String(entitiesCall?.[0])).toContain('status=active')
  })

  it('shows the suggested team name as a hint when unassigned', async () => {
    stubFetch({ workItems: [workItem({ suggested_team_name: 'acme' })] })
    renderPanel()
    expect(await screen.findByText('suggested: acme')).toBeTruthy()
  })

  it('does not show the suggestion hint once a team is confirmed', async () => {
    stubFetch({
      workItems: [workItem({ team_entity_id: 'team-1', suggested_team_name: 'acme' })],
      teams: [{ id: 'team-1', canonical_name: 'Platform Engineering' }],
    })
    renderPanel()
    await screen.findByText('Fix the widget')
    expect(screen.queryByText(/suggested:/)).toBeNull()
    expect((screen.getByLabelText('Team for Fix the widget') as HTMLSelectElement).value).toBe('team-1')
  })

  it('still shows the correct assignment when the confirmed team is outside the first 100 fetched', async () => {
    // `listTeams` fetches at most 100 teams; a work item confirmed to a
    // team outside that page must not silently render as "Unassigned" --
    // the `<select>`'s controlled `value` needs an `<option>` to match.
    stubFetch({
      workItems: [workItem({ team_entity_id: 'team-not-in-page' })],
      teams: [{ id: 'team-1', canonical_name: 'Platform Engineering' }],
    })
    renderPanel()
    const select = (await screen.findByLabelText('Team for Fix the widget')) as HTMLSelectElement
    expect(select.value).toBe('team-not-in-page')
    expect(screen.getByText('Assigned team (not in first 100)')).toBeTruthy()
  })

  it('assigning a team from the dropdown posts the confirmed link and refetches', async () => {
    stubFetch({
      workItems: [workItem()],
      teams: [{ id: 'team-1', canonical_name: 'Platform Engineering' }],
    })
    renderPanel()
    const select = await screen.findByLabelText('Team for Fix the widget')
    fireEvent.change(select, { target: { value: 'team-1' } })

    await waitFor(() => {
      expect((screen.getByLabelText('Team for Fix the widget') as HTMLSelectElement).value).toBe('team-1')
    })
    const calls = (fetch as unknown as { mock: { calls: [RequestInfo | URL, RequestInit?][] } }).mock.calls
    const postCall = calls.find(([, init]) => init?.method === 'POST')
    expect(String(postCall?.[0])).toContain('/api/v1/engineering/work-items/item-1/team')
    expect(JSON.parse(String(postCall?.[1]?.body))).toEqual({ expected_version: 1, team_entity_id: 'team-1' })
  })

  it('surfaces a failed team assignment as an alert next to the dropdown', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input)
        if (url.includes('/knowledge/entities')) return response({ items: [{ id: 'team-1', canonical_name: 'Platform Engineering' }] })
        if (init?.method === 'POST' && url.includes('/team')) {
          return response({ error: { code: 'VERSION_CONFLICT', message: 'Someone else changed this first.' } }, 409)
        }
        return response({ work_items: [workItem()] })
      }),
    )
    renderPanel()
    const select = await screen.findByLabelText('Team for Fix the widget')
    fireEvent.change(select, { target: { value: 'team-1' } })

    expect(await screen.findByRole('alert')).toBeTruthy()
  })

  // --- team filter (read-only view filter, distinct from TeamAssignment) --

  it('filtering by team only shows work items assigned to that team', async () => {
    stubFetch({
      workItems: [
        workItem({ id: 'item-1', title: 'Fix the widget', team_entity_id: 'team-1' }),
        workItem({ id: 'item-2', title: 'Ship the gadget', team_entity_id: null }),
      ],
      teams: [{ id: 'team-1', canonical_name: 'Platform Engineering' }],
    })
    renderPanel()
    await screen.findByText('Fix the widget')
    expect(await screen.findByText('Ship the gadget')).toBeTruthy()

    fireEvent.change(screen.getByLabelText('Filter work items by team'), { target: { value: 'team-1' } })

    expect(await screen.findByText('Fix the widget')).toBeTruthy()
    await waitFor(() => expect(screen.queryByText('Ship the gadget')).toBeNull())
    const calls = (fetch as unknown as { mock: { calls: [RequestInfo | URL, RequestInit?][] } }).mock.calls
    expect(calls.some(([url]) => String(url).includes('team_entity_id=team-1'))).toBe(true)
  })

  it('shows a team-specific empty state when the filtered team has no work items', async () => {
    stubFetch({
      workItems: [workItem({ team_entity_id: null })],
      teams: [{ id: 'team-1', canonical_name: 'Platform Engineering' }],
    })
    renderPanel()
    await screen.findByText('Fix the widget')

    fireEvent.change(screen.getByLabelText('Filter work items by team'), { target: { value: 'team-1' } })

    expect(await screen.findByText('No work items are assigned to this team yet.')).toBeTruthy()
  })
})
