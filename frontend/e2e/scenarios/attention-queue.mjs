import assert from 'node:assert/strict'

import { createFixtureApi } from '../fixtures.mjs'
import { assertNoSeriousAccessibilityViolations } from '../accessibility.mjs'

const item = {
  id: 'attn-1',
  entity_type: 'task',
  entity_id: 'task-1',
  source_entity_version: 1,
  score: 60,
  confidence: 0.9,
  factors: [{ code: 'overdue', label: 'Overdue by 2 days', points: 35 }],
  explanation: 'Finish the board memo',
  generated_at: '2026-07-20T00:00:00Z',
  expires_at: '2026-07-21T00:00:00Z',
  pinned: false,
  dismissed_at: null,
  dismissed_entity_version: null,
  deferred_until: null,
  policy_version: 1,
  override_reason: null,
}

const waitingLink = {
  id: 'wl-1',
  subject_type: 'task',
  subject_id: 'task-2',
  counterparty_entity_id: 'entity-1',
  direction: 'waiting_on_them',
  status: 'open',
  note: 'Waiting on vendor signature',
  since_at: '2026-07-01T00:00:00Z',
  expected_at: '2026-07-15T00:00:00Z',
  superseded_by: null,
  created_at: '2026-07-01T00:00:00Z',
  updated_at: '2026-07-01T00:00:00Z',
  version: 1,
}

/**
 * Attention queue and waiting journey: items group by needs-action/waiting/
 * risks/meetings/safely-deferred (UX-STATES.md), dismiss is reversible via
 * restore, and a waiting item can be recorded as fulfilled.
 */
export async function run({ page, baseURL }) {
  const fixtures = await createFixtureApi(page, { attention: { attentionItems: [item], waitingLinks: [waitingLink] } })

  await page.goto(baseURL)
  await page.getByRole('tab', { name: 'Attention' }).click()
  const section = page.locator('section[aria-labelledby="attention-title"]')
  await section.getByRole('heading', { name: 'Attention queue', level: 1 }).waitFor()
  await section.getByText('Finish the board memo').waitFor()

  await assertNoSeriousAccessibilityViolations(page, { include: 'section[aria-labelledby="attention-title"]' })

  // The item is grouped under "Needs action" (a plain task, not deferred,
  // not a waiting/risk/meeting entity_type).
  const needsAction = section.locator('section[aria-labelledby="attention-group-needs_action"]')
  await needsAction.getByText('Finish the board memo').waitFor()

  // Score is present but secondary -- the plain-language reason is the
  // primary text (UX-STATES.md: "Scores are secondary to plain-language
  // rationale").
  await section.getByLabel('Score (secondary to the reason above)').waitFor()

  // Dismiss, then restore -- both reversible.
  await section.getByRole('button', { name: 'Dismiss Finish the board memo' }).click()
  const restoreButton = section.getByRole('button', { name: 'Restore Finish the board memo' })
  await restoreButton.waitFor()
  await restoreButton.click()
  await needsAction.getByText('Finish the board memo').waitFor()

  const waitingSection = page.locator('section[aria-labelledby="waiting-title"]')
  await waitingSection.getByText('Waiting on vendor signature').waitFor()
  await waitingSection.getByRole('button', { name: 'Fulfil waiting item wl-1' }).click()
  await waitingSection.getByText('Nothing is currently waiting.').waitFor()

  const fulfilRequest = fixtures.requests.find((request) => request.method === 'POST' && request.path === '/api/v1/waiting/wl-1/fulfil')
  assert.ok(fulfilRequest, 'expected a fulfil request for the waiting link')
}
