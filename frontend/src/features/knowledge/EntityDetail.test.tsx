// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import EntityDetail from './EntityDetail'

const personEntity = {
  id: 'entity-1',
  entity_id: null,
  kind: 'person',
  canonical_name: 'Ada Lovelace',
  summary: 'Mathematician',
  status: 'active',
  confidence: 1,
  version: 1,
  created_at: '2026-07-01T00:00:00Z',
  updated_at: '2026-07-01T00:00:00Z',
}

const teamEntity = {
  id: 'team-1',
  entity_id: null,
  kind: 'team',
  canonical_name: 'Platform Engineering',
  summary: null,
  status: 'active',
  confidence: 1,
  version: 1,
  created_at: '2026-07-01T00:00:00Z',
  updated_at: '2026-07-01T00:00:00Z',
}

function jsonResponse(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

function errorResponse(code: string, status = 500) {
  return Promise.resolve(new Response(JSON.stringify({ error: { code } }), { status, headers: { 'Content-Type': 'application/json' } }))
}

type Route = {
  method: string
  match: (pathname: string, search: string) => boolean
  handle: (pathname: string) => Promise<Response>
}

function routedFetch(routes: Route[]) {
  return vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const method = (init?.method ?? 'GET').toUpperCase()
    const url = new URL(String(input), 'http://localhost')
    // `routes` is searched in order, so a more specific route (e.g. the
    // team-roster query's own `relationship_type=MEMBER_OF` filter) must be
    // listed before a broader one matching the same pathname regardless of
    // query string.
    const route = routes.find(
      (candidate) => candidate.method === method && candidate.match(url.pathname, url.search),
    )
    if (!route) return Promise.resolve(new Response(JSON.stringify({ error: { code: 'NOT_FOUND' } }), { status: 404 }))
    return route.handle(url.pathname)
  })
}

function renderDetail(entityId: string = personEntity.id) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <EntityDetail entityId={entityId} onClose={() => {}} />
    </QueryClientProvider>,
  )
}

const base = (path: string) => `/api/v1/knowledge/entities/${personEntity.id}${path}`

beforeEach(() => {
  document.cookie = 'ecc_csrf=knowledge-token; Secure; SameSite=Strict'
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'knowledge-request-id') })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('EntityDetail', () => {
  it('renders empty states when aliases, claims, relationships and timeline are genuinely empty', async () => {
    const fetch = routedFetch([
      { method: 'GET', match: (p) => p === base(''), handle: () => jsonResponse(personEntity) },
      { method: 'GET', match: (p) => p === base('/aliases'), handle: () => jsonResponse({ items: [] }) },
      { method: 'GET', match: (p) => p === base('/claims'), handle: () => jsonResponse({ items: [] }) },
      { method: 'GET', match: (p) => p === base('/relationships'), handle: () => jsonResponse({ items: [] }) },
      { method: 'GET', match: (p) => p === base('/timeline'), handle: () => jsonResponse({ items: [], next_cursor: null }) },
    ])
    vi.stubGlobal('fetch', fetch)
    renderDetail()

    await screen.findByText('No aliases recorded for this entity.')
    await screen.findByText('No claims recorded for this entity.')
    await screen.findByText('No relationships recorded for this entity.')
    await screen.findByText('No timeline entries yet.')
    expect(screen.queryAllByRole('alert')).toHaveLength(0)
  })

  it('shows a distinct error state for aliases, claims, relationships and timeline when their fetch fails, never the empty-state text', async () => {
    const fetch = routedFetch([
      { method: 'GET', match: (p) => p === base(''), handle: () => jsonResponse(personEntity) },
      { method: 'GET', match: (p) => p === base('/aliases'), handle: () => errorResponse('ALIASES_UNAVAILABLE') },
      { method: 'GET', match: (p) => p === base('/claims'), handle: () => errorResponse('CLAIMS_UNAVAILABLE') },
      { method: 'GET', match: (p) => p === base('/relationships'), handle: () => errorResponse('RELATIONSHIPS_UNAVAILABLE') },
      { method: 'GET', match: (p) => p === base('/timeline'), handle: () => errorResponse('TIMELINE_UNAVAILABLE') },
    ])
    vi.stubGlobal('fetch', fetch)
    renderDetail()

    await screen.findByText('Ada Lovelace')
    const alerts = await screen.findAllByRole('alert')
    expect(alerts).toHaveLength(4)

    // The bug this regression-tests: a failed fetch must never render as if the
    // list were genuinely empty -- these strings must never appear.
    expect(screen.queryByText('No aliases recorded for this entity.')).toBeNull()
    expect(screen.queryByText('No claims recorded for this entity.')).toBeNull()
    expect(screen.queryByText('No relationships recorded for this entity.')).toBeNull()
    expect(screen.queryByText('No timeline entries yet.')).toBeNull()
  })

  it('renders aliases, claims, relationships and timeline content when present', async () => {
    const fetch = routedFetch([
      { method: 'GET', match: (p) => p === base(''), handle: () => jsonResponse(personEntity) },
      {
        method: 'GET',
        match: (p) => p === base('/aliases'),
        handle: () =>
          jsonResponse({
            items: [{ id: 'alias-1', entity_id: personEntity.id, alias_type: 'nickname', normalized_value: 'ada', source_id: 'src-1', confidence: 0.9, created_at: '2026-01-01T00:00:00Z' }],
          }),
      },
      {
        method: 'GET',
        match: (p) => p === base('/claims'),
        handle: () =>
          jsonResponse({
            items: [{ id: 'claim-1', subject_id: personEntity.id, predicate: 'title', value: { text: 'Mathematician' }, source_id: 'src-1', confidence: 0.9, superseded_by: null, valid_from: '2026-01-01T00:00:00Z', valid_to: null, created_at: '2026-01-01T00:00:00Z' }],
          }),
      },
      {
        method: 'GET',
        match: (p) => p === base('/relationships'),
        handle: () =>
          jsonResponse({
            items: [{ id: 'rel-1', from_entity_id: personEntity.id, to_entity_id: 'entity-2', relationship_type: 'RELATES_TO', status: 'active', confidence: 0.9, evidence_id: 'src-1', valid_from: null, valid_to: null, from_entity_name: 'Ada Lovelace', from_entity_kind: 'person', to_entity_name: 'Analytical Engine', to_entity_kind: 'project' }],
          }),
      },
      {
        method: 'GET',
        match: (p) => p === base('/timeline'),
        handle: () =>
          jsonResponse({
            items: [{ id: 'tl-1', entity_id: personEntity.id, effective_at: '2026-07-01T00:00:00Z', recorded_at: '2026-07-01T00:00:00Z', event_type: 'entity_created', source_id: null, summary: 'Entity created' }],
            next_cursor: null,
          }),
      },
    ])
    vi.stubGlobal('fetch', fetch)
    renderDetail()

    await screen.findByText('nickname', { exact: false })
    await screen.findByText((_content, element) => element?.tagName === 'STRONG' && element.textContent === 'title')
    await screen.findByText('RELATES_TO → Analytical Engine (project)')
    await screen.findByText('Entity created', { exact: false })
  })

  it('resolves and renders evidence provenance for claims and relationships', async () => {
    const fetch = routedFetch([
      { method: 'GET', match: (p) => p === base(''), handle: () => jsonResponse(personEntity) },
      { method: 'GET', match: (p) => p === base('/aliases'), handle: () => jsonResponse({ items: [] }) },
      {
        method: 'GET',
        match: (p) => p === base('/claims'),
        handle: () =>
          jsonResponse({
            items: [{ id: 'claim-1', subject_id: personEntity.id, predicate: 'title', value: { text: 'Mathematician' }, source_id: 'src-available', confidence: 0.9, superseded_by: null, valid_from: null, valid_to: null, created_at: '2026-01-01T00:00:00Z' }],
          }),
      },
      {
        method: 'GET',
        match: (p) => p === base('/relationships'),
        handle: () =>
          jsonResponse({
            items: [{ id: 'rel-1', from_entity_id: personEntity.id, to_entity_id: 'entity-2', relationship_type: 'RELATES_TO', status: 'active', confidence: 0.9, evidence_id: 'src-missing', valid_from: null, valid_to: null, from_entity_name: 'Ada Lovelace', from_entity_kind: 'person', to_entity_name: 'Analytical Engine', to_entity_kind: 'project' }],
          }),
      },
      { method: 'GET', match: (p) => p === base('/timeline'), handle: () => jsonResponse({ items: [], next_cursor: null }) },
      {
        method: 'GET',
        match: (p) => p === '/api/v1/evidence',
        handle: () =>
          jsonResponse({
            items: [
              { id: 'src-available', status: 'available', source_type: 'manual', label: null, captured_at: null },
              { id: 'src-missing', status: 'missing', source_type: null, label: null, captured_at: null },
            ],
          }),
      },
    ])
    vi.stubGlobal('fetch', fetch)
    renderDetail()

    await screen.findByText('(available)', { exact: false })
    await screen.findByText('(missing)', { exact: false })
    const evidenceCall = fetch.mock.calls.find(([input]) => String(input).startsWith('/api/v1/evidence'))
    expect(evidenceCall).toBeTruthy()
    expect(String(evidenceCall?.[0])).toContain('id=src-available')
    expect(String(evidenceCall?.[0])).toContain('id=src-missing')
  })

  it('records a new claim via the claim form', async () => {
    let claimsListCallCount = 0
    const fetch = routedFetch([
      { method: 'GET', match: (p) => p === base(''), handle: () => jsonResponse(personEntity) },
      { method: 'GET', match: (p) => p === base('/aliases'), handle: () => jsonResponse({ items: [] }) },
      {
        method: 'GET',
        match: (p) => p === base('/claims'),
        handle: () => {
          claimsListCallCount += 1
          return jsonResponse({ items: [] })
        },
      },
      { method: 'GET', match: (p) => p === base('/relationships'), handle: () => jsonResponse({ items: [] }) },
      { method: 'GET', match: (p) => p === base('/timeline'), handle: () => jsonResponse({ items: [], next_cursor: null }) },
      {
        method: 'POST',
        match: (p) => p === base('/claims'),
        handle: () =>
          jsonResponse(
            { id: 'claim-new', subject_id: personEntity.id, predicate: 'employed_at', value: { text: 'Analytical Engines Ltd' }, source_id: 'src-1', confidence: 1, superseded_by: null, valid_from: null, valid_to: null, created_at: '2026-01-01T00:00:00Z' },
            201,
          ),
      },
    ])
    vi.stubGlobal('fetch', fetch)
    renderDetail()
    await screen.findByText('No claims recorded for this entity.')

    fireEvent.change(screen.getByLabelText('Claim predicate'), { target: { value: 'employed_at' } })
    fireEvent.change(screen.getByLabelText('Claim value'), { target: { value: 'Analytical Engines Ltd' } })
    fireEvent.change(screen.getByLabelText('Claim source ID'), { target: { value: 'src-1' } })
    fireEvent.click(screen.getByRole('button', { name: 'Record claim' }))

    await waitFor(() => expect(claimsListCallCount).toBeGreaterThan(1))
    const postCall = fetch.mock.calls.find(([, init]) => init?.method === 'POST')
    expect(postCall).toBeTruthy()
    const payload = JSON.parse(String(postCall?.[1]?.body))
    expect(payload).toEqual({
      predicate: 'employed_at',
      value: { text: 'Analytical Engines Ltd' },
      source_id: 'src-1',
      confidence: 1,
    })
  })

  it('corrects an existing claim via supersede', async () => {
    const existingClaim = { id: 'claim-1', subject_id: personEntity.id, predicate: 'title', value: { text: 'Mathematician' }, source_id: 'src-1', confidence: 0.9, superseded_by: null, valid_from: null, valid_to: null, created_at: '2026-01-01T00:00:00Z' }
    let supersedeCalled = false
    const fetch = routedFetch([
      { method: 'GET', match: (p) => p === base(''), handle: () => jsonResponse(personEntity) },
      { method: 'GET', match: (p) => p === base('/aliases'), handle: () => jsonResponse({ items: [] }) },
      { method: 'GET', match: (p) => p === base('/claims'), handle: () => jsonResponse({ items: [existingClaim] }) },
      { method: 'GET', match: (p) => p === base('/relationships'), handle: () => jsonResponse({ items: [] }) },
      { method: 'GET', match: (p) => p === base('/timeline'), handle: () => jsonResponse({ items: [], next_cursor: null }) },
      {
        method: 'POST',
        match: (p) => p === base(`/claims/${existingClaim.id}/supersede`),
        handle: () => {
          supersedeCalled = true
          return jsonResponse(
            { ...existingClaim, id: 'claim-2', value: { text: 'Countess of Lovelace' } },
            201,
          )
        },
      },
    ])
    vi.stubGlobal('fetch', fetch)
    renderDetail()

    const correctButton = await screen.findByRole('button', { name: 'Correct “title”' })
    fireEvent.click(correctButton)

    const valueInput = await screen.findByLabelText(`Correction value for ${existingClaim.id}`)
    fireEvent.change(valueInput, { target: { value: 'Countess of Lovelace' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save correction' }))

    await waitFor(() => expect(supersedeCalled).toBe(true))
    const postCall = fetch.mock.calls.find(([input]) => String(input).includes('supersede'))
    expect(postCall).toBeTruthy()
    const payload = JSON.parse(String(postCall?.[1]?.body))
    expect(payload).toEqual({
      predicate: 'title',
      value: { text: 'Countess of Lovelace' },
      source_id: 'src-1',
      confidence: 0.9,
    })
  })

  const teamBase = (path: string) => `/api/v1/knowledge/entities/${teamEntity.id}${path}`
  const isRosterQuery = (search: string) =>
    search.includes('relationship_type=MEMBER_OF') && search.includes('direction=incoming')

  it('renders a team roster resolved from MEMBER_OF relationships', async () => {
    const fetch = routedFetch([
      { method: 'GET', match: (p) => p === teamBase(''), handle: () => jsonResponse(teamEntity) },
      { method: 'GET', match: (p) => p === teamBase('/aliases'), handle: () => jsonResponse({ items: [] }) },
      { method: 'GET', match: (p) => p === teamBase('/claims'), handle: () => jsonResponse({ items: [] }) },
      {
        method: 'GET',
        match: (p, s) => p === teamBase('/relationships') && isRosterQuery(s),
        handle: () =>
          jsonResponse({
            items: [
              {
                id: 'rel-member-1',
                from_entity_id: personEntity.id,
                to_entity_id: teamEntity.id,
                relationship_type: 'MEMBER_OF',
                status: 'active',
                confidence: 1,
                evidence_id: 'src-1',
                valid_from: null,
                valid_to: null,
                from_entity_name: 'Ada Lovelace',
                from_entity_kind: 'person',
                to_entity_name: 'Platform Engineering',
                to_entity_kind: 'team',
              },
            ],
          }),
      },
      { method: 'GET', match: (p, s) => p === teamBase('/relationships') && !isRosterQuery(s), handle: () => jsonResponse({ items: [] }) },
      { method: 'GET', match: (p) => p === teamBase('/timeline'), handle: () => jsonResponse({ items: [], next_cursor: null }) },
    ])
    vi.stubGlobal('fetch', fetch)
    renderDetail(teamEntity.id)

    await screen.findByText('Platform Engineering')
    await screen.findByText('Ada Lovelace', { exact: false })
    await screen.findByText((_content, element) => element?.tagName === 'SMALL' && element.textContent === '· person')
  })

  it('shows an empty state when a team has no recorded members', async () => {
    const fetch = routedFetch([
      { method: 'GET', match: (p) => p === teamBase(''), handle: () => jsonResponse(teamEntity) },
      { method: 'GET', match: (p) => p === teamBase('/aliases'), handle: () => jsonResponse({ items: [] }) },
      { method: 'GET', match: (p) => p === teamBase('/claims'), handle: () => jsonResponse({ items: [] }) },
      { method: 'GET', match: (p) => p === teamBase('/relationships'), handle: () => jsonResponse({ items: [] }) },
      { method: 'GET', match: (p) => p === teamBase('/timeline'), handle: () => jsonResponse({ items: [], next_cursor: null }) },
    ])
    vi.stubGlobal('fetch', fetch)
    renderDetail(teamEntity.id)

    await screen.findByText('No members recorded for this team yet.')
  })

  it('does not render a Members section for non-team entities', async () => {
    const fetch = routedFetch([
      { method: 'GET', match: (p) => p === base(''), handle: () => jsonResponse(personEntity) },
      { method: 'GET', match: (p) => p === base('/aliases'), handle: () => jsonResponse({ items: [] }) },
      { method: 'GET', match: (p) => p === base('/claims'), handle: () => jsonResponse({ items: [] }) },
      { method: 'GET', match: (p) => p === base('/relationships'), handle: () => jsonResponse({ items: [] }) },
      { method: 'GET', match: (p) => p === base('/timeline'), handle: () => jsonResponse({ items: [], next_cursor: null }) },
    ])
    vi.stubGlobal('fetch', fetch)
    renderDetail()

    await screen.findByText('No relationships recorded for this entity.')
    expect(screen.queryByText('Members')).toBeNull()
    expect(screen.queryByLabelText('Member person entity ID')).toBeNull()
  })

  it('adds a team member via the add-member form and refetches the roster', async () => {
    let rosterCallCount = 0
    let postBody: unknown = null
    const fetch = routedFetch([
      { method: 'GET', match: (p) => p === teamBase(''), handle: () => jsonResponse(teamEntity) },
      { method: 'GET', match: (p) => p === teamBase('/aliases'), handle: () => jsonResponse({ items: [] }) },
      { method: 'GET', match: (p) => p === teamBase('/claims'), handle: () => jsonResponse({ items: [] }) },
      {
        method: 'GET',
        match: (p, s) => p === teamBase('/relationships') && isRosterQuery(s),
        handle: () => {
          rosterCallCount += 1
          return jsonResponse({ items: [] })
        },
      },
      { method: 'GET', match: (p, s) => p === teamBase('/relationships') && !isRosterQuery(s), handle: () => jsonResponse({ items: [] }) },
      { method: 'GET', match: (p) => p === teamBase('/timeline'), handle: () => jsonResponse({ items: [], next_cursor: null }) },
      {
        method: 'POST',
        match: (p) => p === `/api/v1/knowledge/entities/${personEntity.id}/relationships`,
        handle: () =>
          jsonResponse(
            {
              id: 'rel-member-new',
              from_entity_id: personEntity.id,
              to_entity_id: teamEntity.id,
              relationship_type: 'MEMBER_OF',
              status: 'active',
              confidence: 1,
              evidence_id: 'src-1',
              valid_from: null,
              valid_to: null,
              from_entity_name: 'Ada Lovelace',
              from_entity_kind: 'person',
              to_entity_name: 'Platform Engineering',
              to_entity_kind: 'team',
            },
            201,
          ),
      },
    ])
    vi.stubGlobal('fetch', fetch)
    renderDetail(teamEntity.id)

    await screen.findByText('No members recorded for this team yet.')
    expect(rosterCallCount).toBe(1)

    fireEvent.change(screen.getByLabelText('Member person entity ID'), { target: { value: personEntity.id } })
    fireEvent.change(screen.getByLabelText('Member evidence ID'), { target: { value: 'src-1' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add member' }))

    await waitFor(() => expect(rosterCallCount).toBeGreaterThan(1))
    const postCall = fetch.mock.calls.find(([, init]) => init?.method === 'POST')
    expect(postCall).toBeTruthy()
    postBody = JSON.parse(String(postCall?.[1]?.body))
    expect(postBody).toEqual({
      relationship_type: 'MEMBER_OF',
      to_entity_id: teamEntity.id,
      evidence_id: 'src-1',
    })
    expect((screen.getByLabelText('Member person entity ID') as HTMLInputElement).value).toBe('')
  })

  it('shows an alert when adding a team member fails', async () => {
    const fetch = routedFetch([
      { method: 'GET', match: (p) => p === teamBase(''), handle: () => jsonResponse(teamEntity) },
      { method: 'GET', match: (p) => p === teamBase('/aliases'), handle: () => jsonResponse({ items: [] }) },
      { method: 'GET', match: (p) => p === teamBase('/claims'), handle: () => jsonResponse({ items: [] }) },
      { method: 'GET', match: (p) => p === teamBase('/relationships'), handle: () => jsonResponse({ items: [] }) },
      { method: 'GET', match: (p) => p === teamBase('/timeline'), handle: () => jsonResponse({ items: [], next_cursor: null }) },
      {
        method: 'POST',
        match: (p) => p === `/api/v1/knowledge/entities/${personEntity.id}/relationships`,
        handle: () => errorResponse('MEMBER_ADD_FAILED', 500),
      },
    ])
    vi.stubGlobal('fetch', fetch)
    renderDetail(teamEntity.id)

    await screen.findByText('No members recorded for this team yet.')
    fireEvent.change(screen.getByLabelText('Member person entity ID'), { target: { value: personEntity.id } })
    fireEvent.change(screen.getByLabelText('Member evidence ID'), { target: { value: 'src-1' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add member' }))

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('Request failed')
  })
})
