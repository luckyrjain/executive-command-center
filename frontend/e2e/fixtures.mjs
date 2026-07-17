import { randomUUID } from 'node:crypto'

function nowIso() {
  return new Date().toISOString()
}

function safeJson(raw) {
  if (!raw) return {}
  try {
    return JSON.parse(raw)
  } catch {
    return {}
  }
}

function conflictBody(item) {
  return {
    error: {
      code: 'VERSION_CONFLICT',
      message: 'This record changed elsewhere.',
      details: { current: { current_version: item.version } },
    },
  }
}

/**
 * A mutable in-memory collection that mimics the optimistic-concurrency
 * contract every Phase 1 entity endpoint shares: create returns version 1,
 * PATCH/action endpoints require `expected_version` to match or fail with a
 * 409 VERSION_CONFLICT envelope shaped like the real API.
 */
function createCollection(seed) {
  const items = seed.map((item) => ({ archived_at: null, pre_archive_status: null, ...item }))

  function find(id) {
    return items.find((item) => item.id === id)
  }

  return {
    items,
    find,
    list() {
      return items
    },
    create(fields) {
      const item = { id: randomUUID(), version: 1, archived_at: null, pre_archive_status: null, ...fields }
      items.push(item)
      return item
    },
    /** Applies `updater(item)` to the record if `expectedVersion` matches; otherwise returns a 409. */
    mutate(id, expectedVersion, updater) {
      const item = find(id)
      if (!item) return { status: 404, body: { error: { code: 'NOT_FOUND', message: 'Not found' } } }
      if (item.version !== expectedVersion) return { status: 409, body: conflictBody(item) }
      Object.assign(item, updater(item))
      item.version += 1
      return { status: 200, body: item }
    },
  }
}

function genericPatch(item, body) {
  const { expected_version: _expectedVersion, ...rest } = body
  return rest
}

const archiveAction = (item) => ({ archived_at: nowIso(), pre_archive_status: item.status })
const restoreAction = (item) => ({ archived_at: null, status: item.pre_archive_status ?? item.status, pre_archive_status: null })

/**
 * Builds a resource dispatcher: given a request already known to fall under
 * `base`, resolves GET {base}, POST {base}, GET {base}/:id, PATCH {base}/:id
 * and POST {base}/:id/:action against an in-memory collection. Returns
 * `null` if the path doesn't belong to this resource so the caller's single
 * router can try the next candidate.
 */
function resourceHandler({ base, collection, buildCreate = (body) => body, buildPatch = genericPatch, actions = {}, filterList = (items) => items }) {
  return (pathname, method, body, queryString) => {
    if (pathname !== base && !pathname.startsWith(`${base}/`)) return null
    const afterBase = pathname.slice(base.length)
    const segments = afterBase.split('/').filter(Boolean)

    if (segments.length === 0 && method === 'GET') {
      const params = new URLSearchParams(queryString)
      return { status: 200, body: { items: filterList(collection.list(), params), next_cursor: null } }
    }
    if (segments.length === 0 && method === 'POST') {
      const item = collection.create(buildCreate(body))
      return { status: 201, body: item }
    }
    if (segments.length === 1) {
      const [id] = segments
      if (method === 'GET') {
        const item = collection.find(id)
        if (!item) return { status: 404, body: { error: { code: 'NOT_FOUND', message: 'Not found' } } }
        return { status: 200, body: item }
      }
      if (method === 'PATCH') {
        return collection.mutate(id, body.expected_version, (item) => buildPatch(item, body))
      }
    }
    if (segments.length === 2) {
      const [id, action] = segments
      const actionHandler = actions[action]
      if (!actionHandler) return { status: 404, body: { error: { code: 'NOT_FOUND', message: 'Unknown action' } } }
      return collection.mutate(id, body.expected_version, (item) => actionHandler(item, body))
    }
    return { status: 404, body: { error: { code: 'NOT_FOUND', message: 'Unhandled fixture path' } } }
  }
}

function makeSearch({ corpus, pageSize = 20, degradedQueries = [] }) {
  return (query, cursor) => {
    const needle = query.toLowerCase()
    const matches = corpus.filter((item) => !needle || `${item.title} ${item.snippet ?? ''}`.toLowerCase().includes(needle))
    const start = cursor ? Number(cursor) : 0
    const page = matches.slice(start, start + pageSize)
    const nextIndex = start + pageSize
    return {
      items: page,
      next_cursor: nextIndex < matches.length ? String(nextIndex) : null,
      degraded: degradedQueries.includes(needle),
    }
  }
}

function makeAudit({ corpus, pageSize = 20 }) {
  return (eventType, cursor) => {
    const matches = corpus.filter((item) => !eventType || item.event_type === eventType)
    const start = cursor ? Number(cursor) : 0
    const page = matches.slice(start, start + pageSize)
    const nextIndex = start + pageSize
    return { items: page, next_cursor: nextIndex < matches.length ? String(nextIndex) : null }
  }
}

const defaultDashboardSections = {
  today_schedule: [{ id: 'm1', title: 'Leadership review', starts_at: '2026-07-15T04:30:00Z' }],
  top_priorities: [{ entity_id: 't1', title: 'Approve hiring plan', score: 92, status: 'in_progress' }],
  overdue_commitments: [{ entity_id: 'c1', summary: 'Send board metrics', status: 'active' }],
  risks: [{ entity_id: 'r1', title: 'Vendor concentration', score: 80, status: 'monitoring' }],
  waiting_on: [{ entity_id: 'c2', summary: 'Legal approval', status: 'active' }],
  recently_changed: [{ entity_ref: 'task:t1', message: 'Priority updated', occurred_at: '2026-07-15T03:00:00Z' }],
}

const defaultSearchCorpus = [
  {
    entity_type: 'task',
    entity_id: 't1',
    title: 'Approve hiring plan',
    snippet: 'Approve the hiring plan before Friday.',
    matched_fields: ['title'],
    score: 0.98,
    updated_at: '2026-07-15T03:00:00Z',
    source_type: 'local',
    archived: false,
  },
]

const defaultAuditCorpus = [
  {
    id: 'a1',
    event_type: 'task.updated',
    aggregate_type: 'task',
    aggregate_id: 't1',
    aggregate_version: 2,
    actor_id: null,
    changed_fields: ['manual_priority'],
    authorization_result: 'allowed',
    source: 'user',
    failure_code: null,
    occurred_at: '2026-07-15T03:00:00Z',
  },
]

/**
 * Installs an in-memory fake of the Phase 1 API surface onto `page`, covering
 * every workspace built in Tasks 1-5: tasks, commitments, notes, calendar
 * events, meetings, risks, recommendations, the morning brief, dashboard,
 * search, audit and evidence. Returns the mutable state/collections plus a
 * `requests` array capturing every intercepted call (method, path, search,
 * parsed body) so scenarios can assert on request contracts as well as DOM.
 *
 * `overrides` lets a scenario replace any default seed: e.g.
 * `createFixtureApi(page, { tasks: [taskFixture], risks: [riskFixture] })`.
 *
 * Implementation note: every resource is dispatched from a SINGLE
 * `context.route('**\/*', ...)` handler rather than one `page.route()` per
 * resource pattern. Registering many separate glob patterns on the same
 * context was empirically unreliable in this environment — requests fired
 * synchronously from a React click-handler's mount effect (e.g. switching to
 * the Work tab, which mounts TaskWorkspace and CommitmentWorkspace at once)
 * would intermittently fall through to the real network and 404, even though
 * the same URL fetched via `page.evaluate` immediately afterwards matched
 * fine. A single catch-all pattern removed the race entirely across dozens
 * of repeated runs.
 */
export async function createFixtureApi(page, overrides = {}) {
  await page.context().addCookies([{ name: 'ecc_csrf', value: 'csrf-token', url: 'http://127.0.0.1:4173' }])

  const requests = []

  const tasks = createCollection(overrides.tasks ?? [])
  const commitments = createCollection(overrides.commitments ?? [])
  const notes = createCollection(overrides.notes ?? [])
  const calendarEvents = createCollection(overrides.calendarEvents ?? [])
  const meetings = createCollection(overrides.meetings ?? [])
  const risks = createCollection(overrides.risks ?? [])
  const recommendations = createCollection(overrides.recommendations ?? [])

  const dashboard = {
    date: '2026-07-15',
    timezone: 'Asia/Kolkata',
    generated_at: '2026-07-15T03:30:00Z',
    stale: false,
    sections: defaultDashboardSections,
    ...overrides.dashboard,
  }

  const brief = {
    id: 'b1',
    briefing_date: '2026-07-15',
    generation_version: 3,
    sections: defaultDashboardSections,
    source_versions: { 'task:t1': 2 },
    evidence_ids: [],
    generated_at: '2026-07-15T03:30:00Z',
    timezone: 'Asia/Kolkata',
    algorithm_version: 'phase1-v1',
    ai_status: 'disabled',
    stale: true,
    stale_reason: 'source_version_changed',
    ...overrides.brief,
  }

  const evidence = new Map(Object.entries(overrides.evidence ?? {}))

  const search = makeSearch({
    corpus: overrides.searchCorpus ?? defaultSearchCorpus,
    pageSize: overrides.searchPageSize,
    degradedQueries: overrides.searchDegradedQueries ?? [],
  })
  const audit = makeAudit({ corpus: overrides.auditCorpus ?? defaultAuditCorpus, pageSize: overrides.auditPageSize })

  // `route.fulfill()` synthesizes a response without touching the real
  // network, so `context.setOffline(true)` alone does NOT stop a mocked
  // request from "succeeding" — the CDP-level offline emulation only affects
  // requests that actually reach the network stack. `offline` lets a
  // scenario make the fixture itself refuse requests (route.abort) so the
  // client's real OFFLINE-classification path in api/client.ts gets
  // exercised. Pair with `page.context().setOffline(true)` so
  // `navigator.onLine` also reads false, matching production behavior.
  let offline = false

  const resources = [
    resourceHandler({
      base: '/api/v1/tasks',
      collection: tasks,
      actions: {
        complete: () => ({ status: 'completed' }),
        cancel: () => ({ status: 'cancelled' }),
        archive: archiveAction,
        restore: restoreAction,
      },
    }),
    resourceHandler({
      base: '/api/v1/commitments',
      collection: commitments,
      actions: {
        confirm: () => ({ status: 'confirmed' }),
        fulfil: () => ({ status: 'fulfilled' }),
        cancel: () => ({ status: 'cancelled' }),
        archive: archiveAction,
        restore: restoreAction,
      },
    }),
    resourceHandler({
      base: '/api/v1/notes',
      collection: notes,
      actions: { archive: archiveAction, restore: restoreAction },
    }),
    resourceHandler({
      base: '/api/v1/calendar/events',
      collection: calendarEvents,
      buildCreate: (body) => ({
        ...body,
        external_source: 'manual',
        source_authoritative: true,
        created_at: nowIso(),
        updated_at: nowIso(),
      }),
      actions: { archive: archiveAction, restore: restoreAction },
    }),
    resourceHandler({
      base: '/api/v1/meetings',
      collection: meetings,
      buildCreate: (body) => {
        if (body.calendar_event_id) {
          const linked = calendarEvents.find(body.calendar_event_id)
          return {
            ...body,
            starts_at: linked?.starts_at ?? nowIso(),
            ends_at: linked?.ends_at ?? nowIso(),
            timezone: linked?.timezone ?? 'UTC',
            created_at: nowIso(),
            updated_at: nowIso(),
          }
        }
        return { ...body, created_at: nowIso(), updated_at: nowIso() }
      },
      actions: { archive: archiveAction, restore: restoreAction },
    }),
    resourceHandler({
      base: '/api/v1/risks',
      collection: risks,
      buildCreate: (body) => ({
        owner_id: 'owner-fixture',
        priority_impact: body.probability * body.impact,
        score: body.probability * body.impact * 5,
        factors: [],
        explanation: 'Fixture risk',
        created_at: nowIso(),
        updated_at: nowIso(),
        ...body,
      }),
      actions: { archive: archiveAction, restore: restoreAction },
    }),
    resourceHandler({
      base: '/api/v1/recommendations',
      collection: recommendations,
      // Mirrors RecommendationPanel's real query: it always asks for
      // ?status=proposed&status=pending_confirmation&status=executed&status=failed.
      // Honoring that filter (rather than returning every seeded item) means
      // recommendation-terminals.mjs can trust that a 'rejected'/'expired'/
      // 'superseded' fixture item behaves like production: invisible to this
      // list, not merely hidden by client-side rendering.
      filterList: (items, params) => {
        const statuses = params.getAll('status')
        return statuses.length ? items.filter((item) => statuses.includes(item.status)) : items
      },
      actions: {
        publish: () => ({ status: 'pending_confirmation' }),
        confirm: (item) => ({
          status: 'executed',
          execution_result: { applied: true, target_type: item.target_type, target_id: item.target_id },
        }),
        reject: () => ({ status: 'rejected' }),
        defer: (_item, body) => ({ deferred_until: body.defer_until }),
        pin: (_item, body) => ({ pinned: body.pinned }),
      },
    }),
  ]

  function dispatch(pathname, method, queryString, body) {
    if (pathname === '/api/v1/dashboard/today') {
      return { status: 200, body: dashboard }
    }
    if (pathname === '/api/v1/briefs/morning') {
      if (method === 'POST') {
        brief.generation_version += 1
        brief.stale = false
        brief.stale_reason = null
        brief.generated_at = nowIso()
      }
      return { status: 200, body: brief }
    }
    if (pathname === '/api/v1/search') {
      const params = new URLSearchParams(queryString)
      return { status: 200, body: search(params.get('q') ?? '', params.get('cursor')) }
    }
    if (pathname === '/api/v1/audit') {
      const params = new URLSearchParams(queryString)
      return { status: 200, body: audit(params.get('event_type') ?? '', params.get('cursor')) }
    }
    if (pathname === '/api/v1/evidence') {
      const params = new URLSearchParams(queryString)
      const ids = params.getAll('id')
      const items = ids.map((id) => {
        const found = evidence.get(id)
        return found ? { id, status: 'available', ...found } : { id, status: 'missing', source_type: null, label: null, captured_at: null }
      })
      return { status: 200, body: { items } }
    }
    for (const resource of resources) {
      const result = resource(pathname, method, body, queryString)
      if (result) return result
    }
    return null
  }

  await page.context().route('**/*', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (!url.pathname.startsWith('/api/v1/')) return route.fallback()
    if (offline) return route.abort('internetdisconnected')

    const method = request.method()
    const body = safeJson(request.postData())
    const result = dispatch(url.pathname, method, url.search, body)
    requests.push({ method, path: url.pathname, search: url.search, body })

    if (!result) return route.fulfill({ status: 404, json: { error: { code: 'NOT_FOUND', message: 'No fixture route registered' } } })
    return route.fulfill({ status: result.status, json: result.body })
  })

  return {
    requests,
    collections: { tasks, commitments, notes, calendarEvents, meetings, risks, recommendations },
    dashboard,
    brief,
    evidence,
    setOffline: (value) => { offline = value },
  }
}
