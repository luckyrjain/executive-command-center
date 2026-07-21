import assert from 'node:assert/strict'

import { createFixtureApi } from '../fixtures.mjs'
import { assertNoSeriousAccessibilityViolations } from '../accessibility.mjs'

const seedRecommendation = {
  id: 'rec-execute-1',
  recommendation_type: 'complete_task',
  target_type: 'task',
  target_id: 'task-1',
  proposed_action: { operation: 'complete_task', completed: true },
  expected_version: 2,
  rationale: 'All acceptance checklist items are marked done.',
  confidence: 0.91,
  status: 'proposed',
  evidence_ids: ['evidence-checklist'],
  execution_result: null,
  source: 'rule',
  pinned: false,
  version: 1,
}

/**
 * Recommendation lifecycle happy path: proposed -> publish -> pending
 * confirmation -> confirm -> executed, checking the evidence preview is
 * visible before execution and replaced by "Execution recorded." once the
 * recommendation reaches its terminal state.
 */
export async function run({ page, baseURL }) {
  const fixtures = await createFixtureApi(page, {
    recommendations: [seedRecommendation],
    evidence: { 'evidence-checklist': { source_type: 'document', label: 'Acceptance checklist', captured_at: '2026-07-14T00:00:00Z' } },
  })

  await page.goto(baseURL)
  await page.getByRole('tab', { name: 'Recommendations' }).click()
  const panel = page.locator('section[aria-labelledby="recommendations-title"]')
  await panel.getByRole('heading', { name: 'complete task' }).waitFor()

  await assertNoSeriousAccessibilityViolations(page, { include: 'section[aria-labelledby="recommendations-title"]' })

  // Proposed: only "Publish for confirmation" is offered, and the evidence
  // preview is already visible pre-decision.
  const item = panel.locator('li', { hasText: 'complete task' })
  await item.getByText('proposed').waitFor()
  const evidence = item.getByLabel('Evidence for complete task')
  await evidence.getByText('available').waitFor()
  await evidence.getByText('Acceptance checklist').waitFor()
  assert.equal(await item.getByRole('button', { name: 'Confirm and execute' }).count(), 0)

  await item.getByRole('button', { name: 'Publish for confirmation' }).click()
  await item.getByText('pending confirmation').waitFor()
  const publishRequest = fixtures.requests.find((request) => request.path === '/api/v1/recommendations/rec-execute-1/publish')
  assert.ok(publishRequest)
  assert.equal(publishRequest.body.expected_version, 1)

  // Pending confirmation: evidence preview still shown, confirm executes.
  await evidence.getByText('available').waitFor()
  await item.getByRole('button', { name: 'Confirm and execute' }).click()
  await item.getByText('executed', { exact: true }).waitFor()
  await item.getByText('Execution recorded.').waitFor()
  assert.equal(await item.getByLabel('Evidence for complete task').count(), 0, 'evidence preview should not render once terminal')
  assert.equal(await item.getByRole('button', { name: 'Confirm and execute' }).count(), 0)

  const confirmRequest = fixtures.requests.find((request) => request.path === '/api/v1/recommendations/rec-execute-1/confirm')
  assert.ok(confirmRequest)
  assert.equal(confirmRequest.body.target_expected_version, 2)
  assert.equal(fixtures.collections.recommendations.find('rec-execute-1').execution_result.target_type, 'task')
}
