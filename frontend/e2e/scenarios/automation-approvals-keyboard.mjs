import assert from 'node:assert/strict'

import { createFixtureApi } from '../fixtures.mjs'
import { assertNoSeriousAccessibilityViolations } from '../accessibility.mjs'

/**
 * Presses real `Tab` keys (never `.focus()`, which jumps the DOM's active
 * element directly and proves nothing about actual sequential tab-order
 * reachability or the absence of a keyboard trap) until `locator` becomes
 * the focused element, up to `maxPresses` attempts. Found during
 * independent review: this scenario originally used `.focus()` for every
 * transition after the initial landing tab, which -- unlike `conflict-
 * audit-keyboard.mjs`/`knowledge-keyboard.mjs`'s own real-`Tab`-press
 * precedent -- never actually exercised the browser's real tab order, so
 * it could not have caught a keyboard trap or an unreachable control. A
 * bounded "press Tab until this element is focused" loop (rather than a
 * hardcoded press count, which would require knowing this component
 * tree's exact intervening-element count in advance and would silently go
 * stale the moment that count changed) proves genuine reachability while
 * staying robust to unrelated markup changes.
 */
async function tabTo(page, locator, maxPresses = 15) {
  for (let attempt = 0; attempt < maxPresses; attempt += 1) {
    if (await locator.evaluate((el) => el === document.activeElement).catch(() => false)) return
    await page.keyboard.press('Tab')
  }
  assert.ok(
    await locator.evaluate((el) => el === document.activeElement),
    `expected to reach the target element via real Tab presses within ${maxPresses} attempts`,
  )
}

const RUN_ID = 'keyboard-run-1'
const APPROVAL_ID = 'keyboard-approval-1'
const DIGEST = 'keyboard-digest-xyz789'

const pendingApproval = {
  id: APPROVAL_ID,
  run_id: RUN_ID,
  step_index: 0,
  action_digest: DIGEST,
  high_impact_categories: ['financial'],
  status: 'pending',
  requested_at: '2026-07-26T00:00:00Z',
  expires_at: '2026-07-27T00:00:00Z',
  decided_at: null,
  decision: null,
  decided_by: null,
  created_at: '2026-07-26T00:00:00Z',
  updated_at: '2026-07-26T00:00:00Z',
}

/**
 * Keyboard-only journey through the approval inbox (no `page.click()`
 * anywhere in this file, matching `conflict-audit-keyboard.mjs`/
 * `knowledge-keyboard.mjs`'s own precedent for keyboard-accessibility-
 * critical flows) -- the highest-stakes action surface in this activation
 * (UX-STATES.md's deliberate-confirmation requirement), so it gets its own
 * dedicated keyboard-only proof rather than only the general accessibility
 * scan every scenario already runs.
 */
export async function run({ page, baseURL }) {
  const fixtures = await createFixtureApi(page, {
    automation: {
      approvals: [pendingApproval],
      runs: [
        {
          id: RUN_ID,
          workflow_id: 'quarterly-payout',
          workflow_version: 1,
          policy_id: null,
          trigger_ref: 'manual:fixture-user',
          status: 'waiting_approval',
          current_step_index: 0,
          queued_at: '2026-07-26T00:00:00Z',
          started_at: '2026-07-26T00:00:00Z',
          finished_at: null,
          created_at: '2026-07-26T00:00:00Z',
          updated_at: '2026-07-26T00:00:00Z',
        },
      ],
      runSteps: {
        [RUN_ID]: [
          {
            step_index: 0,
            step_type: 'action',
            status: 'pending',
            action_digest: DIGEST,
            attempt_count: 0,
            action_ref: 'fake.external_action',
            input: { amount: 42 },
            output: null,
            started_at: null,
            finished_at: null,
            error_class: null,
          },
        ],
      },
    },
  })

  await page.goto(baseURL)

  // Reach the Automation workspace tab via the top-level roving-tabindex
  // tablist, keyboard only.
  const todayTab = page.getByRole('tab', { name: 'Today' })
  await todayTab.focus()
  for (let step = 0; step < 11; step += 1) await page.keyboard.press('ArrowRight')
  assert.equal(await page.evaluate(() => document.activeElement?.textContent), 'Automation')
  // ArrowRight/moveWorkspaceFocus already switches the view as focus moves
  // (WorkspaceNavigation.tsx's own `onMove` callback) -- no separate Enter
  // needed, matching `conflict-audit-keyboard.mjs`'s identical precedent.
  assert.equal(await page.getByRole('tab', { name: 'Automation' }).getAttribute('aria-selected'), 'true')

  await page.getByRole('heading', { name: 'Workflows & approvals', level: 1 }).waitFor()

  // Tab from the now-focused outer tablist into the nested Workflows/
  // Approvals tablist (real Tab press, matching `conflict-audit-
  // keyboard.mjs`'s identical outer-to-nested-tablist transition), then
  // reach the Approvals sub-tab via that nested tablist's own roving
  // tabindex, keyboard only.
  await page.keyboard.press('Tab')
  await page.keyboard.press('ArrowRight')
  assert.equal(await page.evaluate(() => document.activeElement?.textContent), 'Approvals')
  assert.equal(await page.getByRole('tab', { name: 'Approvals' }).getAttribute('aria-selected'), 'true')

  const approvalsPanel = page.locator('#automation-panel')
  await approvalsPanel.getByText(`Run ${RUN_ID} · step 0`).waitFor()
  await assertNoSeriousAccessibilityViolations(page, { include: '#automation-panel' })

  // Expand the redacted payload summary via keyboard (a <details>/<summary>
  // element is natively focusable and toggles on Enter) -- reached by real
  // Tab presses from the nested tablist, not `.focus()`, so this actually
  // proves the control sits somewhere in the page's real tab order.
  const summaryToggle = approvalsPanel.getByText('Payload summary (redacted)')
  await tabTo(page, summaryToggle)
  await page.keyboard.press('Enter')
  await approvalsPanel.getByText(/"amount": 42/).waitFor()

  // Tab forward into the digest-echo field -- never pre-filled -- and type
  // the correct digest read directly off the page, keyboard only.
  const digestInput = approvalsPanel.getByLabel(`Echo action digest for run ${RUN_ID} step 0`)
  await tabTo(page, digestInput)
  assert.equal(await digestInput.inputValue(), '')
  await page.keyboard.type(DIGEST)

  const approveButton = approvalsPanel.getByRole('button', { name: 'Approve' })
  await tabTo(page, approveButton)
  await page.keyboard.press('Enter')

  await approvalsPanel.getByText('No approvals are waiting on you.').waitFor()
  const approveRequest = fixtures.requests.find((r) => r.method === 'POST' && r.path === `/api/v1/automations/approvals/${APPROVAL_ID}/approve`)
  assert.ok(approveRequest, 'expected a keyboard-driven approve request')
  assert.equal(approveRequest.body.action_digest, DIGEST)
}
