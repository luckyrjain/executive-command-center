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

/**
 * Phase 2 knowledge-platform endpoints. Kept as one dedicated dispatcher
 * (rather than several `resourceHandler` calls) because merge/reverse/
 * confirm/reject don't fit the generic {base}/:id/:action shape cleanly --
 * `POST /entities/merge` in particular would otherwise be swallowed by the
 * entities resourceHandler as `PATCH {base}/:id` with id="merge".
 */
function makeKnowledgeApi(overrides = {}) {
  const entities = createCollection(overrides.entities ?? [])
  const claims = [...(overrides.claims ?? [])]
  const relationships = [...(overrides.relationships ?? [])]
  const timelineEntries = [...(overrides.timelineEntries ?? [])]
  const resolutionCandidates = createCollection(overrides.resolutionCandidates ?? [])
  const entityOperations = []

  function pushTimeline(entityId, eventType, summary) {
    timelineEntries.push({
      id: randomUUID(),
      entity_id: entityId,
      effective_at: nowIso(),
      recorded_at: nowIso(),
      event_type: eventType,
      source_id: null,
      summary,
    })
  }

  const entitiesResource = resourceHandler({
    base: '/api/v1/knowledge/entities',
    collection: entities,
    buildCreate: (body) => ({
      entity_id: null,
      kind: body.kind,
      canonical_name: body.canonical_name,
      summary: body.summary ?? null,
      status: 'active',
      confidence: 1,
      created_at: nowIso(),
      updated_at: nowIso(),
    }),
    buildPatch: (item, body) => ({
      ...(body.canonical_name !== undefined ? { canonical_name: body.canonical_name } : {}),
      ...('summary' in body ? { summary: body.summary } : {}),
      updated_at: nowIso(),
    }),
    actions: { archive: archiveAction, restore: restoreAction },
  })

  function dispatch(pathname, method, body, queryString) {
    if (!pathname.startsWith('/api/v1/knowledge')) return null

    if (pathname === '/api/v1/knowledge/retrieve' && method === 'GET') {
      const params = new URLSearchParams(queryString)
      const needle = (params.get('q') ?? '').toLowerCase()
      const kind = params.get('kind')
      const items = entities
        .list()
        .filter((item) => item.status === 'active')
        .filter((item) => !kind || item.kind === kind)
        .filter((item) => !needle || item.canonical_name.toLowerCase().includes(needle))
        .map((item) => ({
          entity_type: item.kind,
          entity_id: item.id,
          title: item.canonical_name,
          snippet: item.summary ?? '',
          score: item.canonical_name.toLowerCase() === needle ? 0.95 : 0.5,
          matching_mode: item.canonical_name.toLowerCase() === needle ? 'exact_name' : 'lexical',
          factors: {},
          evidence_state: 'unknown',
          source_version: item.version,
          stale: false,
        }))
      return {
        status: 200,
        body: { items, next_cursor: null, mode: 'lexical', degraded: false, degraded_reason: null },
      }
    }

    if (pathname === '/api/v1/knowledge/entities/merge' && method === 'POST') {
      const candidate = resolutionCandidates.find(body.candidate_id)
      if (!candidate) return { status: 404, body: { error: { code: 'CANDIDATE_NOT_FOUND', message: 'Not found' } } }
      if (candidate.status !== 'confirmed') {
        return { status: 409, body: { error: { code: 'CANDIDATE_NOT_CONFIRMED', message: 'Not confirmed' } } }
      }
      const targetId = body.target_entity_id
      const sourceId = targetId === candidate.left_entity_id ? candidate.right_entity_id : candidate.left_entity_id
      const target = entities.find(targetId)
      const source = entities.find(sourceId)
      if (!target || !source) return { status: 404, body: { error: { code: 'ENTITY_NOT_FOUND', message: 'Not found' } } }
      if (target.version !== body.expected_target_version || source.version !== body.expected_source_version) {
        return { status: 409, body: { error: { code: 'VERSION_CONFLICT', message: 'Version conflict' } } }
      }
      source.status = 'redirected'
      source.version += 1
      const operation = {
        id: randomUUID(),
        operation_type: 'merge',
        status: 'active',
        source_entity_id: sourceId,
        target_entity_id: targetId,
        actor_id: 'fixture-user',
        reason: body.reason,
        reverses_operation_id: null,
        created_at: nowIso(),
      }
      entityOperations.push(operation)
      pushTimeline(targetId, 'entity_operation.merged', `merged ${sourceId} into ${targetId}`)
      pushTimeline(sourceId, 'entity_operation.merged', `redirected to ${targetId}`)
      return { status: 201, body: operation }
    }

    const reverseMatch = pathname.match(/^\/api\/v1\/knowledge\/entity-operations\/([^/]+)\/reverse$/)
    if (reverseMatch && method === 'POST') {
      const operation = entityOperations.find((candidate) => candidate.id === reverseMatch[1])
      if (!operation) return { status: 404, body: { error: { code: 'OPERATION_NOT_FOUND', message: 'Not found' } } }
      if (operation.status !== 'active') {
        return { status: 409, body: { error: { code: 'OPERATION_ALREADY_REVERSED', message: 'Already reversed' } } }
      }
      operation.status = 'reversed'
      const source = entities.find(operation.source_entity_id)
      if (source) {
        source.status = 'active'
        source.version += 1
      }
      const reversal = {
        id: randomUUID(),
        operation_type: 'reverse',
        status: 'active',
        source_entity_id: operation.source_entity_id,
        target_entity_id: operation.target_entity_id,
        actor_id: 'fixture-user',
        reason: body.reason,
        reverses_operation_id: operation.id,
        created_at: nowIso(),
      }
      entityOperations.push(reversal)
      pushTimeline(operation.source_entity_id, 'entity_operation.reversed', `restored from redirect`)
      return { status: 201, body: reversal }
    }

    if (pathname === '/api/v1/knowledge/resolution/candidates' && method === 'GET') {
      const params = new URLSearchParams(queryString)
      const status = params.get('status')
      const items = resolutionCandidates.list().filter((item) => !status || item.status === status)
      return { status: 200, body: { items, next_cursor: null } }
    }
    if (pathname === '/api/v1/knowledge/resolution/candidates' && method === 'POST') {
      const item = resolutionCandidates.create({
        left_entity_id: body.left_entity_id,
        right_entity_id: body.right_entity_id,
        score: 0.5,
        factors: {},
        resolver_version: 'fixture-v1',
        status: 'open',
        created_at: nowIso(),
        resolved_at: null,
        resolved_by: null,
        reason: null,
      })
      return { status: 201, body: { deterministic: false, candidate: item } }
    }
    const decisionMatch = pathname.match(
      /^\/api\/v1\/knowledge\/resolution\/candidates\/([^/]+)\/(confirm|reject)$/,
    )
    if (decisionMatch && method === 'POST') {
      const [, id, decision] = decisionMatch
      const candidate = resolutionCandidates.find(id)
      if (!candidate) return { status: 404, body: { error: { code: 'CANDIDATE_NOT_FOUND', message: 'Not found' } } }
      candidate.status = decision === 'confirm' ? 'confirmed' : 'rejected'
      candidate.resolved_at = nowIso()
      candidate.resolved_by = 'fixture-user'
      candidate.reason = body.reason
      return { status: 200, body: candidate }
    }

    const claimsMatch = pathname.match(/^\/api\/v1\/knowledge\/entities\/([^/]+)\/claims$/)
    if (claimsMatch && method === 'GET') {
      return { status: 200, body: { items: claims.filter((claim) => claim.subject_id === claimsMatch[1]) } }
    }
    if (claimsMatch && method === 'POST') {
      const claim = {
        id: randomUUID(),
        subject_id: claimsMatch[1],
        predicate: body.predicate,
        value: body.value,
        source_id: body.source_id,
        confidence: body.confidence ?? 1,
        valid_from: body.valid_from ?? null,
        valid_to: body.valid_to ?? null,
        superseded_by: null,
        created_at: nowIso(),
      }
      claims.push(claim)
      pushTimeline(claimsMatch[1], 'knowledge_entity.claim_recorded', `claim recorded: ${body.predicate}`)
      return { status: 201, body: claim }
    }

    const relationshipsMatch = pathname.match(/^\/api\/v1\/knowledge\/entities\/([^/]+)\/relationships$/)
    if (relationshipsMatch && method === 'GET') {
      const entityId = relationshipsMatch[1]
      const items = relationships.filter((rel) => rel.from_entity_id === entityId || rel.to_entity_id === entityId)
      return { status: 200, body: { items } }
    }
    if (relationshipsMatch && method === 'POST') {
      const fromId = relationshipsMatch[1]
      const relationship = {
        id: randomUUID(),
        from_entity_id: fromId,
        to_entity_id: body.to_entity_id,
        relationship_type: body.relationship_type,
        confidence: body.confidence ?? 1,
        evidence_id: body.evidence_id ?? null,
        valid_from: body.valid_from ?? null,
        valid_to: body.valid_to ?? null,
        status: 'active',
      }
      relationships.push(relationship)
      pushTimeline(fromId, 'relationship.created', `${body.relationship_type} -> ${body.to_entity_id}`)
      return { status: 201, body: relationship }
    }

    const timelineMatch = pathname.match(/^\/api\/v1\/knowledge\/entities\/([^/]+)\/timeline$/)
    if (timelineMatch && method === 'GET') {
      const items = timelineEntries
        .filter((entry) => entry.entity_id === timelineMatch[1])
        .sort((a, b) => b.effective_at.localeCompare(a.effective_at))
      return { status: 200, body: { items, next_cursor: null } }
    }

    return entitiesResource(pathname, method, body, queryString)
  }

  return { dispatch, entities, claims, relationships, timelineEntries, resolutionCandidates, entityOperations }
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
  const knowledge = makeKnowledgeApi({
    entities: overrides.knowledgeEntities,
    claims: overrides.knowledgeClaims,
    relationships: overrides.knowledgeRelationships,
    timelineEntries: overrides.knowledgeTimelineEntries,
    resolutionCandidates: overrides.resolutionCandidates,
  })

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
    if (pathname.startsWith('/api/v1/knowledge')) {
      const result = knowledge.dispatch(pathname, method, body, queryString)
      if (result) return result
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
    knowledge,
    dashboard,
    brief,
    evidence,
    setOffline: (value) => { offline = value },
  }
}
