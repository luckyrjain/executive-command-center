// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import NoteWorkspace from './NoteWorkspace'

const note = {
  id: 'note-1', owner_id: 'user-1', title: 'Board preparation', body: 'Review revenue and hiring',
  note_type: 'general', meeting_id: null, source_type: 'local', source_ref: null,
  created_at: '2026-07-16T09:00:00Z', updated_at: '2026-07-16T09:00:00Z', version: 4,
  archived_at: null, pre_archive_status: null, links: { audit: '/api/v1/audit' },
}

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

function renderWorkspace() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}><NoteWorkspace /></QueryClientProvider>)
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
      .mockImplementationOnce(() => response({ items: [saved], next_cursor: null }))
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
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(7))
    expect(JSON.parse(String((fetch.mock.calls[6][1] as RequestInit).body))).toEqual({ expected_version: 6, body: 'Draft after conflict' })
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
})
