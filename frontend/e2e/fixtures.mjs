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
 * Phase 3 attention-engine endpoints (attention queue, waiting, risk review
 * queue, plans, meeting prep). Kept as one dedicated dispatcher, same
 * rationale as makeKnowledgeApi below: none of these fit the generic
 * {base}/:id/:action resourceHandler shape -- risks/review-queue is a
 * static path that would otherwise be swallowed as risks/:id (mirroring
 * the real backend's router-registration-order fix), meetings/:id/prep and
 * .../prep/refresh nest under an existing resource's id, and plans has two
 * distinct nested collections (plans and their blocks).
 */
function makeAttentionApi(overrides = {}) {
  const attentionItems = createCollection(overrides.attentionItems ?? [])
  const waitingLinks = createCollection(overrides.waitingLinks ?? [])
  const risks = overrides.risksCollection
  const reviewQueue = [...(overrides.reviewQueue ?? [])]
  const plans = createCollection(overrides.plans ?? [])
  const meetingPacks = new Map(Object.entries(overrides.meetingPacks ?? {}))
  const meetingParticipants = new Map()
  const aiEnrichmentEnabled = Boolean(overrides.aiEnrichmentEnabled)

  function dispatch(pathname, method, body, queryString) {
    if (pathname === '/api/v1/attention' && method === 'GET') {
      return { status: 200, body: { items: attentionItems.list() } }
    }
    const attentionAction = pathname.match(/^\/api\/v1\/attention\/([^/]+)\/(dismiss|defer|restore)$/)
    if (attentionAction && method === 'POST') {
      const [, id, action] = attentionAction
      const item = attentionItems.find(id)
      if (!item) return { status: 404, body: { error: { code: 'NOT_FOUND', message: 'Not found' } } }
      if (action === 'dismiss') Object.assign(item, { dismissed_at: nowIso(), override_reason: body.reason ?? null })
      if (action === 'defer') Object.assign(item, { deferred_until: body.deferred_until, override_reason: body.reason ?? null })
      if (action === 'restore') Object.assign(item, { dismissed_at: null, deferred_until: null, override_reason: null })
      return { status: 200, body: item }
    }

    if (pathname === '/api/v1/waiting' && method === 'GET') {
      return { status: 200, body: { items: waitingLinks.list(), next_cursor: null } }
    }
    if (pathname === '/api/v1/waiting' && method === 'POST') {
      const item = waitingLinks.create({
        ...body,
        status: 'open',
        since_at: body.since_at ?? nowIso(),
        superseded_by: null,
        created_at: nowIso(),
        updated_at: nowIso(),
      })
      return { status: 201, body: item }
    }
    const waitingTerminal = pathname.match(/^\/api\/v1\/waiting\/([^/]+)\/(fulfil|cancel)$/)
    if (waitingTerminal && method === 'POST') {
      const [, id, action] = waitingTerminal
      return waitingLinks.mutate(id, body.expected_version, () => ({ status: action === 'fulfil' ? 'fulfilled' : 'cancelled' }))
    }

    if (pathname === '/api/v1/risks/review-queue' && method === 'GET') {
      return { status: 200, body: { items: reviewQueue } }
    }
    const riskReview = pathname.match(/^\/api\/v1\/risks\/([^/]+)\/review$/)
    if (riskReview && method === 'POST' && risks) {
      const [, id] = riskReview
      const result = risks.mutate(id, body.expected_version, () => ({ status: body.outcome === 'closed' ? 'closed' : 'monitoring' }))
      if (result.status === 200) {
        const index = reviewQueue.findIndex((entry) => entry.risk_id === id)
        if (index !== -1) reviewQueue.splice(index, 1)
      }
      return result
    }

    if (pathname === '/api/v1/plans' && method === 'GET') {
      return { status: 200, body: { items: plans.list(), next_cursor: null } }
    }
    if (pathname === '/api/v1/plans' && method === 'POST') {
      const item = plans.create({
        period_start: body.period_start,
        period_end: body.period_end,
        status: 'proposed',
        policy_version: 1,
        capacity_minutes: overrides.planCapacityMinutes ?? 480,
        conflicts: overrides.planConflicts ?? [],
        unscheduled: overrides.planUnscheduled ?? [],
        superseded_by: null,
        accepted_at: null,
        blocks: overrides.planBlocks ?? [],
        diff: null,
        created_at: nowIso(),
        updated_at: nowIso(),
      })
      return { status: 201, body: item }
    }
    const planAction = pathname.match(/^\/api\/v1\/plans\/([^/]+)\/(accept|supersede|propose)$/)
    if (planAction && method === 'POST') {
      const [, id, action] = planAction
      if (action === 'accept') return plans.mutate(id, body.expected_version, () => ({ status: 'accepted', accepted_at: nowIso() }))
      if (action === 'supersede') return plans.mutate(id, body.expected_version, () => ({ status: 'superseded' }))
      // propose (replan): mark the old plan superseded, create a new one
      // carrying an explicit diff -- UX-STATES.md requires the diff be
      // presented before any acceptance.
      const old = plans.find(id)
      if (!old) return { status: 404, body: { error: { code: 'NOT_FOUND', message: 'Not found' } } }
      if (old.version !== body.expected_version) return { status: 409, body: conflictBody(old) }
      const diff = overrides.replanDiff ?? []
      const created = plans.create({
        period_start: old.period_start,
        period_end: old.period_end,
        status: 'proposed',
        policy_version: old.policy_version,
        capacity_minutes: old.capacity_minutes,
        conflicts: old.conflicts,
        unscheduled: old.unscheduled,
        superseded_by: null,
        accepted_at: null,
        blocks: overrides.replanBlocks ?? old.blocks,
        diff,
        created_at: nowIso(),
        updated_at: nowIso(),
      })
      old.status = 'superseded'
      old.superseded_by = created.id
      old.version += 1
      return { status: 201, body: created }
    }
    const blockAction = pathname.match(/^\/api\/v1\/plans\/([^/]+)\/blocks\/([^/]+)\/(move|remove)$/)
    if (blockAction && method === 'POST') {
      const [, planId, blockId, action] = blockAction
      return plans.mutate(planId, body.expected_version, (plan) => ({
        blocks: action === 'remove'
          ? plan.blocks.filter((block) => block.id !== blockId)
          : plan.blocks.map((block) => (block.id === blockId ? { ...block, starts_at: body.starts_at, ends_at: body.ends_at } : block)),
      }))
    }

    const meetingPrep = pathname.match(/^\/api\/v1\/meetings\/([^/]+)\/prep$/)
    if (meetingPrep) {
      const [, meetingId] = meetingPrep
      if (method === 'GET') {
        const pack = meetingPacks.get(meetingId)
        if (!pack) return { status: 404, body: { error: { code: 'MEETING_PACK_NOT_FOUND', message: 'No pack yet' } } }
        return { status: 200, body: { ...pack, enrichment: aiEnrichmentEnabled ? pack.enrichment : { available: false, summary: null, error_code: 'feature_disabled' } } }
      }
      if (method === 'POST') {
        if (meetingPacks.has(meetingId)) return { status: 409, body: { error: { code: 'MEETING_PACK_EXISTS', message: 'Pack already exists' } } }
        const pack = overrides.buildMeetingPack ? overrides.buildMeetingPack(meetingId) : null
        if (!pack) return { status: 404, body: { error: { code: 'MEETING_NOT_FOUND', message: 'Not found' } } }
        meetingPacks.set(meetingId, pack)
        return { status: 201, body: pack }
      }
    }
    const meetingPrepRefresh = pathname.match(/^\/api\/v1\/meetings\/([^/]+)\/prep\/refresh$/)
    if (meetingPrepRefresh && method === 'POST') {
      const [, meetingId] = meetingPrepRefresh
      const existing = meetingPacks.get(meetingId)
      if (!existing) return { status: 404, body: { error: { code: 'MEETING_PACK_NOT_FOUND', message: 'No pack yet' } } }
      const refreshed = { ...existing, id: randomUUID(), status: 'fresh', generated_at: nowIso() }
      meetingPacks.set(meetingId, refreshed)
      return { status: 201, body: refreshed }
    }
    const meetingParticipantsPath = pathname.match(/^\/api\/v1\/meetings\/([^/]+)\/participants$/)
    if (meetingParticipantsPath) {
      const [, meetingId] = meetingParticipantsPath
      const list = meetingParticipants.get(meetingId) ?? []
      if (method === 'GET') return { status: 200, body: { items: list } }
      if (method === 'POST') {
        const participant = { id: randomUUID(), entity_id: body.entity_id, entity_name: overrides.entityNames?.[body.entity_id] ?? 'Unknown', role: body.role ?? 'attendee' }
        meetingParticipants.set(meetingId, [...list, participant])
        return { status: 201, body: participant }
      }
    }

    return null
  }

  return { dispatch, collections: { attentionItems, waitingLinks, plans }, meetingPacks }
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
  const aliases = [...(overrides.aliases ?? [])]
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
      const items = resolutionCandidates
        .list()
        .filter((item) => !status || item.status === status)
        .filter((item) => !item.deferred_until || new Date(item.deferred_until).getTime() <= Date.now())
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
        deferred_until: null,
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
    const deferMatch = pathname.match(
      /^\/api\/v1\/knowledge\/resolution\/candidates\/([^/]+)\/defer$/,
    )
    if (deferMatch && method === 'POST') {
      const candidate = resolutionCandidates.find(deferMatch[1])
      if (!candidate) return { status: 404, body: { error: { code: 'CANDIDATE_NOT_FOUND', message: 'Not found' } } }
      if (candidate.status !== 'open') {
        return { status: 409, body: { error: { code: 'CANDIDATE_NOT_OPEN', message: 'Not open' } } }
      }
      if (new Date(body.deferred_until).getTime() <= Date.now()) {
        return { status: 422, body: { error: { code: 'DEFER_UNTIL_MUST_BE_FUTURE', message: 'Must be future' } } }
      }
      candidate.deferred_until = body.deferred_until
      return { status: 200, body: candidate }
    }

    const aliasesMatch = pathname.match(/^\/api\/v1\/knowledge\/entities\/([^/]+)\/aliases$/)
    if (aliasesMatch && method === 'GET') {
      return { status: 200, body: { items: aliases.filter((alias) => alias.entity_id === aliasesMatch[1]) } }
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

    const supersedeMatch = pathname.match(/^\/api\/v1\/knowledge\/entities\/([^/]+)\/claims\/([^/]+)\/supersede$/)
    if (supersedeMatch && method === 'POST') {
      const [, entityId, claimId] = supersedeMatch
      const original = claims.find((claim) => claim.id === claimId)
      if (original) original.superseded_by = randomUUID()
      const claim = {
        id: original ? original.superseded_by : randomUUID(),
        subject_id: entityId,
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
      pushTimeline(entityId, 'knowledge_entity.claim_recorded', `claim corrected: ${body.predicate}`)
      return { status: 201, body: claim }
    }

    // Resolves an entity ID to its `canonical_name`/`kind`, mirroring the
    // real backend's join in `relationships.py` -- a team-concept gap
    // analysis found `RelationshipResponse` used to carry bare UUIDs, which
    // made `EntityDetail.tsx`'s relationships list (raw IDs, no names)
    // unreadable, so the real API now always resolves both endpoints.
    function resolveRef(id) {
      const entity = entities.find(id)
      return { name: entity?.canonical_name ?? id, kind: entity?.kind ?? 'unknown' }
    }
    function withResolvedNames(rel) {
      const from = resolveRef(rel.from_entity_id)
      const to = resolveRef(rel.to_entity_id)
      return {
        ...rel,
        from_entity_name: from.name,
        from_entity_kind: from.kind,
        to_entity_name: to.name,
        to_entity_kind: to.kind,
      }
    }

    const relationshipsMatch = pathname.match(/^\/api\/v1\/knowledge\/entities\/([^/]+)\/relationships$/)
    if (relationshipsMatch && method === 'GET') {
      const entityId = relationshipsMatch[1]
      const params = new URLSearchParams(queryString)
      const relationshipType = params.get('relationship_type')
      const direction = params.get('direction')
      const items = relationships
        .filter((rel) => {
          if (direction === 'incoming') return rel.to_entity_id === entityId
          if (direction === 'outgoing') return rel.from_entity_id === entityId
          return rel.from_entity_id === entityId || rel.to_entity_id === entityId
        })
        .filter((rel) => !relationshipType || rel.relationship_type === relationshipType)
        .map(withResolvedNames)
      return { status: 200, body: { items } }
    }
    if (relationshipsMatch && method === 'POST') {
      const fromId = relationshipsMatch[1]
      // evidence_id is required (matching the real backend's DATA-MODEL.md
      // "at least one source reference" invariant) -- unlike a real
      // pydantic-validation 422, this mock returns the app's own error shape.
      if (!body.evidence_id) {
        return { status: 422, body: { error: { code: 'VALIDATION_ERROR', message: 'evidence_id is required' } } }
      }
      const relationship = {
        id: randomUUID(),
        from_entity_id: fromId,
        to_entity_id: body.to_entity_id,
        relationship_type: body.relationship_type,
        confidence: body.confidence ?? 1,
        evidence_id: body.evidence_id,
        valid_from: body.valid_from ?? null,
        valid_to: body.valid_to ?? null,
        status: 'active',
      }
      relationships.push(relationship)
      pushTimeline(fromId, 'relationship.created', `${body.relationship_type} -> ${body.to_entity_id}`)
      return { status: 201, body: withResolvedNames(relationship) }
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

  return { dispatch, entities, aliases, claims, relationships, timelineEntries, resolutionCandidates, entityOperations }
}

/**
 * Phase 4 AI Runtime surface (`POST /api/v1/ai/runs`, `GET /api/v1/ai/
 * runs/{id}`, `POST /api/v1/ai/runs/{id}/cancel`) -- just enough of
 * `phase-004/API-SCHEMAS.md`'s `AiRunResponse` shape for
 * `attention-explanation.mjs` to exercise `AttentionExplanation.tsx`
 * against a mocked backend, matching this file's existing "no live
 * backend, no live model" convention. `overrides.buildRun(body)` lets a
 * scenario decide the response for a given request body (e.g. a specific
 * `attention_item_id`); returning `null` yields the same
 * `ATTENTION_ITEM_NOT_FOUND` 404 shape `runtime.py:create_run` returns for
 * an item that doesn't resolve in the caller's workspace.
 */
function makeAiRuntimeApi(overrides = {}) {
  const runs = new Map()
  const buildRun = overrides.buildRun ?? (() => null)

  function dispatch(pathname, method, body) {
    if (pathname === '/api/v1/ai/runs' && method === 'POST') {
      const run = buildRun(body)
      if (!run) return { status: 404, body: { error: { code: 'ATTENTION_ITEM_NOT_FOUND', message: 'Not found' } } }
      runs.set(run.id, run)
      return { status: 200, body: run }
    }
    const getMatch = pathname.match(/^\/api\/v1\/ai\/runs\/([^/]+)$/)
    if (getMatch && method === 'GET') {
      const run = runs.get(getMatch[1])
      if (!run) return { status: 404, body: { error: { code: 'AI_RUN_NOT_FOUND', message: 'Not found' } } }
      return { status: 200, body: run }
    }
    const cancelMatch = pathname.match(/^\/api\/v1\/ai\/runs\/([^/]+)\/cancel$/)
    if (cancelMatch && method === 'POST') {
      const run = runs.get(cancelMatch[1])
      if (!run) return { status: 404, body: { error: { code: 'AI_RUN_NOT_FOUND', message: 'Not found' } } }
      if (run.status !== 'running') return { status: 409, body: { error: { code: 'AI_RUN_ALREADY_TERMINAL', message: 'Already terminal' } } }
      const cancelled = { ...run, status: 'cancelled', completed_at: nowIso() }
      runs.set(cancelMatch[1], cancelled)
      return { status: 200, body: cancelled }
    }
    return null
  }

  return { dispatch, runs }
}

/**
 * Phase 5 automation surface (`docs/phases/phase-005/API-SCHEMAS.md`):
 * workflows/versions, policies, runs (+ steps/compensation ledger),
 * approvals, kill switches and the Task 7 `GET /automations/triggers`
 * read endpoint. This fixture has no real worker loop behind it (there is
 * no durable-execution engine in a browser-only mock) -- every state
 * transition a real worker would produce automatically (queued -> running
 * -> waiting_approval, a policy-revoked block surfacing on a run, a kill
 * switch discovered mid-dispatch) is instead scripted directly by the
 * scenario itself via the returned `runs`/`policies`/`workflowVersions`/
 * `killSwitches` handles, exactly like `attention-queue.mjs` already
 * drives `fixtures.collections.risks.mutate(...)` directly rather than
 * simulating a real risk-review backend. `POST /workflows` and `POST
 * /policies` are deliberately not full replicas of the real backend's own
 * SCHEMA_INVALID/POLICY validation -- this fixture proves the UI's own
 * request/response wiring and state rendering, not the backend's own
 * validation logic (already covered by `tests/test_automation_*_postgres.
 * py`).
 */
function makeAutomationApi(overrides = {}) {
  let nextVersionSeq = 1
  const workflowVersions = [...(overrides.workflowVersions ?? [])]
  const policies = [...(overrides.policies ?? [])]
  const runs = [...(overrides.runs ?? [])]
  const runSteps = new Map(Object.entries(overrides.runSteps ?? {}))
  const runCompensationSteps = new Map(Object.entries(overrides.runCompensationSteps ?? {}))
  const approvals = [...(overrides.approvals ?? [])]
  const triggers = [...(overrides.triggers ?? [])]
  const simulateResponses = new Map(Object.entries(overrides.simulateResponses ?? {}))
  const buildRun = overrides.buildRun ?? null
  const killSwitches = {
    global: overrides.globalKillSwitch ?? null,
    perWorkflow: new Map(Object.entries(overrides.workflowKillSwitches ?? {})),
    history: new Map(Object.entries(overrides.killSwitchHistory ?? {})),
  }

  function summaries() {
    const byWorkflow = new Map()
    for (const version of workflowVersions) {
      const list = byWorkflow.get(version.workflow_id) ?? []
      list.push(version)
      byWorkflow.set(version.workflow_id, list)
    }
    return [...byWorkflow.entries()].map(([workflow_id, versions]) => {
      const latest = versions[versions.length - 1]
      const active = versions.find((v) => v.status === 'active')
      return {
        workflow_id,
        latest_version: latest.version,
        latest_status: latest.status,
        active_version: active ? active.version : null,
        latest_version_id: latest.id,
        active_version_id: active ? active.id : null,
      }
    })
  }

  function killSwitchStatus(workflowId) {
    const activeWorkflow = killSwitches.perWorkflow.get(workflowId) ?? null
    return {
      workflow_id: workflowId,
      killed: Boolean(killSwitches.global?.active) || Boolean(activeWorkflow?.active),
      active_global: killSwitches.global?.active ? killSwitches.global : null,
      active_workflow: activeWorkflow?.active ? activeWorkflow : null,
      history: killSwitches.history.get(workflowId) ?? [],
    }
  }

  function dispatch(pathname, method, body, queryString) {
    if (!pathname.startsWith('/api/v1/automations')) return null
    const params = new URLSearchParams(queryString)

    if (pathname === '/api/v1/automations/workflows' && method === 'GET') {
      return { status: 200, body: { workflows: summaries() } }
    }
    if (pathname === '/api/v1/automations/workflows' && method === 'POST') {
      const workflowVersionsForId = workflowVersions.filter((v) => v.workflow_id === body.workflow_id)
      const version = workflowVersionsForId.length ? Math.max(...workflowVersionsForId.map((v) => v.version)) + 1 : 1
      const created = {
        id: `automation-version-${nextVersionSeq++}`,
        workflow_id: body.workflow_id,
        version,
        graph: body.graph,
        trigger_refs: body.trigger_refs ?? [],
        policy_ref: body.policy_ref ?? null,
        definition_hash: `fixture-hash-${nextVersionSeq}`,
        status: 'draft',
        created_at: nowIso(),
        updated_at: nowIso(),
      }
      workflowVersions.push(created)
      return { status: 201, body: created }
    }
    const versionMatch = pathname.match(/^\/api\/v1\/automations\/workflows\/([^/]+)$/)
    if (versionMatch && method === 'GET') {
      const version = workflowVersions.find((v) => v.id === versionMatch[1])
      if (!version) return { status: 404, body: { error: { code: 'WORKFLOW_NOT_FOUND', message: 'Not found' } } }
      return { status: 200, body: version }
    }
    const publishMatch = pathname.match(/^\/api\/v1\/automations\/workflows\/([^/]+)\/publish$/)
    if (publishMatch && method === 'POST') {
      const version = workflowVersions.find((v) => v.id === publishMatch[1])
      if (!version) return { status: 404, body: { error: { code: 'WORKFLOW_NOT_FOUND', message: 'Not found' } } }
      if (version.status === 'active') return { status: 200, body: version }
      if (version.status === 'retired') {
        return { status: 409, body: { error: { code: 'WORKFLOW_VERSION_NOT_DRAFT', message: 'Workflow Version Not Draft', details: { status: version.status } } } }
      }
      for (const other of workflowVersions) {
        if (other.workflow_id === version.workflow_id && other.status === 'active') other.status = 'retired'
      }
      version.status = 'active'
      version.updated_at = nowIso()
      return { status: 200, body: version }
    }
    const disableMatch = pathname.match(/^\/api\/v1\/automations\/workflows\/([^/]+)\/disable$/)
    if (disableMatch && method === 'POST') {
      const version = workflowVersions.find((v) => v.id === disableMatch[1])
      if (!version) return { status: 404, body: { error: { code: 'WORKFLOW_NOT_FOUND', message: 'Not found' } } }
      if (version.status !== 'active') {
        return { status: 409, body: { error: { code: 'WORKFLOW_NOT_ACTIVE', message: 'Workflow Not Active', details: { status: version.status } } } }
      }
      version.status = 'retired'
      version.updated_at = nowIso()
      return { status: 200, body: version }
    }
    const simulateMatch = pathname.match(/^\/api\/v1\/automations\/workflows\/([^/]+)\/simulate$/)
    if (simulateMatch && method === 'POST') {
      const version = workflowVersions.find((v) => v.id === simulateMatch[1])
      if (!version) return { status: 404, body: { error: { code: 'WORKFLOW_NOT_FOUND', message: 'Not found' } } }
      const scripted = simulateResponses.get(version.id)
      if (scripted) return { status: 200, body: scripted }
      return {
        status: 200,
        body: {
          workflow_id: version.workflow_id,
          version: version.version,
          steps: (version.graph.steps ?? [])
            .filter((step) => step.step_type === 'action')
            .map((step, index) => ({
              step_index: index,
              step_id: step.step_id,
              step_type: step.step_type,
              action_ref: step.action_ref ?? null,
              compensate_ref: step.compensate_ref ?? null,
              preview: { simulated: true },
              reversible: true,
              high_impact_categories: [],
              dispatch_gate: 'clear',
              policy_block_reason: null,
              error: null,
            })),
        },
      }
    }

    const workflowKillSwitchStatusMatch = pathname.match(/^\/api\/v1\/automations\/workflows\/([^/]+)\/kill_switch$/)
    if (workflowKillSwitchStatusMatch && method === 'GET') {
      return { status: 200, body: killSwitchStatus(decodeURIComponent(workflowKillSwitchStatusMatch[1])) }
    }
    if (workflowKillSwitchStatusMatch && method === 'POST') {
      const workflowId = decodeURIComponent(workflowKillSwitchStatusMatch[1])
      const switchObj = { workflow_id: workflowId, active: body.active, reason: body.reason ?? null, activated_by: 'fixture-user', activated_at: nowIso(), deactivated_by: body.active ? null : 'fixture-user', deactivated_at: body.active ? null : nowIso() }
      killSwitches.perWorkflow.set(workflowId, switchObj)
      const history = killSwitches.history.get(workflowId) ?? []
      killSwitches.history.set(workflowId, [...history, switchObj])
      return { status: 200, body: switchObj }
    }
    if (pathname === '/api/v1/automations/kill_switch' && method === 'POST') {
      const switchObj = { workflow_id: null, active: body.active, reason: body.reason ?? null, activated_by: 'fixture-user', activated_at: nowIso(), deactivated_by: body.active ? null : 'fixture-user', deactivated_at: body.active ? null : nowIso() }
      killSwitches.global = switchObj
      return { status: 200, body: switchObj }
    }

    if (pathname === '/api/v1/automations/policies' && method === 'GET') {
      const workflowId = params.get('workflow_id')
      const items = policies.filter((p) => !workflowId || p.workflow_id === workflowId)
      return { status: 200, body: { policies: items } }
    }
    if (pathname === '/api/v1/automations/policies' && method === 'POST') {
      const created = {
        id: `automation-policy-${policies.length + 1}`,
        workflow_id: body.workflow_id,
        action_types: body.action_types ?? [],
        data_classes: body.data_classes ?? [],
        value_limit: String(body.value_limit ?? 0),
        count_limit: body.count_limit ?? 0,
        rate_limit: body.rate_limit ?? { runs_per_workflow_per_hour: 10 },
        schedule: body.schedule ?? null,
        approval_mode: body.approval_mode,
        expires_at: new Date(Date.now() + 90 * 24 * 60 * 60 * 1000).toISOString(),
        revoked_at: null,
        status: 'active',
        version: 1,
        created_at: nowIso(),
        updated_at: nowIso(),
      }
      policies.push(created)
      return { status: 201, body: created }
    }
    const revokeMatch = pathname.match(/^\/api\/v1\/automations\/policies\/([^/]+)\/revoke$/)
    if (revokeMatch && method === 'POST') {
      const policy = policies.find((p) => p.id === revokeMatch[1])
      if (!policy) return { status: 404, body: { error: { code: 'POLICY_NOT_FOUND', message: 'Not found' } } }
      if (policy.revoked_at) {
        return { status: 409, body: { error: { code: 'POLICY_REVOKED', message: 'Policy Revoked', details: { revoked_at: policy.revoked_at } } } }
      }
      policy.revoked_at = nowIso()
      policy.status = 'revoked'
      policy.updated_at = nowIso()
      return { status: 200, body: policy }
    }

    if (pathname === '/api/v1/automations/runs' && method === 'GET') {
      const statusFilter = params.get('status')
      const items = runs.filter((r) => !statusFilter || r.status === statusFilter)
      return { status: 200, body: { runs: items } }
    }
    if (pathname === '/api/v1/automations/runs' && method === 'POST') {
      const killed = killSwitchStatus(body.workflow_id).killed
      if (killed) {
        return { status: 409, body: { error: { code: 'KILL_SWITCH_ACTIVE', message: 'Kill Switch Active', details: { workflow_id: body.workflow_id } } } }
      }
      const created = buildRun ? buildRun(body) : {
        id: `automation-run-${runs.length + 1}`,
        workflow_id: body.workflow_id,
        workflow_version: 1,
        policy_id: null,
        trigger_ref: 'manual:fixture-user',
        status: 'queued',
        current_step_index: 0,
        queued_at: nowIso(),
        started_at: null,
        finished_at: null,
        created_at: nowIso(),
        updated_at: nowIso(),
      }
      if (!created) return { status: 409, body: { error: { code: 'WORKFLOW_NOT_ACTIVE', message: 'Workflow Not Active', details: { workflow_id: body.workflow_id } } } }
      runs.push(created)
      if (!runSteps.has(created.id)) runSteps.set(created.id, [])
      if (!runCompensationSteps.has(created.id)) runCompensationSteps.set(created.id, [])
      return { status: 201, body: created }
    }
    const runDetailMatch = pathname.match(/^\/api\/v1\/automations\/runs\/([^/]+)$/)
    if (runDetailMatch && method === 'GET') {
      const run = runs.find((r) => r.id === runDetailMatch[1])
      if (!run) return { status: 404, body: { error: { code: 'RUN_NOT_FOUND', message: 'Not found' } } }
      return { status: 200, body: { ...run, steps: runSteps.get(run.id) ?? [], compensation_steps: runCompensationSteps.get(run.id) ?? [] } }
    }
    const runActionMatch = pathname.match(/^\/api\/v1\/automations\/runs\/([^/]+)\/(pause|resume|cancel)$/)
    if (runActionMatch && method === 'POST') {
      const [, runId, action] = runActionMatch
      const run = runs.find((r) => r.id === runId)
      if (!run) return { status: 404, body: { error: { code: 'RUN_NOT_FOUND', message: 'Not found' } } }
      if (action === 'pause') run.status = 'paused'
      if (action === 'resume') {
        if (run.status !== 'paused') return { status: 409, body: { error: { code: 'RUN_NOT_PAUSED', message: 'Run Not Paused', details: { status: run.status } } } }
        run.status = 'queued'
      }
      if (action === 'cancel') run.status = 'cancelled'
      run.updated_at = nowIso()
      return { status: 200, body: run }
    }

    if (pathname === '/api/v1/automations/approvals' && method === 'GET') {
      const statusFilter = params.get('status')
      const items = approvals.filter((a) => !statusFilter || a.status === statusFilter)
      return { status: 200, body: { approvals: items } }
    }
    const decideMatch = pathname.match(/^\/api\/v1\/automations\/approvals\/([^/]+)\/(approve|reject)$/)
    if (decideMatch && method === 'POST') {
      const [, approvalId, decision] = decideMatch
      const approval = approvals.find((a) => a.id === approvalId)
      if (!approval) return { status: 404, body: { error: { code: 'APPROVAL_NOT_FOUND', message: 'Not found' } } }
      if (approval.status !== 'pending') {
        return { status: 409, body: { error: { code: 'APPROVAL_ALREADY_DECIDED', message: 'Approval Already Decided', details: { decision: approval.decision, decided_at: approval.decided_at } } } }
      }
      if (decision === 'approve' && body.action_digest !== approval.action_digest) {
        return { status: 409, body: { error: { code: 'DIGEST_MISMATCH', message: 'Digest Mismatch', details: { expected_digest: approval.action_digest, provided_digest: body.action_digest ?? null } } } }
      }
      approval.status = decision === 'approve' ? 'approved' : 'rejected'
      approval.decision = approval.status
      approval.decided_at = nowIso()
      approval.decided_by = 'fixture-user'
      approval.updated_at = nowIso()
      return { status: 200, body: approval }
    }

    if (pathname === '/api/v1/automations/triggers' && method === 'GET') {
      const workflowId = params.get('workflow_id')
      const items = triggers.filter((t) => !workflowId || t.workflow_id === workflowId)
      return { status: 200, body: { triggers: items } }
    }

    return null
  }

  return { dispatch, workflowVersions, policies, runs, runSteps, runCompensationSteps, approvals, triggers, killSwitches }
}

/**
 * In-memory fake of the Phase 6 Engineering Workspace API surface
 * (`GET|POST /api/v1/engineering/connectors`, `.../sync`, `.../disable`,
 * `GET .../sync-runs`, `.../repositories`, `.../work-items`, `.../incidents`,
 * `.../decisions`, `.../metrics`) -- mirrors `makeAutomationApi`'s own shape.
 * There is no real backend behind this fixture (this codebase's own
 * "browser-only mock" convention, matching every other Phase 1-5
 * scenario) -- a scenario that needs to move a connector between states
 * (e.g. `active` -> `rate_limited` -> `disconnected`) mutates `fixtures.
 * engineering.connectors` directly, the same way `automation-lifecycle.mjs`
 * scripts a run's own state transitions via `fixtures.automation.runs`.
 */
function makeEngineeringApi(overrides = {}) {
  const connectors = [...(overrides.connectors ?? [])]
  const syncRuns = [...(overrides.syncRuns ?? [])]
  const repositories = [...(overrides.repositories ?? [])]
  const workItems = [...(overrides.workItems ?? [])]
  const incidents = [...(overrides.incidents ?? [])]
  const decisions = [...(overrides.decisions ?? [])]
  const metrics = [...(overrides.metrics ?? [])]
  let nextId = 1

  function dispatch(pathname, method, body, queryString) {
    if (!pathname.startsWith('/api/v1/engineering')) return null
    const params = new URLSearchParams(queryString)

    if (pathname === '/api/v1/engineering/connectors' && method === 'GET') {
      return { status: 200, body: { connectors } }
    }
    if (pathname === '/api/v1/engineering/connectors' && method === 'POST') {
      const created = {
        id: `engineering-connector-${nextId++}`,
        provider: body.provider,
        external_account_id: `fixture-${body.provider}-account`,
        display_name: `${body.provider} (fixture)`,
        granted_scopes: [],
        status: 'pending',
        status_detail: null,
        last_synced_at: null,
        last_error: null,
        disconnected_at: null,
        version: 1,
        created_at: nowIso(),
        updated_at: nowIso(),
      }
      connectors.push(created)
      return { status: 201, body: created }
    }
    const syncMatch = pathname.match(/^\/api\/v1\/engineering\/connectors\/([^/]+)\/sync$/)
    if (syncMatch && method === 'POST') {
      const connector = connectors.find((c) => c.id === syncMatch[1])
      if (!connector) return { status: 404, body: { error: { code: 'CONNECTOR_NOT_FOUND', message: 'Not found' } } }
      const run = {
        id: `engineering-sync-run-${nextId++}`,
        connector_account_id: connector.id,
        run_type: body.run_type,
        status: 'succeeded',
        items_processed: 0,
        error_summary: null,
        started_at: nowIso(),
        completed_at: nowIso(),
      }
      syncRuns.push(run)
      connector.last_synced_at = nowIso()
      return { status: 201, body: run }
    }
    const disableMatch = pathname.match(/^\/api\/v1\/engineering\/connectors\/([^/]+)\/disable$/)
    if (disableMatch && method === 'POST') {
      const connector = connectors.find((c) => c.id === disableMatch[1])
      if (!connector) return { status: 404, body: { error: { code: 'CONNECTOR_NOT_FOUND', message: 'Not found' } } }
      connector.status = 'disconnected'
      connector.disconnected_at = nowIso()
      return { status: 200, body: connector }
    }
    if (pathname === '/api/v1/engineering/sync-runs' && method === 'GET') {
      const connectorAccountId = params.get('connector_account_id')
      const items = syncRuns.filter((r) => !connectorAccountId || r.connector_account_id === connectorAccountId)
      return { status: 200, body: { sync_runs: items } }
    }
    if (pathname === '/api/v1/engineering/repositories' && method === 'GET') {
      const connectorAccountId = params.get('connector_account_id')
      const teamEntityId = params.get('team_entity_id')
      const items = repositories.filter((r) =>
        (!connectorAccountId || r.connector_account_id === connectorAccountId)
        && (!teamEntityId || r.team_entity_id === teamEntityId))
      return { status: 200, body: { repositories: items } }
    }
    if (pathname === '/api/v1/engineering/work-items' && method === 'GET') {
      const connectorAccountId = params.get('connector_account_id')
      const statusFilter = params.get('status')
      const teamEntityId = params.get('team_entity_id')
      const items = workItems.filter((item) =>
        (!connectorAccountId || item.connector_account_id === connectorAccountId)
        && (!statusFilter || item.status === statusFilter)
        && (!teamEntityId || item.team_entity_id === teamEntityId))
      return { status: 200, body: { work_items: items } }
    }
    if (pathname === '/api/v1/engineering/incidents' && method === 'GET') {
      const statusFilter = params.get('status')
      const items = incidents.filter((incident) => !statusFilter || incident.status === statusFilter)
      return { status: 200, body: { incidents: items } }
    }
    if (pathname === '/api/v1/engineering/incidents' && method === 'POST') {
      const created = {
        id: `engineering-incident-${nextId++}`,
        title: body.title,
        description: body.description ?? null,
        severity: body.severity ?? 'medium',
        status: 'open',
        detected_at: body.detected_at,
        resolved_at: null,
        change_ids: body.change_ids ?? [],
        version: 1,
        created_at: nowIso(),
        updated_at: nowIso(),
      }
      incidents.push(created)
      return { status: 201, body: created }
    }
    const resolveMatch = pathname.match(/^\/api\/v1\/engineering\/incidents\/([^/]+)\/resolve$/)
    if (resolveMatch && method === 'POST') {
      const incident = incidents.find((i) => i.id === resolveMatch[1])
      if (!incident) return { status: 404, body: { error: { code: 'INCIDENT_NOT_FOUND', message: 'Not found' } } }
      if (incident.status === 'resolved') {
        return { status: 409, body: { error: { code: 'INCIDENT_ALREADY_RESOLVED', message: 'Already resolved' } } }
      }
      incident.status = 'resolved'
      incident.resolved_at = body.resolved_at
      incident.version += 1
      incident.updated_at = nowIso()
      return { status: 200, body: incident }
    }
    if (pathname === '/api/v1/engineering/decisions' && method === 'GET') {
      const statusFilter = params.get('status')
      const items = decisions.filter((decision) => !statusFilter || decision.status === statusFilter)
      return { status: 200, body: { decisions: items } }
    }
    if (pathname === '/api/v1/engineering/decisions' && method === 'POST') {
      const created = {
        id: `engineering-decision-${nextId++}`,
        title: body.title,
        description: body.description ?? null,
        rationale: null,
        status: 'proposed',
        decided_at: null,
        change_ids: body.change_ids ?? [],
        version: 1,
        created_at: nowIso(),
        updated_at: nowIso(),
      }
      decisions.push(created)
      return { status: 201, body: created }
    }
    const decideMatch = pathname.match(/^\/api\/v1\/engineering\/decisions\/([^/]+)\/decide$/)
    if (decideMatch && method === 'POST') {
      const decision = decisions.find((d) => d.id === decideMatch[1])
      if (!decision) return { status: 404, body: { error: { code: 'DECISION_NOT_FOUND', message: 'Not found' } } }
      if (decision.status !== 'proposed') {
        return { status: 409, body: { error: { code: 'DECISION_NOT_PROPOSED', message: 'Not proposed' } } }
      }
      decision.status = 'decided'
      decision.decided_at = body.decided_at
      decision.rationale = body.rationale ?? decision.rationale
      decision.version += 1
      decision.updated_at = nowIso()
      return { status: 200, body: decision }
    }
    if (pathname === '/api/v1/engineering/metrics' && method === 'GET') {
      return { status: 200, body: { metrics } }
    }

    return null
  }

  return { dispatch, connectors, syncRuns, repositories, workItems, incidents, decisions, metrics }
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

// Mirrors `backend/ecc/domains/personal/domains.py`'s own fixed
// `_CLASSIFICATION_BY_DOMAIN` -- the fixture has no server-side source of
// truth to read this from (unlike a real backend), so it keeps its own
// copy of the same six-domain enum the real API enforces.
const PERSONAL_CLASSIFICATION_BY_DOMAIN = {
  habits: 'standard',
  learning: 'standard',
  travel: 'standard',
  relationships: 'sensitive',
  health: 'high_stakes',
  finance: 'high_stakes',
}

/**
 * Shared in-memory backing store for Phase 8's `/api/v1/identity`,
 * `/api/v1/sharing`, `/api/v1/delegations`, `/api/v1/shared/activity` and
 * `/api/v1/ownership` surfaces. Unlike every other `make*Api` in this file,
 * `makeCollaborationApi` does NOT copy its seed arrays -- it is designed to
 * be constructed once per scenario via `createCollaborationStore` and then
 * handed, BY REFERENCE, to two (or more) separate `createFixtureApi` calls
 * (one per `browser.newContext()`, i.e. one per simulated identity), which
 * is what makes a fixture-level "identity A invites identity B, identity B
 * accepts" scenario possible at all: an invitation `push`ed by A's
 * dispatcher must be the exact same array entry B's dispatcher reads.
 */
export function createCollaborationStore(seed = {}) {
  return {
    workspaces: seed.workspaces ?? [],
    memberships: seed.memberships ?? [],
    accounts: seed.accounts ?? [],
    invitations: seed.invitations ?? [],
    delegations: seed.delegations ?? [],
    grants: seed.grants ?? [],
    activity: seed.activity ?? [],
    ownershipTransfers: seed.ownershipTransfers ?? [],
  }
}

/**
 * One identity's view onto a shared `store` (see `createCollaborationStore`
 * above). `self` names which account this browser context is signed in as
 * (`{accountId, usersId, email, displayName}`) -- every endpoint that in
 * the real backend derives its actor from the session (`AuthDep`) derives
 * it from `self` here instead. `self.workspaceId` seeds which workspace
 * this identity's session starts in; `POST .../auth/select-workspace`
 * mutates it exactly like the real endpoint mutates the session row.
 */
function makeCollaborationApi({ store, self }) {
  let currentWorkspaceId = self.workspaceId
  let nextId = 1
  const genId = (prefix) => `${prefix}-${nextId++}`

  if (!store.accounts.some((a) => a.account_id === self.accountId)) {
    store.accounts.push({ account_id: self.accountId, email: self.email, display_name: self.displayName })
  }

  function accountInfo(accountId) {
    return store.accounts.find((a) => a.account_id === accountId)
      ?? { account_id: accountId, email: 'unknown@example.test', display_name: 'Unknown' }
  }

  function activeMembership(workspaceId, accountId) {
    return store.memberships.find((m) => m.workspace_id === workspaceId && m.account_id === accountId && m.status === 'active')
  }

  function myMembership() {
    return activeMembership(currentWorkspaceId, self.accountId)
  }

  function recordActivity(eventType, aggregateType, aggregateId) {
    store.activity.unshift({
      id: genId('event'),
      event_type: eventType,
      aggregate_type: aggregateType,
      aggregate_id: aggregateId,
      actor_id: self.usersId,
      occurred_at: nowIso(),
      workspace_id: currentWorkspaceId,
    })
  }

  function dispatch(pathname, method, body, queryString) {
    if (pathname === '/api/v1/identity/me' && method === 'GET') {
      return { status: 200, body: { account_id: self.accountId, users_id: self.usersId, workspace_id: currentWorkspaceId, email: self.email, display_name: self.displayName } }
    }

    if (pathname === '/api/v1/identity/workspaces' && method === 'GET') {
      const mine = store.memberships.filter((m) => m.account_id === self.accountId && m.status === 'active')
      const items = mine.map((m) => {
        const workspace = store.workspaces.find((w) => w.id === m.workspace_id)
        return { ...workspace, role: m.role, current: m.workspace_id === currentWorkspaceId }
      })
      return { status: 200, body: { workspaces: items } }
    }

    if (pathname === '/api/v1/identity/auth/select-workspace' && method === 'POST') {
      const membership = activeMembership(body.workspace_id, self.accountId)
      if (!membership) return { status: 404, body: { error: { code: 'WORKSPACE_NOT_FOUND', message: 'Not found' } } }
      currentWorkspaceId = body.workspace_id
      return { status: 200, body: { status: 'authenticated', workspace_id: currentWorkspaceId } }
    }

    const membersMatch = pathname.match(/^\/api\/v1\/identity\/workspaces\/([^/]+)\/members$/)
    if (membersMatch && method === 'GET') {
      if (membersMatch[1] !== currentWorkspaceId) return { status: 404, body: { error: { code: 'WORKSPACE_NOT_FOUND', message: 'Not found' } } }
      const items = store.memberships
        .filter((m) => m.workspace_id === currentWorkspaceId && m.status === 'active')
        .map((m) => ({ user_id: m.users_id, account_id: m.account_id, ...accountInfo(m.account_id), role: m.role, status: m.status, created_at: m.created_at, removed_at: null }))
      return { status: 200, body: { members: items } }
    }

    const memberMatch = pathname.match(/^\/api\/v1\/identity\/workspaces\/([^/]+)\/members\/([^/]+)$/)
    if (memberMatch && (method === 'PATCH' || method === 'DELETE')) {
      if (memberMatch[1] !== currentWorkspaceId) return { status: 404, body: { error: { code: 'WORKSPACE_NOT_FOUND', message: 'Not found' } } }
      const membership = store.memberships.find((m) => m.workspace_id === currentWorkspaceId && m.users_id === memberMatch[2] && m.status === 'active')
      if (!membership) return { status: 404, body: { error: { code: 'MEMBER_NOT_FOUND', message: 'Not found' } } }
      if (method === 'PATCH') {
        membership.role = body.role
        return { status: 200, body: { user_id: membership.users_id, account_id: membership.account_id, ...accountInfo(membership.account_id), role: membership.role, status: membership.status, created_at: membership.created_at, removed_at: null } }
      }
      const now = nowIso()
      membership.status = 'removed'
      recordActivity('member.removed', 'membership', membership.users_id)
      return {
        status: 200,
        body: {
          user_id: membership.users_id,
          export: { account_id: membership.account_id, ...accountInfo(membership.account_id), role: membership.role, joined_at: membership.created_at, removed_at: now },
        },
      }
    }

    const invitationsMatch = pathname.match(/^\/api\/v1\/identity\/workspaces\/([^/]+)\/invitations$/)
    if (invitationsMatch && method === 'GET') {
      if (invitationsMatch[1] !== currentWorkspaceId) return { status: 404, body: { error: { code: 'WORKSPACE_NOT_FOUND', message: 'Not found' } } }
      const items = store.invitations.filter((i) => i.workspace_id === currentWorkspaceId)
      return { status: 200, body: { invitations: items.map(({ token: _token, ...rest }) => rest) } }
    }
    if (invitationsMatch && method === 'POST') {
      if (invitationsMatch[1] !== currentWorkspaceId) return { status: 404, body: { error: { code: 'WORKSPACE_NOT_FOUND', message: 'Not found' } } }
      const now = nowIso()
      const invitation = {
        id: genId('invitation'), workspace_id: currentWorkspaceId, email: body.email, role: body.role,
        token: genId('token'), expires_at: new Date(Date.now() + 7 * 24 * 60 * 60 * 1000).toISOString(),
        accepted_at: null, rejected_at: null, revoked_at: null, created_at: now,
      }
      store.invitations.push(invitation)
      return { status: 201, body: invitation }
    }

    const acceptMatch = pathname.match(/^\/api\/v1\/identity\/invitations\/([^/]+)\/accept$/)
    if (acceptMatch && method === 'POST') {
      const invitation = store.invitations.find((i) => i.id === acceptMatch[1])
      if (!invitation) return { status: 404, body: { error: { code: 'INVITATION_NOT_FOUND', message: 'Not found' } } }
      if (invitation.token !== body.token) return { status: 401, body: { error: { code: 'INVITATION_TOKEN_INVALID', message: 'Invalid token' } } }
      if (invitation.accepted_at || invitation.rejected_at || invitation.revoked_at) {
        return { status: 409, body: { error: { code: 'INVITATION_ALREADY_RESOLVED', message: 'Already resolved' } } }
      }
      if (invitation.email !== self.email) return { status: 403, body: { error: { code: 'INVITATION_EMAIL_MISMATCH', message: 'Email mismatch' } } }
      const now = nowIso()
      invitation.accepted_at = now
      store.memberships.push({
        workspace_id: invitation.workspace_id, account_id: self.accountId, users_id: genId('users'),
        role: invitation.role, status: 'active', created_at: now,
      })
      return { status: 200, body: { id: invitation.id, status: 'accepted', workspace_id: invitation.workspace_id, role: invitation.role } }
    }

    const revokeMatch = pathname.match(/^\/api\/v1\/identity\/invitations\/([^/]+)\/revoke$/)
    if (revokeMatch && method === 'POST') {
      const invitation = store.invitations.find((i) => i.id === revokeMatch[1])
      if (!invitation) return { status: 404, body: { error: { code: 'INVITATION_NOT_FOUND', message: 'Not found' } } }
      invitation.revoked_at = nowIso()
      return { status: 200, body: { id: invitation.id, status: 'revoked', workspace_id: invitation.workspace_id, role: invitation.role } }
    }

    if (pathname === '/api/v1/delegations' && method === 'GET') {
      const items = store.delegations.filter((d) => d.workspace_id === currentWorkspaceId)
      return { status: 200, body: { delegations: items } }
    }
    if (pathname === '/api/v1/delegations' && method === 'POST') {
      const now = nowIso()
      const delegation = {
        id: genId('delegation'), workspace_id: currentWorkspaceId,
        delegator_account_id: self.accountId, recipient_account_id: body.recipient_account_id,
        obligation_type: body.obligation_type, obligation_resource_id: body.obligation_resource_id,
        expected_outcome: body.expected_outcome, due_at: body.due_at, status: 'proposed', evidence: [],
        created_at: now, updated_at: now,
      }
      store.delegations.push(delegation)
      recordActivity('delegation.proposed', 'delegation', delegation.id)
      return { status: 201, body: delegation }
    }
    const delegationActionMatch = pathname.match(/^\/api\/v1\/delegations\/([^/]+)\/(accept|reject|revoke|complete)$/)
    if (delegationActionMatch && method === 'POST') {
      const delegation = store.delegations.find((d) => d.id === delegationActionMatch[1])
      if (!delegation) return { status: 404, body: { error: { code: 'DELEGATION_NOT_FOUND', message: 'Not found' } } }
      const nextStatus = { accept: 'accepted', reject: 'rejected', revoke: 'revoked', complete: 'completed' }[delegationActionMatch[2]]
      delegation.status = nextStatus
      delegation.updated_at = nowIso()
      recordActivity(`delegation.${delegationActionMatch[2]}ed`, 'delegation', delegation.id)
      return { status: 200, body: delegation }
    }

    if (pathname === '/api/v1/sharing/grants' && method === 'GET') {
      const items = store.grants.filter((g) => g.workspace_id === currentWorkspaceId)
      return { status: 200, body: { grants: items } }
    }
    if (pathname === '/api/v1/sharing/grants' && method === 'POST') {
      const now = nowIso()
      const grant = {
        id: genId('grant'), workspace_id: currentWorkspaceId, resource_type: body.resource_type, resource_id: body.resource_id,
        grantee_account_id: body.grantee_account_id, actions: body.actions, granted_by: self.accountId,
        expires_at: body.expires_at ?? null, revoked_at: null, created_at: now,
      }
      store.grants.push(grant)
      recordActivity('grant.created', grant.resource_type, grant.resource_id)
      return { status: 201, body: grant }
    }
    if (pathname === '/api/v1/sharing/grants/preview' && method === 'POST') {
      return {
        status: 200,
        body: {
          resource_type: body.resource_type, resource_id: body.resource_id, grantee_account_id: body.grantee_account_id,
          current_visibility: 'owner', proposed_visibility: 'restricted', requires_narrow_visibility_confirmation: false,
          members_losing_default_access: [], grantee_already_has_access: false, grantee_gains_actions: body.actions ?? [],
        },
      }
    }
    const revokeGrantMatch = pathname.match(/^\/api\/v1\/sharing\/grants\/([^/]+)$/)
    if (revokeGrantMatch && method === 'DELETE') {
      const grant = store.grants.find((g) => g.id === revokeGrantMatch[1])
      if (!grant) return { status: 404, body: { error: { code: 'RESOURCE_NOT_FOUND', message: 'Not found' } } }
      grant.revoked_at = nowIso()
      recordActivity('grant.revoked', grant.resource_type, grant.resource_id)
      return { status: 200, body: grant }
    }

    if (pathname === '/api/v1/shared/activity' && method === 'GET') {
      const params = new URLSearchParams(queryString)
      const cursor = params.get('cursor')
      const pageSize = 20
      const items = store.activity.filter((a) => a.workspace_id === currentWorkspaceId)
      const start = cursor ? Number(cursor) : 0
      const page = items.slice(start, start + pageSize)
      const nextIndex = start + pageSize
      return { status: 200, body: { items: page, next_cursor: nextIndex < items.length ? String(nextIndex) : null } }
    }

    if (pathname === '/api/v1/ownership/transfers' && method === 'POST') {
      const now = nowIso()
      const transfer = {
        id: genId('transfer'), resource_type: body.resource_type, resource_id: body.resource_id,
        from_account_id: self.accountId, to_account_id: body.to_account_id, status: 'completed',
        initiated_by: self.accountId, created_at: now, completed_at: now,
      }
      store.ownershipTransfers.push(transfer)
      return { status: 201, body: transfer }
    }
    if (pathname === '/api/v1/ownership/transfers' && method === 'GET') {
      return { status: 200, body: { transfers: store.ownershipTransfers } }
    }

    return null
  }

  return { store, self, myMembership, dispatch }
}

/**
 * Fake of Phase 7's `/api/v1/personal/*` surface (`ecc.domains.personal.
 * {domains,habits,grants,export_deletion,ai_insights}`) -- domains, records,
 * insights, cross-domain grants and export/delete, mirroring `make
 * AutomationApi`/`makeEngineeringApi`'s identical in-memory-collection
 * shape. `overrides.domains`/`records`/`insights`/`grants` seed initial
 * rows.
 *
 * `POST .../insights/generate`'s grant-gating is computed genuinely from
 * this fixture's own live `grants` array (an active, non-revoked, non-
 * expired grant for every requested `source_domain_key`), mirroring the
 * real `personal.get_insight_sources` tool's own fail-closed check --
 * `error_code: 'not_found'` is the real backend's own string for this case
 * (`runtime.py`: `"not_found" if prepared.reason == "not_found" else
 * ...`), not an invented placeholder. `overrides.generateResponse` scripts
 * what a mocked "model" would produce ONLY for the case every requested
 * domain genuinely is granted -- there is no fixture analogue for the real
 * endpoint's actual model call, so a scenario exercising the model-response
 * path (e.g. a missing `professional_referral_note`) still needs to supply
 * one explicitly.
 */
function makePersonalApi(overrides = {}) {
  let nextId = 1
  const domains = [...(overrides.domains ?? [])]
  const records = [...(overrides.records ?? [])]
  const insights = [...(overrides.insights ?? [])]
  const grants = [...(overrides.grants ?? [])]
  const generateResponse = overrides.generateResponse
    ?? { available: false, insight: null, error_code: 'grounding_failed' }

  function hasActiveGrantFor(domainKey) {
    const now = Date.now()
    return grants.some((g) => (
      g.source_domain_key === domainKey
      && g.revoked_at == null
      && (g.expires_at == null || new Date(g.expires_at).getTime() > now)
    ))
  }

  function findDomain(domainKey) {
    return domains.find((d) => d.domain_key === domainKey)
  }

  function setEnabled(domainKey, enabled) {
    const now = nowIso()
    let domain = findDomain(domainKey)
    if (!domain) {
      domain = {
        id: `personal-domain-${nextId++}`,
        domain_key: domainKey,
        classification: PERSONAL_CLASSIFICATION_BY_DOMAIN[domainKey],
        enabled,
        enabled_at: enabled ? now : null,
        version: 1,
        created_at: now,
        updated_at: now,
      }
      domains.push(domain)
    } else {
      domain.enabled = enabled
      if (enabled) domain.enabled_at = now
      domain.updated_at = now
      domain.version += 1
    }
    return domain
  }

  function dispatch(pathname, method, body, queryString) {
    if (!pathname.startsWith('/api/v1/personal')) return null
    const params = new URLSearchParams(queryString)

    if (pathname === '/api/v1/personal/domains' && method === 'GET') {
      return { status: 200, body: { domains } }
    }
    const enableMatch = pathname.match(/^\/api\/v1\/personal\/domains\/([^/]+)\/enable$/)
    if (enableMatch && method === 'POST') {
      return { status: 200, body: setEnabled(enableMatch[1], true) }
    }
    const disableMatch = pathname.match(/^\/api\/v1\/personal\/domains\/([^/]+)\/disable$/)
    if (disableMatch && method === 'POST') {
      return { status: 200, body: setEnabled(disableMatch[1], false) }
    }

    if (pathname === '/api/v1/personal/records' && method === 'GET') {
      const domainKey = params.get('domain_key')
      const items = domainKey ? records.filter((r) => r.domain_key === domainKey) : records
      return { status: 200, body: { records: items } }
    }
    if (pathname === '/api/v1/personal/records' && method === 'POST') {
      const classification = PERSONAL_CLASSIFICATION_BY_DOMAIN[body.domain_key]
      if (classification === 'high_stakes' && !body.retention_acknowledged) {
        return { status: 422, body: { error: { code: 'RETENTION_ACKNOWLEDGEMENT_REQUIRED', message: 'Retention Acknowledgement Required' } } }
      }
      const now = nowIso()
      const record = {
        id: `personal-record-${nextId++}`,
        domain_key: body.domain_key,
        record_type: body.record_type,
        payload: body.payload ?? {},
        domain_source_id: null,
        effective_at: now,
        retention_acknowledged_at: body.retention_acknowledged ? now : null,
        version: 1,
        created_at: now,
        updated_at: now,
      }
      records.push(record)
      return { status: 201, body: record }
    }

    if (pathname === '/api/v1/personal/insights' && method === 'GET') {
      // Mirrors `habits.py:list_insights_endpoint`'s own `AND dismissed_at
      // IS NULL` filter -- the real endpoint never returns a dismissed
      // insight, so neither does this fixture.
      return { status: 200, body: { insights: insights.filter((i) => i.dismissed_at == null) } }
    }
    const dismissMatch = pathname.match(/^\/api\/v1\/personal\/insights\/([^/]+)\/dismiss$/)
    if (dismissMatch && method === 'POST') {
      const insight = insights.find((i) => i.id === dismissMatch[1])
      if (!insight) return { status: 404, body: { error: { code: 'INSIGHT_NOT_FOUND', message: 'Not found' } } }
      insight.dismissed_at = nowIso()
      return { status: 200, body: insight }
    }
    const feedbackMatch = pathname.match(/^\/api\/v1\/personal\/insights\/([^/]+)\/feedback$/)
    if (feedbackMatch && method === 'POST') {
      return {
        status: 201,
        body: { id: `personal-feedback-${nextId++}`, insight_id: feedbackMatch[1], useful: body.useful, comment: body.comment ?? null, created_at: nowIso() },
      }
    }
    if (pathname === '/api/v1/personal/insights/generate' && method === 'POST') {
      const requestedDomainKeys = body.source_domain_keys ?? []
      const everyDomainGranted = requestedDomainKeys.every((k) => hasActiveGrantFor(k))
      if (!everyDomainGranted) {
        return { status: 200, body: { available: false, insight: null, error_code: 'not_found' } }
      }
      if (generateResponse.available && generateResponse.insight) insights.push(generateResponse.insight)
      return { status: 200, body: generateResponse }
    }

    if (pathname === '/api/v1/personal/grants' && method === 'GET') {
      return { status: 200, body: { grants } }
    }
    if (pathname === '/api/v1/personal/grants' && method === 'POST') {
      const now = nowIso()
      const grant = {
        id: `personal-grant-${nextId++}`,
        source_domain_key: body.source_domain_key,
        purpose: body.purpose,
        granted_categories: body.granted_categories,
        granted_at: now,
        expires_at: body.expires_at ?? null,
        revoked_at: null,
        created_at: now,
      }
      grants.push(grant)
      return { status: 201, body: grant }
    }
    const revokeGrantMatch = pathname.match(/^\/api\/v1\/personal\/grants\/([^/]+)\/revoke$/)
    if (revokeGrantMatch && method === 'POST') {
      const grant = grants.find((g) => g.id === revokeGrantMatch[1])
      if (!grant) return { status: 404, body: { error: { code: 'GRANT_NOT_FOUND', message: 'Not found' } } }
      if (!grant.revoked_at) grant.revoked_at = nowIso()
      return { status: 200, body: grant }
    }

    const exportMatch = pathname.match(/^\/api\/v1\/personal\/domains\/([^/]+)\/export$/)
    if (exportMatch && method === 'POST') {
      const domainKey = exportMatch[1]
      return {
        status: 200,
        body: {
          domain_key: domainKey,
          exported_at: nowIso(),
          goals: [],
          routines: [],
          check_ins: [],
          domain_records: records.filter((r) => r.domain_key === domainKey),
        },
      }
    }
    const deleteMatch = pathname.match(/^\/api\/v1\/personal\/domains\/([^/]+)\/delete$/)
    if (deleteMatch && method === 'POST') {
      const domainKey = deleteMatch[1]
      for (let i = records.length - 1; i >= 0; i -= 1) {
        if (records[i].domain_key === domainKey) records.splice(i, 1)
      }
      const now = nowIso()
      return {
        status: 200,
        body: { id: `personal-deletion-${nextId++}`, domain_key: domainKey, status: 'completed', requested_at: now, completed_at: now },
      }
    }

    return null
  }

  return { domains, records, insights, grants, dispatch }
}

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
// `WorkspaceSwitcher.tsx` is mounted globally in `App.tsx`, so every
// scenario now fetches `GET /api/v1/identity/workspaces` on first render --
// not just the ones that care about collaboration. Seeding exactly one
// workspace/membership by default (below) keeps `workspaces.length <= 1`,
// which is `WorkspaceSwitcher`'s own render-nothing case, so the other two
// dozen scenarios in this directory see no new DOM and need no changes.
// `overrides.collaborationStore`/`collaborationSelf` let a scenario replace
// both wholesale -- the multi-identity scenario is the one that does.
const DEFAULT_COLLABORATION_WORKSPACE_ID = 'workspace-default'

export async function createFixtureApi(page, overrides = {}) {
  await page.context().addCookies([{ name: 'ecc_csrf', value: 'csrf-token', url: 'http://127.0.0.1:4173' }])

  const requests = []

  const collaborationStore = overrides.collaborationStore ?? createCollaborationStore({
    workspaces: [{ id: DEFAULT_COLLABORATION_WORKSPACE_ID, name: 'Acme Co', timezone: 'UTC', created_at: '2026-01-01T00:00:00Z' }],
    memberships: [{
      workspace_id: DEFAULT_COLLABORATION_WORKSPACE_ID, account_id: 'account-default', users_id: 'users-default',
      role: 'owner', status: 'active', created_at: '2026-01-01T00:00:00Z',
    }],
  })
  const collaborationSelf = overrides.collaborationSelf ?? {
    accountId: 'account-default', usersId: 'users-default', email: 'me@example.test', displayName: 'Me',
    workspaceId: DEFAULT_COLLABORATION_WORKSPACE_ID,
  }
  const collaboration = makeCollaborationApi({ store: collaborationStore, self: collaborationSelf })

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
    aliases: overrides.knowledgeAliases,
    claims: overrides.knowledgeClaims,
    relationships: overrides.knowledgeRelationships,
    timelineEntries: overrides.knowledgeTimelineEntries,
    resolutionCandidates: overrides.resolutionCandidates,
  })
  const attention = makeAttentionApi({ ...overrides.attention, risksCollection: risks })
  const aiRuntime = makeAiRuntimeApi(overrides.aiRuntime)
  const automation = makeAutomationApi(overrides.automation ?? {})
  const engineering = makeEngineeringApi(overrides.engineering ?? {})
  const personal = makePersonalApi(overrides.personal ?? {})

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
    // Checked before the generic `resources` loop: /api/v1/risks/review-queue
    // is a static path that the generic risks resourceHandler below would
    // otherwise swallow as GET {base}/:id (id="review-queue"), mirroring the
    // real backend's router-registration-order fix for the same collision.
    if (pathname.startsWith('/api/v1/attention') || pathname.startsWith('/api/v1/waiting')
      || pathname === '/api/v1/risks/review-queue' || /^\/api\/v1\/risks\/[^/]+\/review$/.test(pathname)
      || pathname.startsWith('/api/v1/plans') || /^\/api\/v1\/meetings\/[^/]+\/(prep|participants)/.test(pathname)) {
      const result = attention.dispatch(pathname, method, body, queryString)
      if (result) return result
    }
    if (pathname.startsWith('/api/v1/ai/runs')) {
      const result = aiRuntime.dispatch(pathname, method, body)
      if (result) return result
    }
    if (pathname.startsWith('/api/v1/automations')) {
      const result = automation.dispatch(pathname, method, body, queryString)
      if (result) return result
    }
    if (pathname.startsWith('/api/v1/engineering')) {
      const result = engineering.dispatch(pathname, method, body, queryString)
      if (result) return result
    }
    if (pathname.startsWith('/api/v1/personal')) {
      const result = personal.dispatch(pathname, method, body, queryString)
      if (result) return result
    }
    if (pathname.startsWith('/api/v1/identity') || pathname.startsWith('/api/v1/sharing')
      || pathname.startsWith('/api/v1/delegations') || pathname.startsWith('/api/v1/shared/activity')
      || pathname.startsWith('/api/v1/ownership')) {
      const result = collaboration.dispatch(pathname, method, body, queryString)
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
    collaboration,
    attention,
    aiRuntime,
    automation,
    engineering,
    personal,
    dashboard,
    brief,
    evidence,
    setOffline: (value) => { offline = value },
  }
}
