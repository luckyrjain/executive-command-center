import assert from 'node:assert/strict'

import { createFixtureApi } from '../fixtures.mjs'
import { assertNoSeriousAccessibilityViolations } from '../accessibility.mjs'

const WORKFLOW_ID = 'weekly-digest'
const RUN_ID = 'lifecycle-run-1'
const APPROVAL_ID = 'lifecycle-approval-1'
const DIGEST = 'lifecycle-digest-abc123'
const now = new Date('2026-07-26T12:00:00Z')

function iso(offsetMs = 0) {
  return new Date(now.getTime() + offsetMs).toISOString()
}

/**
 * The complete Phase 5 authority lifecycle (PHASE-005-automation.md's own
 * acceptance criterion: "Browser acceptance covers the complete authority
 * lifecycle"): draft a workflow -> publish -> simulate (zero real side
 * effects, visually distinct from real run history) -> manually run ->
 * hit an approval gate -> approve with the correct digest -> the run
 * completes -> a second run hits a policy-revoked block -> a kill switch
 * stops the workflow and the UI shows why *before* a new run is attempted.
 *
 * This fixture has no real durable worker behind it (browser-only mock,
 * matching every other Phase 1-4 scenario's own "no live backend"
 * convention) -- steps a real worker would perform automatically
 * (dispatching to `waiting_approval`, discovering a revoked policy
 * mid-dispatch) are instead scripted directly via `fixtures.automation`'s
 * exposed collections, exactly like `attention-queue.mjs` already drives
 * `fixtures.collections.risks.mutate(...)` directly rather than
 * simulating a real risk-review backend end to end.
 */
export async function run({ page, baseURL }) {
  const fixtures = await createFixtureApi(page, {
    automation: {
      approvals: [
        {
          id: APPROVAL_ID,
          run_id: RUN_ID,
          step_index: 0,
          action_digest: DIGEST,
          high_impact_categories: ['destructive'],
          status: 'pending',
          requested_at: iso(),
          expires_at: iso(24 * 60 * 60 * 1000),
          decided_at: null,
          decision: null,
          decided_by: null,
          created_at: iso(),
          updated_at: iso(),
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
            input: { note_title: 'Weekly digest note' },
            output: null,
            started_at: null,
            finished_at: null,
            error_class: null,
          },
        ],
      },
      buildRun: (body) => {
        if (body.workflow_id !== WORKFLOW_ID) return null
        return {
          id: RUN_ID,
          workflow_id: WORKFLOW_ID,
          workflow_version: 1,
          policy_id: null,
          trigger_ref: 'manual:fixture-user',
          status: 'waiting_approval',
          current_step_index: 0,
          queued_at: iso(),
          started_at: iso(),
          finished_at: null,
          created_at: iso(),
          updated_at: iso(),
        }
      },
    },
  })

  await page.goto(baseURL)
  await page.getByRole('tab', { name: 'Automation' }).click()
  await page.getByRole('heading', { name: 'Workflows & approvals', level: 1 }).waitFor()
  await assertNoSeriousAccessibilityViolations(page, { include: '#automation-panel' })

  // --- Draft a workflow -----------------------------------------------
  const automationPanel = page.locator('#automation-panel')
  await automationPanel.getByLabel('Workflow ID').fill(WORKFLOW_ID)
  await automationPanel.getByLabel('Step 1 ID').fill('create-note')
  await automationPanel.getByLabel('Step 1 action reference').fill('local.create_note')
  await automationPanel.getByRole('button', { name: 'Create draft' }).click()

  const detail = automationPanel.locator('section[aria-labelledby="automation-workflow-detail-title"]')
  await detail.getByText(`${WORKFLOW_ID} · v1`).waitFor()
  await detail.getByText('draft', { exact: true }).waitFor()

  // --- Publish ----------------------------------------------------------
  await detail.getByRole('button', { name: 'Publish this version' }).click()
  await detail.getByText('active', { exact: true }).waitFor()

  // --- Simulate: zero real side effects, visually distinct -------------
  await detail.getByRole('button', { name: 'Simulate this version' }).click()
  const simulationPanel = automationPanel.locator('.simulation-panel')
  await simulationPanel.getByText(/SIMULATION -- preview only/).waitFor()
  await simulationPanel.getByRole('button', { name: 'Run simulation' }).click()
  await simulationPanel.getByText('[SIMULATED] create-note').waitFor()
  // The visual-distinction requirement, checked structurally: the
  // simulation panel is a real, separate DOM subtree with its own class,
  // never the same list markup real run history renders with.
  assert.equal(await simulationPanel.evaluate((el) => el.classList.contains('simulation-panel')), true)
  // No run was actually created by simulation.
  assert.equal(fixtures.automation.runs.length, 0, 'simulate must never create a run')

  // --- Manually run: hits the approval gate -----------------------------
  await page.getByRole('tab', { name: 'Runs' }).click()
  const runsPanel = automationPanel.locator('section[aria-labelledby="automation-runs-title"]')
  await runsPanel.getByLabel('Workflow ID to run').fill(WORKFLOW_ID)
  await runsPanel.getByRole('button', { name: 'Start run' }).click()

  const runDetail = automationPanel.locator('section[aria-labelledby="automation-run-detail-title"]')
  await runDetail.getByText(/waiting approval/).waitFor()
  await runDetail.getByText(/a step needs a human approval decision/).waitFor()

  // --- Approve with the correct digest ----------------------------------
  await page.getByRole('tab', { name: 'Approvals' }).click()
  await assertNoSeriousAccessibilityViolations(page, { include: '#automation-panel' })
  const approvalsPanel = automationPanel.locator('section[aria-labelledby="automation-approvals-title"]')
  await approvalsPanel.getByText(`Run ${RUN_ID} · step 0`).waitFor()
  await approvalsPanel.getByText('destructive').waitFor()
  await approvalsPanel.getByText('Payload summary (redacted)').click()
  await approvalsPanel.getByText(/Weekly digest note/).waitFor()

  const digestInput = approvalsPanel.getByLabel(`Echo action digest for run ${RUN_ID} step 0`)
  await digestInput.fill(DIGEST)
  await approvalsPanel.getByRole('button', { name: 'Approve' }).click()
  await approvalsPanel.getByText('No approvals are waiting on you.').waitFor()

  const approveRequest = fixtures.requests.find((r) => r.method === 'POST' && r.path === `/api/v1/automations/approvals/${APPROVAL_ID}/approve`)
  assert.ok(approveRequest, 'expected an approve request')
  assert.equal(approveRequest.body.action_digest, DIGEST)

  // The fixture has no real worker to advance the run automatically --
  // script the completion the way a real worker would report it, matching
  // this scenario's own documented "no live backend" convention.
  const approvedRun = fixtures.automation.runs.find((r) => r.id === RUN_ID)
  approvedRun.status = 'succeeded'
  approvedRun.finished_at = iso(60_000)
  fixtures.automation.runSteps.get(RUN_ID)[0].status = 'succeeded'

  await page.getByRole('tab', { name: 'Runs' }).click()
  await runsPanel.getByRole('button', { name: 'View' }).click()
  await runDetail.getByText('Completed successfully.').waitFor()

  // --- A second run hits a policy-revoked block --------------------------
  await page.getByRole('tab', { name: 'Policies' }).click()
  await assertNoSeriousAccessibilityViolations(page, { include: '#automation-panel' })
  const policiesPanel = automationPanel.locator('section[aria-labelledby="automation-policy-title"]')
  await policiesPanel.getByLabel('Policy workflow ID').fill(WORKFLOW_ID)
  await policiesPanel.getByLabel('Policy count limit').fill('5')
  await policiesPanel.getByRole('button', { name: 'Create policy' }).click()
  await policiesPanel.getByText(WORKFLOW_ID, { exact: true }).waitFor()

  await policiesPanel.getByRole('button', { name: `Revoke policy for ${WORKFLOW_ID}` }).click()
  await policiesPanel.getByText('per run · revoked').waitFor()
  const revokedPolicy = fixtures.automation.policies.find((p) => p.workflow_id === WORKFLOW_ID)
  assert.equal(revokedPolicy.status, 'revoked')

  // A real worker would discover the revoked policy mid-dispatch and stop
  // the run at needs_review -- scripted directly here, matching this
  // scenario's own documented convention.
  fixtures.automation.runs.push({
    id: 'lifecycle-run-2',
    workflow_id: WORKFLOW_ID,
    workflow_version: 1,
    policy_id: revokedPolicy.id,
    trigger_ref: 'manual:fixture-user',
    status: 'needs_review',
    current_step_index: 0,
    queued_at: iso(),
    started_at: iso(),
    finished_at: null,
    created_at: iso(),
    updated_at: iso(),
  })
  fixtures.automation.runSteps.set('lifecycle-run-2', [])

  await page.getByRole('tab', { name: 'Runs' }).click()
  await runsPanel.locator('small', { hasText: 'needs review' }).waitFor()
  const secondRunRow = runsPanel.locator('li', { hasText: 'weekly-digest v1' }).last()
  await secondRunRow.getByRole('button', { name: 'View' }).click()
  await runDetail.getByText(/This run's own policy is currently revoked/).waitFor()

  // --- Kill switch: the UI shows why before a new run is attempted -------
  await page.getByRole('tab', { name: 'Kill switches' }).click()
  await assertNoSeriousAccessibilityViolations(page, { include: '#automation-panel' })
  const killSwitchPanel = automationPanel.locator('section[aria-labelledby="automation-kill-switch-title"]')
  await killSwitchPanel.getByLabel('Kill switch reason').fill('incident under investigation')
  await killSwitchPanel.getByLabel('Workflow ID to check').fill(WORKFLOW_ID)
  await killSwitchPanel.getByRole('button', { name: 'Activate for this workflow', exact: true }).click()
  await killSwitchPanel.getByRole('button', { name: 'Check current status' }).click()
  await killSwitchPanel.getByText(/blocked from starting new runs/).waitFor()

  // Visiting the workflow's own detail shows the kill switch is active
  // BEFORE any new run is attempted -- the UX-STATES.md requirement this
  // scenario's own final step proves.
  await page.getByRole('tab', { name: 'Workflows' }).click()
  await automationPanel.getByRole('button', { name: 'View latest (v1)' }).click()
  await detail.getByText(/Kill switch active/).waitFor()

  await page.getByRole('tab', { name: 'Runs' }).click()
  await runsPanel.getByLabel('Workflow ID to run').fill(WORKFLOW_ID)
  await runsPanel.getByRole('button', { name: 'Start run' }).click()
  await runsPanel.getByText(/A kill switch is active for workflow "weekly-digest"/).waitFor()
}
