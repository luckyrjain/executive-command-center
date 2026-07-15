import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'

import { chromium } from 'playwright'

const preview = spawn('pnpm', ['exec', 'vite', 'preview', '--host', '127.0.0.1', '--port', '4173'], {
  stdio: 'inherit',
})

async function waitForServer() {
  for (let attempt = 0; attempt < 60; attempt += 1) {
    try {
      const response = await fetch('http://127.0.0.1:4173')
      if (response.ok) return
    } catch {
      // The preview server is still starting.
    }
    await new Promise((resolve) => setTimeout(resolve, 500))
  }
  throw new Error('Vite preview did not start')
}

async function step(name, run) {
  process.stdout.write(`→ ${name}\n`)
  await run()
  process.stdout.write(`✓ ${name}\n`)
}

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

let task = {
  id: 't1',
  owner_id: 'u1',
  title: 'Approve hiring plan',
  description: null,
  status: 'in_progress',
  manual_priority: 'high',
  due_date: '2026-07-15',
  due_at: null,
  blocked_reason: null,
  blocked_on_person_id: null,
  completed_at: null,
  pinned: false,
  source_type: 'local',
  source_ref: null,
  created_at: '2026-07-14T03:00:00Z',
  updated_at: '2026-07-15T03:00:00Z',
  version: 2,
  archived_at: null,
  pre_archive_status: null,
  links: { audit: '/api/v1/audit' },
}

let commitment = {
  id: 'c1',
  owner_id: 'u1',
  summary: 'Send board metrics',
  description: null,
  direction: 'made_by_me',
  counterparty_person_id: null,
  counterparty_name: 'Board',
  status: 'active',
  due_date: '2026-07-15',
  due_at: null,
  importance: 'critical',
  evidence_id: null,
  confidence: null,
  fulfilled_at: null,
  pinned: false,
  created_at: '2026-07-14T03:00:00Z',
  updated_at: '2026-07-15T03:00:00Z',
  version: 3,
  archived_at: null,
  pre_archive_status: null,
  links: { audit: '/api/v1/audit' },
}

let recommendation = {
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

async function main() {
  await waitForServer()
  const browser = await chromium.launch({ headless: true })
  const page = await browser.newPage()
  page.on('pageerror', (error) => console.error('Browser page error:', error))
  page.on('console', (message) => {
    if (message.type() === 'error') console.error('Browser console error:', message.text())
  })

  await page.context().addCookies([
    { name: 'ecc_csrf', value: 'csrf-token', url: 'http://127.0.0.1:4173' },
  ])

  await page.route('**/api/v1/dashboard/today', (route) => route.fulfill({ json: dashboard }))
  await page.route('**/api/v1/briefs/morning', async (route) => {
    const refreshed = { ...brief, stale: false, stale_reason: null, generation_version: 4 }
    await route.fulfill({ json: route.request().method() === 'POST' ? refreshed : brief })
  })
  await page.route('**/api/v1/tasks?**', (route) =>
    route.fulfill({ json: { items: task.status === 'completed' ? [] : [task], next_cursor: null } }),
  )
  await page.route('**/api/v1/tasks/t1/complete', async (route) => {
    assert.deepEqual(route.request().postDataJSON(), { expected_version: 2 })
    task = { ...task, status: 'completed', completed_at: '2026-07-15T09:00:00Z', version: 3 }
    await route.fulfill({ json: task })
  })
  await page.route('**/api/v1/commitments?**', (route) =>
    route.fulfill({ json: { items: commitment.status === 'fulfilled' ? [] : [commitment], next_cursor: null } }),
  )
  await page.route('**/api/v1/commitments/c1/fulfil', async (route) => {
    assert.deepEqual(route.request().postDataJSON(), { expected_version: 3 })
    commitment = { ...commitment, status: 'fulfilled', fulfilled_at: '2026-07-15T09:00:00Z', version: 4 }
    await route.fulfill({ json: commitment })
  })
  await page.route('**/api/v1/recommendations?**', (route) =>
    route.fulfill({ json: { items: [recommendation], next_cursor: null } }),
  )
  await page.route('**/api/v1/recommendations/rec1/confirm', async (route) => {
    assert.deepEqual(route.request().postDataJSON(), {
      expected_version: 4,
      target_expected_version: 2,
    })
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

  try {
    await step('render dashboard and stale brief', async () => {
      await page.goto('http://127.0.0.1:4173')
      await page.getByRole('heading', { name: 'Today' }).waitFor()
      await page.getByText('Leadership review').first().waitFor()
      await page.getByText(/This brief is stale/).waitFor()
    })

    await step('refresh morning brief', async () => {
      await page.getByRole('button', { name: 'Refresh brief' }).click()
      await page.getByText('Generation 4 · disabled').waitFor()
      assert.equal(await page.getByText(/This brief is stale/).count(), 0)
    })

    await step('complete task with optimistic version', async () => {
      await page.getByRole('button', { name: 'Complete' }).click()
      await page.getByText('No open tasks.').waitFor()
    })

    await step('fulfil commitment with optimistic version', async () => {
      await page.getByRole('button', { name: 'Fulfil' }).click()
      await page.getByText('No open commitments.').waitFor()
    })

    await step('confirm recommendation', async () => {
      await page.getByRole('button', { name: 'Confirm and execute' }).click()
      await page.getByText('Execution recorded.').waitFor()
    })

    await step('search with semantic searchbox', async () => {
      const searchTab = page.getByRole('tab', { name: 'Search' })
      await searchTab.focus()
      await page.keyboard.press('Enter')
      await page.getByRole('searchbox').fill('hiring')
      await page.getByRole('button', { name: 'Search', exact: true }).click()
      await page.getByText('98%').waitFor()
    })

    await step('navigate tabs by keyboard and inspect audit', async () => {
      const searchTab = page.getByRole('tab', { name: 'Search' })
      await searchTab.focus()
      await page.keyboard.press('ArrowRight')
      await page.getByRole('tab', { name: 'Audit history' }).waitFor({ state: 'visible' })
      await page.getByText('task.updated').waitFor()
      await page.getByText(/manual_priority/).waitFor()
    })
  } finally {
    await browser.close()
  }
  console.log('Playwright acceptance checks passed')
}

try {
  await main()
} finally {
  preview.kill('SIGTERM')
}
