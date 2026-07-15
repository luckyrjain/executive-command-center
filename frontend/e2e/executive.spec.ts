import { expect, test } from '@playwright/test'

const dashboard = {
  date: '2026-07-15',
  timezone: 'Asia/Kolkata',
  generated_at: '2026-07-15T03:30:00Z',
  stale: false,
  sections: {
    today_schedule: [{ id: 'm1', title: 'Leadership review', starts_at: '2026-07-15T04:30:00Z' }],
    top_priorities: [{ entity_id: 't1', title: 'Approve hiring plan', score: 92, status: 'in_progress' }],
    overdue_commitments: [{ entity_id: 'c1', summary: 'Send board metrics', status: 'active' }],
    risks: [{ entity_id: 'r1', title: 'Vendor concentration', score: 80, status: 'monitoring' }],
    waiting_on: [{ entity_id: 'c2', summary: 'Legal approval', status: 'active' }],
    recently_changed: [{ entity_ref: 'task:t1', message: 'Priority updated', occurred_at: '2026-07-15T03:00:00Z' }],
  },
}

const brief = {
  id: 'b1',
  briefing_date: '2026-07-15',
  generation_version: 3,
  sections: dashboard.sections,
  source_versions: { 'task:t1': 2 },
  evidence_ids: [],
  generated_at: '2026-07-15T03:30:00Z',
  timezone: 'Asia/Kolkata',
  algorithm_version: 'phase1-v1',
  ai_status: 'disabled',
  stale: true,
  stale_reason: 'source_version_changed',
}

const pendingRecommendation = {
  id: 'rec1',
  recommendation_type: 'update_task_priority',
  target_type: 'task',
  target_id: 't1',
  proposed_action: { operation: 'update', manual_priority: 'critical' },
  expected_version: 2,
  rationale: 'The task is overdue and blocks the quarterly plan.',
  confidence: 0.91,
  status: 'pending_confirmation',
  evidence_ids: [],
  expires_at: null,
  execution_result: null,
  source: 'rule',
  pinned: false,
  deferred_until: null,
  version: 4,
}

test.beforeEach(async ({ page }) => {
  await page.context().addCookies([
    { name: 'ecc_csrf', value: 'csrf-token', domain: '127.0.0.1', path: '/' },
  ])

  let recommendation = pendingRecommendation

  await page.route('**/api/v1/dashboard/today', (route) => route.fulfill({ json: dashboard }))
  await page.route('**/api/v1/briefs/morning', async (route) => {
    if (route.request().method() === 'POST') {
      await route.fulfill({ json: { ...brief, stale: false, stale_reason: null, generation_version: 4 } })
      return
    }
    await route.fulfill({ json: brief })
  })
  await page.route('**/api/v1/recommendations?**', (route) =>
    route.fulfill({ json: { items: [recommendation], next_cursor: null } }),
  )
  await page.route('**/api/v1/recommendations/rec1/confirm', async (route) => {
    const payload = route.request().postDataJSON()
    expect(payload).toEqual({ expected_version: 4, target_expected_version: 2 })
    recommendation = {
      ...recommendation,
      status: 'executed',
      version: 6,
      execution_result: { outcome: 'updated' },
    }
    await route.fulfill({ json: recommendation })
  })
  await page.route('**/api/v1/search?**', (route) =>
    route.fulfill({
      json: {
        items: [
          {
            entity_type: 'task',
            entity_id: 't1',
            title: 'Approve hiring plan',
            snippet: 'Approve the hiring plan before Friday.',
            matched_fields: ['title'],
            score: 0.98,
            score_components: { exact: 1 },
            updated_at: '2026-07-15T03:00:00Z',
            timestamp_context: null,
            source_type: 'local',
            archived: false,
            evidence_refs: [],
          },
        ],
        next_cursor: null,
        degraded: false,
      },
    }),
  )
  await page.route('**/api/v1/audit**', (route) =>
    route.fulfill({
      json: {
        items: [
          {
            id: 'a1',
            event_type: 'task.updated',
            aggregate_type: 'task',
            aggregate_id: 't1',
            aggregate_version: 2,
            actor_id: null,
            request_id: '00000000-0000-0000-0000-000000000001',
            correlation_id: '00000000-0000-0000-0000-000000000002',
            idempotency_key_hash: null,
            before: null,
            after: null,
            changed_fields: ['manual_priority'],
            authorization_result: 'allowed',
            source: 'user',
            failure_code: null,
            metadata: {},
            occurred_at: '2026-07-15T03:00:00Z',
          },
        ],
        next_cursor: null,
      },
    }),
  )
})

test('renders the executive dashboard and refreshes a stale morning brief', async ({ page }) => {
  await page.goto('/')

  await expect(page.getByRole('heading', { name: 'Today' })).toBeVisible()
  await expect(page.getByText('Leadership review')).toBeVisible()
  await expect(page.getByText('Approve hiring plan')).toBeVisible()
  await expect(page.getByText(/This brief is stale/)).toBeVisible()

  await page.getByRole('button', { name: 'Refresh brief' }).click()
  await expect(page.getByText('Generation 4 · disabled')).toBeVisible()
  await expect(page.getByText(/This brief is stale/)).toHaveCount(0)
})

test('confirms a recommendation with version-bound execution', async ({ page }) => {
  await page.goto('/')

  await expect(page.getByRole('heading', { name: 'Recommendations' })).toBeVisible()
  await page.getByRole('button', { name: 'Confirm and execute' }).click()
  await expect(page.getByText('Execution recorded.')).toBeVisible()
})

test('supports search and audit navigation with keyboard-accessible tabs', async ({ page }) => {
  await page.goto('/')

  const searchTab = page.getByRole('tab', { name: 'Search' })
  await searchTab.focus()
  await page.keyboard.press('Enter')
  await page.getByRole('searchbox').fill('hiring')
  await page.getByRole('button', { name: 'Search' }).click()
  await expect(page.getByText('Approve hiring plan')).toBeVisible()
  await expect(page.getByText('98%')).toBeVisible()

  const auditTab = page.getByRole('tab', { name: 'Audit history' })
  await auditTab.focus()
  await page.keyboard.press('Enter')
  await expect(page.getByText('task.updated')).toBeVisible()
  await expect(page.getByText('manual_priority')).toBeVisible()
})
