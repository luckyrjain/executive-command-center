import assert from 'node:assert/strict'

import { createFixtureApi } from '../fixtures.mjs'
import { assertNoSeriousAccessibilityViolations } from '../accessibility.mjs'

const seedEntity = {
  id: 'entity-ada',
  entity_id: null,
  kind: 'person',
  canonical_name: 'Ada Lovelace',
  summary: 'Mathematician and writer',
  status: 'active',
  confidence: 1,
  version: 1,
  created_at: '2026-07-01T00:00:00Z',
  updated_at: '2026-07-01T00:00:00Z',
}

const seedProject = {
  id: 'entity-engine',
  entity_id: null,
  kind: 'project',
  canonical_name: 'Analytical Engine',
  summary: null,
  status: 'active',
  confidence: 1,
  version: 1,
  created_at: '2026-07-01T00:00:00Z',
  updated_at: '2026-07-01T00:00:00Z',
}

/**
 * Knowledge entity journey: create an entity, retrieve it by lexical
 * query and inspect the match explanation, open its detail view, and
 * record a relationship (visible immediately in the entity's timeline).
 */
export async function run({ page, baseURL }) {
  const fixtures = await createFixtureApi(page, { knowledgeEntities: [seedEntity, seedProject] })

  await page.goto(baseURL)
  await page.getByRole('tab', { name: 'Knowledge' }).click()
  const explorer = page.locator('section[aria-labelledby="knowledge-title"]')
  await explorer.getByRole('heading', { name: 'Knowledge', level: 1 }).waitFor()
  await explorer.getByText('Ada Lovelace').waitFor()

  await assertNoSeriousAccessibilityViolations(page, { include: 'section[aria-labelledby="knowledge-title"]' })

  // Create a new entity.
  await explorer.getByLabel('Entity kind').selectOption('decision')
  await explorer.getByLabel('Canonical name').fill('Approve quarterly budget')
  await explorer.getByLabel('Summary').fill('Board decision on Q3 spend')
  await explorer.getByRole('button', { name: 'Create entity' }).click()
  const entityList = explorer.locator('section[aria-labelledby="entity-list-heading"]')
  await entityList.getByText('Approve quarterly budget').waitFor()
  const createRequest = fixtures.requests.find(
    (request) => request.method === 'POST' && request.path === '/api/v1/knowledge/entities',
  )
  assert.ok(createRequest, 'expected a POST /api/v1/knowledge/entities request')
  assert.equal(createRequest.body.canonical_name, 'Approve quarterly budget')

  // Retrieve by lexical query and inspect the match explanation.
  await explorer.getByLabel('Search entities').fill('Ada Lovelace')
  await explorer.getByRole('button', { name: 'Search' }).click()
  const searchResults = explorer.locator('section[aria-labelledby="search-results-heading"]')
  const resultButton = searchResults.getByRole('button', { name: 'Ada Lovelace' })
  await resultButton.waitFor()
  await searchResults.getByText(/exact_name/).waitFor()

  // Open entity detail and add a relationship.
  await resultButton.click()
  const detail = page.locator('section[aria-labelledby="entity-detail-title"]')
  await detail.getByRole('heading', { name: 'Ada Lovelace', level: 2 }).waitFor()
  await detail.getByText('No relationships recorded for this entity.').waitFor()

  const evidenceId = 'evidence-quarterly-budget'
  await detail.getByLabel('Relationship type').selectOption('WORKS_ON')
  await detail.getByLabel('Related entity ID').fill(seedProject.id)
  await detail.getByLabel('Evidence ID').fill(evidenceId)
  await detail.getByRole('button', { name: 'Add relationship' }).click()
  const relationships = detail.locator(`section[aria-labelledby="relationships-heading-${seedEntity.id}"]`)
  await relationships.getByText(new RegExp(`WORKS_ON.*${seedProject.id}`)).waitFor()

  // The relationship also produced a timeline entry.
  const timeline = detail.locator(`section[aria-labelledby="timeline-heading-${seedEntity.id}"]`)
  await timeline.getByText(/WORKS_ON/).waitFor()
  const relationshipRequest = fixtures.requests.find(
    (request) => request.method === 'POST' && request.path === `/api/v1/knowledge/entities/${seedEntity.id}/relationships`,
  )
  assert.ok(relationshipRequest, 'expected a relationship create request')
  assert.equal(relationshipRequest.body.to_entity_id, seedProject.id)
  assert.equal(relationshipRequest.body.evidence_id, evidenceId)

  await detail.getByRole('button', { name: 'Close detail' }).click()
  await detail.waitFor({ state: 'detached' })
}
