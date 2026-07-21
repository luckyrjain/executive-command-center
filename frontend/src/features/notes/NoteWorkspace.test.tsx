// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import NoteWorkspace from './NoteWorkspace'
import { createNoteDraftRecoveryStore, type NoteDraftRecoveryStore } from './draftRecovery'

const note = {
  id: 'note-1', owner_id: 'user-1', title: 'Board preparation', body: 'Review revenue and hiring',
  note_type: 'general', meeting_id: null, source_type: 'local', source_ref: null,
  created_at: '2026-07-16T09:00:00Z', updated_at: '2026-07-16T09:00:00Z', version: 4,
  archived_at: null, pre_archive_status: null, links: { audit: '/api/v1/audit' },
}

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

function renderWorkspace(recoveryStore?: NoteDraftRecoveryStore) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}><NoteWorkspace recoveryStore={recoveryStore} /></QueryClientProvider>)
}

async function settleFakeTimers() {
  await act(async () => { await vi.runAllTimersAsync() })
}

beforeEach(() => {
  document.cookie = 'ecc_csrf=test-token; Secure; SameSite=Strict'
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'request-id') })
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('NoteWorkspace', () => {
  it('creates a note without identity fields and displays body content as text', async () => {
    const created = { ...note, title: 'Decision log', body: '<img src=x onerror=alert(1)>' }
    const fetch = vi.fn()
      .mockImplementationOnce(() => response({ items: [], next_cursor: null }))
      .mockImplementationOnce(() => response(created, 201))
      .mockImplementationOnce(() => response({ items: [created], next_cursor: null }))
    vi.stubGlobal('fetch', fetch)
    renderWorkspace()

    fireEvent.change(screen.getByLabelText('Note title'), { target: { value: 'Decision log' } })
    fireEvent.change(screen.getByLabelText('Note body'), { target: { value: '<img src=x onerror=alert(1)>' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create note' }))

    await screen.findByText('<img src=x onerror=alert(1)>')
    expect(document.querySelector('img')).toBeNull()
    const payload = JSON.parse(String((fetch.mock.calls[1][1] as RequestInit).body))
    expect(payload).toEqual({ title: 'Decision log', body: '<img src=x onerror=alert(1)>', note_type: 'general' })
    expect(String((fetch.mock.calls[1][1] as RequestInit).body)).not.toMatch(/owner|workspace|actor/)
  })

  it('invalidates the dashboard and morning brief caches on note mutation success', async () => {
    const created = { ...note, title: 'Decision log' }
    const fetch = vi.fn()
      .mockImplementationOnce(() => response({ items: [], next_cursor: null }))
      .mockImplementationOnce(() => response(created, 201))
      .mockImplementationOnce(() => response({ items: [created], next_cursor: null }))
    vi.stubGlobal('fetch', fetch)
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
    render(<QueryClientProvider client={client}><NoteWorkspace /></QueryClientProvider>)
    const invalidateSpy = vi.spyOn(client, 'invalidateQueries')

    fireEvent.change(screen.getByLabelText('Note title'), { target: { value: 'Decision log' } })
    fireEvent.change(screen.getByLabelText('Note body'), { target: { value: 'Body' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create note' }))

    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(3))
    const invalidatedKeys = invalidateSpy.mock.calls.map((call) => call[0]?.queryKey)
    expect(invalidatedKeys).toContainEqual(['dashboard', 'today'])
    expect(invalidatedKeys).toContainEqual(['brief', 'morning'])
  })

  it('searches the loaded notes locally and archives then restores a note', async () => {
    const second = { ...note, id: 'note-2', title: 'Hiring plan', body: 'Candidate pipeline' }
    const archived = { ...note, version: 5, archived_at: '2026-07-16T10:00:00Z' }
    const restored = { ...note, version: 6 }
    const fetch = vi.fn()
      .mockImplementationOnce(() => response({ items: [note, second], next_cursor: null }))
      .mockImplementationOnce(() => response(archived))
      .mockImplementationOnce(() => response({ items: [archived, second], next_cursor: null }))
      .mockImplementationOnce(() => response(restored))
      .mockImplementationOnce(() => response({ items: [restored, second], next_cursor: null }))
    vi.stubGlobal('fetch', fetch)
    renderWorkspace()

    await screen.findByText('Board preparation')
    fireEvent.change(screen.getByLabelText('Search notes'), { target: { value: 'candidate' } })
    expect(screen.queryByText('Board preparation')).toBeNull()
    expect(screen.getByText('Hiring plan')).toBeTruthy()
    fireEvent.change(screen.getByLabelText('Search notes'), { target: { value: '' } })
    fireEvent.click(screen.getByRole('button', { name: 'Archive Board preparation' }))
    await screen.findByRole('button', { name: 'Restore Board preparation' })
    fireEvent.click(screen.getByRole('button', { name: 'Restore Board preparation' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(5))
    expect(JSON.parse(String((fetch.mock.calls[1][1] as RequestInit).body))).toEqual({ expected_version: 4 })
    expect(JSON.parse(String((fetch.mock.calls[3][1] as RequestInit).body))).toEqual({ expected_version: 5 })
  })

  it('announces saving and saved states and flushes on blur with the latest version', async () => {
    vi.useFakeTimers()
    let finishSave!: (value: Response) => void
    const pending = new Promise<Response>((resolve) => { finishSave = resolve })
    const fetch = vi.fn()
      .mockImplementationOnce(() => response({ items: [note], next_cursor: null }))
      .mockImplementationOnce(() => pending)
    vi.stubGlobal('fetch', fetch)
    renderWorkspace()
    await act(async () => { await vi.runAllTimersAsync() })
    fireEvent.click(screen.getByRole('button', { name: 'Edit Board preparation' }))
    const editor = screen.getByLabelText('Edit note body')
    fireEvent.change(editor, { target: { value: 'Latest local draft' } })
    fireEvent.blur(editor)
    await act(async () => { await Promise.resolve() })
    expect(screen.getByRole('status').textContent).toMatch(/saving/i)
    expect(JSON.parse(String((fetch.mock.calls[1][1] as RequestInit).body))).toEqual({ expected_version: 4, body: 'Latest local draft' })

    finishSave(new Response(JSON.stringify({ ...note, body: 'Latest local draft', version: 5 }), { status: 200 }))
    await act(async () => { await Promise.resolve(); await Promise.resolve() })
    expect(screen.getByRole('status').textContent).toMatch(/saved/i)
  })

  it('preserves local text after network and conflict failures and retries a conflict with the latest version', async () => {
    const conflict = { error: { code: 'VERSION_CONFLICT', message: 'changed', details: { current_version: 5 } } }
    const current = { ...note, body: 'Other tab content', version: 5 }
    const saved = { ...note, body: 'My preserved draft', version: 6 }
    const savedAgain = { ...saved, body: 'Draft after conflict', version: 7 }
    const fetch = vi.fn()
      .mockImplementationOnce(() => response({ items: [note], next_cursor: null }))
      .mockRejectedValueOnce(new TypeError('network'))
      .mockImplementationOnce(() => response(conflict, 409))
      .mockImplementationOnce(() => response(current))
      .mockImplementationOnce(() => response(saved))
      .mockImplementationOnce(() => response(savedAgain))
    vi.stubGlobal('fetch', fetch)
    renderWorkspace()

    await screen.findByText('Board preparation')
    fireEvent.click(screen.getByRole('button', { name: 'Edit Board preparation' }))
    const editor = screen.getByLabelText('Edit note body') as HTMLTextAreaElement
    fireEvent.change(editor, { target: { value: 'My preserved draft' } })
    fireEvent.blur(editor)
    await screen.findByRole('alert')
    expect(editor.value).toBe('My preserved draft')
    fireEvent.click(screen.getByRole('button', { name: 'Retry save' }))
    await screen.findByRole('button', { name: 'Retry with latest version' })
    expect(editor.value).toBe('My preserved draft')
    fireEvent.click(screen.getByRole('button', { name: 'Retry with latest version' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(5))
    expect(JSON.parse(String((fetch.mock.calls[4][1] as RequestInit).body))).toEqual({ expected_version: 5, body: 'My preserved draft' })
    fireEvent.change(editor, { target: { value: 'Draft after conflict' } })
    fireEvent.blur(editor)
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(6))
    expect(JSON.parse(String((fetch.mock.calls[5][1] as RequestInit).body))).toEqual({ expected_version: 6, body: 'Draft after conflict' })
  })

  it('blocks stale conflict retries until the latest note can be reloaded', async () => {
    const conflict = { error: { code: 'VERSION_CONFLICT', message: 'changed', details: { current_version: 5 } } }
    const current = { ...note, version: 5 }
    const fetch = vi.fn()
      .mockImplementationOnce(() => response({ items: [note], next_cursor: null }))
      .mockImplementationOnce(() => response(conflict, 409))
      .mockRejectedValueOnce(new TypeError('network'))
      .mockImplementationOnce(() => response(current))
    vi.stubGlobal('fetch', fetch)
    renderWorkspace()

    await screen.findByText('Board preparation')
    fireEvent.click(screen.getByRole('button', { name: 'Edit Board preparation' }))
    const editor = screen.getByLabelText('Edit note body') as HTMLTextAreaElement
    fireEvent.change(editor, { target: { value: 'Keep this conflict draft' } })
    fireEvent.blur(editor)
    await screen.findByRole('button', { name: 'Reload latest note' })
    expect(screen.queryByRole('button', { name: 'Retry save' })).toBeNull()
    expect(editor.value).toBe('Keep this conflict draft')
    fireEvent.click(screen.getByRole('button', { name: 'Reload latest note' }))
    await screen.findByRole('button', { name: 'Retry with latest version' })
  })

  it('flushes a pending draft before closing the editor', async () => {
    vi.useFakeTimers()
    const saved = { ...note, body: 'Close-safe draft', version: 5 }
    const fetch = vi.fn()
      .mockImplementationOnce(() => response({ items: [note], next_cursor: null }))
      .mockImplementationOnce(() => response(saved))
    vi.stubGlobal('fetch', fetch)
    renderWorkspace()
    await settleFakeTimers()

    fireEvent.click(screen.getByRole('button', { name: 'Edit Board preparation' }))
    fireEvent.change(screen.getByLabelText('Edit note body'), { target: { value: 'Close-safe draft' } })
    fireEvent.click(screen.getByRole('button', { name: 'Close editor' }))
    await settleFakeTimers()

    expect(JSON.parse(String((fetch.mock.calls[1][1] as RequestInit).body))).toEqual({ expected_version: 4, body: 'Close-safe draft' })
    expect(screen.queryByLabelText('Edit note body')).toBeNull()
  })

  it('flushes the current draft before switching notes', async () => {
    vi.useFakeTimers()
    const second = { ...note, id: 'note-2', title: 'Hiring plan', body: 'Candidate pipeline' }
    const fetch = vi.fn()
      .mockImplementationOnce(() => response({ items: [note, second], next_cursor: null }))
      .mockImplementationOnce(() => response({ ...note, body: 'Switch-safe draft', version: 5 }))
    vi.stubGlobal('fetch', fetch)
    renderWorkspace()
    await settleFakeTimers()

    fireEvent.click(screen.getByRole('button', { name: 'Edit Board preparation' }))
    fireEvent.change(screen.getByLabelText('Edit note body'), { target: { value: 'Switch-safe draft' } })
    fireEvent.click(screen.getByRole('button', { name: 'Edit Hiring plan' }))
    await settleFakeTimers()

    expect(JSON.parse(String((fetch.mock.calls[1][1] as RequestInit).body))).toEqual({ expected_version: 4, body: 'Switch-safe draft' })
    expect((screen.getByLabelText('Edit note body') as HTMLTextAreaElement).value).toBe('Candidate pipeline')
  })

  it('flushes a pending draft when the workspace unmounts', async () => {
    vi.useFakeTimers()
    const fetch = vi.fn()
      .mockImplementationOnce(() => response({ items: [note], next_cursor: null }))
      .mockImplementationOnce(() => response({ ...note, body: 'Unmount-safe draft', version: 5 }))
    vi.stubGlobal('fetch', fetch)
    const rendered = renderWorkspace()
    await settleFakeTimers()

    fireEvent.click(screen.getByRole('button', { name: 'Edit Board preparation' }))
    fireEvent.change(screen.getByLabelText('Edit note body'), { target: { value: 'Unmount-safe draft' } })
    rendered.unmount()
    await settleFakeTimers()

    expect(JSON.parse(String((fetch.mock.calls[1][1] as RequestInit).body))).toEqual({ expected_version: 4, body: 'Unmount-safe draft' })
  })

  it('recovers a draft when closing flush fails', async () => {
    vi.useFakeTimers()
    const recoveryNote = { ...note, id: 'note-recovery' }
    const fetch = vi.fn()
      .mockImplementationOnce(() => response({ items: [recoveryNote], next_cursor: null }))
      .mockRejectedValueOnce(new TypeError('network'))
    vi.stubGlobal('fetch', fetch)
    renderWorkspace()
    await settleFakeTimers()

    fireEvent.click(screen.getByRole('button', { name: 'Edit Board preparation' }))
    fireEvent.change(screen.getByLabelText('Edit note body'), { target: { value: 'Recoverable close draft' } })
    fireEvent.click(screen.getByRole('button', { name: 'Close editor' }))
    await settleFakeTimers()
    fireEvent.click(screen.getByRole('button', { name: 'Edit Board preparation' }))

    expect((screen.getByLabelText('Edit note body') as HTMLTextAreaElement).value).toBe('Recoverable close draft')
  })

  it('updates list and detail caches after autosave so reopen and archive use the saved version', async () => {
    const saved = { ...note, body: 'Cached saved body', version: 5 }
    const archived = { ...saved, version: 6, archived_at: '2026-07-16T10:00:00Z' }
    const fetch = vi.fn()
      .mockImplementationOnce(() => response({ items: [note], next_cursor: null }))
      .mockImplementationOnce(() => response(saved))
      .mockImplementationOnce(() => response(archived))
      .mockImplementationOnce(() => response({ items: [archived], next_cursor: null }))
    vi.stubGlobal('fetch', fetch)
    renderWorkspace()

    await screen.findByText('Board preparation')
    fireEvent.click(screen.getByRole('button', { name: 'Edit Board preparation' }))
    fireEvent.change(screen.getByLabelText('Edit note body'), { target: { value: 'Cached saved body' } })
    fireEvent.blur(screen.getByLabelText('Edit note body'))
    await screen.findByText('Cached saved body')
    fireEvent.click(screen.getByRole('button', { name: 'Close editor' }))
    await waitFor(() => expect(screen.queryByLabelText('Edit note body')).toBeNull())
    fireEvent.click(screen.getByRole('button', { name: 'Edit Board preparation' }))
    expect((screen.getByLabelText('Edit note body') as HTMLTextAreaElement).value).toBe('Cached saved body')
    fireEvent.click(screen.getByRole('button', { name: 'Archive Board preparation' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(4))
    expect(JSON.parse(String((fetch.mock.calls[2][1] as RequestInit).body))).toEqual({ expected_version: 5 })
  })

  it('serializes text typed during a conflict retry against the returned version', async () => {
    const conflict = { error: { code: 'VERSION_CONFLICT', message: 'changed', details: { current_version: 5 } } }
    const current = { ...note, version: 5 }
    let finishRetry!: (value: Response) => void
    const retry = new Promise<Response>((resolve) => { finishRetry = resolve })
    const fetch = vi.fn()
      .mockImplementationOnce(() => response({ items: [note], next_cursor: null }))
      .mockImplementationOnce(() => response(conflict, 409))
      .mockImplementationOnce(() => response(current))
      .mockImplementationOnce(() => retry)
      .mockImplementationOnce(() => response({ ...note, body: 'Newest conflict draft', version: 7 }))
    vi.stubGlobal('fetch', fetch)
    renderWorkspace()

    await screen.findByText('Board preparation')
    fireEvent.click(screen.getByRole('button', { name: 'Edit Board preparation' }))
    const editor = screen.getByLabelText('Edit note body') as HTMLTextAreaElement
    fireEvent.change(editor, { target: { value: 'Conflict retry draft' } })
    fireEvent.blur(editor)
    await screen.findByRole('button', { name: 'Retry with latest version' })
    fireEvent.click(screen.getByRole('button', { name: 'Retry with latest version' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(4))
    fireEvent.change(editor, { target: { value: 'Newest conflict draft' } })
    fireEvent.blur(editor)
    finishRetry(new Response(JSON.stringify({ ...note, body: 'Conflict retry draft', version: 6 }), { status: 200 }))

    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(5))
    expect(JSON.parse(String((fetch.mock.calls[4][1] as RequestInit).body))).toEqual({ expected_version: 6, body: 'Newest conflict draft' })
  })

  it('requires explicit reconciliation when a recovered draft base version is older than the server', async () => {
    vi.useFakeTimers()
    const recoveryStore = createNoteDraftRecoveryStore({ namespace: 'recovery-version-test' })
    recoveryStore.put(note.id, { text: 'Failed version four draft', baseVersion: 4 })
    const advanced = { ...note, body: 'Remote version five content', version: 5 }
    const fetch = vi.fn()
      .mockImplementationOnce(() => response({ items: [advanced], next_cursor: null }))
      .mockImplementationOnce(() => response({ ...advanced, body: 'Failed version four draft', version: 6 }))
    vi.stubGlobal('fetch', fetch)
    renderWorkspace(recoveryStore)
    await settleFakeTimers()

    fireEvent.click(screen.getByRole('button', { name: 'Edit Board preparation' }))
    expect((screen.getByLabelText('Edit note body') as HTMLTextAreaElement).value).toBe('Failed version four draft')
    expect(screen.getByRole('button', { name: 'Retry with latest version' })).toBeTruthy()
    await act(async () => { await vi.advanceTimersByTimeAsync(751) })
    expect(fetch).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByRole('button', { name: 'Retry with latest version' }))
    await settleFakeTimers()
    expect(JSON.parse(String((fetch.mock.calls[1][1] as RequestInit).body))).toEqual({ expected_version: 5, body: 'Failed version four draft' })
  })

  it('does not activate a pending note switch after unmount', async () => {
    vi.useFakeTimers()
    const second = { ...note, id: 'note-2', title: 'Hiring plan', body: 'Remote hiring plan' }
    const recoveryStore = createNoteDraftRecoveryStore({ namespace: 'unmount-race-test' })
    let finishSave!: (value: Response) => void
    const pendingSave = new Promise<Response>((resolve) => { finishSave = resolve })
    const fetch = vi.fn()
      .mockImplementationOnce(() => response({ items: [note, second], next_cursor: null }))
      .mockRejectedValueOnce(new TypeError('note two offline'))
      .mockRejectedValueOnce(new TypeError('note two still offline'))
      .mockImplementationOnce(() => pendingSave)
    vi.stubGlobal('fetch', fetch)
    const rendered = renderWorkspace(recoveryStore)
    await settleFakeTimers()

    fireEvent.click(screen.getByRole('button', { name: 'Edit Hiring plan' }))
    fireEvent.change(screen.getByLabelText('Edit note body'), { target: { value: 'Recovered hiring draft' } })
    fireEvent.blur(screen.getByLabelText('Edit note body'))
    await settleFakeTimers()
    fireEvent.click(screen.getByRole('button', { name: 'Edit Board preparation' }))
    await settleFakeTimers()
    fireEvent.change(screen.getByLabelText('Edit note body'), { target: { value: 'Pending first draft' } })
    fireEvent.blur(screen.getByLabelText('Edit note body'))
    fireEvent.click(screen.getByRole('button', { name: 'Edit Hiring plan' }))
    rendered.unmount()
    finishSave(new Response(JSON.stringify({ ...note, body: 'Pending first draft', version: 5 }), { status: 200 }))
    await settleFakeTimers()

    expect(fetch).toHaveBeenCalledTimes(4)
    expect(fetch.mock.calls.slice(4).some(([url]) => String(url).includes('/notes/note-2'))).toBe(false)
  })
})
