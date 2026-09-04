import assert from 'node:assert/strict'

import { createFixtureApi } from '../fixtures.mjs'
import { assertNoSeriousAccessibilityViolations } from '../accessibility.mjs'

/**
 * Today dashboard + Morning Brief characterization. This absorbs the
 * original ad-hoc `e2e/run.mjs` smoke assertions (dashboard sections, brief
 * staleness) rather than the stale "No open tasks."/"No open commitments."
 * text those never actually matched in the current tabbed UI — Today no
 * longer renders TaskWorkspace/CommitmentWorkspace inline, so this scenario
 * checks the dashboard sections and Morning Brief panel that Today
 * genuinely renders, plus the brief refresh action.
 */
export async function run({ page, baseURL }) {
  const fixtures = await createFixtureApi(page)

  await page.goto(baseURL)
  await page.getByRole('heading', { name: 'Today', level: 1 }).waitFor()
  await page.getByText('2026-07-15 · Asia/Kolkata').waitFor()

  await assertNoSeriousAccessibilityViolations(page, { include: '#workspace-panel' })

  // Dashboard sections sourced from GET /api/v1/dashboard/today. "Top
  // priorities" is the page's promoted dominant anchor (DESIGN.md's
  // "Building a new page" Hierarchy rules) -- its own standalone .work-panel,
  // rendered before .dashboard-grid, not one of the grid's .dashboard-cards.
  const topPriorities = page.locator('section[aria-labelledby="section-top-priorities"]')
  await topPriorities.getByRole('heading', { name: 'Top priorities' }).waitFor()
  await topPriorities.getByText('Approve hiring plan').waitFor()
  await topPriorities.getByText('92').waitFor()

  const dashboardGrid = page.locator('.dashboard-grid')
  await dashboardGrid.getByText('Leadership review').waitFor()
  await dashboardGrid.getByText('Send board metrics').waitFor()
  await dashboardGrid.getByText('Vendor concentration').waitFor()
  await dashboardGrid.getByText('Legal approval').waitFor()
  await dashboardGrid.getByText('Priority updated').waitFor()

  const refreshButton = page.getByRole('button', { name: 'Refresh dashboard' })
  const refetchResponse = page.waitForResponse((response) => response.url().includes('/api/v1/dashboard/today'))
  await refreshButton.click()
  await refetchResponse
  const dashboardRequests = fixtures.requests.filter((request) => request.path === '/api/v1/dashboard/today')
  assert.ok(dashboardRequests.length >= 2, 'expected the refresh button to issue a second GET /api/v1/dashboard/today')

  // Morning Brief: stale + AI-disabled banners, generation number, refresh.
  const briefPanel = page.locator('section[aria-labelledby="morning-brief-title"]')
  await briefPanel.getByRole('heading', { name: 'Morning Brief' }).waitFor()
  await briefPanel.getByText('Generation 3 · disabled').waitFor()
  await briefPanel.getByText('AI-assisted sections are disabled; showing deterministic results only.').waitFor()
  await briefPanel.getByText(/This brief is stale: source version changed\. Refresh to regenerate it\./).waitFor()
  // The brief's item lists duplicated the live dashboard grid one-for-one
  // (same categories, same items, styled as identical cards) with nothing
  // distinguishing "live" from "persisted snapshot" -- replaced with a
  // compact count strip; full item detail already lives in the dashboard
  // sections above.
  const briefStats = briefPanel.locator('.brief-stats')
  await briefStats.getByText('Schedule').waitFor()
  await briefStats.getByText('Priorities').waitFor()
  await briefStats.getByText('Overdue').waitFor()
  await briefStats.getByText('Risks').waitFor()

  await briefPanel.getByRole('button', { name: 'Refresh brief' }).click()
  await briefPanel.getByText('Generation 4 · disabled').waitFor()
  assert.equal(await briefPanel.getByText(/This brief is stale/).count(), 0, 'refreshing the brief should clear the stale banner')
  assert.equal(fixtures.brief.generation_version, 4)

  // The scan above (line 22) ran once, before either refresh click -- it
  // never covered the post-refresh state: the stale banner gone, "Refreshing…"
  // having come and gone, and the dashboard grid re-rendered from its second
  // GET. Genuinely different DOM from what line 22 saw, never scanned until now.
  await assertNoSeriousAccessibilityViolations(page, { include: '#workspace-panel' })
}
