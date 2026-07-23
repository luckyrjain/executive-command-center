import assert from 'node:assert/strict'

import { createFixtureApi } from '../fixtures.mjs'
import { assertNoSeriousAccessibilityViolations } from '../accessibility.mjs'

const targetEntity = {
  id: 'entity-target',
  entity_id: null,
  kind: 'person',
  canonical_name: 'Ada Lovelace',
  summary: null,
  status: 'active',
  confidence: 1,
  version: 1,
  created_at: '2026-07-01T00:00:00Z',
  updated_at: '2026-07-01T00:00:00Z',
}

const sourceEntity = {
  id: 'entity-source',
  entity_id: null,
  kind: 'person',
  canonical_name: 'Ada Lovelase',
  summary: null,
  status: 'active',
  confidence: 1,
  version: 1,
  created_at: '2026-07-01T00:00:00Z',
  updated_at: '2026-07-01T00:00:00Z',
}

const seedCandidate = {
  id: 'candidate-ada',
  left_entity_id: targetEntity.id,
  right_entity_id: sourceEntity.id,
  score: 0.86,
  factors: { name_similarity: 0.9, alias_overlap: 0, neighbor_overlap: 0, temporal_compatibility: 1 },
  resolver_version: 'phase2-resolution-v1',
  status: 'open',
  created_at: '2026-07-01T00:00:00Z',
  resolved_at: null,
  resolved_by: null,
  reason: null,
  deferred_until: null,
}

/**
 * Knowledge resolution journey: review an open candidate, confirm it,
 * merge the confirmed pair, verify the source entity was redirected (not
 * deleted), then reverse the merge safely and verify it was restored.
 */
export async function run({ page, baseURL }) {
  const fixtures = await createFixtureApi(page, {
    knowledgeEntities: [targetEntity, sourceEntity],
    resolutionCandidates: [seedCandidate],
  })

  await page.goto(baseURL)
  await page.getByRole('tab', { name: 'Knowledge' }).click()
  const inbox = page.locator('section[aria-labelledby="resolution-inbox-title"]')
  await inbox.getByText(targetEntity.id, { exact: false }).waitFor()
  await inbox.getByText(/name_similarity/).waitFor()

  await assertNoSeriousAccessibilityViolations(page, { include: 'section[aria-labelledby="resolution-inbox-title"]' })

  // Confirm the match.
  await inbox.getByLabel(`Reason for ${seedCandidate.id}`).fill('Verified same person from meeting notes')
  await inbox.getByRole('button', { name: 'Confirm match' }).click()
  await inbox.getByText('No resolution candidates awaiting review.').waitFor()
  const confirmRequest = fixtures.requests.find(
    (request) => request.method === 'POST' && request.path === `/api/v1/knowledge/resolution/candidates/${seedCandidate.id}/confirm`,
  )
  assert.ok(confirmRequest, 'expected a confirm request')

  // The confirmed candidate now shows up in Merge review.
  const mergeReview = page.locator('section[aria-labelledby="merge-review-title"]')
  await mergeReview.getByText('Ada Lovelace', { exact: true }).waitFor()
  await mergeReview.getByText('Ada Lovelase', { exact: true }).waitFor()
  await assertNoSeriousAccessibilityViolations(page, { include: 'section[aria-labelledby="merge-review-title"]' })

  await mergeReview.getByLabel(`Merge reason for ${seedCandidate.id}`).fill('Confirmed duplicate identity')
  await mergeReview.getByRole('button', { name: 'Merge into Ada Lovelace' }).click()
  await mergeReview.getByText(`Merged ${sourceEntity.id} into ${targetEntity.id}`).waitFor()

  // Source entity was redirected, not deleted.
  assert.equal(fixtures.knowledge.entities.find(sourceEntity.id).status, 'redirected')
  assert.equal(fixtures.knowledge.entities.find(targetEntity.id).status, 'active')

  // Reverse the merge.
  const operation = fixtures.knowledge.entityOperations.find((entry) => entry.operation_type === 'merge')
  assert.ok(operation, 'expected a merge operation to have been recorded')
  await mergeReview.getByLabel(`Reversal reason for ${operation.id}`).fill('Merged in error')
  await mergeReview.getByRole('button', { name: 'Reverse merge' }).click()
  await mergeReview.getByText(`Merge ${sourceEntity.id} → ${targetEntity.id} (reversed)`).waitFor()

  assert.equal(fixtures.knowledge.entities.find(sourceEntity.id).status, 'active')
}
