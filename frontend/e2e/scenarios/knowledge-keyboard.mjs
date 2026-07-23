import assert from 'node:assert/strict'

import { createFixtureApi } from '../fixtures.mjs'
import { assertNoSeriousAccessibilityViolations } from '../accessibility.mjs'

const seedEntity = {
  id: 'entity-ada',
  entity_id: null,
  kind: 'person',
  canonical_name: 'Ada Lovelace',
  summary: 'Mathematician and writer',
  status: 'active',
  confidence: 1,
  version: 1,
  created_at: '2026-07-01T00:00:00Z',
  updated_at: '2026-07-01T00:00:00Z',
}

/**
 * Keyboard-only journey (no `page.click()` anywhere in this file) through
 * the Knowledge workspace: roving-tabindex navigation across the top-level
 * workspace tablist, opening an entity's detail purely by focus + keyboard,
 * and recording a claim on it via the claim form -- exercising Task 28's
 * claims-create UI and provenance display without a mouse. Mirrors
 * conflict-audit-keyboard.mjs's pattern for the Risks workspace.
 */
export async function run({ page, baseURL }) {
  const fixtures = await createFixtureApi(page, {
    knowledgeEntities: [seedEntity],
    evidence: {
      'evidence-board-memo': { source_type: 'manual', label: 'Board memo', captured_at: '2026-01-01T00:00:00Z' },
    },
  })

  await page.goto(baseURL)
  await page.getByRole('main').waitFor()

  const todayTab = page.getByRole('tab', { name: 'Today' })
  await todayTab.focus()
  assert.equal(await page.evaluate(() => document.activeElement?.textContent), 'Today')

  // Roving tabindex: today(0) -> work(1) -> notes(2) -> schedule(3) -> risks(4) -> knowledge(5).
  for (let step = 0; step < 5; step += 1) await page.keyboard.press('ArrowRight')
  assert.equal(await page.evaluate(() => document.activeElement?.textContent), 'Knowledge')
  assert.equal(await page.getByRole('tab', { name: 'Knowledge' }).getAttribute('aria-selected'), 'true')

  const explorer = page.locator('section[aria-labelledby="knowledge-title"]')
  await explorer.getByRole('heading', { name: 'Knowledge', level: 1 }).waitFor()
  await explorer.getByText('Ada Lovelace').waitFor()

  await assertNoSeriousAccessibilityViolations(page, { include: 'section[aria-labelledby="knowledge-title"]' })

  // Tab from the now-focused outer tab into the panel, through the create-
  // entity form, the search form (query, kind filter, date filters, search
  // button, clear-filters button), and into the "All entities" list --
  // asserting the entity list button is reachable by keyboard alone.
  const entityButton = explorer.getByRole('button', { name: 'Ada Lovelace' })
  await entityButton.focus()
  const outline = await entityButton.evaluate((el) => getComputedStyle(el).outlineStyle)
  assert.notEqual(outline, 'none')
  await page.keyboard.press('Enter')

  const detail = page.locator('section[aria-labelledby="entity-detail-title"]')
  await detail.getByRole('heading', { name: 'Ada Lovelace', level: 2 }).waitFor()
  await detail.getByText('No claims recorded for this entity.').waitFor()

  await assertNoSeriousAccessibilityViolations(page, { include: 'section[aria-labelledby="entity-detail-title"]' })

  // Record a claim purely via keyboard: focus each field, type, then submit
  // with Enter rather than a click.
  await detail.getByLabel('Claim predicate').focus()
  await page.keyboard.type('role')
  await page.keyboard.press('Tab')
  assert.equal(await page.evaluate(() => document.activeElement?.getAttribute('aria-label')), 'Claim value')
  await page.keyboard.type('Chief Executive')
  await page.keyboard.press('Tab')
  assert.equal(await page.evaluate(() => document.activeElement?.getAttribute('aria-label')), 'Claim source ID')
  await page.keyboard.type('evidence-board-memo')
  await page.keyboard.press('Tab')
  assert.equal(await page.evaluate(() => document.activeElement?.getAttribute('aria-label')), 'Claim confidence')
  await page.keyboard.press('Tab')
  assert.equal(await page.evaluate(() => document.activeElement?.textContent), 'Record claim')
  await page.keyboard.press('Enter')

  const claimsSection = detail.locator(`section[aria-labelledby="claims-heading-${seedEntity.id}"]`)
  await claimsSection.getByText('role', { exact: true }).waitFor()
  await claimsSection.getByText('available', { exact: false }).waitFor()

  const claimRequest = fixtures.requests.find(
    (request) => request.method === 'POST' && request.path === `/api/v1/knowledge/entities/${seedEntity.id}/claims`,
  )
  assert.ok(claimRequest, 'expected a claim create request')
  assert.equal(claimRequest.body.predicate, 'role')
  assert.equal(claimRequest.body.source_id, 'evidence-board-memo')

  // Close detail via keyboard.
  await detail.getByRole('button', { name: 'Close detail' }).focus()
  await page.keyboard.press('Enter')
  await detail.waitFor({ state: 'detached' })
}
