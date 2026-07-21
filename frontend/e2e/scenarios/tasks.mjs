import assert from 'node:assert/strict'

import { createFixtureApi } from '../fixtures.mjs'
import { assertNoSeriousAccessibilityViolations } from '../accessibility.mjs'

const seedTask = {
  id: 'task-1',
  title: 'Prepare board pack',
  description: 'Draft the quarterly board pack',
  status: 'planned',
  manual_priority: 'high',
  due_date: '2026-07-20',
  due_at: null,
  version: 1,
}

/**
 * Task workspace journey: create, edit, resolve a version conflict raised by
 * a concurrent change, complete, archive/restore, and offline mutation
 * disablement (an OFFLINE-classified request surfaces as an accessible
 * alert rather than failing silently).
 */
export async function run({ page, baseURL }) {
  const fixtures = await createFixtureApi(page, { tasks: [seedTask] })

  await page.goto(baseURL)
  assert.equal(await page.title(), 'Executive Command Center')

  await page.getByRole('tab', { name: 'Work' }).click()
  const tasksSection = page.locator('section[aria-labelledby="tasks-title"]')
  await tasksSection.getByRole('heading', { name: 'Tasks', level: 1 }).waitFor()
  await page.getByText('Prepare board pack').waitFor()

  await assertNoSeriousAccessibilityViolations(page, { include: 'section[aria-labelledby="tasks-title"]' })

  // Create a new task.
  await tasksSection.getByLabel('Task title').fill('Approve travel budget')
  await tasksSection.getByLabel('Due date').fill('2026-08-01')
  await tasksSection.getByRole('button', { name: 'Create task', exact: true }).click()
  await tasksSection.getByText('Approve travel budget').waitFor()
  const createRequest = fixtures.requests.find((request) => request.method === 'POST' && request.path === '/api/v1/tasks')
  assert.ok(createRequest, 'expected a POST /api/v1/tasks request')
  assert.equal(createRequest.body.title, 'Approve travel budget')
  assert.equal(createRequest.body.status, 'captured')

  // Edit the seeded task.
  await tasksSection.getByRole('button', { name: 'Edit Prepare board pack' }).click()
  const editHeading = tasksSection.getByRole('heading', { name: 'Edit task' })
  await editHeading.waitFor()
  await tasksSection.getByLabel('Edit priority').selectOption('critical')
  await tasksSection.getByRole('button', { name: 'Save task' }).click()
  await tasksSection.getByText(/planned · critical/).waitFor()

  // Force a version conflict: another actor mutates the task server-side
  // while the operator is mid-edit.
  await tasksSection.getByRole('button', { name: 'Edit Prepare board pack' }).click()
  await editHeading.waitFor()
  fixtures.collections.tasks.mutate('task-1', fixtures.collections.tasks.find('task-1').version, () => ({ manual_priority: 'low' }))
  await tasksSection.getByLabel('Edit task title').fill('Prepare board pack (revised)')
  await tasksSection.getByRole('button', { name: 'Save task' }).click()
  const conflictAlert = tasksSection.getByRole('alert')
  await conflictAlert.waitFor()
  assert.match(await conflictAlert.innerText(), /changed while you were editing it/)
  await tasksSection.getByRole('button', { name: 'Retry with latest version' }).click()
  await tasksSection.getByText('Prepare board pack (revised)').waitFor()

  // Complete the task; terminal actions collapse to Archive only.
  await tasksSection.getByRole('button', { name: 'Complete Prepare board pack (revised)' }).click()
  await tasksSection.getByText(/completed · critical/).waitFor()
  assert.equal(await tasksSection.getByRole('button', { name: 'Edit Prepare board pack (revised)' }).count(), 0)

  // Archive then restore.
  await tasksSection.getByRole('button', { name: 'Archive Prepare board pack (revised)' }).click()
  const restoreButton = tasksSection.getByRole('button', { name: 'Restore Prepare board pack (revised)' })
  await restoreButton.waitFor()
  await restoreButton.click()
  await tasksSection.getByRole('button', { name: 'Archive Prepare board pack (revised)' }).waitFor()

  // Offline mutation disablement: a create attempt while offline surfaces an
  // accessible alert instead of failing silently.
  //
  // `page.context().setOffline(true)` alone is not usable here: it was
  // verified (repeatedly, with request/response/requestfailed listeners) to
  // silently swallow fetches issued from inside a React click-handler's
  // mount/update cycle in this Playwright/Chromium build — no network event
  // fires at all, even after an 8s wait — while it works fine for a fetch
  // issued directly via `page.evaluate`. Combining `fixtures.setOffline(true)`
  // (aborts the intercepted request with `internetdisconnected`, so nothing
  // is actually served) with a direct `navigator.onLine` override reliably
  // exercises the exact same client code path apiRequest uses to classify a
  // failure as OFFLINE (see api/client.ts), without depending on the flaky
  // CDP offline emulation for a click-triggered request.
  fixtures.setOffline(true)
  await page.evaluate(() => Object.defineProperty(navigator, 'onLine', { value: false, configurable: true }))
  await tasksSection.getByLabel('Task title').fill('Should not be created while offline')
  await tasksSection.getByRole('button', { name: 'Create task', exact: true }).click()
  const offlineAlert = tasksSection.getByRole('alert')
  await offlineAlert.waitFor()
  assert.match(await offlineAlert.innerText(), /offline/i)
  fixtures.setOffline(false)
  await page.evaluate(() => Object.defineProperty(navigator, 'onLine', { value: true, configurable: true }))
}
