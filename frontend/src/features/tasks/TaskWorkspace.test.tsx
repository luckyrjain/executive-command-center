// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import TaskWorkspace from './TaskWorkspace'

declare const process: { env: Record<string, string | undefined> }

const task = {
  id: 'task-1', title: 'Prepare board pack', description: 'Draft', status: 'planned',
  manual_priority: 'high', due_date: '2026-07-20', due_at: null, version: 4, archived_at: null,
}

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

function renderWorkspace() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}><TaskWorkspace /></QueryClientProvider>)
}

beforeEach(() => {
  document.cookie = 'ecc_csrf=test-token; Secure; SameSite=Strict'
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'request-id') })
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('TaskWorkspace', () => {
  it('creates a task without owner fields and enforces one due precision', async () => {
    const fetch = vi.fn()
      .mockImplementationOnce(() => response({ items: [], next_cursor: null }))
      .mockImplementationOnce(() => response(task, 201))
      .mockImplementationOnce(() => response({ items: [task], next_cursor: null }))
    vi.stubGlobal('fetch', fetch)
    renderWorkspace()

    fireEvent.change(screen.getByLabelText('Task title'), { target: { value: 'Prepare board pack' } })
    fireEvent.change(screen.getByLabelText('Due date'), { target: { value: '2026-07-20' } })
    expect((screen.getByLabelText('Due time') as HTMLInputElement).disabled).toBe(true)
    fireEvent.submit(screen.getByRole('button', { name: 'Create task' }).closest('form')!)

    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(3))
    const init = fetch.mock.calls[1][1] as RequestInit
    expect(JSON.parse(String(init.body))).toEqual({
      title: 'Prepare board pack', description: null, manual_priority: 'medium', due_date: '2026-07-20', due_at: null, status: 'captured',
    })
    expect(String(init.body)).not.toMatch(/owner|workspace|actor/)
  })

  it('edits using the displayed version and preserves input after a network failure', async () => {
    const fetch = vi.fn()
      .mockImplementationOnce(() => response({ items: [task], next_cursor: null }))
      .mockRejectedValueOnce(new TypeError('network'))
    vi.stubGlobal('fetch', fetch)
    renderWorkspace()

    await screen.findByText('Prepare board pack')
    fireEvent.click(screen.getByRole('button', { name: 'Edit Prepare board pack' }))
    fireEvent.change(screen.getByLabelText('Edit task title'), { target: { value: 'Prepare final board pack' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save task' }))

    await screen.findByRole('alert')
    expect((screen.getByLabelText('Edit task title') as HTMLInputElement).value).toBe('Prepare final board pack')
    const init = fetch.mock.calls[1][1] as RequestInit
    expect(JSON.parse(String(init.body))).toMatchObject({ expected_version: 4, title: 'Prepare final board pack' })
  })

  it('shows only valid lifecycle actions and supports archive then restore', async () => {
    const archived = { ...task, status: 'archived', archived_at: '2026-07-16T10:00:00Z', version: 5 }
    const fetch = vi.fn()
      .mockImplementationOnce(() => response({ items: [task], next_cursor: null }))
      .mockImplementationOnce(() => response(archived))
      .mockImplementationOnce(() => response({ items: [archived], next_cursor: null }))
      .mockImplementationOnce(() => response({ ...task, version: 6 }))
      .mockImplementationOnce(() => response({ items: [{ ...task, version: 6 }], next_cursor: null }))
    vi.stubGlobal('fetch', fetch)
    renderWorkspace()

    await screen.findByText('Prepare board pack')
    expect(screen.getByRole('button', { name: 'Complete Prepare board pack' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Cancel Prepare board pack' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Archive Prepare board pack' }))
    await screen.findByRole('button', { name: 'Restore Prepare board pack' })
    fireEvent.click(screen.getByRole('button', { name: 'Restore Prepare board pack' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(5))
    expect(JSON.parse(String((fetch.mock.calls[3][1] as RequestInit).body))).toEqual({ expected_version: 5 })
  })

  it('reloads current state on conflict and lets the user retry their edit', async () => {
    const current = { ...task, title: 'Board pack from another tab', version: 5 }
    const saved = { ...current, title: 'My board pack', version: 6 }
    const conflict = { error: { code: 'VERSION_CONFLICT', message: 'changed', details: { current_version: 5 } } }
    const fetch = vi.fn()
      .mockImplementationOnce(() => response({ items: [task], next_cursor: null }))
      .mockImplementationOnce(() => response(conflict, 409))
      .mockImplementationOnce(() => response(current))
      .mockImplementationOnce(() => response(saved))
      .mockImplementationOnce(() => response({ items: [saved], next_cursor: null }))
    vi.stubGlobal('fetch', fetch)
    renderWorkspace()

    await screen.findByText('Prepare board pack')
    fireEvent.click(screen.getByRole('button', { name: 'Edit Prepare board pack' }))
    fireEvent.change(screen.getByLabelText('Edit task title'), { target: { value: 'My board pack' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save task' }))
    await screen.findByText(/changed while you were editing/i)
    expect((screen.getByLabelText('Edit task title') as HTMLInputElement).value).toBe('My board pack')
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(3))
    expect(fetch.mock.calls[2][0]).toContain('/api/v1/tasks/task-1')
    fireEvent.click(screen.getByRole('button', { name: 'Retry with latest version' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(5))
    expect(JSON.parse(String((fetch.mock.calls[3][1] as RequestInit).body))).toMatchObject({ expected_version: 5, title: 'My board pack' })
  })

  it('preserves a server due instant when editing another field outside UTC', async () => {
    const originalTimezone = process.env.TZ
    process.env.TZ = 'Asia/Kolkata'
    const timed = { ...task, due_date: null, due_at: '2026-07-20T04:30:00.000Z' }
    const fetch = vi.fn()
      .mockImplementationOnce(() => response({ items: [timed], next_cursor: null }))
      .mockImplementationOnce(() => response({ ...timed, title: 'Updated title', version: 5 }))
      .mockImplementationOnce(() => response({ items: [], next_cursor: null }))
    vi.stubGlobal('fetch', fetch)
    renderWorkspace()
    await screen.findByText('Prepare board pack')
    fireEvent.click(screen.getByRole('button', { name: 'Edit Prepare board pack' }))
    expect((screen.getByLabelText('Edit due time') as HTMLInputElement).value).toBe('2026-07-20T10:00')
    fireEvent.change(screen.getByLabelText('Edit task title'), { target: { value: 'Updated title' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save task' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(3))
    expect(JSON.parse(String((fetch.mock.calls[1][1] as RequestInit).body)).due_at).toBe('2026-07-20T04:30:00.000Z')
    process.env.TZ = originalTimezone
  })
})
