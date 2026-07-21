import assert from 'node:assert/strict'

import { createFixtureApi } from '../fixtures.mjs'
import { assertNoSeriousAccessibilityViolations } from '../accessibility.mjs'

const seedEvent = {
  id: 'event-1',
  title: 'Leadership sync',
  starts_at: '2026-07-20T09:00:00Z',
  ends_at: '2026-07-20T10:00:00Z',
  all_day: false,
  timezone: 'UTC',
  location: 'HQ',
  description: 'Weekly sync',
  status: 'confirmed',
  external_source: 'manual',
  external_id: null,
  source_authoritative: true,
  created_at: '2026-07-01T00:00:00Z',
  updated_at: '2026-07-01T00:00:00Z',
  version: 1,
}

/**
 * Schedule workspace journey: calendar events own authoritative timing,
 * meetings either project timing from a linked event or own it standalone.
 * Covers creating both kinds of meeting, the "Reschedule" handoff from a
 * linked meeting back to its calendar event, and archive/restore.
 */
export async function run({ page, baseURL }) {
  const fixtures = await createFixtureApi(page, { calendarEvents: [seedEvent] })

  await page.goto(baseURL)
  await page.getByRole('tab', { name: 'Schedule' }).click()
  const section = page.locator('section[aria-labelledby="schedule-title"]')
  await section.getByRole('heading', { name: 'Calendar & meetings', level: 1 }).waitFor()
  await section.locator('.work-list strong', { hasText: 'Leadership sync' }).waitFor()

  await assertNoSeriousAccessibilityViolations(page, { include: 'section[aria-labelledby="schedule-title"]' })

  // Create a standalone calendar event.
  await section.getByLabel('Event title').fill('Budget review')
  await section.getByLabel('Event start').fill('2026-08-01T09:00')
  await section.getByLabel('Event end').fill('2026-08-01T10:00')
  await section.getByRole('button', { name: 'Create event' }).click()
  await section.locator('.work-list strong', { hasText: 'Budget review' }).waitFor()

  // Create a standalone meeting (no linked calendar event).
  await section.getByLabel('Meeting title').fill('1:1 with CFO')
  await section.getByLabel('Meeting start').fill('2026-08-02T14:00')
  await section.getByLabel('Meeting end').fill('2026-08-02T14:30')
  await section.getByLabel('Meeting agenda').fill('Runway review')
  await section.getByRole('button', { name: 'Create standalone meeting' }).click()
  await section.getByText('1:1 with CFO').waitFor()
  await section.getByText(/standalone timing/).waitFor()

  // Create a meeting linked to the seeded calendar event; timing projects
  // from the event rather than being entered here.
  await section.getByLabel('Linked calendar event').selectOption({ label: 'Leadership sync' })
  await section.getByText('Timing will be projected from the selected calendar event.').waitFor()
  await section.getByLabel('Meeting title').fill('Leadership sync prep')
  await section.getByRole('button', { name: 'Create linked meeting' }).click()
  await section.getByText('Leadership sync prep').waitFor()
  const linkedMeetingRow = section.locator('li', { hasText: 'Leadership sync prep' })
  await linkedMeetingRow.getByText(/timing from calendar event/).waitFor()
  const meetingCreateRequest = fixtures.requests.find((request) => request.method === 'POST' && request.path === '/api/v1/meetings' && request.body.title === 'Leadership sync prep')
  assert.ok(meetingCreateRequest)
  assert.equal(meetingCreateRequest.body.calendar_event_id, 'event-1')
  assert.equal('starts_at' in meetingCreateRequest.body, false, 'linked meeting create must not send its own timing')

  // Editing a linked meeting hands off to the authoritative calendar event.
  await linkedMeetingRow.getByRole('button', { name: 'Edit meeting Leadership sync prep' }).click()
  await section.getByText('Linked meeting timing is controlled by its calendar event and is display-only here.').waitFor()
  await section.getByRole('button', { name: 'Reschedule Leadership sync prep' }).click()
  const editEventHeading = section.getByRole('heading', { name: 'Edit calendar event' })
  await editEventHeading.waitFor()
  assert.equal(await section.getByLabel('Edit event title').inputValue(), 'Leadership sync')
  await section.getByRole('button', { name: 'Discard event edit' }).click()
  await editEventHeading.waitFor({ state: 'detached' })

  // Archive then restore the standalone event created above.
  const budgetRow = section.locator('li', { hasText: 'Budget review' })
  await budgetRow.getByRole('button', { name: 'Archive event Budget review' }).click()
  const restoreButton = section.getByRole('button', { name: 'Restore event Budget review' })
  await restoreButton.waitFor()
  await restoreButton.click()
  await section.getByRole('button', { name: 'Archive event Budget review' }).waitFor()
}
