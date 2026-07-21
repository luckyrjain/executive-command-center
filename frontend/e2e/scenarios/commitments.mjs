import assert from 'node:assert/strict'

import { createFixtureApi } from '../fixtures.mjs'
import { assertNoSeriousAccessibilityViolations } from '../accessibility.mjs'

const seedCommitment = {
  id: 'commitment-1',
  summary: 'Send board metrics',
  description: 'Quarterly metrics for the board deck',
  direction: 'made_by_me',
  counterparty_name: 'Board Secretary',
  status: 'detected',
  importance: 'high',
  due_date: '2026-07-25',
  due_at: null,
  version: 1,
}

/**
 * Commitment workspace journey: create, confirm a detected commitment, fulfil
 * it and watch lifecycle actions collapse to Archive-only, then edit a live
 * commitment and roundtrip archive/restore. Exercises the status/alert
 * live-region roles the workspace uses for loading and error feedback.
 */
export async function run({ page, baseURL }) {
  const fixtures = await createFixtureApi(page, { commitments: [seedCommitment] })

  await page.goto(baseURL)
  await page.getByRole('tab', { name: 'Work' }).click()
  const section = page.locator('section[aria-labelledby="commitments-title"]')
  await section.getByRole('heading', { name: 'Commitments', level: 1 }).waitFor()
  await section.getByText('Send board metrics').waitFor()

  await assertNoSeriousAccessibilityViolations(page, { include: 'section[aria-labelledby="commitments-title"]' })

  // Create a new commitment made to the operator.
  await section.getByLabel('Commitment summary').fill('Legal approval for vendor contract')
  await section.getByLabel('Direction').selectOption('made_to_me')
  await section.getByLabel('Counterparty name').fill('Legal team')
  await section.getByRole('button', { name: 'Create commitment' }).click()
  await section.getByText('Legal approval for vendor contract').waitFor()
  const createRequest = fixtures.requests.find((request) => request.method === 'POST' && request.path === '/api/v1/commitments')
  assert.ok(createRequest)
  assert.equal(createRequest.body.direction, 'made_to_me')
  assert.equal(createRequest.body.counterparty_name, 'Legal team')

  // Confirm the seeded (detected) commitment, then fulfil it.
  await section.getByRole('button', { name: 'Confirm Send board metrics' }).click()
  await section.getByText(/made by me · Board Secretary · confirmed/).waitFor()
  await section.getByRole('button', { name: 'Fulfil Send board metrics' }).click()
  await section.getByText(/made by me · Board Secretary · fulfilled/).waitFor()

  // Terminal status: only Archive remains.
  assert.equal(await section.getByRole('button', { name: 'Edit Send board metrics' }).count(), 0)
  assert.equal(await section.getByRole('button', { name: 'Confirm Send board metrics' }).count(), 0)
  assert.equal(await section.getByRole('button', { name: 'Cancel Send board metrics' }).count(), 0)
  await section.getByRole('button', { name: 'Archive Send board metrics' }).waitFor()

  // Edit the still-live commitment created above.
  await section.getByRole('button', { name: 'Edit Legal approval for vendor contract' }).click()
  const editHeading = section.getByRole('heading', { name: 'Edit commitment' })
  await editHeading.waitFor()
  await section.getByLabel('Edit commitment summary').fill('Legal approval for vendor contract (final)')
  await section.getByRole('button', { name: 'Save commitment' }).click()
  await section.getByText('Legal approval for vendor contract (final)').waitFor()

  // Cancel it, then archive/restore.
  await section.getByRole('button', { name: 'Cancel Legal approval for vendor contract (final)' }).click()
  await section.getByText(/made to me · Legal team · cancelled/).waitFor()
  await section.getByRole('button', { name: 'Archive Legal approval for vendor contract (final)' }).click()
  const restoreButton = section.getByRole('button', { name: 'Restore Legal approval for vendor contract (final)' })
  await restoreButton.waitFor()
  await restoreButton.click()
  await section.getByRole('button', { name: 'Archive Legal approval for vendor contract (final)' }).waitFor()

  // A rejected version conflict surfaces through the same role="alert" the
  // create/edit forms use, confirming error feedback is consistently
  // announced to assistive technology across mutation types.
  const commitment = fixtures.collections.commitments.find('commitment-1')
  fixtures.collections.commitments.mutate('commitment-1', commitment.version, () => ({}))
  await section.getByRole('button', { name: 'Archive Send board metrics' }).click()
  const alert = section.getByRole('alert')
  await alert.waitFor()
  assert.match(await alert.innerText(), /changed while you were editing it|Version mismatch|changed elsewhere/i)
}
