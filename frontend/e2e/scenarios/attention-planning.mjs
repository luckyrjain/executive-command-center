import assert from 'node:assert/strict'

import { createFixtureApi } from '../fixtures.mjs'
import { assertNoSeriousAccessibilityViolations } from '../accessibility.mjs'

const block = {
  id: 'block-1',
  source_type: 'task',
  source_id: 'task-1',
  starts_at: '2026-07-24T09:00:00Z',
  ends_at: '2026-07-24T09:30:00Z',
  status: 'proposed',
  rationale: 'Write the board memo',
  is_default_effort: true,
}

/**
 * Planner journey: propose a plan, see capacity used/unscheduled/conflicts
 * always visible, move a block with keyboard-only datetime inputs (no
 * drag-and-drop), accept, then replan and require reviewing the diff
 * before a new plan can be accepted (UX-STATES.md: "Replanning presents a
 * diff before acceptance").
 */
export async function run({ page, baseURL }) {
  const fixtures = await createFixtureApi(page, {
    attention: {
      planBlocks: [block],
      planUnscheduled: [{ source_type: 'task', source_id: 'task-2', label: 'Draft the appendix', reason: 'no_capacity' }],
      replanDiff: [{ source_type: 'task', source_id: 'task-1', label: 'Write the board memo', change: 'unchanged' }],
      replanBlocks: [block],
    },
  })

  await page.goto(baseURL)
  await page.getByRole('tab', { name: 'Planner' }).click()
  const section = page.locator('section[aria-labelledby="planner-title"]')
  await section.getByRole('heading', { name: 'Planner', level: 1 }).waitFor()

  await assertNoSeriousAccessibilityViolations(page, { include: 'section[aria-labelledby="planner-title"]' })

  await section.getByLabel('Period start').fill('2026-07-24')
  await section.getByLabel('Period end').fill('2026-07-24')
  await section.getByRole('button', { name: 'Propose plan' }).click()

  await section.getByText('Write the board memo', { exact: false }).waitFor()
  await section.getByText(/30 of 480 minutes used/).waitFor()
  await section.getByText(/Draft the appendix/).waitFor()

  // Move the block with keyboard-operable datetime inputs. exact: true --
  // "Remove Write the board memo" contains "move Write the board memo" as
  // a case-insensitive substring, so the default substring match resolves
  // both buttons.
  await section.getByRole('button', { name: 'Move Write the board memo', exact: true }).click()
  await section.getByLabel('New start for Write the board memo').fill('2026-07-24T11:00')
  await section.getByLabel('New end for Write the board memo').fill('2026-07-24T11:30')
  await section.getByRole('button', { name: 'Save new time' }).click()
  const moveRequest = fixtures.requests.find((request) => request.method === 'POST' && /^\/api\/v1\/plans\/[^/]+\/blocks\/block-1\/move$/.test(request.path))
  assert.ok(moveRequest, 'expected a move request')

  // Replan requires reviewing a diff before the new plan can be accepted.
  const replanButtons = section.getByRole('button', { name: /^Replan / })
  await replanButtons.first().click()
  const diffHeading = section.getByText('Review replan before accepting')
  await diffHeading.waitFor()
  await section.getByText('unchanged').waitFor()
  await section.getByRole('button', { name: 'Accept new plan' }).click()
  await diffHeading.waitFor({ state: 'detached' })

  const acceptRequests = fixtures.requests.filter((request) => request.method === 'POST' && /\/api\/v1\/plans\/[^/]+\/accept$/.test(request.path))
  assert.ok(acceptRequests.length > 0, 'expected an accept request after reviewing the diff')
}
