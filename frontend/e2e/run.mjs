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
      // Preview is still starting.
    }
    await new Promise((resolve) => setTimeout(resolve, 500))
  }
  throw new Error('Vite preview did not start')
}

const sections = {
  today_schedule: [{ id: 'm1', title: 'Leadership review', starts_at: '2026-07-15T04:30:00Z' }],
  top_priorities: [{ entity_id: 't1', title: 'Approve hiring plan', score: 92, status: 'in_progress' }],
  overdue_commitments: [{ entity_id: 'c1', summary: 'Send board metrics', status: 'active' }],
  risks: [{ entity_id: 'r1', title: 'Vendor concentration', score: 80, status: 'monitoring' }],
  waiting_on: [{ entity_id: 'c2', summary: 'Legal approval', status: 'active' }],
  recently_changed: [{ entity_ref: 'task:t1', message: 'Priority updated', occurred_at: '2026-07-15T03:00:00Z' }],
}

async function main() {
  await waitForServer()
  const browser = await chromium.launch({ headless: true })
  const page = await browser.newPage()

  await page.context().addCookies([
    { name: 'ecc_csrf', value: 'csrf-token', url: 'http://127.0.0.1:4173' },
  ])

  await page.route('**/api/v1/dashboard/today', (route) =>
    route.fulfill({
      json: {
        date: '2026-07-15',
        timezone: 'Asia/Kolkata',
        generated_at: '2026-07-15T03:30:00Z',
        stale: false,
        sections,
      },
    }),
  )
  await page.route('**/api/v1/briefs/morning', (route) =>
    route.fulfill({
      json: {
        id: 'b1',
        briefing_date: '2026-07-15',
        generation_version: 3,
        sections,
        source_versions: { 'task:t1': 2 },
        evidence_ids: [],
        generated_at: '2026-07-15T03:30:00Z',
        timezone: 'Asia/Kolkata',
        algorithm_version: 'phase1-v1',
        ai_status: 'disabled',
        stale: true,
        stale_reason: 'source_version_changed',
      },
    }),
  )
  await page.route('**/api/v1/tasks?**', (route) =>
    route.fulfill({ json: { items: [], next_cursor: null } }),
  )
  await page.route('**/api/v1/commitments?**', (route) =>
    route.fulfill({ json: { items: [], next_cursor: null } }),
  )
  await page.route('**/api/v1/recommendations?**', (route) =>
    route.fulfill({ json: { items: [], next_cursor: null } }),
  )
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
            updated_at: '2026-07-15T03:00:00Z',
            source_type: 'local',
            archived: false,
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
            changed_fields: ['manual_priority'],
            authorization_result: 'allowed',
            source: 'user',
            failure_code: null,
            occurred_at: '2026-07-15T03:00:00Z',
          },
        ],
        next_cursor: null,
      },
    }),
  )

  try {
    await page.goto('http://127.0.0.1:4173')
    await page.getByRole('heading', { name: 'Today' }).waitFor()
    await page.getByText('Leadership review').first().waitFor()
    await page.getByText(/This brief is stale/).waitFor()
    await page.getByText('No open tasks.').waitFor()
    await page.getByText('No open commitments.').waitFor()

    const searchTab = page.getByRole('tab', { name: 'Search' })
    await searchTab.focus()
    await page.keyboard.press('Enter')
    await page.getByRole('searchbox').fill('hiring')
    await page.getByRole('button', { name: 'Search', exact: true }).click()
    await page.getByText('98%').waitFor()

    await searchTab.focus()
    await page.keyboard.press('ArrowRight')
    await page.getByText('task.updated').waitFor()
    await page.getByText(/manual_priority/).waitFor()

    assert.equal(await page.getByRole('tab', { name: 'Audit history' }).getAttribute('aria-selected'), 'true')
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
