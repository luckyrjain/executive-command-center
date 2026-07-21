import assert from 'node:assert/strict'

import { createFixtureApi } from '../fixtures.mjs'
import { assertNoSeriousAccessibilityViolations } from '../accessibility.mjs'

const seedRisk = {
  id: 'risk-1',
  description: 'Vendor concentration',
  probability: 3,
  impact: 4,
  status: 'monitoring',
  owner_id: 'owner-fixture',
  mitigation: 'Diversify vendors',
  trigger: 'Single vendor exceeds 50% of spend',
  review_at: '2026-09-01T00:00:00Z',
  project_id: null,
  pinned: false,
  priority_impact: 12,
  score: 60,
  factors: [{ code: 'concentration', label: 'Vendor concentration', points: 60 }],
  explanation: 'Single-vendor exposure',
  created_at: '2026-06-01T00:00:00Z',
  updated_at: '2026-06-01T00:00:00Z',
  version: 1,
}

const auditCorpus = [
  { id: 'audit-1', event_type: 'risk.updated', aggregate_type: 'risk', aggregate_id: 'risk-1', aggregate_version: 2, actor_id: null, changed_fields: ['status'], authorization_result: 'allowed', source: 'user', failure_code: null, occurred_at: '2026-07-15T02:00:00Z' },
  { id: 'audit-2', event_type: 'risk.updated', aggregate_type: 'risk', aggregate_id: 'risk-1', aggregate_version: 3, actor_id: null, changed_fields: ['mitigation'], authorization_result: 'allowed', source: 'user', failure_code: null, occurred_at: '2026-07-15T03:00:00Z' },
  { id: 'audit-3', event_type: 'task.created', aggregate_type: 'task', aggregate_id: 'task-9', aggregate_version: 1, actor_id: null, changed_fields: [], authorization_result: 'allowed', source: 'user', failure_code: null, occurred_at: '2026-07-14T00:00:00Z' },
]

/**
 * Keyboard-only journey (no `page.click()` anywhere in this file): roving-
 * tabindex navigation across the top-level workspace tablist to the Risks
 * workspace, resolving a version conflict there with only `.focus()` +
 * keyboard presses, then continuing keyboard navigation into the nested
 * Search & audit tablist to operate the Audit history view (event-type
 * filter, cursor pagination). Also asserts landmarks, page title and a
 * visible focus outline.
 */
export async function run({ page, baseURL }) {
  const fixtures = await createFixtureApi(page, { risks: [seedRisk], auditCorpus, auditPageSize: 1 })

  await page.goto(baseURL)
  assert.equal(await page.title(), 'Executive Command Center')

  // Landmarks: one main region and a named navigation region for the
  // workspace tablist.
  await page.getByRole('main').waitFor()
  await page.getByRole('navigation', { name: 'Workspace' }).waitFor()

  // The persistent workspace tablist lives outside every other scenario's
  // `include:` scan (it's a sibling of #workspace-panel/#search-panel, never
  // inside them), so it otherwise has no automated a11y regression coverage.
  await assertNoSeriousAccessibilityViolations(page, { include: 'nav[aria-label="Workspace"]' })

  // `.focus()` seeds initial focus into the tablist the way a user who has
  // just Tabbed in from the browser chrome would land on it; every
  // subsequent step below drives the UI with keyboard presses only.
  const todayTab = page.getByRole('tab', { name: 'Today' })
  await todayTab.focus()
  assert.equal(await page.evaluate(() => document.activeElement?.textContent), 'Today')

  // Visible focus: the focused tab must carry a real focus outline, not just
  // programmatic focus.
  const outline = await todayTab.evaluate((el) => getComputedStyle(el).outlineStyle)
  assert.notEqual(outline, 'none')

  // Roving tabindex: today(0) -> work(1) -> notes(2) -> schedule(3) -> risks(4).
  for (let step = 0; step < 4; step += 1) await page.keyboard.press('ArrowRight')
  assert.equal(await page.evaluate(() => document.activeElement?.textContent), 'Risks')
  assert.equal(await page.getByRole('tab', { name: 'Risks' }).getAttribute('aria-selected'), 'true')

  const risksSection = page.locator('section[aria-labelledby="risks-title"]')
  await risksSection.getByRole('heading', { name: 'Risks', level: 1 }).waitFor()
  await risksSection.getByText('Vendor concentration', { exact: true }).waitFor()

  await assertNoSeriousAccessibilityViolations(page, { include: 'section[aria-labelledby="risks-title"]' })

  // Edit the risk and force a version conflict, operating every control by
  // focus + keyboard rather than a mouse click.
  await risksSection.getByRole('button', { name: 'Edit Vendor concentration' }).focus()
  await page.keyboard.press('Enter')
  const editHeading = risksSection.getByRole('heading', { name: 'Edit risk' })
  await editHeading.waitFor()

  fixtures.collections.risks.mutate('risk-1', fixtures.collections.risks.find('risk-1').version, () => ({ mitigation: 'Concurrent mitigation update' }))
  await risksSection.getByLabel('Edit risk description').fill('Vendor concentration (reviewed)')
  await risksSection.getByRole('button', { name: 'Save risk' }).focus()
  await page.keyboard.press('Enter')

  const conflictAlert = risksSection.getByRole('alert')
  await conflictAlert.waitFor()
  assert.match(await conflictAlert.innerText(), /changed while you were editing it/)
  await risksSection.getByRole('button', { name: 'Retry with latest version' }).focus()
  await page.keyboard.press('Enter')
  await risksSection.getByText('Vendor concentration (reviewed)').waitFor()

  // Archive then restore, still keyboard-only. Neither action has any
  // other scenario coverage for risks (tasks.mjs covers archive/restore
  // for tasks; nothing exercises it for risks).
  await risksSection.getByRole('button', { name: 'Archive Vendor concentration (reviewed)' }).focus()
  await page.keyboard.press('Enter')
  const restoreButton = risksSection.getByRole('button', { name: 'Restore Vendor concentration (reviewed)' })
  await restoreButton.waitFor()
  await restoreButton.focus()
  await page.keyboard.press('Enter')
  await risksSection.getByRole('button', { name: 'Archive Vendor concentration (reviewed)' }).waitFor()

  // Continue keyboard navigation: risks(4) -> knowledge(5) -> recommendations(6) -> search-audit(7).
  await page.getByRole('tab', { name: 'Risks' }).focus()
  await page.keyboard.press('ArrowRight')
  await page.keyboard.press('ArrowRight')
  await page.keyboard.press('ArrowRight')
  assert.equal(await page.getByRole('tab', { name: 'Search & audit' }).getAttribute('aria-selected'), 'true')

  // Tab from the now-focused outer tab into the nested Search/Audit tablist,
  // then use its own ArrowRight handler to reach Audit history.
  await page.keyboard.press('Tab')
  assert.equal(await page.evaluate(() => document.activeElement?.id), 'search-tab')
  await page.keyboard.press('ArrowRight')
  assert.equal(await page.evaluate(() => document.activeElement?.id), 'audit-tab')
  await page.getByRole('tab', { name: 'Audit history' }).waitFor()
  assert.equal(await page.getByRole('tab', { name: 'Audit history' }).getAttribute('aria-selected'), 'true')

  const auditPanel = page.locator('#audit-panel')
  await auditPanel.getByText('risk.updated').waitFor()

  // Filter by event type using the keyboard, then paginate with the
  // "Load older events" button (pageSize=1 guarantees a next cursor here).
  await page.keyboard.press('Tab')
  assert.equal(await page.evaluate(() => document.activeElement?.id), 'audit-event-type')
  await page.keyboard.type('risk.updated')
  await auditPanel.getByText('Changed: status').waitFor()

  await page.keyboard.press('Tab')
  const loadOlder = auditPanel.getByRole('button', { name: 'Load older events' })
  await loadOlder.waitFor()
  assert.equal(await page.evaluate(() => document.activeElement?.textContent), 'Load older events')
  await page.keyboard.press('Enter')
  await auditPanel.getByText('Changed: mitigation').waitFor()
}
