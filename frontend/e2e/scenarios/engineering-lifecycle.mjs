import assert from 'node:assert/strict'

import { createFixtureApi } from '../fixtures.mjs'
import { assertNoSeriousAccessibilityViolations } from '../accessibility.mjs'

const now = new Date('2026-07-27T12:00:00Z')

function iso(offsetMs = 0) {
  return new Date(now.getTime() + offsetMs).toISOString()
}

/**
 * Engineering Overview -> Delivery/Reliability (metric definition, window,
 * coverage and evidence, per design doc Decision 3) -> Incidents (capture
 * then resolve) -> Decisions (propose then decide) -> Source coverage,
 * including the "conflicting identities" disclosure (`UX-STATES.md`) --
 * this activation has no identity-resolution mechanism for engineering
 * data, so that state is a disclosure, not an interactive merge flow.
 */
export async function run({ page, baseURL }) {
  const fixtures = await createFixtureApi(page, {
    engineering: {
      connectors: [
        { id: 'conn-1', provider: 'jira', display_name: 'Acme Jira', external_account_id: 'acme', granted_scopes: [], status: 'active', status_detail: null, last_synced_at: iso(-60 * 60 * 1000), last_error: null, disconnected_at: null, version: 1, created_at: iso(-30 * 24 * 60 * 60 * 1000), updated_at: iso() },
      ],
      workItems: [
        { id: 'item-1', connector_account_id: 'conn-1', provider: 'jira', external_id: 'jira-1', title: 'Fix checkout bug', source_url: 'https://acme.atlassian.net/browse/PROJ-1', item_type: 'Bug', status: 'To Do', reporter_external_id: 'jira-user-9001', assignee_external_id: null, permission_state: 'active', freshness_state: 'fresh', provider_created_at: iso(-10 * 24 * 60 * 60 * 1000), observed_at: iso(-60 * 60 * 1000), created_at: iso(-10 * 24 * 60 * 60 * 1000), updated_at: iso(-60 * 60 * 1000) },
        { id: 'item-2', connector_account_id: 'conn-1', provider: 'jira', external_id: 'jira-2', title: 'Improve onboarding', source_url: 'https://acme.atlassian.net/browse/PROJ-2', item_type: 'Story', status: 'In Progress', reporter_external_id: 'jira-user-9002', assignee_external_id: 'jira-user-9001', permission_state: 'active', freshness_state: 'fresh', provider_created_at: iso(-5 * 24 * 60 * 60 * 1000), observed_at: iso(-60 * 60 * 1000), created_at: iso(-5 * 24 * 60 * 60 * 1000), updated_at: iso(-60 * 60 * 1000) },
      ],
      incidents: [],
      decisions: [],
      metrics: [
        { id: 'm-delivery-frequency', metric_key: 'delivery_frequency', window_label: '30d', population: 0, numerator: null, denominator: null, value: null, details: null, coverage_status: 'insufficient_coverage', coverage_percentage: 0, coverage_gap_description: 'No deployments source is available yet', computed_at: iso() },
        { id: 'm-review-latency', metric_key: 'review_latency', window_label: '30d', population: 6, numerator: 2.4, denominator: null, value: 2.4, details: null, coverage_status: 'partial', coverage_percentage: 62, coverage_gap_description: 'GitLab backfill 62% complete', computed_at: iso() },
        { id: 'm-time-to-restore', metric_key: 'time_to_restore', window_label: '30d', population: 3, numerator: 3, denominator: null, value: 4.2, details: null, coverage_status: 'complete', coverage_percentage: 100, coverage_gap_description: null, computed_at: iso() },
      ],
    },
  })

  await page.goto(baseURL)
  await page.getByRole('tab', { name: 'Engineering' }).click()
  await page.getByRole('heading', { name: 'Engineering workspace', level: 1 }).waitFor()
  await assertNoSeriousAccessibilityViolations(page, { include: '#engineering-panel' })

  const panel = page.locator('#engineering-panel')

  // --- Overview: a single glance, with working quick-jump links ---------
  const overviewPanel = panel.locator('section[aria-labelledby="engineering-overview-title"]')
  await overviewPanel.getByText('1 connected. All healthy.').waitFor()
  await overviewPanel.getByText('0 open.').waitFor()

  // --- Delivery: insufficient coverage never shows a misleading number,
  // and partial coverage always shows its own gap description -----------
  await page.getByRole('tab', { name: 'Delivery' }).click()
  await assertNoSeriousAccessibilityViolations(page, { include: '#engineering-panel' })
  const deliveryPanel = panel.locator('section[aria-labelledby="engineering-delivery-title"]')
  await deliveryPanel.getByText('Delivery frequency').waitFor()
  await deliveryPanel.getByText('not yet available').waitFor()
  await deliveryPanel.getByText(/Coverage: insufficient coverage \(0%\)/).waitFor()
  await deliveryPanel.getByText(/Coverage: partial \(62%\)/).waitFor()
  await deliveryPanel.getByText(/GitLab backfill 62% complete/).waitFor()
  await deliveryPanel.locator('li', { hasText: 'Review latency' }).getByText('Evidence').click()
  await deliveryPanel.getByText('6').waitFor()

  // --- Reliability: only the incident-recovery metric --------------------
  await page.getByRole('tab', { name: 'Reliability' }).click()
  await assertNoSeriousAccessibilityViolations(page, { include: '#engineering-panel' })
  const reliabilityPanel = panel.locator('section[aria-labelledby="engineering-reliability-title"]')
  await reliabilityPanel.getByText('4.2 days').waitFor()
  await reliabilityPanel.getByText(/Coverage: complete \(100%\)/).waitFor()

  // --- Incidents: capture, then resolve ------------------------------------
  await page.getByRole('tab', { name: 'Incidents' }).click()
  await assertNoSeriousAccessibilityViolations(page, { include: '#engineering-panel' })
  const incidentsPanel = panel.locator('section[aria-labelledby="engineering-incidents-title"]')
  await incidentsPanel.getByLabel('Incident title').fill('Checkout outage')
  await incidentsPanel.getByLabel('Severity').selectOption('critical')
  await incidentsPanel.getByRole('button', { name: 'Capture incident' }).click()
  await incidentsPanel.getByText('Checkout outage').waitFor()
  assert.equal(fixtures.engineering.incidents.length, 1)
  assert.equal(fixtures.engineering.incidents[0].severity, 'critical')

  await incidentsPanel.getByRole('button', { name: 'Resolve now' }).click()
  await incidentsPanel.getByText('No incidents match this filter.').waitFor()
  assert.equal(fixtures.engineering.incidents[0].status, 'resolved')

  // --- Decisions: propose, then decide -------------------------------------
  await page.getByRole('tab', { name: 'Decisions' }).click()
  await assertNoSeriousAccessibilityViolations(page, { include: '#engineering-panel' })
  const decisionsPanel = panel.locator('section[aria-labelledby="engineering-decisions-title"]')
  await decisionsPanel.getByLabel('Decision title').fill('Adopt trunk-based development')
  await decisionsPanel.getByRole('button', { name: 'Propose decision' }).click()
  await decisionsPanel.getByText('Adopt trunk-based development').waitFor()

  await decisionsPanel.getByLabel('Rationale for Adopt trunk-based development').fill('Reduces merge conflicts')
  await decisionsPanel.getByRole('button', { name: 'Record decision' }).click()
  await decisionsPanel.getByText('No decisions match this filter.').waitFor()
  assert.equal(fixtures.engineering.decisions[0].status, 'decided')
  assert.equal(fixtures.engineering.decisions[0].rationale, 'Reduces merge conflicts')

  // --- Source coverage: rollup plus the conflicting-identities disclosure -
  await page.getByRole('tab', { name: 'Source coverage' }).click()
  await assertNoSeriousAccessibilityViolations(page, { include: '#engineering-panel' })
  const coveragePanel = panel.locator('section[aria-labelledby="engineering-coverage-title"]')
  await coveragePanel.getByText(/2 work items/).waitFor()
  await coveragePanel.getByText(/Unresolved identities \(2\)/).click()
  await coveragePanel.getByText(/no identity-resolution mechanism for engineering data/).waitFor()
  await coveragePanel.getByText('jira-user-9001').waitFor()
  await coveragePanel.getByText('jira-user-9002').waitFor()
}
