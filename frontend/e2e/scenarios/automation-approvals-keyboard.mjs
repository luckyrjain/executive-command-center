import assert from 'node:assert/strict'

import { createFixtureApi } from '../fixtures.mjs'
import { assertNoSeriousAccessibilityViolations } from '../accessibility.mjs'

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

  // Reach the Approvals sub-tab via the nested tablist, keyboard only.
  const workflowsSubTab = page.getByRole('tab', { name: 'Workflows' })
  await workflowsSubTab.focus()
  await page.keyboard.press('ArrowRight')
  assert.equal(await page.evaluate(() => document.activeElement?.textContent), 'Approvals')
  assert.equal(await page.getByRole('tab', { name: 'Approvals' }).getAttribute('aria-selected'), 'true')

  const approvalsPanel = page.locator('#automation-panel')
  await approvalsPanel.getByText(`Run ${RUN_ID} · step 0`).waitFor()
  await assertNoSeriousAccessibilityViolations(page, { include: '#automation-panel' })

  // Expand the redacted payload summary via keyboard (a <details>/<summary>
  // element is natively focusable and toggles on Enter).
  const summaryToggle = approvalsPanel.getByText('Payload summary (redacted)')
  await summaryToggle.focus()
  await page.keyboard.press('Enter')
  await approvalsPanel.getByText(/"amount": 42/).waitFor()

  // Tab forward into the digest-echo field -- never pre-filled -- and type
  // the correct digest read directly off the page, keyboard only.
  const digestInput = approvalsPanel.getByLabel(`Echo action digest for run ${RUN_ID} step 0`)
  await digestInput.focus()
  assert.equal(await digestInput.inputValue(), '')
  await page.keyboard.type(DIGEST)

  const approveButton = approvalsPanel.getByRole('button', { name: 'Approve' })
  await approveButton.focus()
  await page.keyboard.press('Enter')

  await approvalsPanel.getByText('No approvals are waiting on you.').waitFor()
  const approveRequest = fixtures.requests.find((r) => r.method === 'POST' && r.path === `/api/v1/automations/approvals/${APPROVAL_ID}/approve`)
  assert.ok(approveRequest, 'expected a keyboard-driven approve request')
  assert.equal(approveRequest.body.action_digest, DIGEST)
}
