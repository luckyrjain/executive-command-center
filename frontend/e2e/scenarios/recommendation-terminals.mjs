import assert from 'node:assert/strict'

import { createFixtureApi } from '../fixtures.mjs'
import { assertNoSeriousAccessibilityViolations } from '../accessibility.mjs'

const executed = {
  id: 'rec-terminal-executed',
  recommendation_type: 'archive_note',
  target_type: 'note',
  target_id: 'note-9',
  proposed_action: { operation: 'archive' },
  expected_version: 1,
  rationale: 'The note has been superseded by the final memo.',
  confidence: 0.7,
  status: 'executed',
  evidence_ids: [],
  execution_result: { applied: true },
  source: 'rule',
  pinned: false,
  version: 3,
}

const failed = {
  id: 'rec-terminal-failed',
  recommendation_type: 'fulfil_commitment',
  target_type: 'commitment',
  target_id: 'commitment-9',
  proposed_action: { operation: 'fulfil' },
  expected_version: 1,
  rationale: 'The counterparty confirmed delivery.',
  confidence: 0.6,
  status: 'failed',
  evidence_ids: [],
  execution_result: { applied: false, error: 'TARGET_ALREADY_TERMINAL' },
  source: 'rule',
  pinned: false,
  version: 2,
}

// The real backend's list query only ever asks for
// status in (proposed, pending_confirmation, executed, failed); a rejected
// item is included here purely to prove the fixture's status filter (see
// fixtures.mjs resourceHandler filterList) actually excludes it, the same
// way the real API would.
const rejectedButFilteredOut = {
  id: 'rec-terminal-rejected',
  recommendation_type: 'cancel_task',
  target_type: 'task',
  target_id: 'task-9',
  proposed_action: { operation: 'cancel' },
  expected_version: 1,
  rationale: 'No longer relevant.',
  confidence: 0.5,
  status: 'rejected',
  evidence_ids: [],
  execution_result: null,
  source: 'rule',
  pinned: false,
  version: 2,
}

/**
 * Terminal recommendation states (executed, failed) render their outcome
 * but hide every lifecycle action and the evidence/factors preview — there
 * is nothing left to decide. A rejected recommendation, which the real
 * backend's status filter would never return here, proves the fixture
 * applies that same filter rather than showing everything unconditionally.
 */
export async function run({ page, baseURL }) {
  await createFixtureApi(page, { recommendations: [executed, failed, rejectedButFilteredOut] })

  await page.goto(baseURL)
  await page.getByRole('tab', { name: 'Recommendations' }).click()
  const panel = page.locator('section[aria-labelledby="recommendations-title"]')
  await panel.getByRole('heading', { name: 'archive note' }).waitFor()

  await assertNoSeriousAccessibilityViolations(page, { include: 'section[aria-labelledby="recommendations-title"]' })

  assert.equal(await panel.getByRole('heading', { name: 'cancel task' }).count(), 0, 'a rejected recommendation must not appear in this list')

  for (const [heading, statusText] of [['archive note', 'executed'], ['fulfil commitment', 'failed']]) {
    const item = panel.locator('li', { hasText: heading })
    await item.getByText(statusText, { exact: true }).waitFor()
    for (const label of ['Publish for confirmation', 'Confirm and execute', 'Reject', 'Defer 24h', 'Pin', 'Unpin']) {
      assert.equal(await item.getByRole('button', { name: label }).count(), 0, `${heading} (${statusText}) must not expose "${label}"`)
    }
    assert.equal(await item.getByLabel(`Evidence for ${heading}`).count(), 0, `${heading} must not render an evidence preview once terminal`)
  }

  await panel.locator('li', { hasText: 'archive note' }).getByText('Execution recorded.').waitFor()
  await panel.locator('li', { hasText: 'fulfil commitment' }).getByText('Execution recorded.').waitFor()
}
