import assert from 'node:assert/strict'

import { createFixtureApi } from '../fixtures.mjs'
import { assertNoSeriousAccessibilityViolations } from '../accessibility.mjs'

const seedRisk = {
  id: 'risk-decision-1',
  description: 'Vendor renewal may lapse',
  probability: 4,
  impact: 5,
  status: 'monitoring',
  owner_id: 'owner-fixture',
  mitigation: 'Confirm renewal terms',
  trigger: 'No signed contract',
  review_at: '2026-08-01T00:00:00Z',
  project_id: null,
  pinned: false,
  priority_impact: 20,
  score: 25,
  factors: [{ code: 'risk_impact', label: 'Risk impact 20', points: 25, source_field: 'probability,impact' }],
  explanation: 'Risk impact 20',
  created_at: '2026-07-01T00:00:00Z',
  updated_at: '2026-07-10T00:00:00Z',
  version: 1,
}

const seedRecommendation = {
  id: 'rec-decide-1',
  recommendation_type: 'close_risk',
  target_type: 'risk',
  target_id: 'risk-decision-1',
  proposed_action: { operation: 'update_status', status: 'closed' },
  expected_version: 1,
  rationale: 'The vendor contract renewed on schedule.',
  confidence: 0.87,
  status: 'pending_confirmation',
  evidence_ids: ['evidence-memo', 'evidence-missing-doc'],
  execution_result: null,
  source: 'rule',
  pinned: false,
  version: 1,
}

/**
 * Recommendation decision controls on a risk-targeted, pending-confirmation
 * recommendation: the evidence preview surfaces both `available` and
 * `missing` states plus the risk's computed factors, and Pin/Unpin and
 * Defer 24h leave the recommendation live (not terminal) for further review.
 * Reject and terminal-state hiding are covered separately in
 * recommendation-terminals.mjs.
 */
export async function run({ page, baseURL }) {
  const fixtures = await createFixtureApi(page, {
    risks: [seedRisk],
    recommendations: [seedRecommendation],
    evidence: { 'evidence-memo': { source_type: 'document', label: 'Renewal memo', captured_at: '2026-07-01T00:00:00Z' } },
  })

  await page.goto(baseURL)
  await page.getByRole('tab', { name: 'Recommendations' }).click()
  const panel = page.locator('section[aria-labelledby="recommendations-title"]')
  const item = panel.locator('li', { hasText: 'close risk' })
  await item.getByText('pending confirmation').waitFor()

  await assertNoSeriousAccessibilityViolations(page, { include: 'section[aria-labelledby="recommendations-title"]' })

  // Evidence-state assertion: one reference resolves (available, with its
  // label), the other is unknown to the fixture's evidence map (missing).
  const evidence = item.getByLabel('Evidence for close risk')
  await evidence.getByText('available').waitFor()
  await evidence.getByText('Renewal memo').waitFor()
  await evidence.getByText('missing').waitFor()

  // Risk-target recommendations additionally preview the target's computed
  // factors (fetched from GET /api/v1/risks/:id, not the recommendation body).
  const factors = item.getByLabel('Risk factors for close risk')
  await factors.getByText('Risk impact 20').waitFor()
  await item.getByText('Score 25 · Risk impact 20').waitFor()

  // Pin, then unpin. The list re-renders in two steps (mutation result,
  // then the invalidated refetch), so poll the "is-pinned" class rather than
  // reading it the instant the button label flips.
  await item.getByRole('button', { name: 'Pin' }).click()
  await item.getByRole('button', { name: 'Unpin' }).waitFor()
  await page.waitForFunction(
    (li) => li.classList.contains('is-pinned'),
    await item.elementHandle(),
  )
  await item.getByRole('button', { name: 'Unpin' }).click()
  await item.getByRole('button', { name: 'Pin' }).waitFor()
  await page.waitForFunction(
    (li) => !li.classList.contains('is-pinned'),
    await item.elementHandle(),
  )

  // Defer 24h leaves the recommendation pending, decision actions intact.
  await item.getByRole('button', { name: 'Defer 24h' }).click()
  await item.getByText('pending confirmation').waitFor()
  const deferRequest = fixtures.requests.find((request) => request.path === '/api/v1/recommendations/rec-decide-1/defer')
  assert.ok(deferRequest)
  const deferUntil = new Date(deferRequest.body.defer_until).getTime()
  const expected = Date.now() + 24 * 60 * 60 * 1000
  assert.ok(Math.abs(deferUntil - expected) < 5 * 60 * 1000, 'defer_until should be ~24h from now')
  await item.getByRole('button', { name: 'Confirm and execute' }).waitFor()
  await item.getByRole('button', { name: 'Reject' }).waitFor()
}
