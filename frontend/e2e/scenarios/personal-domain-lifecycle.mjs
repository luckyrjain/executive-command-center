import assert from 'node:assert/strict'

import { createFixtureApi } from '../fixtures.mjs'
import { assertNoSeriousAccessibilityViolations } from '../accessibility.mjs'

const now = new Date('2026-07-31T12:00:00Z')

function iso(offsetMs = 0) {
  return new Date(now.getTime() + offsetMs).toISOString()
}

/**
 * Phase 7 Personal workspace (`docs/phases/phase-007/UX-STATES.md`'s Task 8
 * section): enable -> capture -> grant/revoke -> inspect/dismiss insight ->
 * export -> delete, end to end through live actions rather than pre-seeded
 * terminal states (mirroring `engineering-lifecycle.mjs`'s own shape) --
 * `personal-domain-lifecycle.mjs` is the "flow" counterpart to that file's
 * "states" sibling. Two states this flow cannot itself produce (an expired
 * grant, and a `professional_referral_note` boundary on a `high_stakes`
 * insight -- neither reachable without either real clock control or a real
 * model call) are pre-seeded instead, the same "distinct fixture row" choice
 * `engineering-connector-states.mjs`'s own docstring explains.
 *
 * "Import pending" (`UX-STATES.md`'s required state list) has no coverage
 * here deliberately -- this activation has no real import path at all
 * (`IMPLEMENTATION-STATUS.md`: "`imported_file` source type has no real
 * import path yet, manual entry only"), so there is no real state to drive
 * through; faking one would be UI for a capability that does not exist.
 */
export async function run({ page, baseURL }) {
  const fixtures = await createFixtureApi(page, {
    personal: {
      grants: [
        {
          id: 'grant-expired', source_domain_key: 'learning', purpose: 'insight_generation',
          granted_categories: ['course'], granted_at: iso(-60 * 24 * 60 * 60 * 1000),
          expires_at: iso(-1 * 24 * 60 * 60 * 1000), revoked_at: null, created_at: iso(-60 * 24 * 60 * 60 * 1000),
        },
      ],
      insights: [
        {
          id: 'insight-health', domain_key: 'health', kind: 'trend', title: 'Resting heart rate trend',
          evidence: {}, source_period_start: iso(-7 * 24 * 60 * 60 * 1000), source_period_end: iso(),
          missing_data: 'no readings on weekends', confidence: 'medium',
          limitations: 'Based only on the records shown here.',
          professional_referral_note: 'Consider discussing this with your doctor.',
          computed_at: iso(), dismissed_at: null,
        },
      ],
    },
  })

  await page.goto(baseURL)
  await page.getByRole('tab', { name: 'Personal' }).click()
  await page.getByRole('heading', { name: 'Personal workspace', level: 1 }).waitFor()
  await assertNoSeriousAccessibilityViolations(page, { include: '#personal-panel' })

  const panel = page.locator('#personal-panel')

  // --- Domains: every domain starts disabled ------------------------------
  const domainsPanel = panel.locator('section[aria-labelledby="personal-domains-title"]')
  const habitsRow = domainsPanel.locator('li', { hasText: 'Habits' })
  await habitsRow.getByText(/Not enabled/).waitFor()

  await habitsRow.getByRole('button', { name: 'Enable' }).click()
  await habitsRow.getByText(/Enabled/).waitFor()
  assert.equal(fixtures.personal.domains.find((d) => d.domain_key === 'habits')?.enabled, true)

  // --- Records: no data, then a real captured record ----------------------
  await page.getByRole('tab', { name: 'Records' }).click()
  await assertNoSeriousAccessibilityViolations(page, { include: '#personal-panel' })
  const recordsPanel = panel.locator('section[aria-labelledby="personal-records-title"]')
  await recordsPanel.getByText('No records yet for Habits.').waitFor()

  await recordsPanel.getByLabel('Record type').fill('note')
  await recordsPanel.getByLabel('Field name 1').fill('text')
  await recordsPanel.getByLabel('Field value 1').fill('Went for a run')
  await recordsPanel.getByRole('button', { name: 'Save record' }).click()
  await recordsPanel.getByText('note', { exact: true }).waitFor()
  assert.equal(fixtures.personal.records.length, 1)
  assert.equal(fixtures.personal.records[0].payload.text, 'Went for a run')

  // --- Grants: a pre-seeded expired grant, plus a live create -> revoke ---
  await page.getByRole('tab', { name: 'Cross-domain grants' }).click()
  await assertNoSeriousAccessibilityViolations(page, { include: '#personal-panel' })
  const grantsPanel = panel.locator('section[aria-labelledby="personal-grants-title"]')
  const learningGrantRow = grantsPanel.locator('li', { hasText: 'Learning' })
  await learningGrantRow.getByText(/Expired/).waitFor()

  await grantsPanel.getByLabel('Granted categories').fill('note')
  await grantsPanel.getByRole('button', { name: 'Create grant' }).click()
  const habitsGrantRow = grantsPanel.locator('li', { hasText: 'Habits' })
  await habitsGrantRow.getByText(/Active/).waitFor()

  await habitsGrantRow.getByRole('button', { name: 'Revoke' }).click()
  await habitsGrantRow.getByText(/Revoked/).waitFor()
  assert.equal(fixtures.personal.grants.find((g) => g.source_domain_key === 'habits')?.revoked_at != null, true)

  // --- Insights: the sensitive-insight boundary, incomplete-data notice,
  // dismiss, and the fail-open "not available" generate response ----------
  await page.getByRole('tab', { name: 'Insights' }).click()
  await assertNoSeriousAccessibilityViolations(page, { include: '#personal-panel' })
  const insightsPanel = panel.locator('section[aria-labelledby="personal-insights-title"]')
  const healthInsightRow = insightsPanel.locator('li', { hasText: 'Resting heart rate trend' })
  await healthInsightRow.getByText('Consider discussing this with your doctor.').waitFor()
  await healthInsightRow.getByText(/Incomplete data: no readings on weekends/).waitFor()

  // `GET /personal/insights` (habits.py:list_insights_endpoint) filters
  // `dismissed_at IS NULL` server-side -- dismissing this, the only seeded
  // insight, removes it from the list entirely rather than leaving a
  // "Dismissed" row behind.
  await healthInsightRow.getByRole('button', { name: 'Dismiss' }).click()
  await insightsPanel.getByText('No insights yet -- capture a few records first.').waitFor()
  assert.equal(fixtures.personal.insights[0].dismissed_at != null, true)

  await insightsPanel.getByRole('checkbox', { name: /Habits/ }).click()
  await insightsPanel.getByRole('button', { name: 'Generate insight' }).click()
  await insightsPanel.getByText(/No insight available right now \(no_grant\)/).waitFor()

  // --- Export & delete: a real export of the captured record, then delete -
  await page.getByRole('tab', { name: 'Export & delete' }).click()
  await assertNoSeriousAccessibilityViolations(page, { include: '#personal-panel' })
  const exportPanel = panel.locator('section[aria-labelledby="personal-export-title"]')
  await exportPanel.getByRole('button', { name: 'Export Habits' }).click()
  await exportPanel.getByText(/Exported 1 item\(s\)/).waitFor()
  await exportPanel.getByText('Raw export data').click()
  await exportPanel.getByText(/"text": "Went for a run"/).waitFor()

  await exportPanel.getByRole('checkbox').click()
  await exportPanel.getByRole('button', { name: 'Delete Habits' }).click()
  await exportPanel.getByText(/Deletion completed at/).waitFor()
  assert.equal(fixtures.personal.records.some((r) => r.domain_key === 'habits'), false)
}
