import assert from 'node:assert/strict'

import { createFixtureApi } from '../fixtures.mjs'
import { assertNoSeriousAccessibilityViolations } from '../accessibility.mjs'

const syncCorpus = [
  {
    entity_type: 'meeting',
    entity_id: 'meeting-1',
    title: 'Leadership sync',
    snippet: 'Weekly leadership sync meeting',
    matched_fields: ['title'],
    score: 0.95,
    updated_at: '2026-07-14T09:00:00Z',
    source_type: 'local',
    archived: false,
  },
  {
    entity_type: 'calendar_event',
    entity_id: 'event-1',
    title: 'Sync with finance',
    snippet: 'Finance sync on the calendar',
    matched_fields: ['title'],
    score: 0.8,
    updated_at: '2026-07-13T09:00:00Z',
    source_type: 'local',
    archived: false,
  },
]

/**
 * Search journey inside the combined Search & audit workspace: an empty
 * prompt before any query, result rendering for both a meeting and a
 * calendar-event hit, cursor-based pagination via "Load more results", a
 * degraded-search banner, and a no-match empty state.
 */
export async function run({ page, baseURL }) {
  const fixtures = await createFixtureApi(page, {
    searchCorpus: syncCorpus,
    searchPageSize: 1,
    searchDegradedQueries: ['legacy'],
  })

  await page.goto(baseURL)
  await page.getByRole('tab', { name: 'Search & audit' }).click()
  const searchPanel = page.locator('#search-panel')
  await searchPanel.waitFor()
  await searchPanel.getByText('Enter a query to search all Phase 1 entities.').waitFor()

  await assertNoSeriousAccessibilityViolations(page, { include: '#search-panel' })

  // First page of results for "sync".
  await searchPanel.getByLabel(/Search tasks, commitments/).fill('sync')
  await searchPanel.getByRole('button', { name: 'Search', exact: true }).click()
  await searchPanel.getByRole('heading', { name: 'Leadership sync', level: 3 }).waitFor()
  await searchPanel.getByText('95%').waitFor()
  await searchPanel.getByText('meeting', { exact: true }).waitFor()
  const searchRequest = fixtures.requests.find((request) => request.method === 'GET' && request.path === '/api/v1/search')
  assert.ok(searchRequest)

  // Cursor pagination replaces the visible page rather than appending.
  const loadMore = searchPanel.getByRole('button', { name: 'Load more results' })
  await loadMore.waitFor()
  await loadMore.click()
  await searchPanel.getByRole('heading', { name: 'Sync with finance', level: 3 }).waitFor()
  await searchPanel.getByText('calendar event').waitFor()
  assert.equal(await searchPanel.getByRole('heading', { name: 'Leadership sync' }).count(), 0)

  // A query flagged degraded surfaces an accessible status banner.
  await searchPanel.getByLabel(/Search tasks, commitments/).fill('legacy')
  await searchPanel.getByRole('button', { name: 'Search', exact: true }).click()
  const degradedBanner = searchPanel.getByRole('status').filter({ hasText: 'degraded prefix matching' })
  await degradedBanner.waitFor()
  await searchPanel.getByText('No matching entities found.').waitFor()
}
