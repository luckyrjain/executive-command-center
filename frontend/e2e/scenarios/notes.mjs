import assert from 'node:assert/strict'

import { createFixtureApi } from '../fixtures.mjs'
import { assertNoSeriousAccessibilityViolations } from '../accessibility.mjs'

const seedNote = {
  id: 'note-1',
  title: 'Board memo',
  body: 'Draft memo body',
  note_type: 'general',
  version: 1,
}

/**
 * Notes workspace journey: create, filter via client-side search, autosave
 * an edit (flushed on blur rather than waiting out the 750ms debounce),
 * resolve a version conflict raised mid-edit, and archive/restore.
 */
export async function run({ page, baseURL }) {
  const fixtures = await createFixtureApi(page, { notes: [seedNote] })

  await page.goto(baseURL)
  await page.getByRole('tab', { name: 'Notes' }).click()
  const section = page.locator('section[aria-labelledby="notes-title"]')
  await section.getByRole('heading', { name: 'Notes', level: 1 }).waitFor()
  await section.getByText('Draft memo body').waitFor()

  await assertNoSeriousAccessibilityViolations(page, { include: 'section[aria-labelledby="notes-title"]' })

  // Create a second note, then filter the list down to just one of them.
  await section.getByLabel('Note title').fill('Offsite agenda')
  await section.getByLabel('Note body').fill('Draft the offsite agenda for Friday')
  await section.getByLabel('Note type').selectOption('decision')
  await section.getByRole('button', { name: 'Create note' }).click()
  await section.getByText('Offsite agenda').waitFor()

  await section.getByLabel('Search notes').fill('offsite')
  await section.getByText('Offsite agenda').waitFor()
  assert.equal(await section.getByText('Board memo').count(), 0)
  await section.getByLabel('Search notes').fill('')
  await section.getByText('Board memo').waitFor()

  // Edit the seeded note; autosave flushes on blur.
  await section.getByRole('button', { name: 'Edit Board memo' }).click()
  const editor = section.locator('.note-editor')
  await editor.getByRole('heading', { name: 'Edit Board memo' }).waitFor()
  const editBody = editor.getByLabel('Edit note body')
  await editBody.fill('Draft memo body, revised for the board.')
  await editBody.blur()
  const savedStatus = editor.getByRole('status')
  await savedStatus.filter({ hasText: 'Note saved.' }).waitFor()
  const patchRequest = fixtures.requests.find((request) => request.method === 'PATCH' && request.path === '/api/v1/notes/note-1')
  assert.ok(patchRequest, 'expected a PATCH /api/v1/notes/note-1 request')
  assert.equal(patchRequest.body.body, 'Draft memo body, revised for the board.')

  // Force a version conflict: another actor updates the note server-side
  // while the operator keeps typing.
  fixtures.collections.notes.mutate('note-1', fixtures.collections.notes.find('note-1').version, () => ({ body: 'Concurrent edit from elsewhere' }))
  await editBody.fill('Draft memo body, revised for the board and typed further.')
  await editBody.blur()
  const conflictAlert = editor.getByRole('alert')
  await conflictAlert.waitFor()
  assert.match(await conflictAlert.innerText(), /not saved/i)
  await editor.getByText('Your note changed elsewhere. Your text is preserved.').waitFor()
  await editor.getByRole('button', { name: 'Retry with latest version' }).click()
  await savedStatus.filter({ hasText: 'Note saved.' }).waitFor()

  await editor.getByRole('button', { name: 'Close editor' }).click()
  await editor.waitFor({ state: 'detached' })

  // Archive then restore.
  await section.getByRole('button', { name: 'Archive Board memo' }).click()
  const restoreButton = section.getByRole('button', { name: 'Restore Board memo' })
  await restoreButton.waitFor()
  await section.getByText(/general · archived/).waitFor()
  await restoreButton.click()
  await section.getByRole('button', { name: 'Archive Board memo' }).waitFor()
}
