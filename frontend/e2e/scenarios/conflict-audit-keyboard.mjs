import assert from 'node:assert/strict'

import { createFixtureApi } from '../fixtures.mjs'
import { assertNoSeriousAccessibilityViolations } from '../accessibility.mjs'

// Presses real `Tab` keys (never `.focus()`) until `locator` becomes the
// focused element, up to `maxPresses` attempts. Needed since SidebarNavigation.tsx
// renders every workspace link in one flat list -- after focusing this
// scenario's own active "Search & audit" link, real Tab order runs through
// whatever sidebar links come after it (Automation, Engineering, Personal,
// Team) before ever reaching the main panel, unlike the old single
// top-level tablist widget where the active tab was the only stop before
// the panel. Mirrors automation-approvals-keyboard.mjs's own identical
// helper and its own identical reasoning.
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

  await page.goto(`${baseURL}/risks`)
  assert.equal(await page.title(), 'Executive Command Center')

  // Landmarks: one main region and a named navigation region for the sidebar.
  await page.getByRole('main').waitFor()
  const risksLink = page.getByRole('link', { name: 'Risks' })
  await risksLink.waitFor()

  // The persistent sidebar lives outside every other scenario's `include:`
  // scan, so it otherwise has no automated a11y regression coverage.
  await assertNoSeriousAccessibilityViolations(page, { include: 'nav[aria-label="Workspaces"]' })

  // Visible focus: a keyboard user must be able to reach and see focus on
  // the active sidebar link. Seeding focus here (rather than a bare
  // assertion) also gives the next real Tab press in this file a known
  // starting point, same discipline as automation-approvals-keyboard.mjs's
  // own tabTo() helper.
  await risksLink.focus()
  const outline = await risksLink.evaluate((el) => getComputedStyle(el).outlineStyle)
  assert.notEqual(outline, 'none')
  assert.equal(await risksLink.getAttribute('aria-current'), 'page')

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

  // The line 80 scan above only covers the default list view -- rerun it
  // now that the dynamic edit form (select/textarea inputs) and the
  // conflict alert are both on the page at once, neither of which existed
  // for that first scan.
  await assertNoSeriousAccessibilityViolations(page, { include: 'section[aria-labelledby="risks-title"]' })

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

  // formError has no coverage anywhere else in the suite. The create wizard
  // was never touched above, so it still holds its empty initial draft --
  // submitting it fails client-side validation without needing any fixture
  // setup, and mutationError is null here (the last mutation succeeded).
  // Advance through the Details/Plan steps (Continue doesn't validate) to
  // reach the Review step where "Create risk" lives.
  await risksSection.getByRole('button', { name: 'Continue' }).focus()
  await page.keyboard.press('Enter')

  // Line 80's scan only covers the wizard's default Details step -- Plan
  // and Review are distinct DOM states with no coverage of their own.
  await assertNoSeriousAccessibilityViolations(page, { include: 'section[aria-labelledby="risks-title"]' })

  await risksSection.getByRole('button', { name: 'Continue' }).focus()
  await page.keyboard.press('Enter')
  await assertNoSeriousAccessibilityViolations(page, { include: 'section[aria-labelledby="risks-title"]' })

  await risksSection.getByRole('button', { name: 'Create risk' }).focus()
  await page.keyboard.press('Enter')
  const formErrorAlert = risksSection.getByRole('alert')
  await formErrorAlert.waitFor()
  assert.match(await formErrorAlert.innerText(), /Description is required/)

  // The failed submit should have hopped back from Review to the Details
  // step (where the invalid field lives) and focused it directly, rather
  // than leaving the user stranded on Review with only a generic banner.
  await risksSection.getByRole('heading', { name: 'What is the risk?' }).waitFor()
  const descriptionField = risksSection.getByLabel('Risk description')
  assert.equal(await page.evaluate(() => document.activeElement?.getAttribute('aria-label')), 'Risk description')
  assert.equal(await descriptionField.getAttribute('aria-invalid'), 'true')
  const errorId = await formErrorAlert.getAttribute('id')
  assert.equal(await descriptionField.getAttribute('aria-describedby'), errorId)

  await assertNoSeriousAccessibilityViolations(page, { include: 'section[aria-labelledby="risks-title"]' })

  // Continue keyboard navigation: real routing means reaching Search & audit
  // is a direct page.goto, not an ArrowRight traversal across the old
  // roving-tabindex tablist -- matching automation-approvals-keyboard.mjs's
  // identical precedent for migrating a top-level workspace transition
  // inside a keyboard-only scenario. Seeding focus on the active link gives
  // the next real Tab press below (into the nested Search/Audit tablist) a
  // known starting point.
  await page.goto(`${baseURL}/search-audit`)
  const searchAuditLink = page.getByRole('link', { name: 'Search & audit' })
  await searchAuditLink.waitFor()
  assert.equal(await searchAuditLink.getAttribute('aria-current'), 'page')
  await searchAuditLink.focus()

  // Deviation from the brief's prescribed single `page.keyboard.press('Tab')`:
  // verified against the real SidebarNavigation.tsx, real Tab order runs
  // through the rest of the sidebar's own links (Automation, Engineering,
  // Personal, Team) before reaching the main panel at all. `tabTo()`
  // (defined above) presses real Tab keys until the nested tablist's own
  // default Search tab is reached, then its own roving tabindex reaches
  // Audit history.
  await tabTo(page, page.locator('#search-tab'))
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

  // #audit-panel has no scan anywhere else in the suite -- search-calendar.mjs
  // only scans #search-panel, and this file's own earlier scans never
  // touch this nested tab. Covers the filter input, the paginated event
  // list and the "Load older events" button all in one pass.
  await assertNoSeriousAccessibilityViolations(page, { include: '#audit-panel' })

  // "No audit events match this filter" is a genuinely different DOM state
  // from the scan above (list gone, empty-state text in) and had no
  // coverage of its own -- refine the filter to a value with zero matches
  // in the seeded corpus rather than needing any new fixture.
  await page.getByLabel('Filter by event type').fill('nonexistent.event')
  await auditPanel.getByText('No audit events match this filter.').waitFor()
  await assertNoSeriousAccessibilityViolations(page, { include: '#audit-panel' })

  // audit.isError has no coverage anywhere else in the suite. Same
  // technique tasks.mjs uses for its offline-mutation check: fixtures.setOffline
  // aborts every intercepted request, so changing the filter again (a new
  // queryKey, forcing a fresh fetch) reliably drives the query to its error
  // state -- retry:1 means one extra round-trip before it settles.
  fixtures.setOffline(true)
  await page.getByLabel('Filter by event type').fill('another.event')
  const auditErrorAlert = auditPanel.getByRole('alert')
  await auditErrorAlert.waitFor()
  fixtures.setOffline(false)
  await assertNoSeriousAccessibilityViolations(page, { include: '#audit-panel' })
}
